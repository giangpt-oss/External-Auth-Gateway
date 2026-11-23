from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.db import init_db
from app.google.oauth import (
    DifferentGoogleAccountError,
    GoogleAuthRevokedError,
    GoogleOAuthService,
    InvalidOAuthStateError,
    InvalidWorkspaceDomainError,
)
from app.models import GoogleConnection, LinkToken, User
from app.security import decrypt_secret


@pytest.fixture
def google_components(temp_db_path, fernet_key):
    engine, session_factory = init_db(f"sqlite:///{temp_db_path}")

    # Create test user
    with session_factory() as db:
        user = User(
            tenant="mytenant",
            database="mydb",
            odoo_uid=1,
            login="user1@company.com",
            name="User One",
        )
        db.add(user)
        db.commit()
        user_id = user.id

    service = GoogleOAuthService(
        session_factory=session_factory,
        client_id="test_client_id.apps.googleusercontent.com",
        client_secret="test_client_secret",
        redirect_uri="https://hub.company.com/oauth/google/callback",
        encryption_key=fernet_key,
        allowed_domains=["company.com"],
    )

    yield service, session_factory, user_id
    engine.dispose()


def test_begin_oauth_generates_url_and_link_token(google_components):
    service, session_factory, user_id = google_components
    auth_url, state = service.begin(user_id=user_id, session_id=None)

    assert "accounts.google.com/o/oauth2/v2/auth" in auth_url
    assert f"client_id={service.client_id}" in auth_url
    assert "code_challenge=" in auth_url
    assert "code_challenge_method=S256" in auth_url
    assert f"state={state}" in auth_url

    with session_factory() as db:
        token_rec = db.query(LinkToken).filter_by(purpose="google_oauth").first()
        assert token_rec is not None
        assert token_rec.user_id == user_id
        assert token_rec.oauth_nonce is not None
        assert token_rec.encrypted_pkce_verifier is not None
        assert token_rec.used_at is None


def test_callback_invalid_or_used_state(google_components):
    service, _, user_id = google_components
    with pytest.raises(InvalidOAuthStateError):
        service.complete(user_id=user_id, state="invalid_state_token", code="code123")


def test_callback_success_stores_connection(google_components, fernet_key):
    service, session_factory, user_id = google_components
    _, state = service.begin(user_id=user_id, session_id=None)

    mock_token_resp = {
        "access_token": "mock_access_token_123",
        "refresh_token": "mock_refresh_token_456",
        "expires_in": 3600,
        "scope": "openid email https://www.googleapis.com/auth/calendar.events",
        "id_token": "mock_id_token",
    }
    mock_id_info = {
        "sub": "google_sub_9999",
        "email": "user1@company.com",
        "hd": "company.com",
        "nonce": None,  # Will be matched with link_token nonce
    }

    with session_factory() as db:
        lt = db.query(LinkToken).filter_by(purpose="google_oauth").first()
        mock_id_info["nonce"] = lt.oauth_nonce

    with (
        patch("httpx.post") as mock_http,
        patch("google.oauth2.id_token.verify_oauth2_token") as mock_verify,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_token_resp
        mock_http.return_value = mock_resp
        mock_verify.return_value = mock_id_info

        service.complete(user_id=user_id, state=state, code="auth_code_789")

    # Verify DB records
    with session_factory() as db:
        conn = db.query(GoogleConnection).filter_by(user_id=user_id).first()
        assert conn is not None
        assert conn.google_sub == "google_sub_9999"
        assert conn.email == "user1@company.com"
        assert conn.status == "connected"
        assert decrypt_secret(conn.encrypted_access_token, fernet_key) == "mock_access_token_123"
        assert decrypt_secret(conn.encrypted_refresh_token, fernet_key) == "mock_refresh_token_456"

        # LinkToken marked used
        lt = db.query(LinkToken).filter_by(purpose="google_oauth").first()
        assert lt.used_at is not None


def test_callback_rejects_unauthorized_domain(google_components):
    service, session_factory, user_id = google_components
    _, state = service.begin(user_id=user_id, session_id=None)

    mock_id_info = {
        "sub": "google_sub_hack",
        "email": "hacker@otherdomain.com",
        "hd": "otherdomain.com",  # Not in allowed_domains
    }

    with session_factory() as db:
        lt = db.query(LinkToken).filter_by(purpose="google_oauth").first()
        mock_id_info["nonce"] = lt.oauth_nonce

    with (
        patch("httpx.post") as mock_http,
        patch("google.oauth2.id_token.verify_oauth2_token") as mock_verify,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id_token": "token",
            "access_token": "acc",
            "expires_in": 3600,
        }
        mock_http.return_value = mock_resp
        mock_verify.return_value = mock_id_info

        with pytest.raises(InvalidWorkspaceDomainError):
            service.complete(user_id=user_id, state=state, code="code")


def test_callback_reconnect_different_sub_rejected(google_components):
    service, session_factory, user_id = google_components

    # Pre-populate existing connection
    with session_factory() as db:
        existing = GoogleConnection(
            user_id=user_id,
            google_sub="original_sub_111",
            email="user1@company.com",
            scopes="email",
            encrypted_access_token="enc",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="connected",
        )
        db.add(existing)
        db.commit()
        account_id = existing.id

    _, state = service.begin(user_id=user_id, mode="reconnect", target_account_id=account_id)
    mock_id_info = {
        "sub": "different_sub_222",  # Different account!
        "email": "user2@company.com",
        "hd": "company.com",
    }
    with session_factory() as db:
        lt = db.query(LinkToken).filter_by(purpose=f"google_oauth:reconnect:{account_id}").first()
        mock_id_info["nonce"] = lt.oauth_nonce

    with (
        patch("httpx.post") as mock_http,
        patch("google.oauth2.id_token.verify_oauth2_token") as mock_verify,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id_token": "token",
            "access_token": "acc",
            "expires_in": 3600,
        }
        mock_http.return_value = mock_resp
        mock_verify.return_value = mock_id_info

        with pytest.raises(DifferentGoogleAccountError):
            service.complete(user_id=user_id, state=state, code="code")


def test_refresh_token_success_and_invalid_grant(google_components, fernet_key):
    service, session_factory, user_id = google_components

    # Create connection needing refresh
    with session_factory() as db:
        conn = GoogleConnection(
            user_id=user_id,
            google_sub="sub_refresh_test",
            email="user@company.com",
            scopes="email calendar",
            encrypted_access_token="old_acc",
            encrypted_refresh_token=service._encrypt("my_refresh_token"),
            expires_at=datetime.now(UTC) - timedelta(minutes=5),  # expired
            status="connected",
        )
        db.add(conn)
        db.commit()

    # 1. Success refresh
    with patch("httpx.post") as mock_http:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new_refreshed_access_token",
            "expires_in": 3600,
        }
        mock_http.return_value = mock_resp

        new_token = service.get_valid_access_token(user_id)
        assert new_token == "new_refreshed_access_token"

    # 2. invalid_grant
    with patch("httpx.post") as mock_http:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "invalid_grant"}
        mock_http.return_value = mock_resp

        # Force expiry to trigger refresh
        with session_factory() as db:
            c = db.query(GoogleConnection).filter_by(user_id=user_id).first()
            c.expires_at = datetime.now(UTC) - timedelta(minutes=5)
            db.commit()

        with pytest.raises(GoogleAuthRevokedError):
            service.get_valid_access_token(user_id)

        with session_factory() as db:
            c = db.query(GoogleConnection).filter_by(user_id=user_id).first()
            assert c.status == "reconnect_required"
