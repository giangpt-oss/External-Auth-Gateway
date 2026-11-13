import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.workspace.idempotency import (
    IdempotencyManager,
    IdempotencyConflictError,
    compute_payload_hash,
)
from app.workspace.models import WorkspaceIdempotencyRecord


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_compute_payload_hash():
    p1 = {"calendarId": "primary", "summary": "Meeting", "attendees": ["a@b.com"]}
    p2 = {"summary": "Meeting", "attendees": ["a@b.com"], "calendarId": "primary"}
    assert compute_payload_hash(p1) == compute_payload_hash(p2)


def test_idempotent_write_cached_result(db_session):
    manager = IdempotencyManager()
    op_id = "op_test_123"
    payload = {"summary": "Standup Meeting"}

    # 1. First execution creates record
    record = manager.check_or_create(db_session, user_id=1, operation_id=op_id, payload=payload)
    assert record.status == "in_progress"
    
    # Mark as completed
    manager.mark_completed(
        db_session,
        operation_id=op_id,
        resource_id="evt_999",
        response_data={"id": "evt_999", "status": "confirmed"},
    )
    
    # 2. Duplicate call with same operation_id and payload returns completed record
    dup_record = manager.check_or_create(db_session, user_id=1, operation_id=op_id, payload=payload)
    assert dup_record.status == "completed"
    assert dup_record.resource_id == "evt_999"
    assert json.loads(dup_record.response_json)["id"] == "evt_999"


def test_idempotency_conflict_on_different_payload(db_session):
    manager = IdempotencyManager()
    op_id = "op_test_conflict"
    payload1 = {"summary": "Call Alice"}
    payload2 = {"summary": "Call Bob"}

    manager.check_or_create(db_session, user_id=1, operation_id=op_id, payload=payload1)
    
    with pytest.raises(IdempotencyConflictError):
        manager.check_or_create(db_session, user_id=1, operation_id=op_id, payload=payload2)


def test_idempotency_unknown_result_reconciliation(db_session):
    manager = IdempotencyManager()
    op_id = "op_test_unknown"
    payload = {"summary": "Important Sync"}

    manager.check_or_create(db_session, user_id=1, operation_id=op_id, payload=payload)
    
    # Simulate network timeout -> mark unknown_result
    manager.mark_unknown_result(db_session, operation_id=op_id)
    rec = manager.get_record(db_session, operation_id=op_id)
    assert rec.status == "unknown_result"
    
    # Reconcile: verification confirms resource was created on Google
    reconciled = manager.reconcile(
        db_session,
        operation_id=op_id,
        found_resource={"id": "evt_synced", "status": "confirmed"},
    )
    assert reconciled.status == "completed"
    assert reconciled.resource_id == "evt_synced"



def test_claim_never_reclaims_pending_or_other_users_operation(db_session):
    manager = IdempotencyManager()
    record, claimed = manager.claim(db_session, 1, "op_claim", {"value": 1})
    assert claimed is True
    duplicate, claimed = manager.claim(db_session, 1, "op_claim", {"value": 1})
    assert claimed is False
    assert duplicate.status == "in_progress"
    with pytest.raises(IdempotencyConflictError):
        manager.claim(db_session, 2, "op_claim", {"value": 1})
