from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.db import init_db
from app.main import create_app
from app.models import CalendarPair, OdooCredential, SyncIntent, User
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
    assert "Cổng Xác Thực & Đồng Bộ" in res.text
    assert 'name="csrf_token"' in res.text
    assert "Email Odoo" in res.text


def test_portal_dashboard_requires_login(portal_client):
    client, _ = portal_client
    res = client.get("/", follow_redirects=False)
    # Unauthenticated should redirect to /login
    assert res.status_code in (302, 303)
    assert res.headers["location"] == "/login"


def test_portal_dashboard_renders_authenticated_user(portal_client):
    client, session_factory = portal_client
    mock_odoo = MagicMock()
    mock_odoo.authenticate.return_value = 77
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
            "credential_kind": "api_key",
            "csrf_token": csrf_token,
        },
        headers={"Accept": "text/html"},
        follow_redirects=True,
    )
    assert login_res.status_code == 200
    assert "Nguyễn Văn A" in login_res.text
    assert "Google Workspace" in login_res.text
    assert "Telegram" in login_res.text
    assert "Đồng bộ Lịch" in login_res.text
    # Should see conflict and ambiguous section
    assert "Xung đột lịch cần xử lý" in login_res.text
    assert "Lịch chờ đối soát" in login_res.text


def test_calendar_sync_csrf_validation(portal_client):
    client, session_factory = portal_client
    mock_odoo = MagicMock()
    mock_odoo.authenticate.return_value = 77
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

    # 1. Login
    login_page = client.get("/login")
    import re

    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text)
    client.post(
        "/auth/login",
        data={
            "login": "portal_user@company.com",
            "secret": "valid_secret",
            "credential_kind": "api_key",
            "csrf_token": csrf_match.group(1),
        },
        headers={"Accept": "text/html"},
    )

    # 2. Get dashboard and extract dashboard CSRF
    dash = client.get("/")
    assert dash.status_code == 200
    dash_csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', dash.text).group(1)
    # Ensure it's a real 32-char URL safe token, NOT a python object repr!
    assert "Session object" not in dash_csrf
    assert len(dash_csrf) >= 16

    # 3. Post to /calendar/sync with valid CSRF
    sync_res = client.post(
        "/calendar/sync",
        data={"csrf_token": dash_csrf},
        follow_redirects=False,
    )
    # Must redirect (303) back to dashboard, NOT return 403 Invalid CSRF token
    assert sync_res.status_code == 303
    assert "sync_success" in sync_res.headers["location"] or "error" in sync_res.headers["location"]

    # 4. Post to /calendar/sync with INVALID CSRF -> 403
    bad_res = client.post(
        "/calendar/sync",
        data={"csrf_token": "wrong_csrf_token"},
        follow_redirects=False,
    )
    assert bad_res.status_code == 403
    assert "Invalid CSRF token" in bad_res.json()["detail"]


def test_portal_telegram_disconnect_html_redirect(portal_client, temp_db_path):
    client, session_factory = portal_client
    mock_odoo = MagicMock()
    mock_odoo.authenticate.return_value = 77
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
            "credential_kind": "api_key",
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
