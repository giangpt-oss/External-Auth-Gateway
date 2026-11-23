"""Test separation of link_version and connection_version for GoogleConnection.

Proves:
1. Normal routine token refresh increments connection_version but PRESERVES link_version unchanged.
2. Relink via handle_callback increments link_version (invalidating stale indexed data).
3. Disconnect increments link_version and marks connection as disconnected.
4. Token revocation (invalid_grant) increments link_version and marks reconnect_required.
5. Alembic migration d1e2f3a4b5c6 cleanly adds link_version and downgrades back to c7d8e9f0a1b2.
"""

from datetime import UTC, datetime, timedelta
import os
import tempfile
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.google.oauth import GoogleOAuthService, GoogleAuthRevokedError
from app.models import Base, GoogleConnection, LinkToken, User
from app.security import encrypt_secret
from app.workspace.models import OAuthRefreshLease

FERNET_KEY = "_3pDgavXi1u3T76np_zSNJQcNS87hKW7fEdQXjlu3Ew="


@pytest.fixture
def separation_env(tmp_path):
    db_file = tmp_path / "test_separation.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Seed User and GoogleConnection
    with Session() as db:
        user = User(
            id=1,
            tenant="default",
            database="odoo",
            odoo_uid=1,
            login="user1@example.com",
            name="User 1",
            active=True,
        )
        db.add(user)
        db.flush()

        conn = GoogleConnection(
            user_id=1,
            google_sub="sub_google_user_1",
            email="user1@example.com",
            scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_access_token=encrypt_secret("access_token_v1", FERNET_KEY),
            encrypted_refresh_token=encrypt_secret("refresh_token_v1", FERNET_KEY),
            expires_at=datetime.now(UTC) + timedelta(minutes=60),
            status="connected",
            connection_version=1,
            link_version=1,
        )
        db.add(conn)
        db.commit()

    service = GoogleOAuthService(
        session_factory=Session,
        client_id="mock_client_id",
        client_secret="mock_client_secret",
        redirect_uri="http://localhost:8080/auth/callback",
        encryption_key=FERNET_KEY,
    )

    yield Session, service

    engine.dispose()
    import gc
    gc.collect()
    try:
        if db_file.exists():
            db_file.unlink()
    except PermissionError:
        pass


def test_routine_token_refresh_preserves_link_version(separation_env):
    """Case 1: Routine token refresh increments connection_version but keeps link_version unchanged."""
    Session, service = separation_env

    # Mock network refresh returning a new token
    def mock_perform_refresh(user_id: int):
        return "access_token_refreshed_v2", 3600, None

    service._perform_network_refresh = mock_perform_refresh

    # Force refresh token
    new_token = service.get_valid_access_token(user_id=1, force_refresh=True)
    assert new_token == "access_token_refreshed_v2"

    with Session() as db:
        conn = db.query(GoogleConnection).filter_by(user_id=1).first()
        assert conn is not None
        # connection_version MUST increment to coordinate concurrency and avoid stale overwrite
        assert conn.connection_version == 2
        # link_version MUST remain stable so indexed Drive documents are NOT invalidated
        assert conn.link_version == 1


from unittest.mock import MagicMock, patch

def test_relink_increments_link_version(separation_env):
    """Case 2: Relinking Google account increments link_version (invalidating old Drive index)."""
    Session, service = separation_env

    # User initiates OAuth flow to relink
    _, state = service.begin(user_id=1, session_id=None)

    with Session() as db:
        lt = db.query(LinkToken).filter_by(purpose="google_oauth", user_id=1).first()
        expected_nonce = lt.oauth_nonce

    mock_id_info = {
        "sub": "sub_google_user_1",
        "email": "user1@example.com",
        "nonce": expected_nonce,
    }

    mock_tokens = {
        "id_token": "mock_id_token_jwt",
        "access_token": "relinked_access_token_v2",
        "refresh_token": "relinked_refresh_token_v2",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/drive.readonly",
    }

    with (
        patch("httpx.post") as mock_http,
        patch("google.oauth2.id_token.verify_oauth2_token") as mock_verify,
    ):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = mock_tokens
        mock_http.return_value = resp
        mock_verify.return_value = mock_id_info

        service.complete(user_id=1, state=state, code="auth_code_123")

    with Session() as db:
        conn = db.query(GoogleConnection).filter_by(user_id=1).first()
        assert conn is not None
        assert conn.connection_version == 2
        # link_version MUST increment on relink!
        assert conn.link_version == 2
        assert service._decrypt(conn.encrypted_access_token) == "relinked_access_token_v2"
        assert service._decrypt(conn.encrypted_refresh_token) == "relinked_refresh_token_v2"


def test_disconnect_increments_link_version(separation_env):
    """Case 3: Disconnect marks status as disconnected and increments link_version."""
    Session, service = separation_env

    service.disconnect(user_id=1)

    with Session() as db:
        conn = db.query(GoogleConnection).filter_by(user_id=1).first()
        assert conn is not None
        assert conn.status == "disconnected"
        assert conn.connection_version == 2
        # link_version MUST increment on disconnect!
        assert conn.link_version == 2


def test_revoked_token_invalid_grant_increments_link_version(separation_env):
    """Case 4: invalid_grant marks status as reconnect_required and increments link_version."""
    Session, service = separation_env

    import httpx
    # Simulate Google returning 400 invalid_grant
    def mock_post_revoked(url, **kwargs):
        req = httpx.Request("POST", url)
        return httpx.Response(400, json={"error": "invalid_grant"}, request=req)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "post", mock_post_revoked)
        with pytest.raises(GoogleAuthRevokedError):
            service._perform_network_refresh(user_id=1)

    with Session() as db:
        conn = db.query(GoogleConnection).filter_by(user_id=1).first()
        assert conn is not None
        assert conn.status == "reconnect_required"
        assert conn.connection_version == 2
        assert conn.link_version == 2


def test_alembic_upgrade_and_downgrade_d1e2f3a4b5c6(tmp_path):
    """Case 5: Alembic migration d1e2f3a4b5c6 correctly adds link_version and downgrades."""
    db_file = tmp_path / "test_migration_a1.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)

    # 1. Start from schema at c7d8e9f0a1b2
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(255) NOT NULL,
                active BOOLEAN DEFAULT 1
            )
        """))
        conn.execute(text("""
            CREATE TABLE google_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                google_sub VARCHAR(255) NOT NULL UNIQUE,
                email VARCHAR(255) NOT NULL,
                scopes TEXT NOT NULL,
                encrypted_access_token TEXT NOT NULL,
                encrypted_refresh_token TEXT,
                expires_at TIMESTAMP NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'connected',
                connection_version INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """))
        # Stamp alembic version table at c7d8e9f0a1b2
        conn.execute(text("""
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )
        """))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('c7d8e9f0a1b2')"))
        # Insert test connection
        conn.execute(text("INSERT INTO users (id, email) VALUES (1, 'u@example.com')"))
        conn.execute(text("""
            INSERT INTO google_connections (
                user_id, google_sub, email, scopes, encrypted_access_token, expires_at, connection_version
            ) VALUES (
                1, 'sub_1', 'u@example.com', 'openid', 'token', CURRENT_TIMESTAMP, 5
            )
        """))

    alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"))
    cfg = Config(alembic_ini_path)
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Verify link_version does not exist yet
    insp = inspect(engine)
    cols_before = {c["name"] for c in insp.get_columns("google_connections")}
    assert "link_version" not in cols_before

    # 2. Upgrade to head (d1e2f3a4b5c6)
    command.upgrade(cfg, "head")

    insp = inspect(engine)
    cols_after = {c["name"] for c in insp.get_columns("google_connections")}
    assert "link_version" in cols_after

    with engine.begin() as conn:
        row = conn.execute(text("SELECT connection_version, link_version FROM google_connections WHERE user_id = 1")).fetchone()
        assert row[0] == 5
        assert row[1] == 1  # Server default applied

    # 3. Downgrade back to c7d8e9f0a1b2
    command.downgrade(cfg, "c7d8e9f0a1b2")

    insp = inspect(engine)
    cols_downgraded = {c["name"] for c in insp.get_columns("google_connections")}
    assert "link_version" not in cols_downgraded
    assert "connection_version" in cols_downgraded

    engine.dispose()
