"""Advance per-scope Drive change logs alongside durable catalog updates."""
from datetime import UTC, datetime

from app.workspace.drive_cap import (
    DOWNLOAD_ACCESS_ERRORS,
    DRIVE_FILE_CAP,
    MY_DRIVE,
    enforce_cap,
    file_key,
    link_values,
    store_file,
)
from app.workspace.drive_downloader import FILE_FIELDS, FILE_ID, drive_read, observe_file
from app.workspace.drive_permissions import invalidate
from app.workspace.drive_scanner import CURSOR_ERRORS, lose_scope
from app.workspace.drive_storage import DriveLink, lock_drive_link
from app.workspace.models import DriveFileMetadata, DriveSyncCheckpoint
from app.workspace.service import WorkspaceError


class DriveSync:
    def __init__(self, session_factory, service, *, cap=DRIVE_FILE_CAP):
        if not 1 <= cap <= DRIVE_FILE_CAP:
            raise ValueError("Invalid Drive file cap.")
        self.sessions = session_factory
        self.service = service
        self.cap = cap

    def _cursor(self, link, scope, *, attempt=False):
        with self.sessions.begin() as db:
            if attempt and not lock_drive_link(db, link):
                return None
            row = db.get(DriveSyncCheckpoint, file_key(link, scope))
            if not row or row.scan_state != "complete" or not row.start_page_token:
                return None
            if attempt:
                row.last_sync_at = datetime.now(UTC)
            return row.scan_id, row.start_page_token

    @staticmethod
    def _matches(row, cursor):
        return row and row.scan_state == "complete" and (row.scan_id, row.start_page_token) == cursor

    def incremental_sync(self, link: DriveLink, scope: str, *, max_pages=20) -> bool:
        cursor = self._cursor(link, scope, attempt=True)
        for _ in range(max_pages):
            if cursor is None:
                return False
            params = {
                "pageToken": cursor[1],
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "includeRemoved": True,
                "restrictToMyDrive": False,
                "spaces": "drive",
                "pageSize": 100,
                "fields": f"nextPageToken,newStartPageToken,changes(changeType,removed,driveId,fileId,file({FILE_FIELDS}))",
            }
            if scope != MY_DRIVE:
                params["driveId"] = scope
            try:
                response = drive_read(self.service, link, "changes.list", **params)
            except WorkspaceError as exc:
                self._fail(link, scope, cursor, exc.code)
                return False
            next_page = response.get("nextPageToken")
            new_token = next_page or response.get("newStartPageToken")
            if not new_token:
                self._fail(link, scope, cursor, "MISSING_CHANGES_CURSOR")
                return False
            if not self._commit_page(link, scope, cursor, new_token, not next_page, response):
                return False
            if not next_page:
                return True
            cursor = cursor[0], new_token
        return False

    def _commit_page(self, link, scope, cursor, new_token, final, response):
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return False
            row = db.get(DriveSyncCheckpoint, file_key(link, scope))
            if not self._matches(row, cursor):
                return False
            for change in response.get("changes", []):
                self._apply(db, link, scope, change)
            enforce_cap(db, link, cap=self.cap)
            if row.scan_state != "complete":
                return False
            row.start_page_token = new_token
            if final:
                row.last_success_at = datetime.now(UTC)
                row.last_error_code = None
        return True

    @staticmethod
    def _apply(db, link, scope, change):
        if change.get("changeType") == "drive":
            drive_id = change.get("driveId")
            if not drive_id or drive_id == MY_DRIVE:
                return
            if change.get("removed"):
                lose_scope(db, link, drive_id)
            else:
                checkpoint = db.get(DriveSyncCheckpoint, file_key(link, drive_id))
                if checkpoint is None:
                    db.add(DriveSyncCheckpoint(**link_values(link), scope=drive_id, scan_state="rescan"))
                elif checkpoint.scan_state == "lost":
                    checkpoint.scan_state = "rescan"
            return

        file_id = change.get("fileId", "")
        if not FILE_ID.fullmatch(file_id):
            return
        data = change.get("file") or {}
        observed_scope = data.get("driveId") or MY_DRIVE
        invalidate(db, link, file_id=file_id)
        if data and observed_scope != scope:
            return
        file = observe_file(data) if data else None
        if (change.get("removed") or file is None or file.file_id != file_id
                or data.get("capabilities", {}).get("canDownload") is not True):
            row = db.get(DriveFileMetadata, file_key(link, file_id))
            if row and (row.drive_id or MY_DRIVE) == scope:
                db.delete(row)
            return
        store_file(db, link, file)

    def _fail(self, link, scope, cursor, code):
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return
            row = db.get(DriveSyncCheckpoint, file_key(link, scope))
            if not self._matches(row, cursor):
                return
            if scope != MY_DRIVE and code in DOWNLOAD_ACCESS_ERRORS:
                lose_scope(db, link, scope, error=code)
            else:
                row.last_error_code = code
                if code in CURSOR_ERRORS:
                    row.scan_state = "rescan"
                    row.start_page_token = None
