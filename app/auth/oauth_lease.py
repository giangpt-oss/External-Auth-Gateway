"""Distributed OAuth Token Refresh Lease Manager.

Prevents thundering herd across multiple workspace workers and ensures
only one process refreshes an expired Google access token at a time.
"""
import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

from app.models import GoogleConnection
from app.workspace.models import OAuthRefreshLease

logger = logging.getLogger(__name__)


def ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class DistributedOAuthLeaseManager:
    def __init__(
        self,
        oauth_service: Any,
        worker_id: str | None = None,
        lease_seconds: int = 60,
    ):
        self.oauth_service = oauth_service
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds

    async def get_valid_token(
        self,
        user_id: int,
        force_refresh: bool = False,
        google_account_id: int | None = None,
    ) -> str:
        """Retrieve a valid Google access token with distributed lease locking keyed by Google account.

        - If token is valid for > 5 minutes and force_refresh is False, returns token immediately.
        - If token is expiring or force_refresh is requested, acquires a lease to refresh.
        - Keyed by google_account_id so refresh on one account never blocks other accounts.
        """
        now = datetime.now(UTC)

        # 1. Quick check without lease
        with self.oauth_service.session_factory() as db:
            from app.google.account_resolver import GoogleAccountResolver
            conn = GoogleAccountResolver.resolve_for_task(db, user_id, pinned_account_id=google_account_id)
            if not conn or conn.status != "connected":
                from app.google.oauth import GoogleAuthRevokedError
                raise GoogleAuthRevokedError("Google connection is missing or not connected.")

            account_id = conn.id
            if not force_refresh and ensure_utc(conn.expires_at) > now + timedelta(minutes=5):
                return self.oauth_service._decrypt(conn.encrypted_access_token)

            expected_connection_version = getattr(conn, "connection_version", 1)
            expected_sub = conn.google_sub
            expected_refresh_token = conn.encrypted_refresh_token
            initial_token = conn.encrypted_access_token

        # 2. Acquire Lease with atomic conditional update
        max_wait_seconds = max(float(self.lease_seconds) + 5.0, 35.0)
        start_wait = asyncio.get_event_loop().time()
        lease_acquired = False

        while True:
            now = datetime.now(UTC)

            with self.oauth_service.session_factory() as db:
                lease = db.query(OAuthRefreshLease).filter(
                    (OAuthRefreshLease.google_account_id == account_id)
                    | (OAuthRefreshLease.google_account_id.is_(None) & (OAuthRefreshLease.user_id == user_id))
                ).first()
                if not lease:
                    lease = OAuthRefreshLease(
                        user_id=user_id,
                        google_account_id=account_id,
                        lease_holder=None,
                        lease_expires_at=None,
                        connection_version=expected_connection_version,
                    )
                    db.add(lease)
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                elif lease.google_account_id is None:
                    lease.google_account_id = account_id
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()

                # Atomic conditional UPDATE: only exactly 1 worker can acquire for this account
                stmt = (
                    update(OAuthRefreshLease)
                    .where(
                        OAuthRefreshLease.google_account_id == account_id,
                        (OAuthRefreshLease.lease_holder.is_(None))
                        | (OAuthRefreshLease.lease_expires_at <= now)
                        | (OAuthRefreshLease.lease_holder == self.worker_id),
                    )
                    .values(
                        lease_holder=self.worker_id,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    )
                    .execution_options(synchronize_session=False)
                )
                res = db.execute(stmt)
                db.commit()
                if res.rowcount == 1:
                    lease_acquired = True

            if lease_acquired:
                break

            # Another worker is refreshing: wait and poll
            await asyncio.sleep(0.1)

            # Check if refreshed by the other worker
            with self.oauth_service.session_factory() as db:
                conn = db.query(GoogleConnection).filter_by(id=account_id).first()
                lease = db.query(OAuthRefreshLease).filter_by(google_account_id=account_id).first()
                if conn and lease:
                    curr_version = getattr(conn, "connection_version", 1)
                    if (
                        curr_version > expected_connection_version
                        or (conn.encrypted_access_token != initial_token and ensure_utc(conn.expires_at) > datetime.now(UTC) + timedelta(minutes=5))
                        or (not force_refresh and ensure_utc(conn.expires_at) > datetime.now(UTC) + timedelta(minutes=5))
                    ):
                        return self.oauth_service._decrypt(conn.encrypted_access_token)

            if asyncio.get_event_loop().time() - start_wait > max_wait_seconds:
                logger.warning("Worker %s timed out waiting for another worker's OAuth lease (user %s account %s).", self.worker_id, user_id, account_id)
                break

        # If lease was not acquired, strictly forbid network refresh!
        if not lease_acquired:
            with self.oauth_service.session_factory() as db:
                conn = db.query(GoogleConnection).filter_by(id=account_id).first()
                if conn:
                    curr_version = getattr(conn, "connection_version", 1)
                    if (
                        curr_version > expected_connection_version
                        or (conn.encrypted_access_token != initial_token and ensure_utc(conn.expires_at) > datetime.now(UTC) + timedelta(minutes=5))
                        or (not force_refresh and ensure_utc(conn.expires_at) > datetime.now(UTC) + timedelta(minutes=5))
                    ):
                        return self.oauth_service._decrypt(conn.encrypted_access_token)
            raise TimeoutError(f"Worker {self.worker_id} timed out waiting for OAuth lease for user {user_id} account {account_id}; refusing to refresh without lease.")

        # 3. Before dispatching network refresh, check if token was ALREADY refreshed while waiting
        with self.oauth_service.session_factory() as db:
            conn = db.query(GoogleConnection).filter_by(id=account_id).first()
            if conn and (
                getattr(conn, "connection_version", 1) > expected_connection_version
                or (conn.encrypted_access_token != initial_token and ensure_utc(conn.expires_at) > datetime.now(UTC) + timedelta(minutes=5))
            ):
                # Another worker completed the refresh while we were waiting!
                # Release our newly claimed lease and return the refreshed token.
                stmt = (
                    update(OAuthRefreshLease)
                    .where(
                        OAuthRefreshLease.google_account_id == account_id,
                        OAuthRefreshLease.lease_holder == self.worker_id,
                    )
                    .values(
                        lease_holder=None,
                        lease_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                db.execute(stmt)
                db.commit()
                return self.oauth_service._decrypt(conn.encrypted_access_token)

        # 4. Perform the actual refresh under lease
        try:
            rotated_refresh_token = None
            if hasattr(self.oauth_service, "_perform_network_refresh"):
                if google_account_id is not None:
                    try:
                        new_token, expires_in, *rotated = self.oauth_service._perform_network_refresh(user_id, google_account_id=account_id)
                    except TypeError:
                        new_token, expires_in, *rotated = self.oauth_service._perform_network_refresh(user_id)
                else:
                    new_token, expires_in, *rotated = self.oauth_service._perform_network_refresh(user_id)
                if rotated:
                    rotated_refresh_token = rotated[0]
            else:
                if google_account_id is not None:
                    new_token = self.oauth_service.get_valid_access_token(user_id, force_refresh=True, google_account_id=account_id)
                else:
                    new_token = self.oauth_service.get_valid_access_token(user_id, force_refresh=True)
                expires_in = 3600

            with self.oauth_service.session_factory() as db:
                conn = db.query(GoogleConnection).filter_by(id=account_id).with_for_update().first()
                lease = db.query(OAuthRefreshLease).filter_by(google_account_id=account_id).with_for_update().first()
                if not lease or lease.lease_holder != self.worker_id:
                    raise TimeoutError("OAuth refresh lease was lost before saving tokens")
                lease.lease_holder = None
                lease.lease_expires_at = None
                lease.connection_version += 1

                if not conn or conn.status != "connected":
                    db.commit()
                    from app.google.oauth import GoogleAuthRevokedError
                    raise GoogleAuthRevokedError("Connection was disconnected or revoked during token refresh.")

                # Check if connection was relinked or changed during in-flight refresh
                curr_version = getattr(conn, "connection_version", 1)
                if (
                    curr_version != expected_connection_version
                    or conn.google_sub != expected_sub
                    or conn.encrypted_refresh_token != expected_refresh_token
                ):
                    logger.warning(
                        "GoogleConnection changed (version %s != %s or sub changed) during in-flight refresh. Discarding stale tokens.",
                        expected_connection_version, curr_version
                    )
                    db.commit()
                    return self.oauth_service._decrypt(conn.encrypted_access_token)

                conn.encrypted_access_token = self.oauth_service._encrypt(new_token)
                if rotated_refresh_token:
                    conn.encrypted_refresh_token = self.oauth_service._encrypt(rotated_refresh_token)
                conn.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
                conn.status = "connected"
                conn.connection_version = curr_version + 1
                db.commit()
            return new_token
        except Exception:
            # Release lease on failure
            with self.oauth_service.session_factory() as db:
                stmt = (
                    update(OAuthRefreshLease)
                    .where(
                        OAuthRefreshLease.google_account_id == account_id,
                        OAuthRefreshLease.lease_holder == self.worker_id,
                    )
                    .values(
                        lease_holder=None,
                        lease_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                db.execute(stmt)
                db.commit()
            raise

    async def refresh_active_expiring_tokens(
        self,
        max_activity_minutes: int = 30,
        expiry_window_minutes: int = 5,
    ) -> list[int]:
        """Proactively refresh tokens for users whose tokens expire soon AND who were active recently.

        - Filters users who had activity within `max_activity_minutes`.
        - Identifies tokens expiring within `expiry_window_minutes`.
        - Calls `get_valid_token(uid, force_refresh=True)` safely under distributed lease.
        """
        now = datetime.now(UTC)
        activity_threshold = now - timedelta(minutes=max_activity_minutes)
        expiry_threshold = now + timedelta(minutes=expiry_window_minutes)

        candidates = []
        with self.oauth_service.session_factory() as db:
            from app.models import User
            # Active connections expiring soon for users active recently
            query = (
                db.query(GoogleConnection.user_id)
                .join(User, User.id == GoogleConnection.user_id)
                .filter(
                    GoogleConnection.status == "connected",
                    GoogleConnection.expires_at <= expiry_threshold,
                    (User.updated_at >= activity_threshold) | (GoogleConnection.updated_at >= activity_threshold),
                )
            )
            candidates = [row[0] for row in query.all()]

        refreshed_users = []
        for uid in candidates:
            try:
                await self.get_valid_token(uid, force_refresh=True)
                refreshed_users.append(uid)
            except Exception as exc:
                logger.warning(f"Failed proactive token refresh for user {uid}: {exc}")

        return refreshed_users
