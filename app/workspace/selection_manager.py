"""Selection State Machine and Admission Control for Google Workspace.

Governs account-switching lifecycle:
- accepting: accepting new workspace tasks and sync jobs
- draining: rejected new tasks, waiting for blockers to clear
- ready_to_switch: blockers cleared, awaiting manual user switch confirmation
- switch: atomically changes active account and increments selection_version
- resume: cancels draining and resumes accepting tasks on current account
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.google.account_resolver import GoogleAccountResolver
from app.google.account_selection import AccountSelectionManager, SwitchLockError
from app.models import GoogleAccountSelection
from app.workspace.activity_tracker import ActivityType, WorkspaceActivityTracker


@dataclass(frozen=True, slots=True)
class WorkspaceAccountPin:
    user_id: int
    account_id: int
    selection_version: int


active_account_pin: ContextVar[WorkspaceAccountPin | None] = ContextVar(
    "workspace_account_pin", default=None,
)


def current_account_pin(user_id: int) -> WorkspaceAccountPin | None:
    pin = active_account_pin.get()
    return pin if pin is not None and pin.user_id == user_id else None


class SelectionManager:
    @staticmethod
    def check_admission(
        db: Session,
        user_id: int,
        account_id: Optional[int] = None,
        expected_version: Optional[int] = None,
    ) -> tuple[bool, Optional[str], Optional[GoogleAccountSelection]]:
        """Verify whether new Workspace tasks or syncs can be admitted for this user.

        Returns:
            (is_admitted: bool, rejection_reason: Optional[str], selection: Optional[GoogleAccountSelection])
        """
        selection = GoogleAccountResolver.lock_selection(db, user_id)
        if selection is None:
            return False, "Chưa liên kết tài khoản Google nào.", None

        if not selection.active_account_id:
            return False, "Chưa chọn tài khoản Google đang sử dụng.", selection

        if selection.state == "draining":
            return (
                False,
                "Hệ thống đang hoàn tất các tác vụ dang dở để chuẩn bị chuyển tài khoản. "
                "Tạm thời không nhận việc mới.",
                selection,
            )

        if expected_version is not None and selection.selection_version != expected_version:
            return (
                False,
                f"Tài khoản Google đang dùng đã thay đổi (phiên bản {selection.selection_version} != {expected_version}). "
                "Vui lòng thử lại với tài khoản hiện tại.",
                selection,
            )

        if account_id is not None and selection.active_account_id != account_id:
            return (
                False,
                "Tác vụ được chỉ định cho một tài khoản khác với tài khoản đang được chọn.",
                selection,
            )

        return True, None, selection

    @classmethod
    def admit(
        cls, db: Session, user_id: int, *, account_id: int | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceAccountPin:
        """Return the immutable account pin while holding the caller's transaction lock."""
        admitted, reason, selection = cls.check_admission(db, user_id, account_id, expected_version)
        if admitted and selection is not None:
            connection = GoogleAccountResolver.get_account(db, user_id, selection.active_account_id)
            if connection is None or connection.status == "disconnected":
                raise SwitchLockError("NOT_CONNECTED", "Chưa liên kết tài khoản Google nào.")
            if connection.status != "connected":
                raise SwitchLockError("RECONNECT_REQUIRED", "Cần kết nối lại Google trên Portal.")
            return WorkspaceAccountPin(user_id, selection.active_account_id, selection.selection_version)
        if (selection is not None and (
            (expected_version is not None and selection.selection_version != expected_version)
            or (account_id is not None and selection.active_account_id != account_id)
        )):
            raise SwitchLockError("ACCOUNT_CHANGED", "Tài khoản Google đang dùng đã thay đổi.")
        if selection is not None and selection.state == "draining":
            raise SwitchLockError("ACCOUNT_DRAINING", reason)
        raise SwitchLockError("NOT_CONNECTED", reason)

    @classmethod
    @contextmanager
    def admit_activity(
        cls, session_factory, user_id: int, *, account_id: int | None = None,
        expected_version: int | None = None, activity_type: str = ActivityType.TASK_RUNNING,
        has_outbound_write: bool = False,
    ):
        """Admit synchronous work, release the row lock, and retain a durable blocker."""
        pin = current_account_pin(user_id)
        if pin is not None:
            if (
                account_id is not None and pin.account_id != account_id
                or expected_version is not None and pin.selection_version != expected_version
            ):
                raise SwitchLockError("ACCOUNT_CHANGED", "Tài khoản Google đang dùng đã thay đổi.")
            yield pin
            return

        reference = f"workspace_{uuid4().hex}"
        with session_factory.begin() as db:
            pin = cls.admit(db, user_id, account_id=account_id, expected_version=expected_version)
            WorkspaceActivityTracker.register_activity(
                db, user_id, activity_type, reference, google_account_id=pin.account_id,
                has_outbound_write=has_outbound_write,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2)
                if not has_outbound_write and activity_type == ActivityType.TASK_RUNNING else None,
            )
        token = active_account_pin.set(pin)
        reconcile = False
        try:
            yield pin
        except Exception as exc:
            reconcile = has_outbound_write and getattr(exc, "code", None) == "UNKNOWN_RESULT"
            raise
        finally:
            active_account_pin.reset(token)
            with session_factory.begin() as db:
                if reconcile:
                    WorkspaceActivityTracker.mark_reconciling(db, user_id, reference)
                else:
                    WorkspaceActivityTracker.finish_activity(db, user_id, reference)

    # Preserve the original entry points for selection callers.
    request_switch = staticmethod(AccountSelectionManager.request_switch)
    confirm_switch = staticmethod(AccountSelectionManager.confirm_switch)
    resume_current = staticmethod(AccountSelectionManager.resume_current)
