import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import ActionOperation, GoogleConnection, OdooCredential, TelegramConnection, User
from app.security import encrypt_secret


@pytest.fixture
def test_setup(tmp_path):
    db_path = tmp_path / "task3_idempotency.db"
    fernet_key = Fernet.generate_key().decode()
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{db_path}",
        ENCRYPTION_KEY=fernet_key,
        AGENT_TO_GATEWAY_SECRET="secret_agent_gw_32_chars_long_1111",
        GATEWAY_TO_AGENT_SECRET="secret_gw_agent_32_chars_long_2222",
    )
    app = create_app(settings)
    client = TestClient(app)

    with app.state.session_factory() as db:
        user = User(
            id=1,
            tenant="https://odoo.test.local",
            odoo_uid=10,
            login="user@example.com",
            name="Test User",
            active=True,
            database="test_db",
        )
        db.add(user)
        db.commit()

        cred = OdooCredential(
            user_id=1,
            credential_kind="password",
            encrypted_secret=encrypt_secret("valid_secret", fernet_key),
        )
        tg = TelegramConnection(
            user_id=1,
            telegram_user_id=777,
            private_chat_id=777,
            binding_id="tg_bind_task3",
            status="connected",
        )
        g_conn = GoogleConnection(
            user_id=1,
            google_sub="sub_task3",
            email="user@gmail.com",
            scopes="https://www.googleapis.com/auth/calendar.events",
            encrypted_access_token=encrypt_secret("access", fernet_key),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            status="connected",
        )
        db.add_all([cred, tg, g_conn])
        db.commit()

    with patch("app.odoo.service.OdooService.validate_user_account", return_value={"id": 10, "active": True, "share": False}):
        yield client, settings, app.state.session_factory, 1


def test_concurrent_action_insert_no_500(test_setup):
    """Hai tiến trình đồng thời insert cùng một operation_id không được gây ra HTTP 500."""
    client, settings, session_factory, user_id = test_setup
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}

    payload = {
        "operation_id": "op_concurrent_insert_123",
        "telegram_user_id": 777,
        "chat_id": 777,
        "telegram_binding_id": "tg_bind_task3",
        "action": "calendar.create",
        "arguments": {
            "summary": "Concurrent Meeting",
            "start_time": "2026-10-27T10:00:00Z",
            "end_time": "2026-10-27T11:00:00Z",
        },
    }

    responses = []

    def call_action():
        with patch("app.google.client.GoogleClient.create_calendar_event") as mock_cal:
            mock_cal.return_value = {"id": "cal_123", "summary": "Concurrent Meeting"}
            resp = client.post(f"/internal/v1/users/{user_id}/actions", json=payload, headers=headers)
            responses.append(resp)

    threads = [threading.Thread(target=call_action) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Cả hai không được trả về 500
    for r in responses:
        assert r.status_code != 500, f"Got HTTP 500: {r.text}"


def test_action_in_progress_expired_becomes_unknown_result(test_setup):
    """Thao tác đang in_progress quá hạn (30s) chuyển thành unknown_result và từ chối tự động gọi lại."""
    client, settings, session_factory, user_id = test_setup
    headers = {"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"}

    # Tạo bản ghi in_progress từ 60 giây trước
    with session_factory() as db:
        old_time = datetime.now(UTC) - timedelta(seconds=60)
        import hashlib
        import json
        p_hash = hashlib.sha256(json.dumps({"action": "calendar.create", "arguments": {"summary": "Old"}}, sort_keys=True).encode()).hexdigest()
        op = ActionOperation(
            operation_id="op_expired_in_progress",
            user_id=user_id,
            telegram_binding_id="tg_bind_task3",
            action="calendar.create",
            payload_hash=p_hash,
            status="in_progress",
            created_at=old_time,
        )
        db.add(op)
        db.commit()

    resp = client.post(
        f"/internal/v1/users/{user_id}/actions",
        json={
            "operation_id": "op_expired_in_progress",
            "telegram_user_id": 777,
            "chat_id": 777,
            "telegram_binding_id": "tg_bind_task3",
            "action": "calendar.create",
            "arguments": {"summary": "Old"},
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "UNKNOWN_RESULT"
