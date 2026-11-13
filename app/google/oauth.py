import base64
import hashlib
import secrets
import threading
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
from app.workspace.telemetry import measure_stage
from app.workspace.network_slots import outbound_slot


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

    def begin(self, user_id: int, session_id: int | None = None, services: list[str] | None = None) -> tuple[str, str]:
        requested_scopes = self.DEFAULT_SCOPES if services is None else ["openid", "email", *oauth_scopes(services)]
        state = generate_secure_token(32)
        state_hash = hash_token(state)
        nonce = generate_secure_token(24)
        code_verifier, code_challenge = self._generate_pkce()

        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=10)

        # Invalidate any unused prior google_oauth link tokens for this user
        with self.session_factory() as db:
            db.query(LinkToken).filter(
                LinkToken.user_id == user_id,
                LinkToken.purpose == "google_oauth",
                LinkToken.used_at.is_(None),
            ).delete()

            token_rec = LinkToken(
                token_hash=state_hash,
                purpose="google_oauth",
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
            "prompt": "consent",
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
                    LinkToken.purpose == "google_oauth",
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
            db.commit()

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

        # Upsert GoogleConnection and check subject consistency
        with self.session_factory() as db:
            existing = db.query(GoogleConnection).filter_by(user_id=user_id).first()
            if existing:
                if existing.google_sub != google_sub:
                    raise DifferentGoogleAccountError(
                        f"Cannot connect a different Google account. Existing: {existing.email}, New: {email}"
                    )
                existing.email = email
                existing.scopes = granted_scope
                existing.encrypted_access_token = self._encrypt(access_token)
                if refresh_token:
                    existing.encrypted_refresh_token = self._encrypt(refresh_token)
                existing.expires_at = expires_at
                existing.status = "connected"
            else:
                conn = GoogleConnection(
                    user_id=user_id,
                    google_sub=google_sub,
                    email=email,
                    scopes=granted_scope,
                    encrypted_access_token=self._encrypt(access_token),
                    encrypted_refresh_token=self._encrypt(refresh_token) if refresh_token else None,
                    expires_at=expires_at,
                    status="connected",
                )
                db.add(conn)
            db.commit()

    def get_valid_access_token(self, user_id: int, force_refresh: bool = False) -> str:
        lock = self._get_user_lock(user_id)
        with lock:
            now = datetime.now(UTC)
            with self.session_factory() as db:
                conn = db.query(GoogleConnection).filter_by(user_id=user_id).first()
                if not conn or conn.status != "connected":
                    raise GoogleAuthRevokedError(
                        "Google connection is missing or not in connected status."
                    )

                # If token still valid for more than 5 minutes and not force_refresh, return it
                if not force_refresh and ensure_utc(conn.expires_at) > now + timedelta(minutes=5):
                    return self._decrypt(conn.encrypted_access_token)

                refresh_token = self._decrypt(conn.encrypted_refresh_token)
                if not refresh_token:
                    conn.status = "reconnect_required"
                    db.commit()
                    raise GoogleAuthRevokedError("No refresh token available. Reconnect required.")

            # Perform refresh
            refresh_data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
            try:
                with outbound_slot(user_id), measure_stage("token_refresh_ms"):
                    resp = httpx.post(self.TOKEN_ENDPOINT, data=refresh_data, timeout=20.0)
                data = resp.json()
            except Exception as e:
                raise GoogleOAuthError(f"Network error refreshing Google token: {e}") from e

            with self.session_factory() as db:
                conn = db.query(GoogleConnection).filter_by(user_id=user_id).first()
                if resp.status_code != 200:
                    if data.get("error") == "invalid_grant":
                        conn.status = "reconnect_required"
                        db.commit()
                        raise GoogleAuthRevokedError(
                            "Google refresh token revoked. Reconnect required."
                        )
                    raise GoogleOAuthError(f"Failed to refresh token: {data}")

                new_access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                conn.encrypted_access_token = self._encrypt(new_access_token)
                conn.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
                # Google only returns new refresh_token if rotated
                if "refresh_token" in data:
                    conn.encrypted_refresh_token = self._encrypt(data["refresh_token"])
                conn.status = "connected"
                db.commit()

                return new_access_token

    def disconnect(self, user_id: int) -> None:
        with self.session_factory() as db:
            conn = db.query(GoogleConnection).filter_by(user_id=user_id).first()
            if not conn:
                return

            refresh_token = self._decrypt(conn.encrypted_refresh_token)
            conn.status = "disconnected"
            db.commit()

        # Revoke token with Google
        if refresh_token:
            try:
                httpx.post(self.REVOKE_ENDPOINT, params={"token": refresh_token}, timeout=10.0)
            except Exception:
                pass
