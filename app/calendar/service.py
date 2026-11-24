import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import sessionmaker

from app.calendar.normalize import (
    calculate_event_fingerprint,
    google_to_normalized,
    is_google_event_eligible,
    is_odoo_event_eligible,
    normalized_to_google,
    normalized_to_odoo,
    odoo_to_normalized,
)
from app.google.client import GoogleClient
from app.models import CalendarPair, OdooCredential, SyncIntent, User
from app.odoo.client import OdooClient
from app.security import decrypt_secret

_logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    created_in_google: int = 0
    created_in_odoo: int = 0
    updated_in_google: int = 0
    updated_in_odoo: int = 0
    deleted_in_google: int = 0
    deleted_in_odoo: int = 0
    skipped: int = 0
    conflicts: int = 0
    errors: int = 0


class CalendarSyncService:
    def __init__(
        self,
        session_factory: sessionmaker,
        odoo_client: OdooClient,
        google_client: GoogleClient,
        encryption_key: str,
    ):
        self.session_factory = session_factory
        self.odoo_client = odoo_client
        self.google_client = google_client
        self.encryption_key = encryption_key

    def _get_user_secrets(self, user_id: int) -> tuple[int, int, int | None, str, str, str, int] | None:
        with self.session_factory() as db:
            user = db.query(User).filter_by(id=user_id, active=True).first()
            if not user:
                return None
            cred = db.query(OdooCredential).filter_by(user_id=user.id).first()
            if not cred or not cred.encrypted_secret:
                return None
            odoo_secret = decrypt_secret(cred.encrypted_secret, self.encryption_key)
            from app.google.account_resolver import GoogleAccountResolver
            from app.workspace.selection_manager import current_account_pin

            pin = current_account_pin(user_id)
            g_conn = GoogleAccountResolver.resolve_for_task(db, user_id, pin.account_id if pin else None)
            if not g_conn or g_conn.status != "connected":
                return None
            return (
                user.id,
                user.odoo_uid,
                user.partner_id,
                user.tz or "Asia/Bangkok",
                odoo_secret,
                g_conn.google_sub,
                g_conn.id,
            )

    def _generate_deterministic_google_id(self, user_id: int, odoo_event_id: int) -> str:
        raw = f"odoo_sync_{user_id}_{odoo_event_id}"
        # Google Event ID must be 5-1024 characters [a-v0-9]
        # SHA-256 hex digest uses 0-9a-f which is valid in [a-v0-9]
        hex_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"odoo{hex_digest}"

    def sync_user(self, user_id: int) -> SyncResult:
        result = SyncResult()
        auth_data = self._get_user_secrets(user_id)
        if not auth_data:
            return result

        _, odoo_uid, partner_id, user_tz, odoo_secret, google_sub, google_account_id = auth_data

        now = datetime.now(UTC)
        start_window = now - timedelta(days=35)
        end_window = now + timedelta(days=90)

        # 1. Fetch Odoo events in window
        start_str = start_window.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_window.strftime("%Y-%m-%d %H:%M:%S")
        if partner_id:
            odoo_domain = [
                ("active", "=", True),
                ("start", ">=", start_str),
                ("start", "<=", end_str),
                "|",
                ("user_id", "=", odoo_uid),
                ("partner_ids", "in", [partner_id]),
            ]
        else:
            odoo_domain = [
                ("user_id", "=", odoo_uid),
                ("active", "=", True),
                ("start", ">=", start_str),
                ("start", "<=", end_str),
            ]
        odoo_fields = [
            "id",
            "name",
            "description",
            "location",
            "start",
            "stop",
            "allday",
            "start_date",
            "stop_date",
            "privacy",
            "recurrency",
            "active",
            "write_date",
            "user_id",
            "partner_ids",
        ]

        try:
            raw_odoo_events = self.odoo_client.execute(
                odoo_uid,
                odoo_secret,
                "calendar.event",
                "search_read",
                [odoo_domain],
                {"fields": odoo_fields, "order": "id asc", "limit": 200},
            )
        except Exception as e:
            _logger.error(f"Error reading Odoo calendar for user {user_id}: {e}")
            result.errors += 1
            return result

        eligible_odoo = {
            ev["id"]: ev
            for ev in raw_odoo_events
            if is_odoo_event_eligible(ev, odoo_uid, partner_id)
        }

        # 2. Fetch Google events in window
        try:
            g_resp = self.google_client.list_calendar_events(
                user_id=user_id,
                time_min=start_window.isoformat(),
                time_max=end_window.isoformat(),
            )
            raw_google_events = g_resp.get("items", [])
        except Exception as e:
            _logger.error(f"Error reading Google calendar for user {user_id}: {e}")
            result.errors += 1
            return result

        eligible_google = {ev["id"]: ev for ev in raw_google_events if is_google_event_eligible(ev)}

        # 3. Load existing pairs
        with self.session_factory() as db:
            pairs = (
                db.query(CalendarPair)
                .filter_by(user_id=user_id)
                .filter(or_(
                    CalendarPair.google_account_id == google_account_id,
                    and_(CalendarPair.google_account_id.is_(None), CalendarPair.google_subject == google_sub),
                ))
                .all()
            )
            pair_by_odoo = {p.odoo_event_id: p for p in pairs}
            pair_by_google = {p.google_event_id: p for p in pairs}

        # 4. Reconcile Existing Pairs
        for pair in pairs:
            if pair.status == "paused":
                result.skipped += 1
                continue

            odoo_ev = eligible_odoo.get(pair.odoo_event_id)
            google_ev = eligible_google.get(pair.google_event_id)

            # Check if source was deleted / canceled on either side
            if not odoo_ev or not google_ev:
                if odoo_ev and not google_ev:
                    # Event is active in Odoo window, but deleted / cancelled on Google -> Delete from Odoo!
                    try:
                        self.odoo_client.execute(
                            odoo_uid,
                            odoo_secret,
                            "calendar.event",
                            "unlink",
                            [[pair.odoo_event_id]],
                        )
                        with self.session_factory() as db:
                            p = db.query(CalendarPair).filter_by(id=pair.id).first()
                            if p:
                                db.delete(p)
                                db.commit()
                        result.deleted_in_odoo += 1
                        _logger.info(
                            f"Deleted Odoo event {pair.odoo_event_id} because Google event was deleted."
                        )
                    except Exception as e:
                        _logger.error(f"Failed to delete Odoo event {pair.odoo_event_id}: {e}")
                        result.errors += 1
                    continue

                elif google_ev and not odoo_ev:
                    # Event is active in Google window, but deleted in Odoo -> Delete from Google!
                    try:
                        self.google_client.delete_calendar_event(
                            user_id=user_id,
                            calendar_id="primary",
                            event_id=pair.google_event_id,
                        )
                        with self.session_factory() as db:
                            p = db.query(CalendarPair).filter_by(id=pair.id).first()
                            if p:
                                db.delete(p)
                                db.commit()
                        result.deleted_in_google += 1
                        _logger.info(
                            f"Deleted Google event {pair.google_event_id} because Odoo event was deleted."
                        )
                    except Exception as e:
                        _logger.error(f"Failed to delete Google event {pair.google_event_id}: {e}")
                        result.errors += 1
                    continue
                else:
                    # Both missing from current window
                    result.skipped += 1
                    continue

            norm_odoo = odoo_to_normalized(odoo_ev, user_tz=user_tz)
            norm_google = google_to_normalized(google_ev, user_tz=user_tz)

            fp_odoo = calculate_event_fingerprint(norm_odoo)
            fp_google = calculate_event_fingerprint(norm_google)

            odoo_changed = fp_odoo != pair.odoo_fingerprint
            google_changed = fp_google != pair.google_fingerprint

            if not odoo_changed and not google_changed:
                result.skipped += 1
                continue

            if odoo_changed and not google_changed:
                # Odoo updated -> update Google
                organizer = google_ev.get("organizer", {})
                if not organizer.get("self", False) and google_ev.get("attendees"):
                    _logger.info(
                        f"User is attendee on Google event {pair.google_event_id}, updating local fingerprint only"
                    )
                    with self.session_factory() as db:
                        p = db.query(CalendarPair).filter_by(id=pair.id).first()
                        p.odoo_fingerprint = fp_odoo
                        p.status = "synced"
                        db.commit()
                    result.skipped += 1
                    continue

                try:
                    g_vals = normalized_to_google(norm_odoo)
                    updated_g = self.google_client.update_calendar_event(
                        user_id=user_id,
                        calendar_id="primary",
                        event_id=pair.google_event_id,
                        event_data=g_vals,
                        etag=pair.google_etag,
                    )
                    new_g_norm = google_to_normalized(updated_g, user_tz=user_tz)
                    with self.session_factory() as db:
                        p = db.query(CalendarPair).filter_by(id=pair.id).first()
                        p.odoo_fingerprint = fp_odoo
                        p.google_fingerprint = calculate_event_fingerprint(new_g_norm)
                        p.google_etag = updated_g.get("etag")
                        p.status = "synced"
                        db.commit()
                    result.updated_in_google += 1
                except Exception as e:
                    _logger.error(f"Failed to update Google event {pair.google_event_id}: {e}")
                    result.errors += 1

            elif google_changed and not odoo_changed:
                # Google updated -> update Odoo
                try:
                    odoo_vals = normalized_to_odoo(norm_google, odoo_uid=odoo_uid)
                    self.odoo_client.execute(
                        odoo_uid,
                        odoo_secret,
                        "calendar.event",
                        "write",
                        [[pair.odoo_event_id], odoo_vals],
                    )
                    # Re-read to get updated fingerprint
                    re_read = self.odoo_client.execute(
                        odoo_uid,
                        odoo_secret,
                        "calendar.event",
                        "read",
                        [[pair.odoo_event_id]],
                        {"fields": odoo_fields},
                    )
                    new_odoo_norm = odoo_to_normalized(re_read[0], user_tz=user_tz)
                    with self.session_factory() as db:
                        p = db.query(CalendarPair).filter_by(id=pair.id).first()
                        p.google_fingerprint = fp_google
                        p.odoo_fingerprint = calculate_event_fingerprint(new_odoo_norm)
                        p.status = "synced"
                        db.commit()
                    result.updated_in_odoo += 1
                except Exception as e:
                    _logger.error(f"Failed to update Odoo event {pair.odoo_event_id}: {e}")
                    result.errors += 1

            else:
                # Both changed!
                if fp_odoo == fp_google:
                    # Same changes on both sides -> update baseline
                    with self.session_factory() as db:
                        p = db.query(CalendarPair).filter_by(id=pair.id).first()
                        p.odoo_fingerprint = fp_odoo
                        p.google_fingerprint = fp_google
                        p.status = "synced"
                        db.commit()
                    result.skipped += 1
                else:
                    # Real Conflict!
                    with self.session_factory() as db:
                        p = db.query(CalendarPair).filter_by(id=pair.id).first()
                        p.status = "conflict"
                        p.conflict_detail = (
                            f"Odoo title: {norm_odoo.name} vs Google title: {norm_google.name}"
                        )
                        db.commit()
                    result.conflicts += 1

        # 5. Create Google events for unpaired Odoo events
        for odoo_id, odoo_ev in eligible_odoo.items():
            if odoo_id in pair_by_odoo:
                continue

            norm = odoo_to_normalized(odoo_ev, user_tz=user_tz)
            fp_odoo = calculate_event_fingerprint(norm)
            det_google_id = self._generate_deterministic_google_id(user_id, odoo_id)

            # Record Intent
            with self.session_factory() as db:
                intent = SyncIntent(
                    user_id=user_id,
                    google_account_id=google_account_id,
                    source="odoo",
                    target="google",
                    source_event_id=str(odoo_id),
                    deterministic_google_id=det_google_id,
                    status="pending",
                )
                db.add(intent)
                db.commit()
                intent_id = intent.id

            try:
                g_vals = normalized_to_google(norm)
                g_vals["id"] = det_google_id
                created_g = self.google_client.create_calendar_event(
                    user_id=user_id,
                    calendar_id="primary",
                    event_data=g_vals,
                )
                norm_created_g = google_to_normalized(created_g, user_tz=user_tz)
                fp_google = calculate_event_fingerprint(norm_created_g)

                with self.session_factory() as db:
                    new_pair = CalendarPair(
                        user_id=user_id,
                        google_account_id=google_account_id,
                        google_subject=google_sub,
                        google_calendar_id="primary",
                        odoo_event_id=odoo_id,
                        google_event_id=created_g.get("id", det_google_id),
                        odoo_fingerprint=fp_odoo,
                        google_fingerprint=fp_google,
                        google_etag=created_g.get("etag"),
                        status="synced",
                    )
                    db.add(new_pair)
                    intent_rec = db.query(SyncIntent).filter_by(id=intent_id).first()
                    if intent_rec:
                        intent_rec.status = "completed"
                        intent_rec.result_id = created_g.get("id")
                    db.commit()

                result.created_in_google += 1
            except Exception as e:
                _logger.error(f"Error creating Google event for Odoo event {odoo_id}: {e}")
                with self.session_factory() as db:
                    intent_rec = db.query(SyncIntent).filter_by(id=intent_id).first()
                    if intent_rec:
                        intent_rec.status = "failed"
                        db.commit()
                result.errors += 1

        # 6. Create Odoo events for unpaired Google events
        for g_id, g_ev in eligible_google.items():
            if g_id in pair_by_google:
                continue

            norm = google_to_normalized(g_ev, user_tz=user_tz)
            fp_google = calculate_event_fingerprint(norm)

            with self.session_factory() as db:
                intent = SyncIntent(
                    user_id=user_id,
                    google_account_id=google_account_id,
                    source="google",
                    target="odoo",
                    source_event_id=str(g_id),
                    status="pending",
                )
                db.add(intent)
                db.commit()
                intent_id = intent.id

            try:
                odoo_vals = normalized_to_odoo(norm, odoo_uid=odoo_uid)
                new_odoo_id = self.odoo_client.execute(
                    odoo_uid, odoo_secret, "calendar.event", "create", [odoo_vals]
                )
                if not new_odoo_id or not isinstance(new_odoo_id, int):
                    raise RuntimeError("Odoo create did not return an integer ID.")

                # Read back to get accurate Odoo fingerprint
                re_read = self.odoo_client.execute(
                    odoo_uid,
                    odoo_secret,
                    "calendar.event",
                    "read",
                    [[new_odoo_id]],
                    {"fields": odoo_fields},
                )
                fp_odoo = calculate_event_fingerprint(
                    odoo_to_normalized(re_read[0], user_tz=user_tz)
                )

                with self.session_factory() as db:
                    new_pair = CalendarPair(
                        user_id=user_id,
                        google_account_id=google_account_id,
                        google_subject=google_sub,
                        google_calendar_id="primary",
                        odoo_event_id=new_odoo_id,
                        google_event_id=g_id,
                        odoo_fingerprint=fp_odoo,
                        google_fingerprint=fp_google,
                        google_etag=g_ev.get("etag"),
                        status="synced",
                    )
                    db.add(new_pair)
                    intent_rec = db.query(SyncIntent).filter_by(id=intent_id).first()
                    if intent_rec:
                        intent_rec.status = "completed"
                        intent_rec.result_id = str(new_odoo_id)
                    db.commit()

                result.created_in_odoo += 1
            except Exception as e:
                _logger.error(f"Error creating Odoo event for Google event {g_id}: {e}")
                with self.session_factory() as db:
                    intent_rec = db.query(SyncIntent).filter_by(id=intent_id).first()
                    if intent_rec:
                        intent_rec.status = "ambiguous"
                        db.commit()
                result.errors += 1

        return result

    def resolve_conflict(self, user_id: int, pair_id: int, source: str) -> None:
        auth_data = self._get_user_secrets(user_id)
        if not auth_data:
            raise RuntimeError("User credentials not available.")

        _, odoo_uid, partner_id, user_tz, odoo_secret, *_ = auth_data

        with self.session_factory() as db:
            pair = db.query(CalendarPair).filter_by(id=pair_id, user_id=user_id).first()
            if not pair:
                raise ValueError("Calendar pair not found.")

        if source == "odoo":
            # Keep Odoo version -> update Google
            odoo_records = self.odoo_client.execute(
                odoo_uid, odoo_secret, "calendar.event", "read", [[pair.odoo_event_id]]
            )
            norm = odoo_to_normalized(odoo_records[0], user_tz=user_tz)
            g_vals = normalized_to_google(norm)
            updated_g = self.google_client.update_calendar_event(
                user_id=user_id,
                calendar_id="primary",
                event_id=pair.google_event_id,
                event_data=g_vals,
                etag=pair.google_etag,
            )
            new_fp = calculate_event_fingerprint(norm)
            with self.session_factory() as db:
                p = db.query(CalendarPair).filter_by(id=pair_id).first()
                p.odoo_fingerprint = new_fp
                p.google_fingerprint = new_fp
                p.google_etag = updated_g.get("etag")
                p.status = "synced"
                p.conflict_detail = None
                db.commit()

        elif source == "google":
            # Keep Google version -> update Odoo
            g_ev = self.google_client.get_calendar_event(
                user_id=user_id,
                calendar_id="primary",
                event_id=pair.google_event_id,
            )
            norm = google_to_normalized(g_ev, user_tz=user_tz)
            odoo_vals = normalized_to_odoo(norm, odoo_uid=odoo_uid)
            self.odoo_client.execute(
                odoo_uid, odoo_secret, "calendar.event", "write", [[pair.odoo_event_id], odoo_vals]
            )
            new_fp = calculate_event_fingerprint(norm)
            with self.session_factory() as db:
                p = db.query(CalendarPair).filter_by(id=pair_id).first()
                p.odoo_fingerprint = new_fp
                p.google_fingerprint = new_fp
                p.status = "synced"
                p.conflict_detail = None
                db.commit()
