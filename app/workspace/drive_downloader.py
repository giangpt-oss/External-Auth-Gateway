"""Versioned Drive reads through the Workspace REST boundary."""
import base64
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.workspace.drive_storage import DriveLink, IndexedDriveFile
from app.workspace.network_slots import request_deadline
from app.workspace.service import MAX_MEDIA_BYTES, WorkspaceError

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF = "application/pdf"
EXPORTS = {
    "application/vnd.google-apps.document": DOCX,
    "application/vnd.google-apps.spreadsheet": XLSX,
    "application/vnd.google-apps.presentation": PPTX,
}
SUFFIX_MIMES = {
    ".docx": DOCX, ".xlsx": XLSX, ".pptx": PPTX, ".pdf": PDF,
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
}
SUPPORTED_MIMES = frozenset((*EXPORTS, *SUFFIX_MIMES.values(), "application/octet-stream"))
SUPPORTED_MIME_QUERY = " or ".join(f"mimeType = '{mime}'" for mime in sorted(SUPPORTED_MIMES))
FILE_FIELDS = "id,name,mimeType,driveId,version,modifiedTime,trashed,capabilities(canDownload)"
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


@dataclass(frozen=True)
class DownloadedDriveFile:
    file: IndexedDriveFile
    parser_mime: str
    content: bytes = field(repr=False)


def check_link(service, link: DriveLink) -> None:
    conn = service.google.get_user_google_connection(link.user_id)
    if (not conn or conn.status != "connected" or conn.google_sub != link.google_sub
            or conn.link_version != link.link_version):
        raise WorkspaceError("STALE_DRIVE_LINK", "The Google account link changed.")


def drive_read(service, link: DriveLink, method: str, **parameters):
    check_link(service, link)
    try:
        result = service.read(link.user_id, "drive", {
            "method": method, "parameters": parameters,
        }, force_rest=True)["data"]
    except TimeoutError as exc:
        deadline = request_deadline.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise WorkspaceError("DEADLINE_EXCEEDED", "Drive request deadline expired.") from exc
        raise WorkspaceError("SERVICE_ERROR", "Drive network capacity is temporarily unavailable.") from exc
    check_link(service, link)
    return result


def parser_mime(mime: str, name: str) -> str | None:
    if mime in EXPORTS:
        return EXPORTS[mime]
    if mime in SUFFIX_MIMES.values():
        if mime == "text/plain" and name.lower().endswith((".md", ".csv")):
            return SUFFIX_MIMES["." + name.lower().rsplit(".", 1)[-1]]
        return mime
    if mime == "application/octet-stream":
        return next((value for suffix, value in SUFFIX_MIMES.items()
                     if name.lower().endswith(suffix)), None)
    return None


def observe_file(row: dict) -> IndexedDriveFile | None:
    file_id, name, mime = row.get("id", ""), row.get("name", ""), row.get("mimeType", "")
    version = str(row.get("version") or "")
    if (not FILE_ID.fullmatch(file_id) or not name or not parser_mime(mime, name)
            or row.get("trashed") or not version.isascii() or not version.isdecimal()
            or len(version) > 64):
        return None
    modified_time = row.get("modifiedTime")
    if modified_time:
        try:
            modified_time = datetime.fromisoformat(modified_time).astimezone(UTC)
        except (TypeError, ValueError):
            modified_time = None
    return IndexedDriveFile(file_id, name, mime, version,
                            modified_time=modified_time, drive_id=row.get("driveId"))


def verify_file_version(service, link: DriveLink, file_id: str,
                        expected_version: str | None = None) -> IndexedDriveFile:
    if not FILE_ID.fullmatch(file_id):
        raise WorkspaceError("INVALID_REQUEST", "Invalid Drive file ID.")
    row = drive_read(service, link, "files.get", fileId=file_id, fields=FILE_FIELDS,
                     supportsAllDrives=True)
    if row.get("id") != file_id or row.get("trashed"):
        raise WorkspaceError("NOT_FOUND", "Drive file is unavailable.")
    if row.get("capabilities", {}).get("canDownload") is not True:
        raise WorkspaceError("PERMISSION_DENIED", "Drive download is unavailable.")
    file = observe_file(row)
    if file is None:
        raise WorkspaceError("UNSUPPORTED_FILE", "Unsupported Drive file or missing version.")
    if expected_version is not None and file.version != expected_version:
        raise WorkspaceError("FILE_VERSION_CHANGED", "Drive file version changed.")
    return file


def download_file(service, link: DriveLink, file_id: str, *,
                  expected_version: str | None = None) -> DownloadedDriveFile:
    file = verify_file_version(service, link, file_id, expected_version)
    target = parser_mime(file.mime_type, file.name)
    if file.mime_type in EXPORTS:
        response = drive_read(service, link, "files.export", fileId=file_id, mimeType=target)
    else:
        response = drive_read(service, link, "files.get", fileId=file_id, alt="media",
                              supportsAllDrives=True)
    encoded = response.get("content_base64", "")
    if len(encoded) > ((MAX_MEDIA_BYTES + 2) // 3) * 4:
        raise WorkspaceError("RESPONSE_TOO_LARGE", "Drive file exceeds 8 MiB.")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise WorkspaceError("SERVICE_ERROR", "Invalid Drive media response.") from exc
    if len(content) > MAX_MEDIA_BYTES:
        raise WorkspaceError("RESPONSE_TOO_LARGE", "Drive file exceeds 8 MiB.")
    return DownloadedDriveFile(file, target, content)
