import asyncio
import base64
import hashlib
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy.orm import sessionmaker

from app.models import GoogleConnection, LinkToken
from app.security import (
    decrypt_secret,
    encrypt_secret,
    ensure_utc,
    generate_secure_token,
    hash_token,
)
from app.workspace.catalog import oauth_scopes
from app.workspace.network_slots import outbound_slot, request_deadline
from app.workspace.telemetry import measure_stage


class GoogleOAuthError(Exception):
    pass


class InvalidOAuthStateError(GoogleOAuthError):
    pass


class InvalidWorkspaceDomainError(GoogleOAuthError):
    pass


class DifferentGoogleAccountError(GoogleOAuthError):
    pass


class GoogleAuthRevokedError(GoogleOAuthError):
    pass


class GoogleOAuthService:
    _user_locks: dict[tuple[int, int], threading.Lock] = {}
    _global_lock = threading.Lock()

    AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

    DEFAULT_SCOPES = sorted(set([
        "openid",
        "email",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ] + oauth_scopes()))

    def __init__(
        self,
        session_factory: sessionmaker,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        encryption_key: str,
        allowed_domains: list[str] | None = None,
    ):
        self.session_factory = session_factory
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.encryption_key = encryption_key
        self.allowed_domains = allowed_domains or []

    def _get_user_lock(self, user_id: int) -> threading.Lock:
        with self._global_lock:
            key = (id(self.session_factory), user_id)
            if key not in self._user_locks:
                self._user_locks[key] = threading.Lock()
            return self._user_locks[key]

    def _encrypt(self, text: str | None) -> str | None:
        if not text:
            return None
        return encrypt_secret(text, self.encryption_key)

    def _decrypt(self, enc_text: str | None) -> str | None:
        if not enc_text:
            return None
        return decrypt_secret(enc_text, self.encryption_key)

    def _generate_pkce(self) -> tuple[str, str]:
        # 43-128 chars code verifier
        code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        return code_verifier, code_challenge

    def begin(
        self,
        user_id: int,
        session_id: int | None = None,
        services: list[str] | None = None,
        mode: str = "add",
        target_account_id: int | None = None,
    ) -> tuple[str, str]:
        requested_scopes = self.DEFAULT_SCOPES if services is None else ["openid", "email", *oauth_scopes(services)]
        state = generate_secure_token(32)
        state_hash = hash_token(state)
        nonce = generate_secure_token(24)
        code_verifier, code_challenge = self._generate_pkce()

        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=10)
        purpose = (
            f"google_oauth:reconnect:{target_account_id}"
            if mode == "reconnect" and target_account_id
            else "google_oauth"
        )

        # Invalidate any unused prior google_oauth link tokens for this user
        with self.session_factory() as db:
            db.query(LinkToken).filter(
                LinkToken.user_id == user_id,
                LinkToken.purpose.like("google_oauth%"),
                LinkToken.used_at.is_(None),
            ).delete(synchronize_session=False)

            token_rec = LinkToken(
                token_hash=state_hash,
                purpose=purpose,
                user_id=user_id,
                session_id=session_id,
                oauth_nonce=nonce,
                encrypted_pkce_verifier=self._encrypt(code_verifier),
                expires_at=expires_at,
            )
            db.add(token_rec)
            db.commit()

        scopes_str = " ".join(requested_scopes)
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": scopes_str,
            "access_type": "offline",
            "prompt": "select_account consent",
            "include_granted_scopes": "true",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        from urllib.parse import urlencode

        auth_url = f"{self.AUTH_ENDPOINT}?{urlencode(params)}"
        return auth_url, state

    def complete(self, user_id: int, state: str, code: str) -> None:
        state_hash = hash_token(state)
        now = datetime.now(UTC)

        with self.session_factory() as db:
            token_rec = (
                db.query(LinkToken)
                .filter(
                    LinkToken.token_hash == state_hash,
                    LinkToken.purpose.like("google_oauth%"),
                    LinkToken.user_id == user_id,
                    LinkToken.used_at.is_(None),
                    LinkToken.expires_at > now,
                )
                .first()
            )
            if not token_rec:
                raise InvalidOAuthStateError("Invalid, expired, or already used OAuth state.")

            code_verifier = self._decrypt(token_rec.encrypted_pkce_verifier)
            expected_nonce = token_rec.oauth_nonce
            token_rec.used_at = now
            purpose_str = token_rec.purpose or "google_oauth:add"
            db.commit()

        # Parse mode and target_account_id
        parts = purpose_str.split(":")
        if len(parts) >= 3 and parts[1] == "reconnect":
            mode = "reconnect"
            target_account_id = int(parts[2]) if parts[2].isdigit() else None
        else:
            mode = "add"
            target_account_id = None

        # Exchange authorization code for tokens
        token_data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }

        try:
            resp = httpx.post(self.TOKEN_ENDPOINT, data=token_data, timeout=20.0)
            if resp.status_code != 200:
                raise GoogleOAuthError(f"Failed to exchange token with Google: {resp.text}")
            tokens = resp.json()
        except httpx.RequestError as e:
            raise GoogleOAuthError(f"Network error communicating with Google: {e}") from e

        raw_id_token = tokens.get("id_token")
        if not raw_id_token:
            raise GoogleOAuthError("Google did not return an ID token.")

        # Verify ID token
        try:
            request = google_requests.Request()
            id_info = id_token.verify_oauth2_token(
                raw_id_token,
                request,
                audience=self.client_id,
                clock_skew_in_seconds=120,
            )
        except Exception as e:
            raise GoogleOAuthError(f"Failed to verify Google ID token: {e}") from e

        # Verify nonce
        token_nonce = id_info.get("nonce")
        if expected_nonce and token_nonce != expected_nonce:
            raise GoogleOAuthError("ID token nonce does not match expected nonce.")

        # Verify Workspace domain if configured
        if self.allowed_domains:
            hd = id_info.get("hd")
            if not hd or hd not in self.allowed_domains:
                raise InvalidWorkspaceDomainError(
                    f"Account domain '{hd}' is not in allowed Workspace domains: {self.allowed_domains}"
                )

        google_sub = id_info.get("sub")
        email = id_info.get("email", "")
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        expires_in = tokens.get("expires_in", 3600)
        granted_scope = tokens.get("scope", "")

        expires_at = now + timedelta(seconds=expires_in)

        # Upsert GoogleConnection and update GoogleAccountSelection
        with self.session_factory() as db:
            from app.google.account_resolver import GoogleAccountResolver
            from app.models import GoogleAccountSelection

            selection = GoogleAccountResolver.lock_selection(db, user_id)

            # 1. Enforce that google_sub is not bound to another gateway user
            other_user_conn = (
                db.query(GoogleConnection)
                .filter(GoogleConnection.google_sub == google_sub, GoogleConnection.user_id != user_id)
                .first()
            )
            if other_user_conn:
                raise GoogleOAuthError(
                    f"Tài khoản Google ({email}) đã được liên kết với một người dùng khác trên hệ thống."
                )

            # 2. Handle Reconnect vs Add
            target_conn = None
            if mode == "reconnect" and target_account_id:
                reconnect_target = (
                    db.query(GoogleConnection)
                    .filter_by(id=target_account_id, user_id=user_id)
                    .with_for_update()
                    .first()
                )
                if not reconnect_target:
                    raise GoogleOAuthError("Không tìm thấy tài khoản đích cần kết nối lại.")
                if reconnect_target.google_sub != google_sub:
                    raise DifferentGoogleAccountError(
                        f"Tài khoản Google được chọn ({email}) không khớp với tài khoản ban đầu ({reconnect_target.email})."
                    )
                reconnect_target.email = email
                reconnect_target.scopes = granted_scope
                reconnect_target.encrypted_access_token = self._encrypt(access_token)
                if refresh_token:
                    reconnect_target.encrypted_refresh_token = self._encrypt(refresh_token)
                reconnect_target.expires_at = expires_at
                reconnect_target.status = "connected"
                reconnect_target.connection_version += 1
                reconnect_target.link_version = getattr(reconnect_target, "link_version", 1) + 1
                target_conn = reconnect_target
            else:
                # Add mode: check if this user already linked this exact google_sub
                existing_own = (
                    db.query(GoogleConnection)
                    .filter_by(user_id=user_id, google_sub=google_sub)
                    .with_for_update()
                    .first()
                )
                if existing_own:
                    existing_own.email = email
                    existing_own.scopes = granted_scope
                    existing_own.encrypted_access_token = self._encrypt(access_token)
                    if refresh_token:
                        existing_own.encrypted_refresh_token = self._encrypt(refresh_token)
                    existing_own.expires_at = expires_at
                    existing_own.status = "connected"
                    existing_own.connection_version += 1
                    existing_own.link_version = getattr(existing_own, "link_version", 1) + 1
                    target_conn = existing_own
                else:
                    new_conn = GoogleConnection(
                        user_id=user_id,
                        google_sub=google_sub,
                        email=email,
                        scopes=granted_scope,
                        encrypted_access_token=self._encrypt(access_token),
                        encrypted_refresh_token=self._encrypt(refresh_token) if refresh_token else None,
                        expires_at=expires_at,
                        status="connected",
                        connection_version=1,
                        link_version=1,
                    )
                    db.add(new_conn)
                    db.flush()
                    target_conn = new_conn

            # 3. Update account selection: if user has no active account, auto-set as active
            if not selection:
                selection = GoogleAccountSelection(
                    user_id=user_id,
                    active_account_id=target_conn.id,
                    selection_version=1,
                    state="accepting",
                )
                db.add(selection)
            elif not selection.active_account_id:
                selection.active_account_id = target_conn.id
                selection.selection_version = getattr(selection, "selection_version", 0) + 1
                selection.state = "accepting"

            db.commit()

    def _perform_network_refresh(
        self, user_id: int, google_account_id: int | None = None
    ) -> tuple[str, int, str | None]:
        """Thực hiện HTTP POST refresh token với Google endpoint."""
        with self.session_factory() as db:
            from app.google.account_resolver import GoogleAccountResolver
            conn = GoogleAccountResolver.resolve_for_task(db, user_id, pinned_account_id=google_account_id)
            if not conn or conn.status != "connected":
                raise GoogleAuthRevokedError("Google connection is missing or not connected.")
            conn_id = conn.id
            refresh_token = self._decrypt(conn.encrypted_refresh_token)
            identity = (conn.google_sub, conn.connection_version, conn.encrypted_refresh_token)
            if not refresh_token:
                conn.status = "reconnect_required"
                db.commit()
                raise GoogleAuthRevokedError("No refresh token available. Reconnect required.")

        refresh_data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            with outbound_slot(user_id), measure_stage("token_refresh_ms"):
                deadline = request_deadline.get()
                remaining = min(20.0, deadline - time.monotonic()) if deadline is not None else 20.0
                if remaining <= 0:
                    raise TimeoutError("OAuth request deadline expired")
                resp = httpx.post(self.TOKEN_ENDPOINT, data=refresh_data, timeout=remaining)
            data = resp.json()
        except Exception as e:
            raise GoogleOAuthError(f"Network error refreshing Google token: {e}") from e

        if resp.status_code != 200:
            if data.get("error") == "invalid_grant":
                with self.session_factory() as db:
                    conn = db.query(GoogleConnection).filter_by(id=conn_id).with_for_update().first()
                    if conn and (conn.google_sub, conn.connection_version, conn.encrypted_refresh_token) == identity:
                        conn.status = "reconnect_required"
                        conn.connection_version = getattr(conn, "connection_version", 1) + 1
                        conn.link_version = getattr(conn, "link_version", 1) + 1
                        db.commit()
                raise GoogleAuthRevokedError("Google refresh token revoked. Reconnect required.")
            raise GoogleOAuthError(f"Failed to refresh token: {data}")

        new_access_token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        if not isinstance(new_access_token, str) or not new_access_token:
            raise GoogleOAuthError("Google token refresh response did not contain an access token")
        return new_access_token, expires_in, data.get("refresh_token")

    def get_valid_access_token(
        self,
        user_id: int,
        force_refresh: bool = False,
        google_account_id: int | None = None,
    ) -> str:
        # Check distributed lease in database to coordinate across multiple worker processes
        now = datetime.now(UTC)
        resolved_account_id = None
        with self.session_factory() as db:
            from app.google.account_resolver import GoogleAccountResolver
            conn = GoogleAccountResolver.resolve_for_task(db, user_id, pinned_account_id=google_account_id)
            if not conn or conn.status != "connected":
                raise GoogleAuthRevokedError(
                    "Google connection is missing or not in connected status."
                )

            # If token still valid for more than 5 minutes and not force_refresh, return it directly
            if not force_refresh and ensure_utc(conn.expires_at) > now + timedelta(minutes=5):
                return self._decrypt(conn.encrypted_access_token)
            resolved_account_id = conn.id

        # Acquire distributed lease across processes keyed by google_account_id
        from app.auth.oauth_lease import DistributedOAuthLeaseManager
        lease_mgr = DistributedOAuthLeaseManager(self)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run,
                    lease_mgr.get_valid_token(
                        user_id,
                        force_refresh=force_refresh,
                        google_account_id=resolved_account_id,
                    ),
                )
                return future.result(timeout=max(float(lease_mgr.lease_seconds) + 15.0, 45.0))
        else:
            return asyncio.run(
                lease_mgr.get_valid_token(
                    user_id,
                    force_refresh=force_refresh,
                    google_account_id=resolved_account_id,
                )
            )

    def disconnect(self, user_id: int, google_account_id: int | None = None) -> None:
        with self.session_factory() as db:
            from app.google.account_resolver import GoogleAccountResolver
            from app.workspace.activity_tracker import WorkspaceActivityTracker
            selection = GoogleAccountResolver.lock_selection(db, user_id)
            conn = GoogleAccountResolver.resolve_for_task(db, user_id, pinned_account_id=google_account_id)
            if not conn:
                return

            blockers = WorkspaceActivityTracker.get_active_blockers(db, user_id, account_id=conn.id)
            if blockers:
                raise GoogleOAuthError(
                    f"Không thể ngắt kết nối tài khoản ({conn.email}) khi còn tác vụ đang chạy."
                )

            refresh_token = self._decrypt(conn.encrypted_refresh_token)
            conn.status = "disconnected"
            conn.connection_version = getattr(conn, "connection_version", 1) + 1
            conn.link_version = getattr(conn, "link_version", 1) + 1

            if selection and selection.active_account_id == conn.id:
                selection.active_account_id = None
                selection.selection_version += 1
                selection.state = "accepting"
                selection.draining_since = None
                selection.target_account_id = None
            db.commit()

        # Revoke token with Google
        if refresh_token:
            try:
                httpx.post(self.REVOKE_ENDPOINT, params={"token": refresh_token}, timeout=10.0)
            except Exception:
                pass

    def delete(self, user_id: int, google_account_id: int) -> None:
        with self.session_factory() as db:
            from app.google.account_resolver import GoogleAccountResolver
            from app.workspace.activity_tracker import WorkspaceActivityTracker

            selection = GoogleAccountResolver.lock_selection(db, user_id)
            conn = GoogleAccountResolver.resolve_for_task(db, user_id, pinned_account_id=google_account_id)
            if not conn:
                return

            blockers = WorkspaceActivityTracker.get_active_blockers(db, user_id, account_id=conn.id)
            if blockers:
                raise GoogleOAuthError(
                    f"Không thể xóa tài khoản ({conn.email}) khi còn tác vụ đang chạy."
                )

            refresh_token = self._decrypt(conn.encrypted_refresh_token)

            # Clear selection if this account is active
            if selection and selection.active_account_id == conn.id:
                selection.active_account_id = None
                selection.selection_version += 1
                selection.state = "accepting"
                selection.draining_since = None
                selection.target_account_id = None

            db.delete(conn)
            db.commit()

        # Revoke token with Google
        if refresh_token:
            try:
                httpx.post(self.REVOKE_ENDPOINT, params={"token": refresh_token}, timeout=10.0)
            except Exception:
                pass
