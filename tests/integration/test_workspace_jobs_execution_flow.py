import asyncio
import json
import time
import pytest
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import GoogleConnection, User
from app.security import decrypt_secret, encrypt_secret
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus
from app.workspace.worker import WorkspaceWorker


class MockGoogleTransport(httpx.BaseTransport):
    def __init__(self):
        super().__init__()
        self.requests_log = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        self.requests_log.append({"method": method, "url": url})

        # Read calendar events
        if "events?" in url or url.endswith("/events"):
            if method == "GET":
                content = json.dumps({"items": [{"id": "evt_1", "summary": "Team Sync"}]}).encode()
                return httpx.Response(200, content=content, headers={"Content-Type": "application/json"})
            elif method == "POST":
                # Mutation request
                content = json.dumps({"id": "evt_created_999", "status": "confirmed", "summary": "New Event"}).encode()
                return httpx.Response(200, content=content, headers={"Content-Type": "application/json"})

        return httpx.Response(200, json={}, headers={"Content-Type": "application/json"})


@pytest.fixture
def integrated_env(tmp_path, monkeypatch):
    db_file = tmp_path / "test_flow.db"
    db_url = f"sqlite:///{db_file}"
    fernet_key = "_3pDgavXi1u3T76np_zSNJQcNS87hKW7fEdQXjlu3Ew="
    agent_secret = "hopita_agent_to_gw_secret_key_32chars_min_5678"

    settings = Settings(
        APP_ENV="test",
        PUBLIC_BASE_URL="http://localhost:8080",
        ODOO_URL="https://mycompany.odoo.com",
        ODOO_DB="test_db",
        DATABASE_URL=db_url,
        ENCRYPTION_KEY=fernet_key,
        TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        TELEGRAM_BOT_USERNAME="TestBot",
        TELEGRAM_WEBHOOK_SECRET="expected_webhook_secret_xyz",
        AGENT_TO_GATEWAY_SECRET=agent_secret,
        GOOGLE_WORKSPACE_MCP_ENABLED=False,
    )
    app = create_app(settings)

    mock_transport = MockGoogleTransport()

    # Pre-seed user with Google connection
    from datetime import UTC, datetime, timedelta
    from app.security import encrypt_secret
    with app.state.session_factory() as session:
        user = User(
            id=1,
            tenant="default",
            database="odoo",
            odoo_uid=1,
            login="admin",
            name="Admin",
            active=True,
        )
        session.add(user)
        session.flush()

        gconn = GoogleConnection(
            user_id=1,
            google_sub="google_123",
            email="admin@example.com",
            scopes="https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events",
            encrypted_access_token=encrypt_secret("valid_token", fernet_key),
            encrypted_refresh_token=encrypt_secret("valid_refresh", fernet_key),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            status="connected",
        )
        session.add(gconn)
        session.commit()

    callbacks = []

    def receive_callback(request):
        assert request.url.path == "/internal/v1/workspace/job-completed"
        callbacks.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    worker = WorkspaceWorker(
        settings=settings,
        session_factory=app.state.session_factory,
        worker_id="test-worker-1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(receive_callback)),
    )
    # Exercise the production worker and substitute only the Google HTTP transport.
    from app.workspace.service import WorkspaceService
    original_init = WorkspaceService.__init__

    def init_with_transport(self, google_client, signing_key, transport=None, *, use_mcp=False):
        original_init(self, google_client, signing_key, transport=mock_transport, use_mcp=False)

    monkeypatch.setattr(WorkspaceService, "__init__", init_with_transport)
    from app.google.oauth import GoogleOAuthService

    def token_exchange(url, **kwargs):
        assert url == GoogleOAuthService.TOKEN_ENDPOINT
        assert kwargs["data"]["grant_type"] == "refresh_token"
        return httpx.Response(200, json={"access_token": "refreshed-token", "expires_in": 7200})

    monkeypatch.setattr(httpx, "post", token_exchange)

    with TestClient(app) as client:
        yield client, agent_secret, worker, mock_transport, app.state.session_factory, fernet_key, callbacks


@pytest.mark.asyncio
async def test_end_to_end_jobs_flow_read_prepare_execute_and_invalid(integrated_env, monkeypatch):
    from app.workspace import telemetry

    measurements = telemetry.TelemetryStore()
    monkeypatch.setattr(telemetry, "telemetry_store", measurements)
    client, secret, worker, transport, sf, fernet_key, callbacks = integrated_env
    headers = {"Authorization": f"Bearer {secret}"}

    # =========================================================================
    # 1. READ OPERATION: returns valid data
    # =========================================================================
    read_resp = client.post(
        "/internal/v1/users/1/workspace/jobs",
        json={
            "request_id": "req_read_01",
            "service": "calendar",
            "operation": "events.list",
            "parameters": {"calendarId": "primary"},
            "is_write": False,
            "wait_seconds": 0.01,
        },
        headers=headers,
    )
    assert read_resp.status_code in (200, 202)
    job_id_read = read_resp.json()["job_id"]

    # Worker processes the read job
    await worker.process_one_cycle()

    with sf() as db:
        j_read = db.query(WorkspaceJob).filter_by(job_id=job_id_read).first()
        assert j_read.status == WorkspaceJobStatus.COMPLETED.value
        dec_read = decrypt_secret(j_read.result_encrypted, fernet_key)
        assert "Team Sync" in dec_read

    # Verify only GET request was made
    get_reqs = [r for r in transport.requests_log if r["method"] == "GET"]
    assert len(get_reqs) >= 1
    post_reqs_start = [r for r in transport.requests_log if r["method"] == "POST"]
    assert len(post_reqs_start) == 0

    # =========================================================================
    # 2. PREPARE OPERATION: generates preview & ticket, 0 mutation requests
    # =========================================================================
    prep_resp = client.post(
        "/internal/v1/users/1/workspace/jobs",
        json={
            "request_id": "req_prep_02",
            "action": "prepare",
            "service": "calendar",
            "operation": "events.insert",
            "parameters": {"calendarId": "primary"},
            "body": {
                "summary": "Sprint Planning",
                "start": {"dateTime": "2026-11-20T10:00:00Z"},
                "end": {"dateTime": "2026-11-20T11:00:00Z"},
            },
            "is_write": True,
            "wait_seconds": 0.01,
        },
        headers=headers,
    )
    assert prep_resp.status_code in (200, 202)
    job_id_prep = prep_resp.json()["job_id"]

    # Worker processes the prepare job
    await worker.process_one_cycle()

    with sf() as db:
        j_prep = db.query(WorkspaceJob).filter_by(job_id=job_id_prep).first()
        # MUST BE WAITING_CONFIRMATION, NOT COMPLETED!
        assert j_prep.status == WorkspaceJobStatus.WAITING_CONFIRMATION.value
        dec_prep = decrypt_secret(j_prep.result_encrypted, fernet_key)
        prep_data = json.loads(dec_prep)
        assert "_ticket" in prep_data["arguments"]
        valid_ticket = prep_data["arguments"]["_ticket"]

    # Verify still 0 mutation requests
    post_reqs_prep = [r for r in transport.requests_log if r["method"] == "POST"]
    assert len(post_reqs_prep) == 0

    # =========================================================================
    # 3. EXECUTE OPERATION WITH VALID TICKET: exactly 1 mutation request
    # =========================================================================
    exec_resp = client.post(
        "/internal/v1/users/1/workspace/jobs",
        json={
            "request_id": "req_exec_03",
            "action": "execute",
            "operation_id": "op_exec_001",
            "ticket": valid_ticket,
            "service": "calendar",
            "operation": "events.insert",
            "parameters": {"calendarId": "primary"},
            "body": {
                "summary": "Sprint Planning",
                "start": {"dateTime": "2026-11-20T10:00:00Z"},
                "end": {"dateTime": "2026-11-20T11:00:00Z"},
            },
            "is_write": True,
            "wait_seconds": 0.01,
        },
        headers=headers,
    )
    assert exec_resp.status_code in (200, 202)
    job_id_exec = exec_resp.json()["job_id"]

    # Worker processes the execute job
    await worker.process_one_cycle()

    with sf() as db:
        j_exec = db.query(WorkspaceJob).filter_by(job_id=job_id_exec).first()
        assert j_exec.status == WorkspaceJobStatus.COMPLETED.value
        dec_exec = decrypt_secret(j_exec.result_encrypted, fernet_key)
        assert "evt_created_999" in dec_exec

    # Exactly 1 POST mutation request must have occurred
    post_reqs_exec = [r for r in transport.requests_log if r["method"] == "POST"]
    assert len(post_reqs_exec) == 1

    # =========================================================================
    # 4. EXECUTE OPERATION WITH INVALID / FORGED TICKET: 0 mutation requests
    # =========================================================================
    invalid_ticket = "invalid.forged.ticket.signature"
    bad_resp = client.post(
        "/internal/v1/users/1/workspace/jobs",
        json={
            "request_id": "req_bad_04",
            "action": "execute",
            "operation_id": "op_bad_002",
            "ticket": invalid_ticket,
            "service": "calendar",
            "operation": "events.insert",
            "parameters": {"calendarId": "primary"},
            "body": {
                "summary": "Hacked Meeting",
                "start": {"dateTime": "2026-11-20T10:00:00Z"},
                "end": {"dateTime": "2026-11-20T11:00:00Z"},
            },
            "is_write": True,
            "wait_seconds": 0.01,
        },
        headers=headers,
    )
    assert bad_resp.status_code in (200, 202)
    job_id_bad = bad_resp.json()["job_id"]

    # Worker processes the invalid execute job
    await worker.process_one_cycle()

    with sf() as db:
        j_bad = db.query(WorkspaceJob).filter_by(job_id=job_id_bad).first()
        assert j_bad.status == WorkspaceJobStatus.FAILED.value
        assert j_bad.error_message is not None

    # Still exactly 1 mutation request (no new mutation occurred!)
    post_reqs_final = [r for r in transport.requests_log if r["method"] == "POST"]
    assert len(post_reqs_final) == 1
    assert [(row["request_id"], row["job_id"], row["status"]) for row in callbacks] == [
        ("req_read_01", job_id_read, "completed"),
        ("req_prep_02", job_id_prep, "waiting_confirmation"),
        ("req_exec_03", job_id_exec, "completed"),
        ("req_bad_04", job_id_bad, "failed"),
    ]
    stages = measurements.get_summary()["stages"]
    assert stages["queue_wait_ms"]["count"] == 4
    assert stages["permission_check_ms"]["count"] == 3
    assert stages["external_call_ms"]["count"] == 2
    assert stages["token_refresh_ms"]["count"] == 1
    print(json.dumps(measurements.get_summary(), sort_keys=True))
    await worker.stop()
