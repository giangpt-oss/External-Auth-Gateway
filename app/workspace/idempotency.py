"""Idempotency management and write reconciliation for Google Workspace operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.workspace.models import WorkspaceIdempotencyRecord


class IdempotencyConflictError(Exception):
    """Raised when an operation_id is reused with a different payload."""
    pass


def compute_payload_hash(payload: Any) -> str:
    """Compute deterministic SHA256 hash of a payload dictionary or object."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyManager:
    """Ensures write operations are executed exactly once and safely cached."""

    def claim(self, db: Session, user_id: int, operation_id: str, payload: Any):
        """Atomically reserve an operation. Existing reservations must never be resent."""
        payload_hash = compute_payload_hash(payload)
        record = WorkspaceIdempotencyRecord(user_id=user_id, operation_id=operation_id,
                                             payload_hash=payload_hash, status="in_progress")
        db.add(record)
        try:
            db.commit()
            db.refresh(record)
            return record, True
        except IntegrityError:
            db.rollback()
        record = self.get_record(db, operation_id)
        if record is None:
            raise RuntimeError("Unable to reserve operation")
        if record.user_id != user_id or record.payload_hash != payload_hash:
            raise IdempotencyConflictError("Operation ID already belongs to another request")
        return record, False

    def check_or_create(
        self,
        db: Session,
        user_id: int,
        operation_id: str,
        payload: Any,
    ) -> WorkspaceIdempotencyRecord:
        """Check for existing idempotency record or register a new in_progress one."""
        p_hash = compute_payload_hash(payload)

        record = (
            db.query(WorkspaceIdempotencyRecord)
            .filter(WorkspaceIdempotencyRecord.operation_id == operation_id)
            .first()
        )

        if record:
            if record.payload_hash != p_hash:
                raise IdempotencyConflictError(
                    f"Operation ID '{operation_id}' was already used with a different payload."
                )
            return record

        record = WorkspaceIdempotencyRecord(
            user_id=user_id,
            operation_id=operation_id,
            payload_hash=p_hash,
            status="in_progress",
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    def get_record(self, db: Session, operation_id: str) -> Optional[WorkspaceIdempotencyRecord]:
        return (
            db.query(WorkspaceIdempotencyRecord)
            .filter(WorkspaceIdempotencyRecord.operation_id == operation_id)
            .first()
        )

    def mark_completed(
        self,
        db: Session,
        operation_id: str,
        resource_id: Optional[str],
        response_data: Any,
    ) -> WorkspaceIdempotencyRecord:
        record = self.get_record(db, operation_id)
        if not record:
            raise ValueError(f"Idempotency record not found for {operation_id}")

        record.status = "completed"
        record.resource_id = resource_id
        record.response_json = json.dumps(response_data) if response_data is not None else None
        db.commit()
        db.refresh(record)
        return record

    def mark_unknown_result(self, db: Session, operation_id: str) -> WorkspaceIdempotencyRecord:
        record = self.get_record(db, operation_id)
        if not record:
            raise ValueError(f"Idempotency record not found for {operation_id}")

        record.status = "unknown_result"
        db.commit()
        db.refresh(record)
        return record

    def reconcile(
        self,
        db: Session,
        operation_id: str,
        found_resource: Optional[Dict[str, Any]] = None,
    ) -> WorkspaceIdempotencyRecord:
        """Reconcile an unknown_result operation."""
        record = self.get_record(db, operation_id)
        if not record:
            raise ValueError(f"Idempotency record not found for {operation_id}")

        if found_resource:
            record.status = "completed"
            record.resource_id = found_resource.get("id") or str(found_resource)
            record.response_json = json.dumps(found_resource)
        else:
            record.status = "failed"
        db.commit()
        db.refresh(record)
        return record

