"""Maintain the permanent Drive selection and its scan candidates."""
from datetime import UTC

from sqlalchemy import delete, select

from app.workspace.drive_permissions import invalidate
from app.workspace.drive_storage import DriveLink, IndexedDriveFile, link_predicates
from app.workspace.models import DriveFileChunk, DriveFileMetadata, DriveScanCandidate

DRIVE_FILE_CAP = 3000
MY_DRIVE = "my_drive"
DOWNLOAD_ACCESS_ERRORS = {"PERMISSION_DENIED", "NOT_FOUND"}


def aware(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def link_values(link: DriveLink):
    return {"user_id": link.user_id, "google_sub": link.google_sub, "link_version": link.link_version}


def file_key(link: DriveLink, file_id: str):
    return link.user_id, link.google_sub, link.link_version, file_id


def _ordered(model, link):
    return select(model).where(*link_predicates(model, link)).order_by(
        model.modified_time.desc().nulls_last(), model.file_id.asc(),
    )


def enforce_cap(db, link: DriveLink, *, cap=DRIVE_FILE_CAP, model=DriveFileMetadata):
    """Trim below the cutoff while the caller holds the linked account's transaction lock."""
    for row in db.scalars(_ordered(model, link).offset(cap)):
        if model is DriveFileMetadata:
            invalidate(db, link, file_id=row.file_id)
        db.delete(row)


def scan_cutoff(db, link, cap):
    row = db.scalar(_ordered(DriveScanCandidate, link).offset(cap - 1).limit(1))
    return aware(row.modified_time) if row else None


def store_file(db, link: DriveLink, file: IndexedDriveFile, *, model=DriveFileMetadata):
    row = db.get(model, file_key(link, file.file_id))
    if row is None:
        row = model(**link_values(link), file_id=file.file_id)
        db.add(row)
    elif row.version.isascii() and row.version.isdecimal() and int(row.version) > int(file.version):
        return
    elif model is DriveFileMetadata and row.version != file.version:
        invalidate(db, link, file_id=file.file_id)
        db.execute(delete(DriveFileChunk).where(
            *link_predicates(DriveFileChunk, link), DriveFileChunk.file_id == file.file_id,
        ))
        row.status = "pending"
        row.status_reason = None
        row.indexed_at = None
    elif model is DriveFileMetadata and row.status_reason in DOWNLOAD_ACCESS_ERRORS:
        row.status = "pending"
        row.status_reason = None
    row.name = file.name
    row.mime_type = file.mime_type
    row.drive_id = file.drive_id
    row.version = file.version
    row.modified_time = file.modified_time


def publish_scan(db, link: DriveLink):
    """Reconcile metadata only when all scope snapshots have finished."""
    candidates = {row.file_id: row for row in db.scalars(
        select(DriveScanCandidate).where(*link_predicates(DriveScanCandidate, link)),
    )}
    for row in db.scalars(select(DriveFileMetadata).where(*link_predicates(DriveFileMetadata, link))):
        if row.file_id not in candidates:
            invalidate(db, link, file_id=row.file_id)
            db.delete(row)
    for row in candidates.values():
        store_file(db, link, IndexedDriveFile(
            row.file_id, row.name, row.mime_type, row.version, row.modified_time, row.drive_id,
        ))
        db.delete(row)


