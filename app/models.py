from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant = Column(String(255), nullable=False)
    database = Column(String(255), nullable=False)
    odoo_uid = Column(Integer, nullable=False)
    login = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    partner_id = Column(Integer, nullable=True)
    tz = Column(String(64), default="Asia/Bangkok", nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    credential = relationship(
        "OdooCredential", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    google_connections = relationship(
        "GoogleConnection", back_populates="user", cascade="all, delete-orphan"
    )
    account_selection = relationship(
        "GoogleAccountSelection", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    workspace_activities = relationship(
        "WorkspaceActivity", back_populates="user", cascade="all, delete-orphan"
    )
    telegram_connection = relationship(
        "TelegramConnection", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    calendar_pairs = relationship(
        "CalendarPair", back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def google_connection(self):
        """Backward-compatibility helper: returns active GoogleConnection or first available."""
        if hasattr(self, "account_selection") and self.account_selection and self.account_selection.active_account:
            return self.account_selection.active_account
        if hasattr(self, "google_connections") and self.google_connections:
            return self.google_connections[0]
        return None

    __table_args__ = (
        UniqueConstraint("tenant", "database", "odoo_uid", name="uq_users_tenant_db_uid"),
    )


class OdooCredential(Base):
    __tablename__ = "odoo_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    credential_kind = Column(String(32), nullable=False)  # 'password' or 'api_key'
    encrypted_secret = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="credential")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    csrf_secret = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    user = relationship("User", back_populates="sessions")


class LinkToken(Base):
    __tablename__ = "link_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    purpose = Column(String(64), nullable=False)  # 'telegram_link', 'google_oauth'
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    oauth_nonce = Column(String(128), nullable=True)
    encrypted_pkce_verifier = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


class GoogleConnection(Base):
    __tablename__ = "google_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    google_sub = Column(String(255), unique=True, index=True, nullable=False)
    email = Column(String(255), nullable=False)
    scopes = Column(Text, nullable=False)
    encrypted_access_token = Column(Text, nullable=False)
    encrypted_refresh_token = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    status = Column(
        String(32), default="connected", nullable=False
    )  # 'connected', 'reconnect_required', 'disconnected'
    connection_version = Column(Integer, nullable=False, default=1)
    link_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="google_connections")

    @property
    def account_id(self) -> int:
        return self.id


class GoogleAccountSelection(Base):
    __tablename__ = "google_account_selections"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    active_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    selection_version = Column(Integer, nullable=False, default=1)
    state = Column(String(32), nullable=False, default="accepting")  # 'accepting', 'draining'
    draining_since = Column(DateTime, nullable=True)
    target_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="SET NULL"), nullable=True
    )
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="account_selection")
    active_account = relationship("GoogleConnection", foreign_keys=[active_account_id])
    target_account = relationship("GoogleConnection", foreign_keys=[target_account_id])


class WorkspaceActivity(Base):
    __tablename__ = "workspace_activities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    google_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="CASCADE"), nullable=True, index=True
    )
    activity_type = Column(
        String(64), nullable=False, index=True
    )  # 'task_queued', 'task_running', 'preview_pending', 'write_reconciling', 'callback_pending', 'telegram_outbox', 'calendar_sync', 'drive_sync'
    reference_id = Column(String(128), nullable=False, index=True)
    status = Column(
        String(32), nullable=False, default="active", index=True
    )  # 'active', 'reconciling', 'completed', 'cancelled'
    has_outbound_write = Column(Boolean, nullable=False, default=False)
    lease_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="workspace_activities")
    google_account = relationship("GoogleConnection", foreign_keys=[google_account_id])


TELEGRAM_WEB_BINDING_PREFIX = "tg_bind_web_"


class TelegramConnection(Base):
    __tablename__ = "telegram_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    telegram_user_id = Column(BigInteger, unique=True, index=True, nullable=False)
    private_chat_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String(255), nullable=True)
    binding_id = Column(String(64), nullable=True, index=True)
    status = Column(
        String(32), default="connected", nullable=False
    )  # 'connected', 'blocked', 'disconnected'
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="telegram_connection")

    @property
    def verified_on_web(self) -> bool:
        return self.status == "connected" and bool(
            self.binding_id and self.binding_id.startswith(TELEGRAM_WEB_BINDING_PREFIX)
        )


class CalendarPair(Base):
    __tablename__ = "calendar_pairs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    google_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="CASCADE"), nullable=True, index=True
    )
    google_subject = Column(String(255), nullable=False)
    google_calendar_id = Column(String(255), default="primary", nullable=False)
    odoo_event_id = Column(Integer, nullable=False)
    google_event_id = Column(String(255), nullable=False)
    odoo_write_date = Column(DateTime, nullable=True)
    google_etag = Column(String(255), nullable=True)
    odoo_fingerprint = Column(String(64), nullable=True)
    google_fingerprint = Column(String(64), nullable=True)
    status = Column(
        String(32), default="synced", nullable=False
    )  # 'synced', 'conflict', 'source_missing', 'unavailable', 'paused'
    conflict_detail = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="calendar_pairs")
    google_account = relationship("GoogleConnection", foreign_keys=[google_account_id])

    __table_args__ = (
        UniqueConstraint("user_id", "google_account_id", "odoo_event_id", name="uq_calpair_user_account_odoo_event"),
        UniqueConstraint(
            "google_subject",
            "google_calendar_id",
            "google_event_id",
            name="uq_calpair_google_event",
        ),
    )


class SyncIntent(Base):
    __tablename__ = "sync_intents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    google_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source = Column(String(16), nullable=False)  # 'odoo', 'google'
    target = Column(String(16), nullable=False)  # 'odoo', 'google'
    source_event_id = Column(String(255), nullable=False)
    deterministic_google_id = Column(String(255), nullable=True)
    status = Column(
        String(32), default="pending", nullable=False
    )  # 'pending', 'completed', 'ambiguous', 'failed'
    result_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    update_id = Column(BigInteger, unique=True, index=True, nullable=False)
    payload = Column(Text, nullable=False)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    google_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claim_token = Column(String(64), nullable=True, index=True)
    status = Column(
        String(32), default="pending", nullable=False, index=True
    )  # 'pending', 'processing_ai', 'ai_received', 'retry_wait', 'processed', 'failed'
    attempts = Column(Integer, default=0, nullable=False)
    ai_response = Column(Text, nullable=True)
    delivery_status = Column(
        String(32), default="none", nullable=False
    )  # 'none', 'sending', 'delivered', 'delivery_unknown', 'failed'
    delivery_chunks = Column(Text, nullable=True)
    delivery_index = Column(Integer, default=0, nullable=False)
    lease_expires_at = Column(DateTime, nullable=True)
    retry_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class Outbox(Base):
    __tablename__ = "outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    dedup_key = Column(String(255), unique=True, index=True, nullable=False)
    message_type = Column(String(32), default="reminder", nullable=False)
    payload = Column(Text, nullable=False)
    status = Column(
        String(32), default="pending", nullable=False
    )  # 'pending', 'sending', 'sent', 'delivery_unknown', 'failed'
    attempts = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime, default=utc_now, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)


class JobRun(Base):
    __tablename__ = "job_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_name = Column(String(64), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)


class ActionOperation(Base):
    __tablename__ = "action_operations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    operation_id = Column(String(64), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    google_account_id = Column(
        Integer, ForeignKey("google_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    telegram_binding_id = Column(String(64), nullable=True)
    status = Column(
        String(32), default="in_progress", nullable=False
    )  # 'in_progress', 'completed', 'unknown_result', 'failed'
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "operation_id", name="uq_action_operation_user"),
    )


# Import all domain models to register with Base.metadata
try:
    import app.business.models
except ImportError:
    pass



try:
    import app.reporting.models
except ImportError:
    pass

try:
    import app.quotations.approval
    import app.quotations.execution
    import app.quotations.jobs
    import app.quotations.tracking  # noqa: F401
    import app.quotations.interaction  # noqa: F401
    import app.quotations.approval  # noqa: F401
except ImportError:
    pass

# Register Workspace tables and preserve the public model imports used by callers.
from app.workspace.models import (  # noqa: E402, F401
    OAuthRefreshLease,
    WorkspaceIdempotencyRecord,
    WorkspaceJob,
    WorkspaceJobStatus,
)
