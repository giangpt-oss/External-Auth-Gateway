from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import GoogleConnection, OdooCredential, TelegramConnection, User
from app.odoo.client import OdooAccessError, OdooClient, OdooNetworkError
from app.odoo.service import (
    OdooAccountInactiveError,
    OdooExternalUserError,
    OdooService,
)
from app.security import encrypt_secret


def test_validate_user_account_success():
    mock_client = MagicMock(spec=OdooClient)
    mock_client.execute.return_value = [{"id": 10, "active": True, "share": False, "name": "Test User", "login": "test"}]
    service = OdooService(mock_client)

    profile = service.validate_user_account(uid=10, secret="secret123")
    assert profile["id"] == 10
    assert profile["active"] is True
    assert profile["share"] is False
    mock_client.execute.assert_called_once()


def test_validate_user_account_inactive_raises_error():
    mock_client = MagicMock(spec=OdooClient)
    mock_client.execute.return_value = [{"id": 10, "active": False, "share": False, "name": "Inactive User"}]
    service = OdooService(mock_client)

    with pytest.raises(OdooAccountInactiveError):
        service.validate_user_account(uid=10, secret="secret123")


def test_validate_user_account_external_user_raises_error():
    mock_client = MagicMock(spec=OdooClient)
    mock_client.execute.return_value = [{"id": 10, "active": True, "share": True, "name": "Portal User"}]
    service = OdooService(mock_client)

    with pytest.raises(OdooExternalUserError):
        service.validate_user_account(uid=10, secret="secret123")


def test_validate_user_account_network_error_propagates():
    mock_client = MagicMock(spec=OdooClient)
    mock_client.execute.side_effect = OdooNetworkError("Connection timed out")
    service = OdooService(mock_client)

    with pytest.raises(OdooNetworkError):
        service.validate_user_account(uid=10, secret="secret123")


def test_compute_permissions_exact_google_scopes():
    mock_client = MagicMock(spec=OdooClient)
    service = OdooService(mock_client)

    # 1. calendar.events.readonly -> only calendar.read, NOT calendar.write
    perms1 = service.compute_permissions(
        uid=10,
        secret="valid_secret",
        is_internal_user=True,
        google_status="connected",
        google_scopes=["https://www.googleapis.com/auth/calendar.events.readonly"],
    )
    assert "calendar.read" in perms1
    assert "calendar.write" not in perms1

    # 2. calendar.events -> has both read and write
    perms2 = service.compute_permissions(
        uid=10,
        secret="valid_secret",
        is_internal_user=True,
        google_status="connected",
        google_scopes=["https://www.googleapis.com/auth/calendar.events"],
    )
    assert "calendar.read" in perms2
    assert "calendar.write" in perms2

    # 3. gmail.send only -> only gmail.send, NOT gmail.read
    perms3 = service.compute_permissions(
        uid=10,
        secret="valid_secret",
        is_internal_user=True,
        google_status="connected",
        google_scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    assert "gmail.send" in perms3
    assert "gmail.read" not in perms3

    # 4. gmail.readonly -> only gmail.read, NOT gmail.send
    perms4 = service.compute_permissions(
        uid=10,
        secret="valid_secret",
        is_internal_user=True,
        google_status="connected",
        google_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    assert "gmail.read" in perms4
    assert "gmail.send" not in perms4


def test_compute_permissions_crm_independent_handling():
    mock_client = MagicMock(spec=OdooClient)
    def side_effect(uid, secret, model, method, args=None, kwargs=None):
        if method == "check_access_rights":
            op = args[0]
            if op == "read":
                return True
            if op == "create":
                raise Exception("DB error on create check")
            if op == "write":
                return True
        return False

    mock_client.execute.side_effect = side_effect
    service = OdooService(mock_client)

    perms = service.compute_permissions(
        uid=10,
        secret="valid_secret",
        is_internal_user=True,
        google_status="disconnected",
        google_scopes=[],
    )
    assert "crm.read" in perms
    assert "crm.create" not in perms
    assert "crm.write" in perms


# Setup fixtures for API tests
@pytest.fixture
def api_test_setup(tmp_path):
    db_path = tmp_path / "test_verification.db"
    from cryptography.fernet import Fernet
    encryption_key = Fernet.generate_key().decode()
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{db_path}",
        ENCRYPTION_KEY=encryption_key,
        ENABLE_AI_AGENT_FORWARDING=False,
        AGENT_TO_GATEWAY_SECRET="test_secret",
        TELEGRAM_BOT_TOKEN="mock_token",
    )

    app = create_app(settings)
    session_factory = app.state.session_factory

    with session_factory() as db:
        user = User(
            id=1,
            tenant="http://odoo.local",
            database="odoo_db",
            odoo_uid=10,
            name="John Doe",
            login="john@example.com",
            active=True,
        )
        db.add(user)
        db.commit()

        encrypted_pwd = encrypt_secret("odoo_password_123", encryption_key)
        cred = OdooCredential(user_id=1, credential_kind="password", encrypted_secret=encrypted_pwd)
        db.add(cred)

        tg = TelegramConnection(
            user_id=1,
            telegram_user_id=123456,
            private_chat_id=123456,
            status="connected",
            binding_id="tg_bind_fixed123",
        )
        db.add(tg)

        g_conn = GoogleConnection(
            user_id=1,
            google_sub="sub_12345",
            email="john@gmail.com",
            scopes="https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.send",
            encrypted_access_token="enc_token",
            expires_at=datetime.now(UTC),
            status="connected",
        )
        db.add(g_conn)
        db.commit()

    client = TestClient(app)
    return client, session_factory, settings


def test_resolve_identity_blocks_when_credential_revoked(api_test_setup):
    client, session_factory, settings = api_test_setup

    with patch("app.internal.routes._get_odoo_service") as mock_get_svc:
        mock_svc = MagicMock()
        # Credential bị thu hồi/mật khẩu sai -> validate_user_account ném OdooAccessError
        mock_svc.validate_user_account.side_effect = OdooAccessError("Access Denied")
        mock_get_svc.return_value = mock_svc

        resp = client.post(
            "/internal/v1/identity/resolve",
            json={"telegram_user_id": 123456, "chat_id": 123456, "contract_version": 2},
            headers={"Authorization": "Bearer test_secret"},
        )
        print("DEBUG RESPONSE:", resp.status_code, resp.text)
        assert resp.status_code == 403
        data = resp.json()
        assert data["detail"]["code"] in ["ODOO_CREDENTIAL_INVALID", "ODOO_ACCESS_DENIED"]


def test_resolve_identity_blocks_when_odoo_inactive_or_external(api_test_setup):
    client, session_factory, settings = api_test_setup

    with patch("app.internal.routes._get_odoo_service") as mock_get_svc:
        mock_svc = MagicMock()
        mock_svc.validate_user_account.side_effect = OdooAccountInactiveError("Tài khoản Odoo bị khóa")
        mock_get_svc.return_value = mock_svc

        resp = client.post(
            "/internal/v1/identity/resolve",
            json={"telegram_user_id": 123456, "chat_id": 123456, "contract_version": 2},
            headers={"Authorization": "Bearer test_secret"},
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["detail"]["code"] == "ODOO_ACCOUNT_INACTIVE"


def test_resolve_identity_returns_503_on_odoo_network_error(api_test_setup):
    client, session_factory, settings = api_test_setup

    with patch("app.internal.routes._get_odoo_service") as mock_get_svc:
        mock_svc = MagicMock()
        mock_svc.validate_user_account.side_effect = OdooNetworkError("Odoo connection timeout")
        mock_get_svc.return_value = mock_svc

        resp = client.post(
            "/internal/v1/identity/resolve",
            json={"telegram_user_id": 123456, "chat_id": 123456, "contract_version": 2},
            headers={"Authorization": "Bearer test_secret"},
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data["detail"]["code"] == "ODOO_UNAVAILABLE"


def test_resolve_identity_hr_employee_failure_only_blanks_job_title(api_test_setup):
    client, session_factory, settings = api_test_setup

    with patch("app.internal.routes._get_odoo_service") as mock_get_svc:
        mock_svc = MagicMock()
        mock_svc.validate_user_account.return_value = {"id": 10, "active": True, "share": False}
        # hr.employee bị lỗi quyền
        mock_svc.get_employee_profile.return_value = {"job_title": None, "department": None, "manager_name": None}
        mock_svc.compute_permissions.return_value = ["tasks.read", "tasks.write"]
        mock_get_svc.return_value = mock_svc

        resp = client.post(
            "/internal/v1/identity/resolve",
            json={"telegram_user_id": 123456, "chat_id": 123456, "contract_version": 2},
            headers={"Authorization": "Bearer test_secret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "OK"
        assert data["job_title"] is None
        assert data["department"] is None
        assert "tasks.read" in data["permissions"]


def test_execute_action_blocks_when_odoo_credential_revoked(api_test_setup):
    client, session_factory, settings = api_test_setup

    with patch("app.internal.routes._get_odoo_service") as mock_get_svc:
        mock_svc = MagicMock()
        mock_svc.validate_user_account.side_effect = OdooAccessError("Access Denied")
        mock_get_svc.return_value = mock_svc

        resp = client.post(
            "/internal/v1/users/1/actions",
            json={
                "operation_id": "op_test_revoked_123",
                "telegram_user_id": 123456,
                "chat_id": 123456,
                "telegram_binding_id": "tg_bind_fixed123",
                "action": "calendar.create",
                "arguments": {"summary": "Meeting"},
            },
            headers={"Authorization": "Bearer test_secret"},
        )
        # Bị chặn ngay từ khâu xác minh Odoo, không được thực thi
        assert resp.status_code in [403, 200]
        data = resp.json()
        if resp.status_code == 200:
            assert data["status"] in ["PERMISSION_DENIED", "ODOO_CREDENTIAL_INVALID"]
        else:
            assert data["detail"]["code"] in ["ODOO_CREDENTIAL_INVALID", "ODOO_ACCESS_DENIED"]
