"""Resumable discovery and reconciliation of My Drive and Shared Drives."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select

from app.workspace.drive_cap import (
    DOWNLOAD_ACCESS_ERRORS,
    DRIVE_FILE_CAP,
    MY_DRIVE,
    aware,
    enforce_cap,
    file_key,
    link_values,
    publish_scan,
    scan_cutoff,
    store_file,
)
from app.workspace.drive_downloader import (
    FILE_FIELDS,
    SUPPORTED_MIME_QUERY,
    drive_read,
    observe_file,
)
from app.workspace.drive_permissions import invalidate
from app.workspace.drive_storage import DriveLink, link_predicates, lock_drive_link
from app.workspace.models import DriveFileMetadata, DriveScanCandidate, DriveSyncCheckpoint
from app.workspace.service import WorkspaceError

SCANNING_STATES = {"scan_pending", "scanning", "scan_ready"}
CURSOR_ERRORS = {"INVALID_CURSOR", "INVALID_REQUEST"}


def _scopes(db, link):
    return list(db.scalars(select(DriveSyncCheckpoint).where(*link_predicates(DriveSyncCheckpoint, link))))


def _clear_candidates(db, link):
    db.execute(delete(DriveScanCandidate).where(*link_predicates(DriveScanCandidate, link)))


def _reset_scan(db, link, checkpoints):
    _clear_candidates(db, link)
    scan_id = str(uuid4())
    for row in checkpoints:
        if row.scan_state == "lost":
            continue
        row.scan_id = scan_id
        row.scan_state = "scan_pending"
        row.scan_page_token = row.start_page_token = None
        row.last_success_at = None


def lose_scope(db, link, scope, *, error="PERMISSION_DENIED"):
    if scope == MY_DRIVE:
        raise ValueError("My Drive cannot be removed as a Shared Drive.")
    row = db.get(DriveSyncCheckpoint, file_key(link, scope))
    if row is None:
        row = DriveSyncCheckpoint(**link_values(link), scope=scope)
        db.add(row)
    row.scan_state = "lost"
    row.last_error_code = error
    row.last_success_at = None
    row.scan_page_token = row.start_page_token = None
    invalidate(db, link, scope=scope)
    db.execute(delete(DriveFileMetadata).where(
        *link_predicates(DriveFileMetadata, link), DriveFileMetadata.drive_id == scope,
    ))
    checkpoints = _scopes(db, link)
    if any(checkpoint.scan_state in SCANNING_STATES for checkpoint in checkpoints):
        # A missing drive may have displaced older candidates from surviving scopes.
        _reset_scan(db, link, checkpoints)


class DriveScanner:
    def __init__(self, session_factory, service, *, cap=DRIVE_FILE_CAP):
        if not 1 <= cap <= DRIVE_FILE_CAP:
            raise ValueError("Invalid Drive file cap.")
        self.sessions = session_factory
        self.service = service
        self.cap = cap

    def initial_scan(self, link: DriveLink, *, max_pages=20) -> bool:
        return self._scan(link, max_pages, daily=False, force=False)

    def daily_reconcile(self, link: DriveLink, *, max_pages=20, force=False) -> bool:
        return self._scan(link, max_pages, daily=True, force=force)

    def _discover(self, link):
        scopes, token = {MY_DRIVE}, None
        seen_tokens = set()
        while True:
            params = {"fields": "nextPageToken,drives(id)", "pageSize": 100}
            if token:
                params["pageToken"] = token
            response = drive_read(self.service, link, "drives.list", **params)
            scopes.update(row["id"] for row in response.get("drives", []) if row.get("id"))
            token = response.get("nextPageToken")
            if not token:
                return scopes
            if token in seen_tokens:
                raise WorkspaceError("INVALID_CURSOR", "Shared Drive pagination returned a repeated token.")
            seen_tokens.add(token)

    @staticmethod
    def _due(checkpoints, daily, force):
        active = [row for row in checkpoints if row.scan_state != "lost"]
        if not active or any(row.scan_state != "complete" for row in active):
            return True
        if daily and any(row.last_full_scan_at is None for row in active):
            return True
        oldest = min((aware(row.last_full_scan_at) for row in active if row.last_full_scan_at),
                     default=None)
        return force or bool(daily and (
            oldest is None or datetime.now(UTC) - oldest >= timedelta(days=1)
        ))

    def _prepare(self, link, daily, force):
        with self.sessions() as db:
            checkpoints = _scopes(db, link)
            if any(row.scan_state in SCANNING_STATES for row in checkpoints):
                return True
            if not self._due(checkpoints, daily, force):
                return False
            before = {row.scope: (row.scan_id, row.scan_state) for row in checkpoints}
        try:
            scopes = self._discover(link)
        except WorkspaceError as exc:
            self._record_discovery_error(link, exc.code)
            raise
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return False
            checkpoints = _scopes(db, link)
            if any(row.scan_state in SCANNING_STATES for row in checkpoints):
                return True
            if not self._due(checkpoints, daily, force):
                return False
            if before != {row.scope: (row.scan_id, row.scan_state) for row in checkpoints}:
                return False
            known = {row.scope: row for row in checkpoints}
            for scope in known.keys() - scopes:
                if known[scope].scan_state != "lost":
                    lose_scope(db, link, scope)
            for scope in scopes:
                if scope not in known:
                    db.add(DriveSyncCheckpoint(**link_values(link), scope=scope))
                elif known[scope].scan_state == "lost":
                    known[scope].scan_state = "pending"
            _reset_scan(db, link, _scopes(db, link))
        return True

    def _record_discovery_error(self, link, code):
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return
            checkpoints = _scopes(db, link)
            if not checkpoints:
                db.add(DriveSyncCheckpoint(**link_values(link), scope=MY_DRIVE, last_error_code=code))
            for row in checkpoints:
                if row.scan_state != "lost":
                    row.last_error_code = code

    def _next_page(self, link):
        with self.sessions() as db:
            return db.execute(select(
                DriveSyncCheckpoint.scope,
                DriveSyncCheckpoint.scan_id,
                DriveSyncCheckpoint.scan_state,
                DriveSyncCheckpoint.scan_page_token,
            ).where(
                *link_predicates(DriveSyncCheckpoint, link),
                DriveSyncCheckpoint.scan_state.in_(("scan_pending", "scanning")),
            ).order_by(
                (DriveSyncCheckpoint.scan_state == "scanning").desc(),
                (DriveSyncCheckpoint.scope == MY_DRIVE).desc(),
                DriveSyncCheckpoint.scope,
            ).limit(1)).first()

    @staticmethod
    def _matches(row, cursor):
        return row and (row.scope, row.scan_id, row.scan_state, row.scan_page_token) == tuple(cursor)

    def _scan(self, link, max_pages, *, daily, force):
        if not self._prepare(link, daily, force):
            return True
        for _ in range(max_pages):
            cursor = self._next_page(link)
            if cursor is None:
                break
            if cursor.scan_state == "scan_pending":
                if not self._capture_start(link, cursor):
                    return False
                cursor = self._next_page(link)
                if cursor is None or cursor.scan_state == "scan_pending":
                    return False
            if not self._list_page(link, cursor):
                return False
        return self._publish(link)

    def _capture_start(self, link, cursor):
        params = {"supportsAllDrives": True}
        if cursor.scope != MY_DRIVE:
            params["driveId"] = cursor.scope
        try:
            token = drive_read(self.service, link, "changes.getStartPageToken", **params).get("startPageToken")
            if not token:
                raise WorkspaceError("SERVICE_ERROR", "Google Drive did not return a Changes token.")
        except WorkspaceError as exc:
            return self._fail(link, cursor, exc.code)
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return False
            row = db.get(DriveSyncCheckpoint, file_key(link, cursor.scope))
            if self._matches(row, cursor):
                row.start_page_token = token
                row.scan_state = "scanning"
        return True

    def _list_page(self, link, cursor):
        shared = cursor.scope != MY_DRIVE
        params = {
            "corpora": "drive" if shared else "user",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": shared,
            "orderBy": "modifiedTime desc",
            "spaces": "drive",
            "pageSize": 100,
            "q": f"trashed = false and ({SUPPORTED_MIME_QUERY})",
            "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
        }
        if shared:
            params["driveId"] = cursor.scope
        if cursor.scan_page_token:
            params["pageToken"] = cursor.scan_page_token
        try:
            response = drive_read(self.service, link, "files.list", **params)
        except WorkspaceError as exc:
            return self._fail(link, cursor, exc.code)
        if response.get("incompleteSearch"):
            return self._fail(link, cursor, "INCOMPLETE_SEARCH")
        files = [file for row in response.get("files", [])
                 if row.get("capabilities", {}).get("canDownload") is True
                 and (file := observe_file(row)) is not None
                 and (file.drive_id or MY_DRIVE) == cursor.scope]
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return False
            row = db.get(DriveSyncCheckpoint, file_key(link, cursor.scope))
            if not self._matches(row, cursor):
                return True
            for file in files:
                store_file(db, link, file, model=DriveScanCandidate)
            enforce_cap(db, link, cap=self.cap, model=DriveScanCandidate)
            cutoff = scan_cutoff(db, link, self.cap)
            # Files are sorted by modified time, but ties can span pages.
            stop = bool(cutoff and files and files[-1].modified_time
                        and files[-1].modified_time < cutoff)
            row.scan_page_token = None if stop else response.get("nextPageToken")
            if not row.scan_page_token:
                row.scan_state = "scan_ready"
        return True

    def _fail(self, link, cursor, code):
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return False
            row = db.get(DriveSyncCheckpoint, file_key(link, cursor.scope))
            if not self._matches(row, cursor):
                return False
            if cursor.scope != MY_DRIVE and code in DOWNLOAD_ACCESS_ERRORS:
                lose_scope(db, link, cursor.scope, error=code)
            else:
                row.last_error_code = code
                if code == "INCOMPLETE_SEARCH" or (cursor.scan_page_token and code in CURSOR_ERRORS):
                    _reset_scan(db, link, _scopes(db, link))
        return False

    def _publish(self, link):
        with self.sessions.begin() as db:
            if not lock_drive_link(db, link):
                return False
            checkpoints = [row for row in _scopes(db, link) if row.scan_state != "lost"]
            if not checkpoints or any(row.scan_state not in {"scan_ready", "complete"} for row in checkpoints):
                return False
            if any(row.scan_state == "scan_ready" for row in checkpoints):
                publish_scan(db, link)
                for row in checkpoints:
                    row.scan_state = "complete"
                    row.last_full_scan_at = datetime.now(UTC)
                    row.last_error_code = None
        return True
