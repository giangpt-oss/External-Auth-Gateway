"""Stage 2 concurrency checks against a disposable PostgreSQL database."""

import os
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_app_engine
from app.models import User
from app.workspace.models import WorkspaceJob
from app.workspace.network_slots import NetworkSlots
from app.workspace.worker import WorkspaceWorker


@pytest.fixture
def pg_engine():
    url = os.environ.get("STAGE2_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("Requires disposable PostgreSQL database bootstrapped by app.db_bootstrap")
    engine = create_app_engine(url)
    try:
        assert engine.dialect.name == "postgresql"
        yield engine
    finally:
        engine.dispose()


def test_postgres_advisory_slots_are_shared(pg_engine):
    first = NetworkSlots(pg_engine)
    second = NetworkSlots(pg_engine)
    result = []

    def compete():
        try:
            with second.acquire(123, write=True):
                result.append("acquired")
        except TimeoutError:
            result.append("limited")

    with first.acquire(123, write=True):
        thread = Thread(target=compete)
        thread.start()
        thread.join(timeout=5)
        assert result == ["limited"]
    with second.acquire(123, write=True):
        pass


def test_postgres_read_and_global_limits(pg_engine):
    first, second, background = NetworkSlots(pg_engine), NetworkSlots(pg_engine), NetworkSlots(pg_engine, background=True)
    with ExitStack() as slots:
        for limiter in (first, first, second, second):
            slots.enter_context(limiter.acquire(124))
        with pytest.raises(TimeoutError):
            with background.acquire(124):
                pytest.fail("Fifth read acquired a slot")
        slots.enter_context(background.acquire(125))
        with pytest.raises(TimeoutError):
            with background.acquire(125):
                pytest.fail("Second indexer request for the same user acquired a slot")
    with ExitStack() as slots:
        for i in range(16):
            slots.enter_context((first if i < 8 else second).acquire(1000 + i))
        with pytest.raises(TimeoutError):
            with background.acquire(2000):
                pytest.fail("Seventeenth request acquired a global slot")


@pytest.mark.asyncio
async def test_postgres_worker_claim_skips_locked_row(pg_engine):
    import uuid

    factory = sessionmaker(bind=pg_engine)
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    with factory.begin() as db:
        user = User(tenant="test", database="stage2", odoo_uid=int(suffix[:7], 16), login=suffix,
                    name="Stage 2 fixture", active=True)
        db.add(user)
        db.flush()
        user_id = user.id
        for i in range(2):
            db.add(WorkspaceJob(job_id=f"{suffix}-{i}", user_id=user_id, request_id=f"{suffix}-{i}",
                                service="calendar", operation="events.list", payload_encrypted="{}",
                                created_at=now + timedelta(seconds=i), deadline_at=now + timedelta(seconds=120)))
    worker = WorkspaceWorker(Settings(APP_ENV="test"), factory, worker_id="pg-fixture-worker")
    try:
        with factory() as locked, factory() as claimant:
            locked.query(WorkspaceJob).filter_by(job_id=f"{suffix}-0").with_for_update().one()
            job = worker.claim_next_job(claimant)
            assert job.job_id == f"{suffix}-1"
            assert worker.renew_lease(claimant, job.job_id, job.retry_count)
    finally:
        await worker.stop()
        with factory.begin() as db:
            db.query(WorkspaceJob).filter_by(user_id=user_id).delete()
            db.query(User).filter_by(id=user_id).delete()


@pytest.mark.asyncio
async def test_two_processes_recover_drive_job_and_queue_one_delivery(pg_engine, tmp_path):
    import json
    import subprocess
    import sys
    import uuid
    from pathlib import Path
    from unittest.mock import Mock

    from app.models import GoogleConnection, TelegramConnection, TelegramUpdate
    from app.main import create_app
    from app.security import encrypt_secret
    from app.telegram.worker import TelegramAIWorker
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    factory = sessionmaker(bind=pg_engine)
    suffix = uuid.uuid4().hex
    chat = int(suffix[:12], 16)
    key = Fernet.generate_key().decode()
    now = datetime.now(UTC)
    with factory.begin() as db:
        user = User(tenant="test", database="stage2", odoo_uid=int(suffix[:7], 16), login=suffix,
                    name="Stage 2 fixture", active=True)
        db.add(user)
        db.flush()
        uid = user.id
        db.add(TelegramConnection(user_id=uid, telegram_user_id=chat, private_chat_id=chat, binding_id=suffix))
        db.add(GoogleConnection(user_id=uid, google_sub=suffix, email="stage2@example.invalid",
                                status="connected", scopes="https://www.googleapis.com/auth/drive.readonly",
                                encrypted_access_token=encrypt_secret("fixture", key),
                                expires_at=now + timedelta(hours=1)))
    agent_dir = Path(__file__).resolve().parents[3] / "Module AI - Agent"
    agent_python = agent_dir / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    assert agent_python.is_file(), "The Agent test environment is required for Stage 2 acceptance"
    env = {**os.environ, "STAGE2_FIXTURE_KEY": key, "STAGE2_FIXTURE_OUTPUT": str(tmp_path / "callback.json"),
           "STAGE2_AGENT_PYTHON": str(agent_python), "STAGE2_AGENT_DIR": str(agent_dir),
           "AGENT_DATA_DIR": str(tmp_path / "agent-data"), "GATEWAY_TO_AGENT_SECRET": "g" * 40,
           "AGENT_TO_GATEWAY_SECRET": "a" * 40, "GEMINI_API_KEY": "", "GROQ_API_KEY": ""}
    env["STAGE2_AGENT_SCRIPT"] = r"""
import json, os, sys
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from app.main import app
from app.api.v1 import internal
from app.knowledge import drive

payload = json.loads(sys.stdin.buffer.read())
internal.gateway_client.resolve_identity = AsyncMock(return_value={
    'user_id': payload['user_id'], 'telegram_binding_id': payload['telegram_binding_id'],
    'tenant': 'test', 'database': 'stage2',
})
async def synthesize(question, evidence):
    assert question == 'PN22 còn bao nhiêu bộ?'
    assert evidence[0].content == 'Tồn kho PN22 là 15 bộ.'
    return 'PN22 còn 15 bộ [Nguồn [1]: Kho.txt].', evidence
drive.rag_service.answer_from_evidence = synthesize
client = TestClient(app)
response = client.post('/internal/v1/workspace/job-completed', json=payload,
                       headers={'Authorization': 'Bearer ' + os.environ['GATEWAY_TO_AGENT_SECRET']})
assert response.status_code == 200, response.text
sys.stdout.buffer.write(response.content)
"""
    script = r"""
import asyncio, json, os, subprocess, sys
from pathlib import Path
import httpx
from sqlalchemy.orm import sessionmaker
from app.config import Settings
from app.db import create_app_engine
from app.workspace.worker import WorkspaceWorker
from app.workspace.service import WorkspaceService

async def main():
    engine = create_app_engine(os.environ['STAGE2_POSTGRES_TEST_URL'])
    factory = sessionmaker(bind=engine)
    def google(request):
        if request.url.params.get('alt') == 'media':
            return httpx.Response(200, content='Tồn kho PN22 là 15 bộ.'.encode(), headers={'Content-Type':'text/plain'})
        return httpx.Response(200, json={'id':'fixture','name':'Kho.txt','capabilities':{'canDownload':True}})
    original = WorkspaceService.__init__
    def configure(self, google_client, signing_key, transport=None, *, use_mcp=False):
        original(self, google_client, signing_key, transport=httpx.MockTransport(google), use_mcp=False)
    WorkspaceService.__init__ = configure
    def callback(request):
        body = json.loads(request.content)
        Path(os.environ['STAGE2_FIXTURE_OUTPUT']).write_text(json.dumps(body), encoding='utf-8')
        assert body['result']['files'][0]['name'] == 'Kho.txt'
        output = subprocess.run([os.environ['STAGE2_AGENT_PYTHON'], '-c', os.environ['STAGE2_AGENT_SCRIPT']],
                                input=request.content, capture_output=True, timeout=20,
                                cwd=os.environ['STAGE2_AGENT_DIR'])
        assert output.returncode == 0, output.stderr.decode(errors='replace')
        return httpx.Response(200, json=json.loads(output.stdout))
    worker = WorkspaceWorker(Settings(APP_ENV='test', ENCRYPTION_KEY=os.environ['STAGE2_FIXTURE_KEY'],
                                       AI_AGENT_BASE_URL='http://agent.invalid', GATEWAY_TO_AGENT_SECRET='g' * 40,
                                       AGENT_TO_GATEWAY_SECRET='a' * 40),
                             factory, worker_id=sys.argv[1],
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(callback)))
    try:
        if sys.argv[1] == 'lost-process':
            with factory() as db:
                assert worker.claim_next_job(db) is not None
        else:
            assert await worker.process_one_cycle()
    finally:
        await worker.stop()
        engine.dispose()

asyncio.run(main())
"""
    try:
        gateway = create_app(Settings(APP_ENV="test", DATABASE_URL=os.environ["STAGE2_POSTGRES_TEST_URL"],
                                      ENCRYPTION_KEY=key, AGENT_TO_GATEWAY_SECRET="a" * 40,
                                      GATEWAY_TO_AGENT_SECRET="g" * 40))
        with TestClient(gateway) as client:
            response = client.post(f"/internal/v1/users/{uid}/workspace/jobs", headers={"Authorization": "Bearer " + "a" * 40},
                                   json={"request_id": suffix, "service": "drive", "operation": "knowledge.search",
                                         "parameters": {"file_id": "fixture"}, "wait_seconds": 0,
                                         "body": {"question": "PN22 còn bao nhiêu bộ?"}, "telegram_binding_id": suffix,
                                         "telegram_chat_id": chat, "telegram_user_id": chat, "deadline_seconds": 300})
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
        for process in ("lost-process", "recovery-process"):
            result = subprocess.run([sys.executable, "-c", script, process], env=env,
                                    capture_output=True, timeout=25)
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            if process == "lost-process":
                with factory.begin() as db:
                    job = db.query(WorkspaceJob).filter_by(job_id=job_id).one()
                    assert job.worker_id == process
                    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        callback = json.loads((tmp_path / "callback.json").read_text(encoding="utf-8"))
        assert callback["job_id"] == job_id and callback["request_id"] == suffix
        assert callback["telegram_binding_id"] == suffix
        with factory() as db:
            job = db.query(WorkspaceJob).filter_by(job_id=job_id).one()
            assert job.status == "completed" and job.retry_count == 2 and job.callback_delivered_at
            assert job.worker_id == "recovery-process"
            delivery = db.query(TelegramUpdate).filter_by(user_id=uid).one()
            message = json.loads(delivery.ai_response)["messages"][0]
            assert "PN22 còn 15 bộ" in message and "open?id=fixture" in message
        telegram = Mock()
        delivery_worker = TelegramAIWorker(Settings(APP_ENV="test"), factory, telegram_client=telegram)
        assert delivery_worker.process_pending_once() == 1
        assert delivery_worker.process_pending_once() == 0
        telegram.send_message.assert_called_once()
        assert telegram.send_message.call_args.args[:2] == (chat, message)
    finally:
        with factory.begin() as db:
            for model in (TelegramUpdate, WorkspaceJob, GoogleConnection, TelegramConnection):
                db.query(model).filter_by(user_id=uid).delete()
            db.query(User).filter_by(id=uid).delete()
