"""Verified evidence reaches the Gateway boundary and is withheld on relink."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_app_engine
from app.models import Base, GoogleConnection, TelegramConnection, TelegramUpdate, User
from app.security import decrypt_secret
from app.telegram.worker import TelegramAIWorker
from app.workspace.drive_permissions import DrivePermissions, invalidate
from app.workspace.drive_runtime import DriveRuntime, safe_result
from app.workspace.drive_storage import DriveLink, DriveStorage, IndexedDriveChunk, IndexedDriveFile
from app.workspace.models import DriveFileMetadata, DriveSyncCheckpoint, WorkspaceJob
from app.workspace.routes import router
from app.workspace.service import WorkspaceError
from app.workspace.worker import WorkspaceWorker


@pytest.fixture
def seeded(tmp_path):
    engine = create_app_engine(f"sqlite:///{tmp_path / 'drive-runtime.db'}")
    # Create just this fixture's tables; importing another suite can register other models.
    table_names = {
        "users", "google_connections", "drive_file_metadata", "drive_file_chunks",
        "drive_sync_checkpoints", "drive_permission_states",
        "drive_scan_candidates", "workspace_jobs", "telegram_connections", "telegram_updates",
        "google_account_selections", "workspace_activities",
    }
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables if table.name in table_names])
    db = sessionmaker(engine)
    with db.begin() as session:
        session.add(User(id=1, tenant="test", database="test", odoo_uid=1,
                         login="drive-test@example.org", name="Drive test", active=True))
        session.flush()
        session.add(GoogleConnection(
            user_id=1, google_sub="account-a", status="connected", link_version=1,
            email="drive-test@example.org", scopes="drive", encrypted_access_token="test-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ))
        session.add(DriveSyncCheckpoint(
            user_id=1, google_sub="account-a", link_version=1, scope="my_drive",
            scan_state="complete", last_success_at=datetime.now(UTC),
        ))
        session.add(TelegramConnection(
            user_id=1, telegram_user_id=101, private_chat_id=101, binding_id="stage4-test",
        ))
    key = Fernet.generate_key().decode()
    link = DriveLink(1, "account-a", 1)
    assert DriveStorage(db, key).replace_chunks(
        link, IndexedDriveFile("doc-a", "Bảo hành", "text/plain", "1"),
        [IndexedDriveChunk("Thời hạn bảo hành là mười hai tháng.", None, "Trang 1")],
    )
    yield db, key, link
    engine.dispose()


def file_meta(version="1"):
    return {"data": {
        "id": "doc-a", "version": version, "trashed": False,
        "capabilities": {"canDownload": True},
    }}


@pytest.mark.asyncio
async def test_read_plan_uses_durable_job_and_hides_old_account_result(seeded):
    db, key, _ = seeded
    settings = Settings(
        _env_file=None, APP_ENV="test", ENCRYPTION_KEY=key,
        AI_AGENT_BASE_URL="http://test-agent",
        AGENT_TO_GATEWAY_SECRET="a" * 32, GATEWAY_TO_AGENT_SECRET="b" * 32,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = db
    app.include_router(router)
    expected_step = {"id": "calendar", "tool_name": "workspace.calendar.read",
                     "parameters": {"method": "events.list"}, "depends_on": []}
    steps = [{"id": "calendar", "tool": "workspace.calendar.read",
              "status": "completed", "messages": ["Kết quả tài khoản A"]}]

    def agent_request(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/read-plan"):
            assert payload["steps"] == [expected_step]
            assert payload["link"] == {"google_sub": "account-a", "link_version": 1}
            assert 0 < payload["remaining_seconds"] <= 120
            return httpx.Response(200, json={"steps": steps, "partial": False})
        assert payload["result"]["steps"] == steps
        return httpx.Response(200, json={"messages": steps[0]["messages"]})

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway",
                          headers={"Authorization": "Bearer " + "a" * 32}) as api,
        httpx.AsyncClient(transport=httpx.MockTransport(agent_request)) as http,
    ):
        worker = WorkspaceWorker(settings, db, http_client=http)
        url = "/internal/v1/users/1/workspace/jobs"
        request = {"request_id": "stage5-dag", "service": "workspace", "operation": "plan.read",
                   "body": {"steps": [expected_step]}, "wait_seconds": 0, "deadline_seconds": 120,
                   "telegram_chat_id": 101, "telegram_user_id": 101,
                   "telegram_binding_id": "stage4-test"}
        job = await api.post(url, json=request)
        assert job.status_code == 202
        job_id = job.json()["job_id"]
        assert await worker.process_one_cycle()
        with db() as session:
            row = session.query(WorkspaceJob).filter_by(job_id=job_id).one()
            assert json.loads(decrypt_secret(row.result_encrypted, key))["steps"] == steps
            assert session.query(TelegramUpdate).one().ai_response.startswith("drive1:")
        with db.begin() as session:
            session.query(GoogleConnection).filter_by(user_id=1).one().link_version += 1
        result = await api.get(f"{url}/{job_id}")
        assert result.json()["result"]["status"] == "link_changed"
        assert "Kết quả tài khoản A" not in result.text
        with db.begin() as session:
            old_time = datetime.now(UTC) - timedelta(hours=25)
            session.query(WorkspaceJob).filter_by(job_id=job_id).one().created_at = old_time
            session.query(TelegramUpdate).one().created_at = old_time
        worker.purge_expired_read_plans()
        with db() as session:
            row = session.query(WorkspaceJob).filter_by(job_id=job_id).one()
            assert row.status == "completed" and row.payload_encrypted == "" and row.result_encrypted is None
            assert session.query(TelegramUpdate).one().ai_response is None
        await worker.stop()


def test_cache_is_shared_and_changes_invalidates(seeded):
    db, _, link = seeded
    service = Mock()
    service.read.return_value = file_meta()
    assert DrivePermissions(db, service).check(link, "doc-a", "1", None)
    assert DrivePermissions(db, service).check(link, "doc-a", "1", None)
    assert service.read.call_count == 1
    with db.begin() as session:
        invalidate(session, link, file_id="doc-a")
    service.read.side_effect = WorkspaceError("PERMISSION_DENIED", "revoked")
    assert not DrivePermissions(db, service).check(link, "doc-a", "1", None)
    assert service.read.call_count == 2


def test_failed_refresh_of_permission_is_not_a_quota_eviction(seeded):
    db, _, link = seeded
    service = Mock()
    service.read.side_effect = WorkspaceError("RATE_LIMIT", "quota")
    with pytest.raises(WorkspaceError, match="quota"):
        DrivePermissions(db, service).check(link, "doc-a", "1", None)
    service.read.side_effect = None
    service.read.return_value = file_meta()
    assert DrivePermissions(db, service).check(link, "doc-a", "1", None)


@pytest.mark.asyncio
async def test_search_returns_checked_chunks_and_relink_withholds_old_result(seeded):
    db, key, _ = seeded
    service = Mock()
    service.read.return_value = file_meta()
    settings = SimpleNamespace(ENCRYPTION_KEY=key, EMBEDDING_API_KEY="", EMBEDDING_MODEL="text-embedding-3-small")
    async with httpx.AsyncClient() as http:
        runtime = DriveRuntime(settings, db, service, http)
        result = await runtime.search(1, "bao hanh")
        assert result["status"] == "ok"
        assert result["chunks"][0]["content"] == "Thời hạn bảo hành là mười hai tháng."
        assert result["verification"] == [{"file_id": "doc-a", "version": "1", "drive_id": None}]
        with db.begin() as session:
            conn = session.query(GoogleConnection).filter_by(user_id=1).one()
            conn.link_version += 1
        with db() as session:
            assert safe_result(session, 1, result)["chunks"] == []
        stale = await runtime.search(1, "bao hanh", expected=result["link"])
        assert stale["status"] == "link_changed"


def test_unhealthy_checkpoint_skips_permission_cache(seeded):
    db, _, link = seeded
    service = Mock()
    service.read.return_value = file_meta()
    permission = DrivePermissions(db, service)
    assert permission.check(link, "doc-a", "1", None)
    with db.begin() as session:
        row = session.get(DriveSyncCheckpoint, (1, "account-a", 1, "my_drive"))
        row.last_success_at = datetime.now(UTC) - timedelta(minutes=11)
    assert permission.check(link, "doc-a", "1", None)
    assert service.read.call_count == 2


@pytest.mark.asyncio
async def test_gateway_job_callback_rechecks_link_and_encrypts_telegram_delivery(seeded):
    db, key, _ = seeded
    settings = Settings(
        _env_file=None, APP_ENV="test", DRIVE_INDEX_ENABLED=True, ENCRYPTION_KEY=key,
        ENABLE_AI_AGENT_FORWARDING=True, AI_AGENT_BASE_URL="http://test-agent",
        AGENT_TO_GATEWAY_SECRET="a" * 32, GATEWAY_TO_AGENT_SECRET="b" * 32,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = db
    app.include_router(router)
    callbacks = []

    def receive(request):
        assert request.headers["Authorization"] == f"Bearer {settings.GATEWAY_TO_AGENT_SECRET}"
        callbacks.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": ["Bảo hành mười hai tháng."]})

    outbound = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    worker = WorkspaceWorker(settings, db, http_client=outbound)
    worker.workspace_svc = Mock()
    worker.workspace_svc.read.return_value = file_meta()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test-gateway",
            headers={"Authorization": f"Bearer {settings.AGENT_TO_GATEWAY_SECRET}"},
        ) as client:
            jobs = "/internal/v1/users/1/workspace/jobs"
            body = {
                "service": "drive", "operation": "knowledge.search", "body": {"question": "Bao hanh"},
                "telegram_chat_id": 101, "telegram_user_id": 101, "telegram_binding_id": "stage4-test",
                "wait_seconds": 0,
            }
            accepted = await client.post(jobs, json={**body, "request_id": "first-stage4"})
            assert accepted.status_code == 202
            assert await worker.process_one_cycle()
            assert callbacks[-1]["result"]["chunks"][0]["file_id"] == "doc-a"
            with db() as session:
                update = session.query(TelegramUpdate).one()
                assert update.ai_response.startswith("drive1:")
                assert "Bảo hành" not in update.ai_response
                delivery = json.loads(decrypt_secret(update.ai_response.removeprefix("drive1:"), key))
                assert delivery["drive_link"] == {"google_sub": "account-a", "link_version": 1}
                job = session.query(WorkspaceJob).one()
                assert "Bảo hành" not in job.result_encrypted
            old_job = await client.post(jobs, json={**body, "request_id": "second-stage4"})
            assert old_job.status_code == 202
            with db.begin() as session:
                session.query(GoogleConnection).update({"link_version": 2})
            first = await client.get(f"{jobs}/{accepted.json()['job_id']}")
            assert first.json()["result"]["status"] == "link_changed"
            assert first.json()["result"]["chunks"] == []
            assert await worker.process_one_cycle()
            assert callbacks[-1]["result"]["status"] == "link_changed"
            assert callbacks[-1]["result"]["chunks"] == []
            gateway = Mock()
            telegram = TelegramAIWorker(settings, db, telegram_client=gateway)
            assert await asyncio.to_thread(telegram.process_pending_once) == 1
            gateway.send_message.assert_not_called()
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_fallback_is_parsed_by_background_worker_and_expires(seeded):
    db, key, _ = seeded
    settings = Settings(_env_file=None, APP_ENV="test", DRIVE_INDEX_ENABLED=True, ENCRYPTION_KEY=key)
    reader = WorkspaceWorker(settings, db)
    indexer = WorkspaceWorker(settings, db, background=True, poll_interval=0.05)
    service = Mock()
    service.google.get_user_google_connection.return_value = SimpleNamespace(
        status="connected", google_sub="account-a", link_version=1,
    )

    def read(_user_id, _service, payload, *, force_rest):
        assert force_rest is True
        parameters = payload["parameters"]
        if parameters.get("alt") == "media":
            encoded = base64.b64encode("Bản lưu cũ yêu cầu đối chiếu bản gốc.".encode()).decode()
            return {"data": {"content_base64": encoded}}
        assert parameters["fileId"] == "archive"
        return {"data": {
            **file_meta()["data"], "id": "archive", "name": "archive.txt",
            "mimeType": "text/plain",
        }}

    service.read.side_effect = read
    reader.workspace_svc = indexer.workspace_svc = service
    indexer.start()
    try:
        async with asyncio.timeout(12):
            result = await reader.drive_runtime.search(1, "đối chiếu", file_id="archive")
        assert result["status"] == "ok"
        assert result["chunks"][0]["content"] == "Bản lưu cũ yêu cầu đối chiếu bản gốc."
        assert result["verification"][0]["version"] == "1"
        with db.begin() as session:
            assert not session.query(DriveFileMetadata).filter_by(file_id="archive").first()
            job = session.query(WorkspaceJob).filter_by(operation="index.preview").one()
            assert "Bản lưu cũ" not in job.result_encrypted
            job.created_at = datetime.now(UTC) - timedelta(hours=25)
        reader.drive_runtime.cleanup()
        with db() as session:
            job = session.query(WorkspaceJob).filter_by(operation="index.preview").one()
            assert job.result_encrypted is None
            assert job.payload_encrypted == ""
    finally:
        await indexer.stop()
        await reader.stop()
