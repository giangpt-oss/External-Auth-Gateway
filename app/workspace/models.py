"""SQLAlchemy Models for Workspace Background Jobs, Idempotency Records, and Distributed OAuth Refresh Leases."""
import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.types import UserDefinedType

from app.models import Base
from app.workspace.drive_embedding import EMBEDDING_DIMENSIONS


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
    google_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    selection_version = Column(Integer, nullable=True, default=1)
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
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    google_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="CASCADE"), unique=True, nullable=True, index=True
    )
    lease_holder = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    connection_version = Column(Integer, nullable=False, default=1)

    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))


DRIVE_LINK_COLUMNS = ("user_id", "google_sub", "link_version")
DRIVE_FILE_COLUMNS = (*DRIVE_LINK_COLUMNS, "file_id")


class DriveVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kwargs):
        return f"VECTOR({EMBEDDING_DIMENSIONS})"


class _DriveLinkColumns:
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    google_sub = Column(String(255), primary_key=True)
    link_version = Column(Integer, primary_key=True)


class _DriveFileColumns(_DriveLinkColumns):
    file_id = Column(String(255), primary_key=True)
    name = Column(Text, nullable=False)
    mime_type = Column(String(255), nullable=False)
    drive_id = Column(String(255), nullable=True)
    version = Column(String(64), nullable=False)
    modified_time = Column(DateTime(timezone=True), nullable=True)


class DriveFileMetadata(_DriveFileColumns, Base):
    __tablename__ = "drive_file_metadata"

    indexed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(32), nullable=False, default="pending", server_default="pending")
    status_reason = Column(Text, nullable=True)
    embedding_model = Column(String(128), nullable=True)
    parser_version = Column(String(64), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint(*DRIVE_FILE_COLUMNS),
        Index("ix_drive_metadata_link_modified", *DRIVE_LINK_COLUMNS, "modified_time"),
        Index("ix_drive_metadata_link_scope", *DRIVE_LINK_COLUMNS, "drive_id"),
    )


class DriveScanCandidate(_DriveFileColumns, Base):
    __tablename__ = "drive_scan_candidates"

    __table_args__ = (
        PrimaryKeyConstraint(*DRIVE_FILE_COLUMNS),
        Index("ix_drive_scan_link_modified", *DRIVE_LINK_COLUMNS, "modified_time"),
    )


class DriveFileChunk(Base):
    __tablename__ = "drive_file_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    google_sub = Column(String(255), nullable=False)
    link_version = Column(Integer, nullable=False)
    file_id = Column(String(255), nullable=False)
    file_version = Column(String(64), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    page_or_section = Column(String(128), nullable=True)
    encrypted_content = Column(Text, nullable=False)
    tsv = Column(TSVECTOR().with_variant(Text(), "sqlite"), nullable=False)
    embedding = Column(DriveVector().with_variant(Text(), "sqlite"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        ForeignKeyConstraint(
            DRIVE_FILE_COLUMNS,
            [f"drive_file_metadata.{column}" for column in DRIVE_FILE_COLUMNS],
            ondelete="CASCADE",
        ),
        UniqueConstraint(*DRIVE_FILE_COLUMNS, "chunk_index", name="uq_drive_file_chunk"),
        Index("ix_drive_chunks_tsv", "tsv", postgresql_using="gin").ddl_if(dialect="postgresql"),
        {"sqlite_autoincrement": True},
    )


class DriveSyncCheckpoint(_DriveLinkColumns, Base):
    __tablename__ = "drive_sync_checkpoints"

    scope = Column(String(255), primary_key=True)
    scan_id = Column(String(36), nullable=True)
    scan_page_token = Column(Text, nullable=True)
    start_page_token = Column(Text, nullable=True)
    scan_state = Column(String(32), nullable=False, default="pending", server_default="pending")
    last_error_code = Column(String(64), nullable=True)
    last_full_scan_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (PrimaryKeyConstraint(*DRIVE_LINK_COLUMNS, "scope"),)


class DrivePermissionState(_DriveLinkColumns, Base):
    __tablename__ = "drive_permission_states"

    file_id = Column(String(255), primary_key=True)
    generation = Column(Integer, nullable=False, default=0, server_default="0")
    checked_at = Column(DateTime(timezone=True), nullable=True)
    file_version = Column(String(64), nullable=True)
    scope = Column(String(255), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint(*DRIVE_FILE_COLUMNS),
        Index("ix_drive_permissions_link_scope", *DRIVE_LINK_COLUMNS, "scope"),
    )

