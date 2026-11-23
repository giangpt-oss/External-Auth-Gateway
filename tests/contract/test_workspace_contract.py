import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import GoogleConnection, User
from app.security import encrypt_secret
from app.workspace.models import WorkspaceJob
from app.workspace.service import WorkspaceService

CASES = json.loads(Path(__file__).with_name("workspace-v1.json").read_text())["cases"]
SECRET = "contract-test-agent-to-gateway-secret"


@pytest.mark.parametrize("name", CASES)
def test_workspace_contract(name, tmp_path, fernet_key, monkeypatch):
    case = CASES[name]
    app = create_app(Settings(
        _env_file=None, APP_ENV="test", DATABASE_URL=f"sqlite:///{tmp_path / 'contract.db'}",
        ENCRYPTION_KEY=fernet_key, AGENT_TO_GATEWAY_SECRET=SECRET,
        GOOGLE_WORKSPACE_MCP_ENABLED=False,
    ))
    expected = dict(case["response"])
    with app.state.session_factory() as db:
        db.add(User(id=1, tenant="test", database="test", odoo_uid=1, name="Fixture", login="fixture"))
        db.flush()
        db.add(GoogleConnection(
            user_id=1, google_sub="contract-sub", email="fixture@example.invalid",
            scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_access_token=encrypt_secret("unused", fernet_key),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ))
        if case["method"] == "GET":
            result = expected.get("result")
            db.add(WorkspaceJob(
                user_id=1, job_id=expected["job_id"], request_id="fixture-request",
                service="drive", operation="files.create", is_write=True,
                status=expected["status"], error_code=expected.get("error_code"),
                payload_encrypted=encrypt_secret("{}", fernet_key),
                result_encrypted=encrypt_secret(json.dumps(result), fernet_key) if result else None,
                deadline_at=datetime.now(UTC) + timedelta(minutes=2),
            ))
        db.commit()

    if name in {"read", "permission"}:
        from app.internal.dependencies import _get_workspace_service
        from app.workspace import routes

        monkeypatch.setattr(routes, "service_for", lambda request, _: _get_workspace_service(request))
        monkeypatch.setattr(
            WorkspaceService, "_read_response",
            lambda *_args: {"files": [{"id": "fixture-file", "name": "Policy.txt"}]},
        )
        if name == "permission":
            with app.state.session_factory.begin() as db:
                db.query(GoogleConnection).one().scopes = "openid"
    with TestClient(app) as client:
        response = client.request(
            case["method"], case["path"], json=case.get("request"),
            headers={"Authorization": f"Bearer {SECRET}"},
        )
    assert response.status_code == case["http_status"], response.text
    actual = response.json()
    if name == "queued":
        with app.state.session_factory() as db:
            job = db.query(WorkspaceJob).filter_by(request_id="fixture-request").one()
            assert job.google_account_id == db.query(GoogleConnection.id).scalar()
            expected["job_id"] = job.job_id
    elif name == "permission":
        assert actual["message"]
        expected.pop("message")
    assert {field: actual[field] for field in expected} == expected
