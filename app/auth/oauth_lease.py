"""Distributed OAuth Token Refresh Lease Manager.

Prevents thundering herd across multiple workspace workers and ensures
only one process refreshes an expired Google access token at a time.
"""
import asyncio
from datetime import datetime, timedelta, UTC
import logging
import uuid
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import GoogleConnection
from app.workspace.models import OAuthRefreshLease

logger = logging.getLogger(__name__)


def ensure_utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class DistributedOAuthLeaseManager:
    def __init__(
        self,
        oauth_service: Any,
        worker_id: Optional[str] = None,
        lease_seconds: int = 60,
    ):
        self.oauth_service = oauth_service
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds

    async def get_valid_token(self, user_id: int, force_refresh: bool = False) -> str:
        """Retrieve a valid Google access token with distributed lease locking.
        
        - If token is valid for > 5 minutes and force_refresh is False, returns token immediately.
        - If token is expiring or force_refresh is requested, acquires a lease to refresh.
        - Other workers requesting the same user simultaneously wait for the winner to finish.
        """
        now = datetime.now(UTC)
        
        # 1. Quick check without lease
        with self.oauth_service.session_factory() as db:
            conn = db.query(GoogleConnection).filter_by(user_id=user_id).first()
            if not conn or conn.status != "connected":
                from app.google.oauth import GoogleAuthRevokedError
                raise GoogleAuthRevokedError("Google connection is missing or not connected.")

            if not force_refresh and ensure_utc(conn.expires_at) > now + timedelta(minutes=5):
                return self.oauth_service._decrypt(conn.encrypted_access_token)

        # 2. Acquire Lease
        max_wait_seconds = 10.0
        start_wait = asyncio.get_event_loop().time()
        
        while True:
            now = datetime.now(UTC)
            lease_acquired = False
            
            with self.oauth_service.session_factory() as db:
                lease = db.query(OAuthRefreshLease).filter_by(user_id=user_id).first()
                if not lease:
                    lease = OAuthRefreshLease(
                        user_id=user_id,
                        lease_holder=self.worker_id,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        connection_version=1,
                    )
                    db.add(lease)
                    try:
                        db.commit()
                        lease_acquired = True
                    except Exception:
                        db.rollback()
                else:
                    if lease.lease_expires_at is None or ensure_utc(lease.lease_expires_at) <= now or lease.lease_holder == self.worker_id:
                        lease.lease_holder = self.worker_id
                        lease.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                        db.commit()
                        lease_acquired = True

            if lease_acquired:
                break

            # Another worker is refreshing: wait and poll
            await asyncio.sleep(0.2)
            
            # Check if refreshed by the other worker
            with self.oauth_service.session_factory() as db:
                conn = db.query(GoogleConnection).filter_by(user_id=user_id).first()
                if conn and ensure_utc(conn.expires_at) > datetime.now(UTC) + timedelta(minutes=5) and not force_refresh:
                    return self.oauth_service._decrypt(conn.encrypted_access_token)

            if asyncio.get_event_loop().time() - start_wait > max_wait_seconds:
                logger.warning("Timed out waiting for another worker's OAuth lease; attempting override.")
                break

        # 3. Perform the actual refresh under lease
        try:
            if hasattr(self.oauth_service, "_perform_network_refresh"):
                new_token, expires_in = self.oauth_service._perform_network_refresh(user_id)
            else:
                # Delegate to GoogleOAuthService refresh logic
                new_token = self.oauth_service.get_valid_access_token(user_id, force_refresh=True)
                expires_in = 3600

            with self.oauth_service.session_factory() as db:
                conn = db.query(GoogleConnection).filter_by(user_id=user_id).first()
                if not conn or conn.status != "connected":
                    lease = db.query(OAuthRefreshLease).filter_by(user_id=user_id).first()
                    if lease:
                        lease.lease_holder = None
                        lease.lease_expires_at = None
                    db.commit()
                    from app.google.oauth import GoogleAuthRevokedError
                    raise GoogleAuthRevokedError("Connection was disconnected or revoked during token refresh.")

                conn.encrypted_access_token = self.oauth_service._encrypt(new_token)
                conn.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
                conn.status = "connected"
                    
                lease = db.query(OAuthRefreshLease).filter_by(user_id=user_id).first()
                if lease:
                    lease.lease_holder = None
                    lease.lease_expires_at = None
                    lease.connection_version += 1
                db.commit()
            return new_token
        except Exception:
            # Release lease on failure
            with self.oauth_service.session_factory() as db:
                lease = db.query(OAuthRefreshLease).filter_by(user_id=user_id).first()
                if lease and lease.lease_holder == self.worker_id:
                    lease.lease_holder = None
                    lease.lease_expires_at = None
                    db.commit()
            raise
