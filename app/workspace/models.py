"""SQLAlchemy Models for Workspace Background Jobs, Idempotency Records, and Distributed OAuth Refresh Leases."""
from datetime import datetime, UTC
import enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.models import Base


class WorkspaceJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN_RESULT = "unknown_result"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class WorkspaceJob(Base):
    __tablename__ = "workspace_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    request_id = Column(String(128), nullable=False, index=True)
    operation_id = Column(String(64), nullable=True, index=True)
    
    action = Column(String(32), nullable=False, default="read")  # read, prepare, execute
    service = Column(String(32), nullable=False)  # gmail, calendar, drive, docs, sheets, slides
    operation = Column(String(128), nullable=False)
    protocol = Column(String(16), nullable=False, default="auto")  # auto, rest, mcp
    is_write = Column(Boolean, nullable=False, default=False)
    
    status = Column(String(32), nullable=False, default=WorkspaceJobStatus.QUEUED.value, index=True)
    phase = Column(String(64), nullable=True)
    
    payload_encrypted = Column(Text, nullable=False)
    result_encrypted = Column(Text, nullable=True)
    
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    
    worker_id = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    retry_count = Column(Integer, nullable=False, default=0)
    deadline_at = Column(DateTime, nullable=False, index=True)
    
    telegram_chat_id = Column(BigInteger, nullable=True)
    telegram_user_id = Column(BigInteger, nullable=True)
    
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
    completed_at = Column(DateTime, nullable=True)
    callback_delivered_at = Column(DateTime, nullable=True)
    callback_retry_at = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        Index("ix_workspace_jobs_user_request", "user_id", "request_id", unique=True),
        Index("ix_workspace_jobs_worker_lease", "status", "lease_expires_at"),
    )


class WorkspaceIdempotencyRecord(Base):
    __tablename__ = "workspace_idempotency_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    operation_id = Column(String(64), unique=True, nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False, index=True)
    
    status = Column(String(32), nullable=False, default="in_progress")
    resource_id = Column(String(128), nullable=True)
    response_json = Column(Text, nullable=True)
    
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_workspace_idempotency_lookup", "user_id", "operation_id"),
    )


class OAuthRefreshLease(Base):
    __tablename__ = "oauth_refresh_leases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    lease_holder = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    connection_version = Column(Integer, nullable=False, default=1)
    
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

