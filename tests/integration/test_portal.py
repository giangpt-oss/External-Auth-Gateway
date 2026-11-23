from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.db import init_db
from app.main import create_app
from app.models import CalendarPair, OdooCredential, SyncIntent, User
from app.odoo.client import OdooAuthError
from app.security import encrypt_secret


@pytest.fixture
def portal_client(temp_db_path, fernet_key):
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{temp_db_path}",
        ENCRYPTION_KEY=fernet_key,
        ODOO_URL="https://mycompany.odoo.com",
        ODOO_DB="mycompany",
    )
    app = create_app(settings)

    # Seed an active user
    _, session_factory = init_db(f"sqlite:///{temp_db_path}")
    with session_factory() as db:
        user = User(
            tenant="https://mycompany.odoo.com",
            database="mycompany",
            odoo_uid=77,
            login="portal_user@company.com",
            name="Nguyễn Văn A",
            tz="Asia/Bangkok",
        )
        db.add(user)
        db.flush()
        db.add(
            OdooCredential(
                user_id=user.id,
                credential_kind="api_key",
                encrypted_secret=encrypt_secret("valid_secret", fernet_key),
            )
        )
        # Add a conflict calendar pair
        db.add(
            CalendarPair(
                user_id=user.id,
                google_subject="sub_77",
                google_calendar_id="primary",
                odoo_event_id=501,
                google_event_id="g_501",
                status="conflict",
            )
        )
        # Add an ambiguous intent
        db.add(
            SyncIntent(
                user_id=user.id,
                source="google",
                target="odoo",
                source_event_id="g_event_123",
                status="ambiguous",
            )
        )
        db.commit()

    with TestClient(app) as client:
        yield client, session_factory


def test_portal_login_page_renders_form(portal_client):
    client, _ = portal_client
    res = client.get("/login")
    assert res.status_code == 200
    assert "External Auth Gateway" in res.text
    assert "Đăng nhập bằng Odoo" in res.text
    assert 'data-stitch-theme="light"' in res.text
    assert 'data-stitch-theme="dark"' in res.text
    assert 'name="csrf_token"' in res.text
    assert "Tài khoản Odoo" in res.text
    assert "Mật khẩu Odoo" in res.text
    assert 'name="credential_kind"' not in res.text
    assert "API Key" not in res.text


def test_portal_rejected_password_stays_in_login_card(portal_client):
    client, _ = portal_client
    mock_odoo = MagicMock()
    mock_odoo.authenticate_password.side_effect = OdooAuthError("invalid password")
    client.app.state.odoo_client = mock_odoo
    client.get("/login")
    response = client.post(
        "/auth/login?theme=dark",
        data={"login": "portal_user@company.com", "secret": "incorrect",
              "csrf_token": client.cookies["anon_csrf"]},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert 'id="odooLoginForm"' in response.text
    assert 'data-stitch-theme="dark"' in response.text
    assert 'action="/auth/login?theme=dark"' in response.text
    assert 'role="alert"' in response.text
    assert 'value="portal_user@company.com"' in response.text
    assert "incorrect" not in response.text


def test_portal_dashboard_requires_login(portal_client):
    client, _ = portal_client
    res = client.get("/", follow_redirects=False)
    # Unauthenticated should redirect to /login
    assert res.status_code in (302, 303)
    assert res.headers["location"] == "/login"


def test_portal_dashboard_renders_authenticated_user(portal_client):
    client, session_factory = portal_client
    mock_odoo = MagicMock()
    mock_odoo.authenticate_password.return_value = 77
    mock_odoo.execute.return_value = [
        {
            "id": 77,
            "name": "Nguyễn Văn A",
            "login": "portal_user@company.com",
            "active": True,
            "share": False,
            "tz": "Asia/Bangkok",
            "partner_id": [77, "Nguyễn Văn A"],
        }
    ]
    client.app.state.odoo_client = mock_odoo

    # 1. Fetch CSRF from login page
    login_page = client.get("/login")
    import re

    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text)
    assert csrf_match is not None
    csrf_token = csrf_match.group(1)

    # 2. Login via form
    login_res = client.post(
        "/auth/login",
        data={
            "login": "portal_user@company.com",
            "secret": "valid_secret",
            "csrf_token": csrf_token,
        },
        headers={"Accept": "text/html"},
        follow_redirects=True,
    )
    assert login_res.status_code == 200
    assert "Nguyễn Văn A" in login_res.text
    assert "Google Workspace" in login_res.text
    assert "Kết nối tài khoản Google" in login_res.text
    assert "Telegram" in login_res.text
    with session_factory() as db:
        user = db.query(User).filter_by(odoo_uid=77).one()
        assert db.query(OdooCredential).filter_by(user_id=user.id).one().credential_kind == "password"




def test_portal_telegram_disconnect_html_redirect(portal_client, temp_db_path):
    client, session_factory = portal_client
    mock_odoo = MagicMock()
    mock_odoo.authenticate_password.return_value = 77
    mock_odoo.execute.return_value = [
        {
            "id": 77,
            "name": "Nguyễn Văn A",
            "login": "portal_user@company.com",
            "active": True,
            "share": False,
            "tz": "Asia/Bangkok",
            "partner_id": [77, "Nguyễn Văn A"],
        }
    ]
    client.app.state.odoo_client = mock_odoo

    # Add a connected telegram connection
    from app.models import TelegramConnection
    with session_factory() as db:
        user = db.query(User).filter_by(login="portal_user@company.com").first()
        db.add(
            TelegramConnection(
                user_id=user.id,
                telegram_user_id=888999,
                private_chat_id=888999,
                username="test_tele_user",
                status="connected",
            )
        )
        db.commit()

    # 1. Login
    login_page = client.get("/login")
    import re
    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text)
    client.post(
        "/auth/login",
        data={
            "login": "portal_user@company.com",
            "secret": "valid_secret",
            "csrf_token": csrf_match.group(1),
        },
        headers={"Accept": "text/html"},
    )

    # 2. Form submission from portal to /connections/telegram/disconnect
    # Browser sends Content-Type: application/x-www-form-urlencoded and Accept: text/html
    res = client.post(
        "/connections/telegram/disconnect",
        data={"csrf_token": "dummy"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    # Must redirect (303) back to dashboard, NOT return 200 with raw JSON
    assert res.status_code == 303
    assert res.headers["location"] == "/"

    # 3. Verify status in DB is disconnected
    with session_factory() as db:
        conn = db.query(TelegramConnection).filter_by(telegram_user_id=888999).first()
        assert conn.status == "disconnected"
