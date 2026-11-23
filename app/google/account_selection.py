"""Google account selection transitions and transaction ownership."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.google.account_resolver import GoogleAccountResolver
from app.models import GoogleAccountSelection, GoogleConnection
from app.workspace.activity_tracker import WorkspaceActivityTracker


class SwitchLockError(Exception):
    def __init__(self, code: str, message: str, blockers: list[dict[str, Any]] | None = None):
        self.code = code
        self.message = message
        self.blockers = blockers or []
        super().__init__(message)


class AccountSelectionManager:
    @staticmethod
    def request_switch(
        db: Session,
        user_id: int,
        target_account_id: int,
        *,
        activate_if_idle: bool = False,
        current_version: int | None = None,
    ) -> dict[str, Any]:
        """User requests switching from current active account to target_account_id.

        - If already on target_account_id, returns status 'already_active'.
        - Validates ownership and status of target_account_id.
        - Sets state = 'draining'.
        - Queries blockers on current active account.
        - If blockers exist: returns status 'busy' (409) with blocker list.
        - If no blockers: returns status 'ready_to_switch'.
        """
        selection = GoogleAccountResolver.lock_selection(db, user_id)
        if selection is None:
            raise SwitchLockError("NO_ACCOUNT", "Người dùng chưa có tài khoản Google nào.")

        AccountSelectionManager._check_version(selection, current_version)
        if activate_if_idle and selection.state == "draining" and selection.target_account_id != target_account_id:
            raise SwitchLockError("SWITCH_PENDING", "Hãy hoàn tất hoặc hủy yêu cầu chuyển đang chờ.")

        target_conn = GoogleAccountResolver.get_account(db, user_id, target_account_id)
        if not target_conn:
            raise SwitchLockError("NOT_FOUND", "Tài khoản đích không tồn tại hoặc không thuộc quyền sở hữu.")
        if target_conn.status != "connected":
            raise SwitchLockError("TARGET_NOT_CONNECTED", f"Tài khoản đích đang ở trạng thái '{target_conn.status}'. Cần kết nối lại trước.")

        if selection.active_account_id == target_account_id:
            # If already active and in draining, resume it
            if selection.state == "draining":
                selection.state = "accepting"
                selection.draining_since = None
                selection.target_account_id = None
                db.commit()
            return {
                "status": "already_active",
                "active_account_id": target_account_id,
                "active_email": target_conn.email,
                "selection_version": selection.selection_version,
                "state": "accepting",
                "can_switch": False,
                "blockers": [],
            }

        already_draining = selection.state == "draining"

        # Put into draining state
        now = datetime.now(UTC)
        selection.state = "draining"
        selection.draining_since = selection.draining_since or now
        selection.target_account_id = target_account_id
        blockers = WorkspaceActivityTracker.get_active_blockers(
            db, user_id, account_id=selection.active_account_id
        )

        can_switch = not blockers
        if activate_if_idle and can_switch and not already_draining:
            return AccountSelectionManager._apply_switch(db, selection, target_conn)
        result = {
            "status": "ready_to_switch" if can_switch else "busy",
            "state": "draining",
            "active_account_id": selection.active_account_id,
            "target_account_id": target_account_id,
            "target_email": target_conn.email,
            "selection_version": selection.selection_version,
            "can_switch": can_switch,
            "blockers": blockers,
        }
        db.commit()
        return result

    @staticmethod
    def confirm_switch(
        db: Session,
        user_id: int,
        target_account_id: int,
        current_version: int | None = None,
    ) -> dict[str, Any]:
        """User confirms switching to target_account_id.

        Must verify:
        1. 0 blockers remain.
        2. target_account_id is valid and connected.
        3. selection_version matches expected (if provided).
        Atomically switches active account, increments version, and sets state = 'accepting'.
        """
        selection = GoogleAccountResolver.get_selection(db, user_id, for_update=True)
        if not selection:
            raise SwitchLockError("NO_SELECTION", "Chưa tìm thấy thông tin lựa chọn tài khoản.")

        AccountSelectionManager._check_version(selection, current_version)

        if selection.state != "draining" or selection.target_account_id != target_account_id:
            raise SwitchLockError(
                "SWITCH_NOT_REQUESTED", "Chưa yêu cầu chuyển sang tài khoản Google này.",
            )

        target_conn = GoogleAccountResolver.get_account(db, user_id, target_account_id)
        if not target_conn or target_conn.status != "connected":
            raise SwitchLockError("TARGET_NOT_CONNECTED", "Tài khoản đích không sẵn sàng kết nối.")

        # Strict blocker check under lock
        blockers = WorkspaceActivityTracker.get_active_blockers(
            db, user_id, account_id=selection.active_account_id
        )
        if blockers:
            raise SwitchLockError(
                "ACCOUNT_BUSY",
                "Còn tác vụ đang thực thi trên tài khoản hiện tại. Chưa thể chuyển ngay.",
                blockers=blockers,
            )

        return AccountSelectionManager._apply_switch(db, selection, target_conn)

    @staticmethod
    def _check_version(selection: GoogleAccountSelection, current_version: int | None) -> None:
        if current_version is not None and selection.selection_version != current_version:
            raise SwitchLockError(
                "VERSION_MISMATCH",
                f"Phiên bản lựa chọn đã thay đổi ({selection.selection_version} != {current_version}). Vui lòng tải lại.",
            )

    @staticmethod
    def _apply_switch(db: Session, selection: GoogleAccountSelection, target_conn: GoogleConnection) -> dict[str, Any]:
        target_account_id = target_conn.id
        old_account_id = selection.active_account_id
        selection.active_account_id = target_account_id
        selection.selection_version += 1
        selection.state = "accepting"
        selection.draining_since = None
        selection.target_account_id = None
        selection.updated_at = datetime.now(UTC)

        result = {
            "status": "switched",
            "previous_account_id": old_account_id,
            "active_account_id": target_account_id,
            "active_email": target_conn.email,
            "selection_version": selection.selection_version,
            "state": "accepting",
        }
        db.commit()
        return result

    @staticmethod
    def resume_current(db: Session, user_id: int, *, current_version: int | None = None) -> dict[str, Any]:
        """User clicks 'Tiếp tục dùng A' to cancel draining mode and resume accepting work."""
        selection = GoogleAccountResolver.get_selection(db, user_id, for_update=True)
        if not selection:
            raise SwitchLockError("NO_SELECTION", "Chưa có cấu hình lựa chọn tài khoản.")

        AccountSelectionManager._check_version(selection, current_version)
        selection.state = "accepting"
        selection.draining_since = None
        selection.target_account_id = None
        selection.updated_at = datetime.now(UTC)
        active_conn = GoogleAccountResolver.get_active_account(db, user_id)
        result = {
            "status": "resumed",
            "state": "accepting",
            "active_account_id": selection.active_account_id,
            "active_email": active_conn.email if active_conn else "",
            "selection_version": selection.selection_version,
        }
        db.commit()
        return result
