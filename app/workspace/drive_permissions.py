"""Version and access checks for content held in the Drive index."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import GoogleConnection, User
from app.workspace.drive_storage import DriveLink, lock_drive_link
from app.workspace.models import DriveFileMetadata, DrivePermissionState, DriveSyncCheckpoint
from app.workspace.service import WorkspaceError

FIELDS = "id,version,trashed,capabilities(canDownload)"


def current_link(db, user_id, account_id=None):
    from app.google.account_resolver import GoogleAccountResolver

    conn = GoogleAccountResolver.resolve_for_task(db, user_id, pinned_account_id=account_id)
    if conn is None or conn.status != "connected" or not conn.google_sub:
        return None
    return DriveLink(user_id, conn.google_sub, conn.link_version)


def link_payload(link):
    return {"google_sub": link.google_sub, "link_version": link.link_version} if link else None


def matches_link(db, user_id, expected):
    return isinstance(expected, dict) and expected == link_payload(current_link(db, user_id))


def state_key(link, file_id):
    return (link.user_id, link.google_sub, link.link_version, file_id)


def invalidate(db, link, *, file_id=None, scope=None):
    """Invalidate in the same transaction as Changes API checkpoint/catalog updates."""
    query = select(DrivePermissionState).where(
        DrivePermissionState.user_id == link.user_id,
        DrivePermissionState.google_sub == link.google_sub,
        DrivePermissionState.link_version == link.link_version,
    )
    if file_id is not None:
        query = query.where(DrivePermissionState.file_id == file_id)
    if scope is not None:
        query = query.where(DrivePermissionState.scope == scope)
    for row in db.scalars(query.with_for_update()):
        row.generation += 1
        row.checked_at = None
        row.file_version = None


def _utc(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def delivery_evidence_current(db, user_id, expected, verification):
    link = current_link(db, user_id)
    if link is None or link_payload(link) != expected:
        return False
    now = datetime.now(UTC)
    for ref in verification:
        row = db.get(DrivePermissionState, state_key(link, ref["file_id"]))
        if not row or row.file_version != ref["version"] or row.scope != (ref.get("drive_id") or "my_drive"):
            return False
        if not row.checked_at or now - _utc(row.checked_at) >= timedelta(seconds=60):
            return False
    return True


class DrivePermissions:
    def __init__(self, session_factory, service):
        self.session_factory = session_factory
        self.service = service

    def _snapshot(self, link, file_id, version, scope, fresh):
        with self.session_factory.begin() as db:
            if not lock_drive_link(db, link):
                return None, False
            key = state_key(link, file_id)
            row = db.get(DrivePermissionState, key)
            if row is None:
                row = DrivePermissionState(
                    user_id=link.user_id, google_sub=link.google_sub,
                    link_version=link.link_version, file_id=file_id, generation=0, scope=scope,
                )
                db.add(row)
                return 0, False
            checkpoint = db.get(DriveSyncCheckpoint, (*key[:3], scope))
            now = datetime.now(UTC)
            cached = bool(
                not fresh and row.scope == scope and row.file_version == version and row.checked_at
                and now - _utc(row.checked_at) < timedelta(seconds=60)
                and checkpoint is not None and not checkpoint.last_error_code
                and checkpoint.last_success_at
                and now - _utc(checkpoint.last_success_at) <= timedelta(minutes=10)
            )
            return row.generation, cached

    def check(self, link, file_id, version, scope, *, fresh=False):
        scope = scope or "my_drive"
        generation, cached = self._snapshot(link, file_id, version, scope, fresh)
        if generation is None or cached:
            return cached
        try:
            response = self.service.read(link.user_id, "drive", {
                "method": "files.get",
                "parameters": {"fileId": file_id, "fields": FIELDS, "supportsAllDrives": True},
            }, force_rest=True)["data"]
        except WorkspaceError as exc:
            if exc.code not in {"PERMISSION_DENIED", "NOT_FOUND", "RECONNECT_REQUIRED"}:
                raise
            response = {}
        confirmed_at = datetime.now(UTC)
        observed = response.get("version")
        accessible = (
            response.get("id") == file_id and response.get("trashed") is False
            and response.get("capabilities", {}).get("canDownload") is True
        )
        allowed = accessible and observed is not None and str(observed) == str(version)
        with self.session_factory.begin() as db:
            if not lock_drive_link(db, link):
                return False
            row = db.get(DrivePermissionState, state_key(link, file_id))
            if row is None or row.generation != generation:
                return False
            if not allowed:
                row.generation += 1
            row.scope = scope
            row.file_version = str(version) if allowed else None
            row.checked_at = confirmed_at if allowed else None
            metadata = db.get(DriveFileMetadata, state_key(link, file_id))
            if metadata is not None and metadata.version == version and not allowed:
                if accessible and observed is not None:
                    metadata.version = str(observed)
                    metadata.status = "pending"
                    metadata.status_reason = None
                elif response.get("id") == file_id or not response:
                    db.delete(metadata)
        return allowed

    async def filter(self, link, candidates):
        files = {}
        for candidate in candidates:
            files.setdefault(candidate.file_id, candidate)
        files = list(files.values())[:4]
        outcomes = await asyncio.gather(*(
            asyncio.to_thread(self.check, link, item.file_id, item.file_version, item.drive_id)
            for item in files
        ), return_exceptions=True)
        valid = {(item.file_id, item.file_version) for item, outcome in zip(files, outcomes, strict=True)
                 if outcome is True}
        errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        return [item for item in candidates if (item.file_id, item.file_version) in valid], errors
