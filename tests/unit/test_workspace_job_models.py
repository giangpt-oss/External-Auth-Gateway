from datetime import datetime, timedelta, UTC
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User
from app.workspace.models import (
    WorkspaceJob,
    WorkspaceJobStatus,
    WorkspaceIdempotencyRecord,
    OAuthRefreshLease,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    
    user = User(
        tenant="https://odoo.local",
        database="odoo_db",
        odoo_uid=1,
        login="test@company.com",
        name="Test User",
    )
    session.add(user)
    session.commit()
    
    yield session
    session.close()


def test_create_workspace_job(db_session):
    now = datetime.now(UTC)
    job = WorkspaceJob(
        job_id="wsjob_test_001",
        user_id=1,
        request_id="req_001",
        service="drive",
        operation="files.list",
        protocol="rest",
        is_write=False,
        status=WorkspaceJobStatus.QUEUED,
        payload_encrypted="enc_payload",
        deadline_at=now + timedelta(seconds=120),
        telegram_chat_id=12345,
        telegram_user_id=12345,
    )
    db_session.add(job)
    db_session.commit()
    
    saved = db_session.query(WorkspaceJob).filter_by(job_id="wsjob_test_001").first()
    assert saved is not None
    assert saved.status == WorkspaceJobStatus.QUEUED
    assert saved.service == "drive"
    assert saved.is_write is False


def test_workspace_idempotency_record(db_session):
    rec = WorkspaceIdempotencyRecord(
        user_id=1,
        operation_id="op_cal_001",
        payload_hash="hash_abc123",
        status="completed",
        resource_id="event_google_id_777",
        response_json='{"status": "OK", "id": "event_google_id_777"}',
    )
    db_session.add(rec)
    db_session.commit()
    
    saved = db_session.query(WorkspaceIdempotencyRecord).filter_by(operation_id="op_cal_001").first()
    assert saved is not None
    assert saved.resource_id == "event_google_id_777"
    assert saved.status == "completed"


def test_oauth_refresh_lease(db_session):
    now = datetime.now(UTC)
    lease = OAuthRefreshLease(
        user_id=1,
        lease_holder="worker_proc_1",
        lease_expires_at=now + timedelta(seconds=60),
        connection_version=1,
    )
    db_session.add(lease)
    db_session.commit()
    
    saved = db_session.query(OAuthRefreshLease).filter_by(user_id=1).first()
    assert saved is not None
    assert saved.lease_holder == "worker_proc_1"
    assert saved.connection_version == 1
