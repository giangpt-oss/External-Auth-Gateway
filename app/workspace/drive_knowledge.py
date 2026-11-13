"""Read a bounded set of Drive documents with the requesting user's OAuth grant."""
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from app.workspace.service import WorkspaceError

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORTS = {"application/vnd.google-apps.document": (DOCX, ".docx"),
           "application/vnd.google-apps.spreadsheet": (XLSX, ".xlsx")}
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
SUFFIXES = (".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md")
FIELDS = "id,name,mimeType,trashed,capabilities(canDownload)"


def drive_documents(service, user_id, terms, file_id=None):
    def read(method, **parameters):
        return service.read(user_id, "drive", {"method": method, "parameters": parameters})["data"]

    if file_id:
        rows = [read("files.get", fileId=file_id, fields=FIELDS, supportsAllDrives=True)]
    else:
        query = "trashed = false"
        supported = [f"mimeType = '{mime}'" for mime in (*EXPORTS, DOCX, XLSX, "application/pdf", "text/plain", "text/csv", "text/markdown", "application/octet-stream")]
        query += " and (" + " or ".join(supported) + ")"
        escaped = [term.replace("\\", "\\\\").replace("'", "\\'") for term in terms]
        options = {"fields": f"nextPageToken,incompleteSearch,files({FIELDS})", "pageSize": 30,
                   "includeItemsFromAllDrives": True, "supportsAllDrives": True}
        match = " or ".join(f"fullText contains '{term}'" for term in escaped)
        response = (read("files.list", q=f"{query} and ({match})", **options) if match
                    else read("files.list", q=query, orderBy="modifiedTime desc", **options))
        rows = response.get("files", [])
        if not rows and match:
            rows = read("files.list", q=query, orderBy="modifiedTime desc", **options).get("files", [])

    def download(row):
        key = row.get("id", "")
        name = row.get("name", "")
        mime = row.get("mimeType")
        if not FILE_ID.fullmatch(key) or row.get("trashed") or row.get("capabilities", {}).get("canDownload") is not True:
            return None
        if mime in EXPORTS:
            target, suffix = EXPORTS[mime]
            content = read("files.export", fileId=key, mimeType=target)
        else:
            suffix = next((ext for ext in SUFFIXES if name.lower().endswith(ext)), None)
            if not suffix:
                return None
            content = read("files.get", fileId=key, alt="media", supportsAllDrives=True)
        return {"id": key, "name": name, "suffix": suffix, "content_base64": content["content_base64"]}

    supported = [row for row in rows if row.get("mimeType") in EXPORTS
                 or row.get("name", "").lower().endswith(SUFFIXES)]
    candidates = [row for row in supported if row.get("capabilities", {}).get("canDownload") is True][:4]
    files, skipped = [], 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in [pool.submit(copy_context().run, download, row) for row in candidates]:
            try:
                result = future.result()
            except WorkspaceError as exc:
                if exc.code not in {"PERMISSION_DENIED", "NOT_FOUND", "RESPONSE_TOO_LARGE"}:
                    raise
                result = None
            if result is None:
                skipped += 1
            else:
                files.append(result)
    return {"status": "OK", "files": files, "skipped": skipped + int(bool(rows) and not candidates)}
