from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import create_sqlite_engine
from app.models import Base, GoogleConnection, User
from app.workspace.drive_scanner import DriveScanner
from app.workspace.drive_storage import DriveLink
from app.workspace.models import DriveFileMetadata, DriveSyncCheckpoint
from app.workspace.service import WorkspaceError


@pytest.fixture
def drive_account(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'drive.sqlite'}")
    table_names = {
        "users", "google_connections", "drive_file_metadata", "drive_file_chunks",
        "drive_sync_checkpoints", "drive_permission_states", "drive_scan_candidates",
    }
    Base.metadata.create_all(
        engine, tables=[table for table in Base.metadata.sorted_tables if table.name in table_names],
    )
    sessions = sessionmaker(engine)
    link = DriveLink(1, "sub", 1)
    with sessions.begin() as db:
        db.add(User(id=1, tenant="test", database="test", odoo_uid=1, login="a", name="A"))
        db.add(GoogleConnection(user_id=1, google_sub="sub", email="a@example.com",
                                scopes="", encrypted_access_token="", expires_at=datetime.now(UTC)))
    service = Mock()
    service.google.get_user_google_connection.return_value = SimpleNamespace(
        status="connected", google_sub="sub", link_version=1,
    )
    yield sessions, link, service
    engine.dispose()


def file(file_id, day, *, drive_id=None, version="1"):
    return {"id": file_id, "name": file_id, "mimeType": "text/plain", "version": version,
            "modifiedTime": f"2026-04-{day:02}T10:00:00Z", "driveId": drive_id,
            "capabilities": {"canDownload": True}}


def test_scan_checkpoints_scope_and_resumes_before_publishing_global_cap(drive_account):
    sessions, link, service = drive_account
    service.read.side_effect = [
        {"data": {"drives": [{"id": "shared"}], "nextPageToken": "drives2"}},
        {"data": {"drives": []}},
        {"data": {"startPageToken": "tail-my"}},
        {"data": {"files": [file("a", 7)], "nextPageToken": "files2"}},
        {"data": {"files": [file("b", 6)]}},
        {"data": {"startPageToken": "tail-shared"}},
        {"data": {"files": [file("c", 10, drive_id="shared")], "nextPageToken": "files3"}},
        {"data": {"files": [file("d", 3, drive_id="shared")], "nextPageToken": "not-needed"}},
    ]
    assert not DriveScanner(sessions, service, cap=2).initial_scan(link, max_pages=1)
    with sessions() as db:
        checkpoint = db.get(DriveSyncCheckpoint, (1, "sub", 1, "my_drive"))
        assert (checkpoint.scan_page_token, checkpoint.start_page_token) == ("files2", "tail-my")
        assert db.scalars(select(DriveFileMetadata)).all() == []
    assert DriveScanner(sessions, service, cap=2).initial_scan(link, max_pages=3)
    with sessions() as db:
        files = db.scalars(select(DriveFileMetadata)).all()
        assert {(row.file_id, row.status) for row in files} == {("a", "pending"), ("c", "pending")}
        assert {row.scope: row.start_page_token for row in db.scalars(select(DriveSyncCheckpoint))} == {
            "my_drive": "tail-my", "shared": "tail-shared",
        }
    assert service.read.call_args_list[4].args[2]["parameters"]["pageToken"] == "files2"
    assert service.read.call_args_list[5].args[2]["parameters"]["driveId"] == "shared"


def test_reconcile_replaces_files_absent_from_fresh_listing(drive_account):
    sessions, link, service = drive_account
    service.read.side_effect = [
        {"data": {"drives": []}}, {"data": {"startPageToken": "tail1"}},
        {"data": {"files": [file("gone", 10), file("keep", 7)]}},
        {"data": {"drives": []}}, {"data": {"startPageToken": "tail2"}},
        {"data": {"files": [file("keep", 7), file("older", 1)]}},
    ]
    scanner = DriveScanner(sessions, service, cap=2)
    assert scanner.initial_scan(link)
    with sessions.begin() as db:
        checkpoint = db.get(DriveSyncCheckpoint, (1, "sub", 1, "my_drive"))
        checkpoint.last_full_scan_at = datetime(2026, 4, 1, tzinfo=UTC)
        db.get(DriveFileMetadata, (1, "sub", 1, "keep")).status = "indexed"
    assert scanner.daily_reconcile(link)
    with sessions() as db:
        assert {row.file_id: row.status for row in db.scalars(select(DriveFileMetadata))} == {
            "keep": "indexed", "older": "pending",
        }


def test_incomplete_listing_cannot_publish_or_advance_changes_cursor(drive_account):
    sessions, link, service = drive_account
    service.read.side_effect = [
        {"data": {"drives": []}}, {"data": {"startPageToken": "tail"}},
        {"data": {"files": [file("a", 10)], "incompleteSearch": True}},
    ]
    assert not DriveScanner(sessions, service).initial_scan(link)
    with sessions() as db:
        checkpoint = db.get(DriveSyncCheckpoint, (1, "sub", 1, "my_drive"))
        assert checkpoint.scan_state != "complete"
        assert checkpoint.last_error_code == "INCOMPLETE_SEARCH"
        assert db.scalars(select(DriveFileMetadata)).all() == []


def test_rejected_files_cursor_restarts_snapshot_from_new_start_token(drive_account):
    sessions, link, service = drive_account
    service.read.side_effect = [
        {"data": {"drives": []}}, {"data": {"startPageToken": "tail1"}},
        {"data": {"files": [file("old", 10)], "nextPageToken": "bad"}},
        WorkspaceError("INVALID_REQUEST", "expired cursor"),
        {"data": {"startPageToken": "tail2"}}, {"data": {"files": [file("new", 7)]}},
    ]
    scanner = DriveScanner(sessions, service)
    assert not scanner.initial_scan(link)
    assert scanner.initial_scan(link)
    with sessions() as db:
        assert db.get(DriveSyncCheckpoint, (1, "sub", 1, "my_drive")).start_page_token == "tail2"
        assert [row.file_id for row in db.scalars(select(DriveFileMetadata))] == ["new"]
