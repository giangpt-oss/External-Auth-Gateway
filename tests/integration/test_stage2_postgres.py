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
