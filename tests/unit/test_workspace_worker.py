import asyncio
from datetime import UTC, datetime, timedelta
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus
from app.workspace.worker import WorkspaceWorker


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        user = User(
            id=1,
            tenant="default",
            database="odoo",
            odoo_uid=1,
            login="admin",
            name="Admin User",
            active=True,
        )
        session.add(user)
        session.commit()
        yield session, Session
    finally:
        session.close()


def test_worker_claim_and_renew_job(db_session):
    session, session_factory = db_session
    
    # 1. Insert a queued job
    now = datetime.now(UTC)
    job = WorkspaceJob(
        job_id="job_claim_test",
        user_id=1,
        request_id="req_claim_1",
        service="calendar",
        operation="events.list",
        status=WorkspaceJobStatus.QUEUED.value,
        payload_encrypted=json.dumps({"service": "calendar", "operation": "events.list"}),
        deadline_at=now + timedelta(seconds=120),
    )
    session.add(job)
    session.commit()
    
    settings = MagicMock()
    settings.ENCRYPTION_KEY = None
    settings.AI_AGENT_BASE_URL = "http://localhost:8000"
    settings.GATEWAY_TO_AGENT_SECRET = "gw_to_agent_secret"
    
    worker = WorkspaceWorker(settings=settings, session_factory=session_factory, worker_id="worker-test-1")
    
    # 2. Claim job
    with session_factory() as db:
        claimed = worker.claim_next_job(db)
        assert claimed is not None
        assert claimed.job_id == "job_claim_test"
        assert claimed.status == WorkspaceJobStatus.RUNNING.value
        assert claimed.worker_id == "worker-test-1"
        lease_exp = claimed.lease_expires_at
        if lease_exp.tzinfo is None:
            lease_exp = lease_exp.replace(tzinfo=UTC)
        assert lease_exp > now
        
    # 3. Renew lease
    with session_factory() as db:
        renewed = worker.renew_lease(db, job_id="job_claim_test")
        assert renewed is True


@pytest.mark.asyncio
async def test_worker_complete_job_and_send_callback(db_session):
    session, session_factory = db_session
    now = datetime.now(UTC)
    job = WorkspaceJob(
        job_id="job_exec_test",
        user_id=1,
        request_id="req_exec_1",
        service="calendar",
        operation="events.list",
        status=WorkspaceJobStatus.QUEUED.value,
        payload_encrypted=json.dumps({"service": "calendar", "operation": "events.list"}),
        deadline_at=now + timedelta(seconds=120),
    )
    session.add(job)
    session.commit()
    
    settings = MagicMock()
    settings.ENCRYPTION_KEY = None
    settings.AI_AGENT_BASE_URL = "http://localhost:8000"
    settings.GATEWAY_TO_AGENT_SECRET = "gw_to_agent_secret"
    
    worker = WorkspaceWorker(settings=settings, session_factory=session_factory, worker_id="worker-test-1")
    
    # Mock executor & callback
    worker.execute_job_payload = AsyncMock(return_value={"status": "completed", "result": {"items": [{"id": "ev1", "summary": "Test Meeting"}]}})
    worker.send_callback = AsyncMock(return_value=True)
    
    # Run single job processing
    await worker.process_one_cycle()
    
    # Verify status changed to completed
    with session_factory() as db:
        updated = db.query(WorkspaceJob).filter_by(job_id="job_exec_test").first()
        assert updated.status == WorkspaceJobStatus.COMPLETED.value
        assert updated.completed_at is not None
        assert "Test Meeting" in updated.result_encrypted
        
    worker.send_callback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [False, True])
async def test_reclaim_never_resends_mutation(db_session, mutation):
    session, factory = db_session
    now = datetime.now(UTC)
    session.add(WorkspaceJob(
        job_id="reclaim", user_id=1, request_id="reclaim", service="calendar",
        operation="events.insert" if mutation else "events.list", is_write=mutation,
        status="running", worker_id="lost", retry_count=1,
        payload_encrypted=json.dumps({"action": "execute" if mutation else "read"}),
        deadline_at=now + timedelta(seconds=60), lease_expires_at=now - timedelta(seconds=1),
    ))
    session.commit()
    settings = MagicMock(ENCRYPTION_KEY=None)
    worker = WorkspaceWorker(settings, factory)
    worker.execute_job_payload = AsyncMock(return_value={"result": {"items": []}})
    worker.send_callback = AsyncMock(side_effect=[False, True])
    try:
        assert await worker.process_one_cycle()
        assert worker.execute_job_payload.await_count == (0 if mutation else 1)
        with factory() as db:
            job = db.query(WorkspaceJob).filter_by(job_id="reclaim").one()
            assert job.status == ("unknown_result" if mutation else "completed")
            assert job.callback_delivered_at is None
            assert job.callback_retry_at is not None
            job.callback_retry_at = now
            db.commit()
        assert await worker.deliver_pending_callback()
        assert not await worker.deliver_pending_callback()
        assert worker.send_callback.await_count == 2
    finally:
        await worker.stop()


def test_telegram_outbox_deduplicates_and_checks_binding(db_session):
    from app.models import TelegramConnection, TelegramUpdate

    session, factory = db_session
    session.add(TelegramConnection(user_id=1, telegram_user_id=7000000001,
                                   private_chat_id=7000000001, binding_id="verified-binding"))
    session.commit()
    job = WorkspaceJob(job_id="delivery", user_id=1, telegram_user_id=7000000001,
                       telegram_chat_id=7000000001)
    worker = WorkspaceWorker(MagicMock(), factory, http_client=MagicMock())
    worker.queue_telegram_result(job, "different-binding", ["Wrong recipient"])
    worker.queue_telegram_result(job, "verified-binding", ["Result"])
    worker.queue_telegram_result(job, "verified-binding", ["Duplicate"])
    with factory() as db:
        record = db.query(TelegramUpdate).one()
        assert record.delivery_status == "ready"
        assert json.loads(record.ai_response) == {
            "messages": ["Result"], "attachments": [], "confirmation_buttons": {},
            "telegram_binding_id": "verified-binding",
        }
