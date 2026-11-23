"""Workspace Activity Tracker and Blocker Detection Engine.

Tracks long-running Workspace activities, queued jobs, pending confirmations,
unknown writes undergoing reconciliation, and undelivered Telegram notifications.
Used to gate account-switching operations.
"""

from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    ActionOperation,
    GoogleAccountSelection,
    SyncIntent,
    TelegramUpdate,
    WorkspaceActivity,
)
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus


class ActivityType:
    TASK_QUEUED = "task_queued"
    TASK_RUNNING = "task_running"
    PREVIEW_PENDING = "preview_pending"
    WRITE_RECONCILING = "write_reconciling"
    CALLBACK_PENDING = "callback_pending"
    TELEGRAM_OUTBOX = "telegram_outbox"
    CALENDAR_SYNC = "calendar_sync"
    DRIVE_SYNC = "drive_sync"


class WorkspaceActivityTracker:
    @staticmethod
    def register_activity(
        db: Session,
        user_id: int,
        activity_type: str,
        reference_id: str,
        google_account_id: Optional[int] = None,
        has_outbound_write: bool = False,
        lease_expires_at: Optional[datetime] = None,
    ) -> WorkspaceActivity:
        """Register a new active Workspace activity that can hold an account-switch lock."""
        if google_account_id is None:
            # Auto-resolve from user's active account
            selection = db.query(GoogleAccountSelection).filter_by(user_id=user_id).first()
            if selection:
                google_account_id = selection.active_account_id

        # Check if activity record already exists for this reference_id
        activity = (
            db.query(WorkspaceActivity)
            .filter_by(user_id=user_id, reference_id=reference_id)
            .first()
        )
        now = datetime.now(UTC)
        if activity:
            activity.google_account_id = google_account_id
            activity.activity_type = activity_type
            activity.status = "active"
            activity.has_outbound_write = has_outbound_write
            activity.lease_expires_at = lease_expires_at
            activity.updated_at = now
        else:
            activity = WorkspaceActivity(
                user_id=user_id,
                google_account_id=google_account_id,
                activity_type=activity_type,
                reference_id=reference_id,
                status="active",
                has_outbound_write=has_outbound_write,
                lease_expires_at=lease_expires_at,
                created_at=now,
                updated_at=now,
            )
            db.add(activity)

        db.flush()
        return activity

    @staticmethod
    def finish_activity(
        db: Session,
        user_id: int,
        reference_id: str,
        status: str = "completed",
    ) -> bool:
        """Mark an activity as completed or cancelled, releasing its switch blocker lock."""
        activity = (
            db.query(WorkspaceActivity)
            .filter_by(user_id=user_id, reference_id=reference_id)
            .first()
        )
        if not activity:
            return False

        activity.status = status
        activity.updated_at = datetime.now(UTC)
        db.flush()
        return True

    @staticmethod
    def mark_reconciling(
        db: Session,
        user_id: int,
        reference_id: str,
    ) -> bool:
        """Mark an activity as undergoing reconciliation (e.g. unknown write result)."""
        activity = (
            db.query(WorkspaceActivity)
            .filter_by(user_id=user_id, reference_id=reference_id)
            .first()
        )
        if not activity:
            return False

        activity.status = "reconciling"
        activity.has_outbound_write = True
        activity.updated_at = datetime.now(UTC)
        db.flush()
        return True

    @staticmethod
    def get_active_blockers(
        db: Session,
        user_id: int,
        account_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Scan all blocker sources for this user and account:

        1. WorkspaceActivity records with status 'active' or 'reconciling'.
        2. WorkspaceJob with status in (queued, running, waiting_confirmation, unknown_result).
        3. ActionOperation with status in (in_progress, unknown_result).
        4. TelegramUpdate with pending processing or undelivered chunks.
        5. SyncIntent with pending status.

        Returns a clean list of safe public dictionaries with keys:
          - 'type': human-readable activity type
          - 'reference_id': safe public id (e.g. job_id or operation_id)
          - 'started_at': ISO-8601 string
          - 'description': brief description
          - 'is_reconciling': boolean
        """
        blockers: list[dict[str, Any]] = []
        seen_refs: set[str] = set()

        # 1. Check workspace_activities table
        act_query = db.query(WorkspaceActivity).filter(
            WorkspaceActivity.user_id == user_id,
            WorkspaceActivity.status.in_(["active", "reconciling"]),
            or_(
                WorkspaceActivity.activity_type != ActivityType.TASK_RUNNING,
                WorkspaceActivity.has_outbound_write.is_(True),
                WorkspaceActivity.lease_expires_at.is_(None),
                WorkspaceActivity.lease_expires_at > datetime.now(UTC),
            ),
        )
        if account_id is not None:
            act_query = act_query.filter(
                (WorkspaceActivity.google_account_id == account_id)
                | (WorkspaceActivity.google_account_id.is_(None))
            )
        for act in act_query.all():
            seen_refs.add(act.reference_id)
            blockers.append({
                "type": act.activity_type,
                "reference_id": act.reference_id,
                "started_at": act.created_at.isoformat() if act.created_at else "",
                "description": f"Hoạt động {act.activity_type} đang thực thi ({act.reference_id})",
                "is_reconciling": act.status == "reconciling",
            })

        # 2. Check workspace_jobs table
        job_statuses = [
            WorkspaceJobStatus.QUEUED.value,
            WorkspaceJobStatus.RUNNING.value,
            WorkspaceJobStatus.WAITING_CONFIRMATION.value,
            WorkspaceJobStatus.UNKNOWN_RESULT.value,
        ]
        job_query = db.query(WorkspaceJob).filter(
            WorkspaceJob.user_id == user_id,
            or_(
                WorkspaceJob.status.in_(job_statuses),
                (WorkspaceJob.callback_retry_at.is_not(None))
                & (WorkspaceJob.callback_delivered_at.is_(None)),
            ),
        )
        if account_id is not None:
            job_query = job_query.filter(
                (WorkspaceJob.google_account_id == account_id)
                | (WorkspaceJob.google_account_id.is_(None))
            )
        for job in job_query.all():
            if job.job_id not in seen_refs:
                seen_refs.add(job.job_id)
                desc = (
                    f"Tác vụ {job.service}.{job.operation} đang "
                    f"{'chờ xác nhận' if job.status == WorkspaceJobStatus.WAITING_CONFIRMATION.value else 'xử lý'}"
                )
                pending_callback = job.status not in job_statuses
                blockers.append({
                    "type": (
                        ActivityType.CALLBACK_PENDING if pending_callback else ActivityType.PREVIEW_PENDING
                        if job.status == WorkspaceJobStatus.WAITING_CONFIRMATION.value
                        else (
                            ActivityType.WRITE_RECONCILING
                            if job.status == WorkspaceJobStatus.UNKNOWN_RESULT.value
                            else ActivityType.TASK_RUNNING
                        )
                    ),
                    "reference_id": job.job_id,
                    "started_at": job.created_at.isoformat() if job.created_at else "",
                    "description": desc,
                    "is_reconciling": job.status == WorkspaceJobStatus.UNKNOWN_RESULT.value,
                })

        # 3. Check action_operations table (pending confirmations or unknown writes)
        op_query = db.query(ActionOperation).filter(
            ActionOperation.user_id == user_id,
            ActionOperation.status.in_(["in_progress", "unknown_result"]),
        )
        if account_id is not None:
            op_query = op_query.filter(
                (ActionOperation.google_account_id == account_id)
                | (ActionOperation.google_account_id.is_(None))
            )
        for op in op_query.all():
            if op.operation_id not in seen_refs:
                seen_refs.add(op.operation_id)
                blockers.append({
                    "type": (
                        ActivityType.WRITE_RECONCILING
                        if op.status == "unknown_result"
                        else ActivityType.TASK_RUNNING
                    ),
                    "reference_id": op.operation_id,
                    "started_at": op.created_at.isoformat() if op.created_at else "",
                    "description": f"Thao tác {op.action} đang xử lý ({op.operation_id})",
                    "is_reconciling": op.status == "unknown_result",
                })

        # 4. Check undelivered Telegram updates
        tg_query = db.query(TelegramUpdate).filter(
            TelegramUpdate.user_id == user_id,
            (
                TelegramUpdate.status.in_(["pending", "processing_ai", "ai_received", "retry_wait"])
                | TelegramUpdate.delivery_status.in_(["sending", "delivery_unknown"])
            ),
        )
        if account_id is not None:
            tg_query = tg_query.filter(
                (TelegramUpdate.google_account_id == account_id)
                | (TelegramUpdate.google_account_id.is_(None))
            )
        for tg in tg_query.all():
            ref = f"tg_update_{tg.update_id}"
            if ref not in seen_refs:
                seen_refs.add(ref)
                blockers.append({
                    "type": ActivityType.TELEGRAM_OUTBOX,
                    "reference_id": ref,
                    "started_at": tg.created_at.isoformat() if tg.created_at else "",
                    "description": f"Tin nhắn Telegram đang giao kết quả ({tg.update_id})",
                    "is_reconciling": tg.delivery_status == "delivery_unknown",
                })

        # 5. Check pending Calendar Sync Intents
        sync_query = db.query(SyncIntent).filter(
            SyncIntent.user_id == user_id,
            SyncIntent.status.in_(["pending", "ambiguous"]),
        )
        if account_id is not None:
            sync_query = sync_query.filter(
                (SyncIntent.google_account_id == account_id)
                | (SyncIntent.google_account_id.is_(None))
            )
        for si in sync_query.all():
            ref = f"sync_intent_{si.id}"
            if ref not in seen_refs:
                seen_refs.add(ref)
                blockers.append({
                    "type": ActivityType.CALENDAR_SYNC,
                    "reference_id": ref,
                    "started_at": si.created_at.isoformat() if si.created_at else "",
                    "description": f"Đồng bộ lịch {si.source} -> {si.target} đang chờ",
                    "is_reconciling": False,
                })

        return blockers
