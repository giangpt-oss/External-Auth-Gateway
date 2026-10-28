import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import ActionOperation, GoogleConnection, OdooCredential, TelegramConnection, User
from app.security import encrypt_secret


@pytest.fixture
def action_status_env(tmp_path):
    db_path = tmp_path / "action_status_test.db"
    fernet_key = Fernet.generate_key().decode()
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{db_path}",
        ENCRYPTION_KEY=fernet_key,
        AGENT_TO_GATEWAY_SECRET="test_agent_secret_32_chars_min_length",
        GATEWAY_TO_AGENT_SECRET="test_gw_secret_32_chars_min_length",
    )
    app = create_app(settings)
    client = TestClient(app)

    with app.state.session_factory() as db:
        user = User(
            id=10,
            tenant="https://test.odoo.local",
            odoo_uid=20,
            login="user@test.local",
            name="Test Action User",
            active=True,
            database="test_db",
        )
        tg = TelegramConnection(
            user_id=10,
            telegram_user_id=888999,
            private_chat_id=888999,
            binding_id="bind_action_status_1",
            status="connected",
        )
        cred = OdooCredential(
            user_id=10,
            credential_kind="api_key",
            encrypted_secret=encrypt_secret("valid_secret", fernet_key),
        )
        g_conn = GoogleConnection(
            user_id=10,
            google_sub="sub_10",
            email="user@test.local",
            scopes="https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.send",
            encrypted_access_token=encrypt_secret("token", fernet_key),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            status="connected",
        )
        db.add_all([user, tg, cred, g_conn])
        db.commit()

    return client, settings, app.state.session_factory, 10


def test_action_status_requires_auth(action_status_env):
    client, settings, _, user_id = action_status_env
    resp = client.get(
        f"/internal/v1/users/{user_id}/actions/op_test_1",
        params={
            "telegram_user_id": 888999,
            "chat_id": 888999,
            "telegram_binding_id": "bind_action_status_1",
        },
    )
    assert resp.status_code in [401, 403]


def test_action_status_not_found(action_status_env):
    client, settings, _, user_id = action_status_env
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}
    resp = client.get(
        f"/internal/v1/users/{user_id}/actions/op_non_existent",
        params={
            "telegram_user_id": 888999,
            "chat_id": 888999,
            "telegram_binding_id": "bind_action_status_1",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["operation_id"] == "op_non_existent"
    assert data["status"] == "not_found"
    assert data["result"] is None


def test_action_status_completed_returns_result(action_status_env):
    client, settings, session_factory, user_id = action_status_env
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}

    # Tạo bản ghi completed
    with session_factory() as db:
        op = ActionOperation(
            operation_id="op_completed_123",
            user_id=user_id,
            action="calendar.create",
            payload_hash="hash_123",
            telegram_binding_id="bind_action_status_1",
            status="completed",
            result_json=json.dumps({"status": "completed", "result": {"id": "cal_event_999", "summary": "Họp chiến lược"}}),
        )
        db.add(op)
        db.commit()

    with patch("app.internal.routes._verify_odoo_account", return_value={"id": 20, "login": "user@test.local"}), \
         patch("app.odoo.service.OdooService.compute_permissions", return_value=["calendar.write", "calendar.read"]):
        resp = client.get(
            f"/internal/v1/users/{user_id}/actions/op_completed_123",
            params={
                "telegram_user_id": 888999,
                "chat_id": 888999,
                "telegram_binding_id": "bind_action_status_1",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["operation_id"] == "op_completed_123"
        assert data["action"] == "calendar.create"
        assert data["status"] == "completed"
        assert data["result"]["id"] == "cal_event_999"
        assert data["result"]["summary"] == "Họp chiến lược"


def test_action_status_rejects_binding_mismatch(action_status_env):
    client, settings, session_factory, user_id = action_status_env
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}

    with session_factory() as db:
        op = ActionOperation(
            operation_id="op_mismatch_456",
            user_id=user_id,
            action="gmail.send",
            payload_hash="hash_456",
            telegram_binding_id="bind_different_user",
            status="completed",
            result_json=json.dumps({"status": "completed", "result": {"id": "msg_secret"}}),
        )
        db.add(op)
        db.commit()

    # Truy vấn với bind_action_status_1 -> phải bị từ chối 403 BINDING_MISMATCH
    resp = client.get(
        f"/internal/v1/users/{user_id}/actions/op_mismatch_456",
        params={
            "telegram_user_id": 888999,
            "chat_id": 888999,
            "telegram_binding_id": "bind_action_status_1",
        },
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "BINDING_MISMATCH"


def test_action_status_refuses_when_odoo_account_revoked(action_status_env):
    """Khi tài khoản Odoo bị thu hồi, GET status phải trả 403 và không được tiết lộ kết quả đã lưu."""
    client, settings, session_factory, user_id = action_status_env
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}

    with session_factory() as db:
        op = ActionOperation(
            operation_id="op_revoked_789",
            user_id=user_id,
            action="calendar.create",
            payload_hash="hash_789",
            telegram_binding_id="bind_action_status_1",
            status="completed",
            result_json=json.dumps({"status": "completed", "result": {"id": "confidential_event"}}),
        )
        db.add(op)
        db.commit()

    with patch("app.internal.routes._verify_odoo_account") as mock_verify:
        mock_verify.side_effect = HTTPException(status_code=403, detail={"code": "ODOO_CREDENTIAL_INVALID", "message": "Thu hồi"})
        resp = client.get(
            f"/internal/v1/users/{user_id}/actions/op_revoked_789",
            params={
                "telegram_user_id": 888999,
                "chat_id": 888999,
                "telegram_binding_id": "bind_action_status_1",
            },
            headers=headers,
        )
        assert resp.status_code == 403
        assert "confidential_event" not in resp.text


def test_action_status_refuses_when_odoo_unavailable(action_status_env):
    """Khi máy chủ Odoo mất kết nối, GET status phải trả 503 và không trả kết quả."""
    client, settings, session_factory, user_id = action_status_env
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}

    with session_factory() as db:
        op = ActionOperation(
            operation_id="op_unavail_101",
            user_id=user_id,
            action="calendar.create",
            payload_hash="hash_101",
            telegram_binding_id="bind_action_status_1",
            status="completed",
            result_json=json.dumps({"status": "completed", "result": {"id": "event_data"}}),
        )
        db.add(op)
        db.commit()

    with patch("app.internal.routes._verify_odoo_account") as mock_verify:
        mock_verify.side_effect = HTTPException(status_code=503, detail={"code": "ODOO_UNAVAILABLE", "message": "Timeout"})
        resp = client.get(
            f"/internal/v1/users/{user_id}/actions/op_unavail_101",
            params={
                "telegram_user_id": 888999,
                "chat_id": 888999,
                "telegram_binding_id": "bind_action_status_1",
            },
            headers=headers,
        )
        assert resp.status_code == 503
        assert "event_data" not in resp.text


def test_action_status_refuses_when_action_permission_revoked(action_status_env):
    """Khi quyền của action (vd calendar.write) bị thu hồi, GET status phải trả 403."""
    client, settings, session_factory, user_id = action_status_env
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}

    with session_factory() as db:
        op = ActionOperation(
            operation_id="op_perm_revoked_202",
            user_id=user_id,
            action="calendar.create",
            payload_hash="hash_202",
            telegram_binding_id="bind_action_status_1",
            status="completed",
            result_json=json.dumps({"status": "completed", "result": {"id": "secret_calendar"}}),
        )
        db.add(op)
        db.commit()

    with patch("app.internal.routes._verify_odoo_account", return_value={"id": 20, "login": "user@test.local"}), \
         patch("app.odoo.service.OdooService.compute_permissions", return_value=["tasks.read"]):  # Thiếu calendar.write!
        resp = client.get(
            f"/internal/v1/users/{user_id}/actions/op_perm_revoked_202",
            params={
                "telegram_user_id": 888999,
                "chat_id": 888999,
                "telegram_binding_id": "bind_action_status_1",
            },
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "PERMISSION_DENIED"
        assert "secret_calendar" not in resp.text
