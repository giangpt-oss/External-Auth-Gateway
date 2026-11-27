from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.calendar.service import CalendarSyncService
from app.db import init_db
from app.google.client import GoogleClient
from app.models import CalendarPair, GoogleConnection, OdooCredential, User
from app.odoo.client import OdooClient
from app.security import encrypt_secret


@pytest.fixture
def sync_components(temp_db_path, fernet_key):
    engine, session_factory = init_db(f"sqlite:///{temp_db_path}")

    # Create User
    with session_factory() as db:
        user = User(
            tenant="https://mycompany.odoo.com",
            database="mycompany",
            odoo_uid=77,
            login="user77@company.com",
            name="Calendar User",
            tz="Asia/Bangkok",
        )
        db.add(user)
        db.flush()
        db.add(
            OdooCredential(
                user_id=user.id,
                credential_kind="api_key",
                encrypted_secret=encrypt_secret("odoo_key_77", fernet_key),
            )
        )
        db.add(
            GoogleConnection(
                user_id=user.id,
                google_sub="g_sub_77",
                email="user77@company.com",
                scopes="https://www.googleapis.com/auth/calendar.events",
                encrypted_access_token=encrypt_secret("g_access_token", fernet_key),
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                status="connected",
            )
        )
        db.commit()
        user_id = user.id

    mock_odoo = MagicMock(spec=OdooClient)
    mock_google = MagicMock(spec=GoogleClient)

    service = CalendarSyncService(
        session_factory=session_factory,
        odoo_client=mock_odoo,
        google_client=mock_google,
        encryption_key=fernet_key,
    )

    yield service, session_factory, user_id, mock_odoo, mock_google
    engine.dispose()


def test_sync_creates_google_event_from_odoo(sync_components):
    service, session_factory, user_id, mock_odoo, mock_google = sync_components

    # Odoo has 1 event, Google has 0 events
    mock_odoo.execute.return_value = [
        {
            "id": 501,
            "name": "Họp Chiến Lược Quý 4",
            "description": "Chiến lược mở rộng",
            "location": "Tầng 12",
            "start": "2026-10-22 03:00:00",
            "stop": "2026-10-22 04:30:00",
            "allday": False,
            "privacy": "public",
            "recurrency": False,
            "active": True,
            "user_id": 77,
            "write_date": "2026-10-21 08:00:00",
        }
    ]
    mock_google.list_calendar_events.return_value = {"items": []}
    mock_google.create_calendar_event.return_value = {
        "id": "det_google_id_501",
        "etag": "etag_123",
        "summary": "Họp Chiến Lược Quý 4",
        "start": {"dateTime": "2026-10-22T03:00:00Z"},
        "end": {"dateTime": "2026-10-22T04:30:00Z"},
        "eventType": "default",
        "organizer": {"self": True},
        "updated": "2026-10-21T08:00:00.000Z",
    }

    result = service.sync_user(user_id=user_id)
    assert result.created_in_google == 1
    assert result.created_in_odoo == 0
    assert result.conflicts == 0

    # Verify CalendarPair recorded
    with session_factory() as db:
        pair = db.query(CalendarPair).filter_by(user_id=user_id, odoo_event_id=501).first()
        assert pair is not None
        assert pair.google_event_id == "det_google_id_501"
        assert pair.status == "synced"
        assert pair.odoo_fingerprint is not None
        assert pair.google_fingerprint is not None


def test_sync_conflict_detection_and_resolution(sync_components):
    service, session_factory, user_id, mock_odoo, mock_google = sync_components

    # Pre-populate synced pair
    with session_factory() as db:
        pair = CalendarPair(
            user_id=user_id,
            google_subject="g_sub_77",
            google_calendar_id="primary",
            odoo_event_id=601,
            google_event_id="g_ev_601",
            odoo_fingerprint="old_fp_odoo",
            google_fingerprint="old_fp_google",
            status="synced",
        )
        db.add(pair)
        db.commit()
        pair_id = pair.id

    # Both sides changed differently!
    mock_odoo.execute.return_value = [
        {
            "id": 601,
            "name": "Tiêu đề Odoo Đã Sửa",
            "description": "Desc Odoo",
            "start": "2026-10-23 03:00:00",
            "stop": "2026-10-23 04:00:00",
            "allday": False,
            "privacy": "public",
            "recurrency": False,
            "active": True,
            "user_id": 77,
        }
    ]
    mock_google.list_calendar_events.return_value = {
        "items": [
            {
                "id": "g_ev_601",
                "summary": "Tiêu đề Google Đã Sửa Khác",
                "description": "Desc Google",
                "start": {"dateTime": "2026-10-23T10:00:00+07:00"},
                "end": {"dateTime": "2026-10-23T11:00:00+07:00"},
                "eventType": "default",
                "organizer": {"self": True},
                "etag": "etag_conflict",
            }
        ]
    }

    result = service.sync_user(user_id=user_id)
    assert result.conflicts == 1

    with session_factory() as db:
        p = db.query(CalendarPair).filter_by(id=pair_id).first()
        assert p.status == "conflict"

    # User resolves conflict: choose "odoo"
    mock_google.update_calendar_event.return_value = {
        "id": "g_ev_601",
        "etag": "new_etag_resolved",
    }
    service.resolve_conflict(user_id=user_id, pair_id=pair_id, source="odoo")

    with session_factory() as db:
        p = db.query(CalendarPair).filter_by(id=pair_id).first()
        assert p.status == "synced"


@pytest.mark.parametrize("owner", [77, 88])
def test_sync_deletes_owned_odoo_event_when_google_explicitly_cancelled(sync_components, owner):
    service, session_factory, user_id, mock_odoo, mock_google = sync_components

    # Pre-populate synced pair
    with session_factory() as db:
        db.get(User, user_id).partner_id = 177
        pair = CalendarPair(
            user_id=user_id,
            google_subject="g_sub_77",
            google_calendar_id="primary",
            odoo_event_id=701,
            google_event_id="g_ev_701",
            status="synced",
        )
        db.add(pair)
        db.commit()
        pair_id = pair.id

    # Odoo still has the event in window, but Google list has 0 events (deleted in Google)
    mock_odoo.execute.return_value = [
        {
            "id": 701,
            "name": "Sự kiện sắp bị xóa",
            "start": "2026-10-25 03:00:00",
            "stop": "2026-10-25 04:00:00",
            "allday": False,
            "privacy": "public",
            "recurrency": False,
            "active": True,
            "user_id": owner,
            "partner_ids": [177],
        }
    ]
    mock_google.list_calendar_events.return_value = {"items": []}

    mock_google.get_calendar_event.return_value = {"id": "g_ev_701", "status": "cancelled"}

    result = service.sync_user(user_id=user_id)
    assert result.deleted_in_odoo == int(owner == 77)
    if owner != 77:
        assert all(call.args[3] != "unlink" for call in mock_odoo.execute.call_args_list)
        return
    # Verify Odoo unlink was called
    mock_odoo.execute.assert_called_with(77, "odoo_key_77", "calendar.event", "unlink", [[701]])

    # Verify CalendarPair is removed
    with session_factory() as db:
        p = db.query(CalendarPair).filter_by(id=pair_id).first()
        assert p is None


def test_sync_preserves_odoo_when_google_event_becomes_ineligible(sync_components):
    service, session_factory, user_id, mock_odoo, mock_google = sync_components
    with session_factory() as db:
        db.add(CalendarPair(user_id=user_id, google_subject="g_sub_77",
                            google_calendar_id="primary", odoo_event_id=901,
                            google_event_id="g_ev_901", status="synced"))
        db.commit()
    mock_odoo.execute.return_value = [{
        "id": 901, "name": "Recurring event", "start": "2026-11-27 03:00:00",
        "stop": "2026-11-27 04:00:00", "active": True, "user_id": 77,
    }]
    mock_google.list_calendar_events.return_value = {"items": []}
    mock_google.get_calendar_event.return_value = {
        "id": "g_ev_901", "status": "confirmed", "recurrence": ["RRULE:FREQ=WEEKLY"],
    }
    result = service.sync_user(user_id)
    assert result.deleted_in_odoo == 0
    assert all(call.args[3] != "unlink" for call in mock_odoo.execute.call_args_list)
    with session_factory() as db:
        assert db.query(CalendarPair).filter_by(odoo_event_id=901).count() == 1


def test_sync_reads_all_calendar_pages(sync_components):
    service, _, user_id, mock_odoo, mock_google = sync_components
    mock_odoo.execute.side_effect = [
        [{"id": index, "active": False} for index in range(1, 201)],
        [{"id": 201, "active": False}],
    ]
    mock_google.list_calendar_events.side_effect = [
        {"items": [], "nextPageToken": "second"},
        {"items": []},
    ]
    result = service.sync_user(user_id)
    assert result.errors == 0
    assert mock_odoo.execute.call_count == 2
    assert ("id", ">", 200) in mock_odoo.execute.call_args.args[4][0]
    assert mock_google.list_calendar_events.call_args.kwargs["page_token"] == "second"


@pytest.mark.parametrize("archived", [False, True])
def test_sync_deletes_google_only_when_odoo_explicitly_archived(sync_components, archived):
    service, session_factory, user_id, mock_odoo, mock_google = sync_components

    # Pre-populate synced pair
    with session_factory() as db:
        pair = CalendarPair(
            user_id=user_id,
            google_subject="g_sub_77",
            google_calendar_id="primary",
            odoo_event_id=801,
            google_event_id="g_ev_801",
            status="synced",
        )
        db.add(pair)
        db.commit()
        pair_id = pair.id

    # Odoo has 0 events (deleted in Odoo), Google has the event
    mock_odoo.execute.side_effect = [[], [{"id": 801, "active": False}] if archived else []]
    mock_google.list_calendar_events.return_value = {
        "items": [
            {
                "id": "g_ev_801",
                "summary": "Sự kiện bị xóa trên Odoo",
                "start": {"dateTime": "2026-10-25T10:00:00+07:00"},
                "end": {"dateTime": "2026-10-25T11:00:00+07:00"},
                "eventType": "default",
                "organizer": {"self": True},
            }
        ]
    }
    mock_google.delete_calendar_event.return_value = True

    result = service.sync_user(user_id=user_id)
    assert result.deleted_in_google == int(archived)
    if archived:
        mock_google.delete_calendar_event.assert_called_once_with(
            user_id=user_id, calendar_id="primary", event_id="g_ev_801"
        )
    else:
        mock_google.delete_calendar_event.assert_not_called()

    # Verify CalendarPair is removed
    with session_factory() as db:
        p = db.query(CalendarPair).filter_by(id=pair_id).first()
        assert (p is None) == archived
