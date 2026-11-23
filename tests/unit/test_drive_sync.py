from datetime import UTC, datetime

from sqlalchemy import select
from test_drive_scanner import drive_account as _drive_account
from test_drive_scanner import file

from app.workspace.drive_scanner import DriveScanner
from app.workspace.drive_storage import DriveStorage, IndexedDriveChunk, IndexedDriveFile
from app.workspace.drive_sync import DriveSync
from app.workspace.models import (
    DriveFileChunk,
    DriveFileMetadata,
    DrivePermissionState,
    DriveSyncCheckpoint,
)
from app.workspace.service import WorkspaceError

drive_account = _drive_account


def test_changes_atomically_removes_file_and_advances_next_then_start_cursor(drive_account):
    sessions, link, service = drive_account
    service.read.side_effect = [
        {"data": {"drives": []}}, {"data": {"startPageToken": "initial"}},
        {"data": {"files": [file("a", 7), file("b", 6)]}},
        {"data": {"changes": [{"fileId": "c", "file": file("c", 8)}],
                  "nextPageToken": "second"}},
        {"data": {"changes": [{"fileId": "a", "removed": True}],
                  "newStartPageToken": "new"}},
    ]
    assert DriveScanner(sessions, service, cap=2).initial_scan(link)
    with sessions.begin() as db:
        db.add(DrivePermissionState(user_id=1, google_sub="sub", link_version=1,
                                    file_id="a", scope="my_drive", checked_at=datetime.now(UTC)))
    sync = DriveSync(sessions, service, cap=2)
    assert not sync.incremental_sync(link, "my_drive", max_pages=1)
    with sessions() as db:
        checkpoint = db.get(DriveSyncCheckpoint, (1, "sub", 1, "my_drive"))
        assert checkpoint.start_page_token == "second"
        assert checkpoint.last_success_at is None
        assert {row.file_id for row in db.scalars(select(DriveFileMetadata))} == {"a", "c"}
    assert sync.incremental_sync(link, "my_drive")
    with sessions() as db:
        assert {row.file_id for row in db.scalars(select(DriveFileMetadata))} == {"c"}
        permission = db.get(DrivePermissionState, (1, "sub", 1, "a"))
        assert permission.generation == 1 and permission.checked_at is None
        checkpoint = db.get(DriveSyncCheckpoint, (1, "sub", 1, "my_drive"))
        assert checkpoint.last_success_at is not None and checkpoint.start_page_token == "new"


def test_lost_shared_drive_drops_chunks_and_invalidates_permission(drive_account, fernet_key):
    sessions, link, service = drive_account
    service.read.side_effect = [
        {"data": {"drives": [{"id": "shared"}]}}, {"data": {"startPageToken": "tail1"}},
        {"data": {"files": [file("my", 7)]}}, {"data": {"startPageToken": "tail2"}},
        {"data": {"files": [file("shared-file", 8, drive_id="shared")]}},
        WorkspaceError("PERMISSION_DENIED", "shared drive removed"),
    ]
    assert DriveScanner(sessions, service).initial_scan(link)
    source = IndexedDriveFile("shared-file", "Shared file", "text/plain", "1", drive_id="shared")
    storage = DriveStorage(sessions, fernet_key)
    assert storage.replace_chunks(link, source, [IndexedDriveChunk("Restricted text", None)],
                                  require_metadata=True)
    with sessions.begin() as db:
        db.add(DrivePermissionState(user_id=1, google_sub="sub", link_version=1,
                                    file_id="shared-file", scope="shared", checked_at=datetime.now(UTC)))
    assert not DriveSync(sessions, service).incremental_sync(link, "shared")
    with sessions() as db:
        assert [row.file_id for row in db.scalars(select(DriveFileMetadata))] == ["my"]
        assert db.scalars(select(DriveFileChunk)).all() == []
        assert db.get(DrivePermissionState, (1, "sub", 1, "shared-file")).checked_at is None
        checkpoint = db.get(DriveSyncCheckpoint, (1, "sub", 1, "shared"))
        assert (checkpoint.scan_state, checkpoint.last_error_code) == ("lost", "PERMISSION_DENIED")
    assert not storage.replace_chunks(link, source, [IndexedDriveChunk("Restricted text", None)],
                                      require_metadata=True)
