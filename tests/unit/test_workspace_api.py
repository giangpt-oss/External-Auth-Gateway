from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import ActionOperation, GoogleConnection, OdooCredential, TelegramConnection, User
from app.security import encrypt_secret


@pytest.fixture
def workspace_env(tmp_path):
    key = Fernet.generate_key().decode()
    settings = Settings(_env_file=None, APP_ENV="test", ENABLE_AI_AGENT_FORWARDING=False, DATABASE_URL=f"sqlite:///{tmp_path / 'workspace.db'}", ENCRYPTION_KEY=key, AGENT_TO_GATEWAY_SECRET="workspace-internal-test-secret")
    app = create_app(settings)
    with app.state.session_factory() as db:
        user = User(tenant="https://odoo.invalid", database="test", odoo_uid=1, login="test@example.com", name="Test", active=True)
        db.add(user)
        db.flush()
        uid = user.id
        db.add_all([
            OdooCredential(user_id=uid, credential_kind="api_key", encrypted_secret=encrypt_secret("test-only", key)),
            TelegramConnection(user_id=uid, telegram_user_id=7, private_chat_id=7, binding_id="binding", status="connected"),
            GoogleConnection(user_id=uid, google_sub="test", email="test@example.com", encrypted_access_token=encrypt_secret("test-access", key), scopes="https://www.googleapis.com/auth/drive", status="connected", expires_at=datetime.now(UTC) + timedelta(hours=1)),
        ])
        db.commit()
    odoo = Mock()
    odoo.validate_user_account.return_value = {"active": True, "share": False}
    odoo.compute_permissions.return_value = ["workspace.drive.read", "workspace.drive.write"]
    with TestClient(app) as client, patch("app.internal.routes._get_odoo_service", return_value=odoo):
        yield client, app, uid, {"Authorization": "Bearer workspace-internal-test-secret"}


def test_workspace_routes_require_service_secret(workspace_env):
    client, _, uid, _ = workspace_env
    response = client.post(f"/internal/v1/users/{uid}/workspace/query", json={"service": "drive", "method": "files.list"})
    assert response.status_code == 401


def test_drive_knowledge_requires_matching_binding_and_exports_native_docs(workspace_env, caplog):
    client, _, uid, headers = workspace_env
    path = f"/internal/v1/users/{uid}/workspace/drive-knowledge"
    payload = {"telegram_user_id": 7, "chat_id": 7, "telegram_binding_id": "binding", "file_id": "file123"}
    with patch("app.workspace.service.WorkspaceService._send") as send:
        assert client.post(path, headers=headers, json={**payload, "chat_id": 8}).status_code == 403
        send.assert_not_called()
        send.side_effect = [
            {"id": "file123", "name": "Policy", "mimeType": "application/vnd.google-apps.document",
             "capabilities": {"canDownload": True}},
            {"content_base64": "dGVzdA=="},
        ]
        with caplog.at_level("INFO", logger="app.workspace.telemetry"):
            response = client.post(path, headers={**headers, "X-Request-ID": "drive-correlation"}, json=payload)
    assert response.status_code == 200
    assert response.json()["files"] == [{"id": "file123", "name": "Policy", "suffix": ".docx", "content_base64": "dGVzdA=="}]
    assert [call.args[:3] for call in send.call_args_list] == [(uid, "drive", "files.get"), (uid, "drive", "files.export")]
    assert "req_id=drive-correlation" in caplog.text


def test_query_rejects_write_without_google_call(workspace_env):
    client, _, uid, headers = workspace_env
    with patch("app.workspace.service.WorkspaceService._send") as send:
        response = client.post(f"/internal/v1/users/{uid}/workspace/query", headers=headers, json={"service": "drive", "method": "files.create", "body": {"name": "Test"}})
    assert response.status_code == 400
    send.assert_not_called()


def test_preview_confirm_replay_and_changed_binding(workspace_env):
    client, app, uid, headers = workspace_env
    path = f"/internal/v1/users/{uid}"
    with patch("app.workspace.service.WorkspaceService._send", return_value={"id": "new-file"}) as send:
        preview = client.post(path + "/workspace/preview", headers=headers, json={"service": "drive", "method": "files.create", "body": {"name": "Test"}})
        assert preview.status_code == 200
        send.assert_not_called()
        request = {"operation_id": "op_workspace", "telegram_user_id": 7, "chat_id": 7, "telegram_binding_id": "binding", "action": "workspace.drive.write", "arguments": preview.json()["arguments"]}
        first = client.post(path + "/actions", headers=headers, json=request)
        assert first.json()["status"] == "completed"
        second = client.post(path + "/actions", headers=headers, json=request)
        assert second.json() == first.json()
        assert send.call_count == 1
        status = client.get(path + "/actions/op_workspace", headers=headers, params={"telegram_user_id": 7, "chat_id": 7, "telegram_binding_id": "binding"})
        assert status.status_code == 200 and status.json()["status"] == "completed"
        request["telegram_binding_id"] = "other"
        assert client.post(path + "/actions", headers=headers, json=request).json()["status"] == "RECONNECT_REQUIRED"
        assert send.call_count == 1
    with app.state.session_factory() as db:
        assert db.query(ActionOperation).filter_by(operation_id="op_workspace").count() == 1


def test_disconnected_user_cannot_preview(workspace_env):
    client, app, uid, headers = workspace_env
    with app.state.session_factory() as db:
        db.query(GoogleConnection).filter_by(user_id=uid).update({"status": "disconnected"})
        db.commit()
    response = client.post(f"/internal/v1/users/{uid}/workspace/preview", headers=headers, json={"service": "drive", "method": "files.create", "body": {"name": "Test"}})
    assert response.json()["status"] == "NOT_CONNECTED"


def test_binary_query_returns_user_bound_download_link(workspace_env):
    client, _, uid, headers = workspace_env
    import base64
    with patch("app.workspace.service.WorkspaceService._send", return_value={"content_base64": base64.b64encode(b"PDF").decode(), "mime_type": "application/pdf", "size_bytes": 3}):
        response = client.post(f"/internal/v1/users/{uid}/workspace/query", headers=headers, json={"service": "drive", "method": "files.export", "parameters": {"fileId": "file", "mimeType": "application/pdf"}})
    data = response.json()["data"]
    assert "download_url" in data
    assert "content_base64" not in data
