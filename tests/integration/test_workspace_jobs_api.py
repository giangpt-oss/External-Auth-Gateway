import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.main import create_app
from app.models import Base, User
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus


@pytest.fixture
def client_with_db(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    db_url = f"sqlite:///{db_file}"
    fernet_key = "_3pDgavXi1u3T76np_zSNJQcNS87hKW7fEdQXjlu3Ew="
    agent_secret = "hopita_agent_to_gw_secret_key_32chars_min_5678"

    settings = Settings(
        APP_ENV="development",
        PUBLIC_BASE_URL="http://localhost:8080",
        ODOO_URL="https://mycompany.odoo.com",
        ODOO_DB="test_db",
        DATABASE_URL=db_url,
        ENCRYPTION_KEY=fernet_key,
        TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        TELEGRAM_BOT_USERNAME="TestBot",
        TELEGRAM_WEBHOOK_SECRET="expected_webhook_secret_xyz",
        AGENT_TO_GATEWAY_SECRET=agent_secret,
        GOOGLE_WORKSPACE_MCP_ENABLED=True,
    )
    app = create_app(settings)

    # Insert test user
    with app.state.session_factory() as session:
        test_user = User(
            id=1,
            tenant="default",
            database="odoo",
            odoo_uid=1,
            login="admin",
            name="Admin User",
            active=True,
        )
        session.add(test_user)
        session.commit()

    with TestClient(app) as client:
        yield client, agent_secret


def test_workspace_job_conflict_different_payload(client_with_db):
    client, secret = client_with_db
    headers = {"Authorization": f"Bearer {secret}"}
    req_payload_1 = {
        "request_id": "req_conflict_1",
        "service": "calendar",
        "operation": "events.list",
        "parameters": {"calendarId": "primary"},
        "is_write": False,
        "wait_seconds": 0.05,
    }
    # Create first job
    resp1 = client.post("/internal/v1/users/1/workspace/jobs", json=req_payload_1, headers=headers)
    assert resp1.status_code in (200, 202)
    
    # Same request_id with different parameters -> 409 Conflict
    req_payload_2 = dict(req_payload_1)
    req_payload_2["parameters"] = {"calendarId": "secondary"}
    resp2 = client.post("/internal/v1/users/1/workspace/jobs", json=req_payload_2, headers=headers)
    assert resp2.status_code == 409


def test_workspace_job_cancel(client_with_db):
    client, secret = client_with_db
    headers = {"Authorization": f"Bearer {secret}"}
    req_payload = {
        "request_id": "req_cancel_1",
        "service": "drive",
        "operation": "files.list",
        "parameters": {},
        "is_write": False,
        "wait_seconds": 0.01,
    }
    resp = client.post("/internal/v1/users/1/workspace/jobs", json=req_payload, headers=headers)
    assert resp.status_code in (200, 202)
    data = resp.json()
    job_id = data.get("job_id")
    assert job_id is not None
    
    cancel_resp = client.post(f"/internal/v1/users/1/workspace/jobs/{job_id}/cancel", headers=headers)
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"
