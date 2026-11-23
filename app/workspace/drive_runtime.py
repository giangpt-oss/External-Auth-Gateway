"""Coordinate indexed Drive reads with the dedicated parsing worker."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import aliased

from app.models import GoogleAccountSelection, GoogleConnection, TelegramUpdate, User
from app.security import decrypt_secret, encrypt_secret
from app.workspace.activity_tracker import ActivityType
from app.workspace.drive_permissions import (
    DrivePermissions,
    current_link,
    link_payload,
    matches_link,
)
from app.workspace.drive_retriever import DriveRetriever
from app.workspace.drive_storage import DriveLink, DriveStorage, lock_drive_link
from app.workspace.models import (
    DriveFileMetadata,
    DrivePermissionState,
    DriveSyncCheckpoint,
    WorkspaceJob,
)
from app.workspace.network_slots import request_deadline
from app.workspace.selection_manager import SelectionManager, SwitchLockError, current_account_pin
from app.workspace.service import WorkspaceError
from app.workspace.telemetry import measure_stage

FORMAT = "indexed_drive_v1"
PREVIEW = "index.preview"
RESYNC = "index.resync"
BACKGROUND_OPERATIONS = {PREVIEW, RESYNC, "index.fetch"}


def response(status, link=None, chunks=None, *, partial=False, verification=()):
    chunks = chunks or []
    files = {chunk["file_id"]: chunk["name"] for chunk in chunks}
    return {
        "format": FORMAT, "status": status, "link": link_payload(link),
        "chunks": chunks, "partial": partial,
        "verification": [ref for ref in verification if ref["file_id"] in files],
        "sources": [
            {"file_id": file_id, "name": name, "url": f"https://drive.google.com/open?id={file_id}"}
            for file_id, name in files.items()
        ],
    }


def safe_result(db, user_id, result):
    if isinstance(result, dict) and result.get("format") == "workspace_plan_v1":
        if not matches_link(db, user_id, result.get("link")):
            return {"format": "workspace_plan_v1", "status": "link_changed"}
    if isinstance(result, dict) and result.get("format") == FORMAT and result.get("link"):
        if not matches_link(db, user_id, result["link"]):
            return response("link_changed")
    return result


class DriveRuntime:
    def __init__(self, settings, session_factory, workspace, http_client):
        self.settings = settings
        self.db = session_factory
        self.workspace = workspace
        self.http_client = http_client
        self.key = settings.ENCRYPTION_KEY
        self.storage = DriveStorage(session_factory, self.key)
        self.retriever = DriveRetriever(session_factory)
        self.permissions = DrivePermissions(session_factory, workspace)
        self.parse_pool = None
        self.retry_at = {}

    def _link(self, user_id):
        with self.db() as db:
            return current_link(db, user_id)

    def _coverage(self, link):
        with self.db() as db:
            key = (link.user_id, link.google_sub, link.link_version)
            checkpoints = list(db.scalars(select(DriveSyncCheckpoint).where(
                DriveSyncCheckpoint.user_id == key[0], DriveSyncCheckpoint.google_sub == key[1],
                DriveSyncCheckpoint.link_version == key[2],
            )))
            rows = db.query(DriveFileMetadata.status, DriveFileMetadata.status_reason).filter_by(
                user_id=key[0], google_sub=key[1], link_version=key[2],
            ).all()
            counts = {}
            reasons = {}
            for status, reason in rows:
                counts[status] = counts.get(status, 0) + 1
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
            last_success = max((row.last_success_at for row in checkpoints if row.last_success_at), default=None)
            return {
                "counts": counts, "reasons": reasons, "max_files": 3000,
                "last_success_at": last_success.isoformat() if last_success else None,
                "complete": bool(checkpoints) and all(
                    row.scan_state == "complete" and not row.last_error_code for row in checkpoints
                ) and not counts.get("pending", 0),
            }

    def sync_status(self, user_id):
        link = self._link(user_id)
        if link is None:
            return {"status": "connection_error", "max_files": 3000}
        return {"status": "ok", **self._coverage(link), "scope": "latest_supported_files"}

    async def _embed(self, texts):
        if not self.settings.EMBEDDING_API_KEY:
            return [None] * len(texts)
        from app.workspace.drive_embedding import DriveEmbedder

        deadline = request_deadline.get()
        timeout = max(0.001, deadline - time.monotonic()) if deadline else 30
        return await DriveEmbedder(
            self.http_client, self.settings.EMBEDDING_API_KEY, self.settings.EMBEDDING_MODEL,
        ).embed(texts, timeout=timeout)

    async def search(self, user_id, query, *, file_id=None, terms=(), expected=None):
        try:
            with SelectionManager.admit_activity(self.db, user_id):
                return await self._search(user_id, query, file_id=file_id, terms=terms, expected=expected)
        except SwitchLockError as exc:
            return response("link_changed" if exc.code == "ACCOUNT_CHANGED" else "connection_error")

    async def _search(self, user_id, query, *, file_id, terms, expected):
        link = await asyncio.to_thread(self._link, user_id)
        if expected is not None and expected != link_payload(link):
            return response("link_changed")
        if link is None:
            return response("connection_error")
        from app.workspace.drive_embedding import DriveEmbeddingError

        try:
            with measure_stage("embedding_query_ms"):
                (vector,) = await self._embed([query])
        except DriveEmbeddingError:
            vector = None
        with measure_stage("internal_retrieval_ms"):
            candidates = await asyncio.to_thread(
                self.retriever.search, link, query, vector, file_id=file_id,
                embedding_model=self.settings.EMBEDDING_MODEL, terms=terms,
            )
        with measure_stage("drive_permission_check_ms"):
            allowed, errors = await self.permissions.filter(link, candidates)
        with measure_stage("drive_content_decrypt_ms"):
            content = await asyncio.to_thread(self.storage.decrypt_candidates, link, allowed, require_permission=True)
        chunks = [{
            "file_id": item.file_id, "name": item.name, "page_or_section": item.page_or_section,
            "content": content[item.chunk_id], "score": item.score,
        } for item in allowed if item.chunk_id in content]
        coverage = await asyncio.to_thread(self._coverage, link)
        partial = not coverage["complete"] or len(allowed) < len(candidates) or bool(errors)
        verification = {
            item.file_id: {"file_id": item.file_id, "version": item.file_version, "drive_id": item.drive_id}
            for item in allowed
        }
        if not chunks and self.key:
            chunks, fallback_partial, refs = await self._fallback(link, query, terms, file_id)
            partial = partial or fallback_partial
            verification = {ref["file_id"]: ref for ref in refs}
        if await asyncio.to_thread(self._link, user_id) != link:
            return response("link_changed")
        status = ("ok" if chunks else "connection_error" if errors else
                  "not_indexed" if not coverage["counts"].get("indexed") else "no_evidence")
        return response(status, link, chunks, partial=partial, verification=verification.values())

    def enqueue(self, link, operation, body, *, deadline_seconds=120):
        job_id = "job_" + uuid4().hex
        encrypted = encrypt_secret(json.dumps({
            "action": "index", "body": body, "drive_link": link_payload(link),
        }), self.key)
        with self.db.begin() as db:
            try:
                pin = current_account_pin(link.user_id) or SelectionManager.admit(db, link.user_id)
            except SwitchLockError:
                return None
            if current_link(db, link.user_id, pin.account_id) != link or not lock_drive_link(db, link):
                return None
            db.add(WorkspaceJob(
                job_id=job_id, user_id=link.user_id, request_id=job_id, action="index",
                service="drive", operation=operation, status="queued", payload_encrypted=encrypted,
                google_account_id=pin.account_id, selection_version=pin.selection_version,
                deadline_at=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
            ))
        return job_id

    def _preview_result(self, link, job_id):
        with self.db() as db:
            job = db.query(WorkspaceJob).filter_by(job_id=job_id, user_id=link.user_id).first()
            if not job or job.status in {"queued", "running"}:
                return None
            if job.status != "completed" or not job.result_encrypted:
                return response("connection_error")
            return safe_result(db, link.user_id, json.loads(decrypt_secret(job.result_encrypted, self.key)))

    async def _fallback(self, link, query, terms, file_id):
        limit = 85 if request_deadline.get() else 24
        deadline = min(request_deadline.get() or float("inf"), time.monotonic() + limit)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], True, []
        job_id = await asyncio.to_thread(
            self.enqueue, link, PREVIEW, {"query": query, "terms": list(terms), "file_id": file_id},
            deadline_seconds=remaining,
        )
        while job_id and time.monotonic() < deadline:
            result = await asyncio.to_thread(self._preview_result, link, job_id)
            if result is not None:
                return (result.get("chunks", []),
                        result.get("partial", False) or result["status"] == "connection_error",
                        result.get("verification", []))
            await asyncio.sleep(0.1)
        return [], True, []

    def _pool(self):
        if self.parse_pool is None:
            from app.workspace.drive_parse_pool import ParsePool
            self.parse_pool = ParsePool()
        return self.parse_pool

    async def close(self):
        if self.parse_pool is not None:
            await self.parse_pool.close()

    def cleanup(self):
        from app.workspace.models import DriveScanCandidate

        cutoff = datetime.now(UTC) - timedelta(hours=24)
        with self.db.begin() as db:
            for model in (DrivePermissionState, DriveScanCandidate, DriveFileMetadata, DriveSyncCheckpoint):
                active = select(GoogleConnection.user_id).join(User, User.id == GoogleConnection.user_id).where(
                    GoogleConnection.user_id == model.user_id,
                    GoogleConnection.google_sub == model.google_sub,
                    GoogleConnection.link_version == model.link_version,
                    GoogleConnection.status == "connected", User.active.is_(True),
                ).exists()
                db.execute(delete(model).where(~active))
            expired = db.query(WorkspaceJob).filter(
                WorkspaceJob.service == "drive", WorkspaceJob.operation.in_(BACKGROUND_OPERATIONS | {"knowledge.search"}),
                WorkspaceJob.created_at <= cutoff, WorkspaceJob.payload_encrypted != "",
            ).order_by(WorkspaceJob.id).limit(100).with_for_update(skip_locked=True).all()
            for job in expired:
                job.payload_encrypted = ""
                job.result_encrypted = None
                job.callback_retry_at = None
                if job.status in {"queued", "running"}:
                    job.status = "expired"
            expired_delivery = db.query(TelegramUpdate).filter(
                TelegramUpdate.created_at <= cutoff, TelegramUpdate.ai_response.like("drive1:%"),
                TelegramUpdate.delivery_status != "sending",
            ).order_by(TelegramUpdate.id).limit(100).with_for_update(skip_locked=True).all()
            for update in expired_delivery:
                update.ai_response = None
                update.delivery_chunks = None
                if update.status == "ai_received":
                    update.status = "failed"
                    update.delivery_status = "failed"

    async def index_file(self, link, file_id):
        from app.workspace.drive_downloader import download_file, verify_file_version
        from app.workspace.drive_storage import IndexedDriveChunk
        from app.workspace.parsers import PARSER_VERSION

        item = await asyncio.to_thread(download_file, self.workspace, link, file_id)
        if await asyncio.to_thread(self._link, link.user_id) != link:
            return
        model = self.settings.EMBEDDING_MODEL if self.settings.EMBEDDING_API_KEY else None
        if await asyncio.to_thread(
            self.storage.is_indexed, link, item.file, embedding_model=model, parser_version=PARSER_VERSION,
        ):
            return
        parsed = await self._pool().parse(
            item.parser_mime, item.content, deadline=request_deadline.get(),
        )
        if await asyncio.to_thread(self._link, link.user_id) != link:
            return
        await asyncio.to_thread(
            verify_file_version, self.workspace, link, file_id, item.file.version,
        )
        if not parsed:
            await asyncio.to_thread(
                self._set_file_error, link, file_id, item.file.version,
                WorkspaceError("NO_TEXT", "Drive file has no extractable text."),
            )
            return
        vectors = await self._embed([part.content for part in parsed])
        chunks = [
            IndexedDriveChunk(part.content, vector, part.page_or_section, part.normalized)
            for part, vector in zip(parsed, vectors, strict=True)
        ]
        await asyncio.to_thread(
            verify_file_version, self.workspace, link, file_id, item.file.version,
        )
        await asyncio.to_thread(
            self.storage.replace_chunks, link, item.file, chunks,
            require_metadata=True, embedding_model=model, parser_version=PARSER_VERSION,
        )

    def _next_index_input(self, link):
        from app.workspace.parsers import PARSER_VERSION

        with self.db() as db:
            filters = {"user_id": link.user_id, "google_sub": link.google_sub, "link_version": link.link_version}
            checkpoint = db.query(DriveSyncCheckpoint).filter_by(**filters).all()
            model = self.settings.EMBEDDING_MODEL if self.settings.EMBEDDING_API_KEY else None
            pending = db.query(DriveFileMetadata.file_id, DriveFileMetadata.version).filter_by(
                **filters,
            ).filter(
                (DriveFileMetadata.status == "pending") |
                ((DriveFileMetadata.status == "indexed") &
                 (DriveFileMetadata.parser_version.is_distinct_from(PARSER_VERSION) |
                  DriveFileMetadata.embedding_model.is_distinct_from(model))),
            ).order_by(DriveFileMetadata.modified_time.desc(), DriveFileMetadata.file_id).limit(30).all()
            now = datetime.now(UTC).replace(tzinfo=None)
            start_scan = not checkpoint or any(row.scan_state not in {"complete", "lost"} for row in checkpoint)
            daily = any(
                row.scan_state == "complete" and (
                    not row.last_full_scan_at or now - row.last_full_scan_at.replace(tzinfo=None) >= timedelta(days=1)
                ) for row in checkpoint
            )
            changes = [
                row.scope for row in checkpoint if row.scan_state == "complete"
                and (
                    not row.last_sync_at
                    or now - row.last_sync_at.replace(tzinfo=None) >= timedelta(minutes=5)
                    or (not row.last_error_code and
                        (not row.last_success_at or row.last_sync_at > row.last_success_at))
                )
            ]
            return start_scan, daily, changes, pending

    def _active_links(self):
        with self.db() as db:
            legacy_connection = aliased(GoogleConnection)
            legacy_first = (
                select(func.min(legacy_connection.id))
                .where(legacy_connection.user_id == GoogleConnection.user_id)
                .scalar_subquery()
            )
            rows = db.execute(select(GoogleConnection.user_id, GoogleConnection.google_sub, GoogleConnection.link_version)
                .join(User, User.id == GoogleConnection.user_id)
                .outerjoin(GoogleAccountSelection, GoogleAccountSelection.user_id == User.id)
                .where(
                    User.active.is_(True), GoogleConnection.status == "connected",
                    or_(
                        (GoogleAccountSelection.state == "accepting")
                        & (GoogleAccountSelection.active_account_id == GoogleConnection.id),
                        GoogleAccountSelection.user_id.is_(None) & (GoogleConnection.id == legacy_first),
                    ),
                )
                .order_by(GoogleConnection.user_id)).all()
            return [DriveLink(*row) for row in rows if row.google_sub]

    def _set_file_error(self, link, file_id, version, error):
        code = getattr(error, "code", type(error).__name__)
        if code in {"RATE_LIMIT", "DEADLINE_EXCEEDED", "CONNECTION_ERROR", "RECONNECT_REQUIRED",
                    "FILE_VERSION_CHANGED", "STALE_DRIVE_LINK", "DriveEmbeddingError"}:
            return
        with self.db.begin() as db:
            if not lock_drive_link(db, link):
                return
            row = db.get(DriveFileMetadata, (link.user_id, link.google_sub, link.link_version, file_id))
            if row is not None and row.status == "pending" and row.version == version:
                row.status = "skipped" if code in {"NO_TEXT", "NOT_FOUND", "UNSUPPORTED_FILE",
                                                   "RESPONSE_TOO_LARGE", "PERMISSION_DENIED"} else "error"
                row.status_reason = code

    async def tick(self, slots):
        from app.workspace.drive_scanner import DriveScanner
        from app.workspace.drive_sync import DriveSync
        from app.workspace.network_slots import network_slots

        scan = DriveScanner(self.db, self.workspace)
        sync = DriveSync(self.db, self.workspace)
        token = network_slots.set(slots)
        try:
            self.retry_at = {key: until for key, until in self.retry_at.items() if until > time.monotonic()}
            for link in await asyncio.to_thread(self._active_links):
                throttle_key = (link, None)
                if throttle_key in self.retry_at:
                    continue
                deadline = request_deadline.set(time.monotonic() + 90)
                try:
                    with SelectionManager.admit_activity(
                        self.db, link.user_id, activity_type=ActivityType.DRIVE_SYNC,
                    ):
                        if await asyncio.to_thread(self._link, link.user_id) == link:
                            await self._sync_link(link, scan, sync)
                except SwitchLockError:
                    continue
                except WorkspaceError:
                    self.retry_at[throttle_key] = time.monotonic() + 300
                finally:
                    request_deadline.reset(deadline)
        finally:
            network_slots.reset(token)

    async def _sync_link(self, link, scan, sync):
        start, daily, _, _ = await asyncio.to_thread(self._next_index_input, link)
        if start:
            if not await asyncio.to_thread(scan.initial_scan, link, max_pages=4):
                return
        elif daily and not await asyncio.to_thread(scan.daily_reconcile, link, max_pages=4):
            return
        _, _, scopes, _ = await asyncio.to_thread(self._next_index_input, link)
        caught_up = True
        for scope in scopes:
            caught_up = await asyncio.to_thread(sync.incremental_sync, link, scope, max_pages=4) and caught_up
        if not caught_up:
            return
        incomplete, _, _, pending = await asyncio.to_thread(self._next_index_input, link)
        if not incomplete:
            ready = [row for row in pending if (link, row.file_id) not in self.retry_at][:2]
            await asyncio.gather(*(self._index_pending(link, row) for row in ready))

    async def _index_pending(self, link, row):
        try:
            await self.index_file(link, row.file_id)
        except Exception as exc:
            self.retry_at[(link, row.file_id)] = time.monotonic() + 60
            await asyncio.to_thread(self._set_file_error, link, row.file_id, row.version, exc)

    async def process_preview(self, user_id, payload):
        from app.workspace.drive_downloader import download_file
        from app.workspace.vn_normalizer import normalize_vietnamese

        link = await asyncio.to_thread(self._link, user_id)
        if not link or payload.get("drive_link") != link_payload(link):
            return response("link_changed")
        body = payload["body"]
        file_id = body.get("file_id")
        query = body["query"]
        terms = body.get("terms") or query.split()[:8]
        query_words = set(normalize_vietnamese(query).split())
        partial = False
        if file_id:
            ids = [file_id]
        else:
            escaped = [term.replace("\\", "\\\\").replace("'", "\\'")[:80] for term in terms[:8]]
            conditions = " or ".join(f"fullText contains '{term}'" for term in escaped if term)
            if not conditions:
                return response("no_evidence", link)
            options = {
                "q": f"trashed = false and ({conditions})", "corpora": "allDrives",
                "pageSize": 30, "includeItemsFromAllDrives": True, "supportsAllDrives": True,
                "fields": "nextPageToken,incompleteSearch,files(id,mimeType,capabilities(canDownload))",
            }
            result = await asyncio.to_thread(self.workspace.read, user_id, "drive", {
                "method": "files.list", "parameters": options,
            }, force_rest=True)
            data = result["data"]
            ids = [
                row["id"] for row in data.get("files", [])
                if row.get("id") and row.get("capabilities", {}).get("canDownload") is True
                and row.get("mimeType") != "application/vnd.google-apps.folder"
            ][:4]
            partial = bool(data.get("incompleteSearch") or data.get("nextPageToken"))

        async def read_file(fid):
            item = await asyncio.to_thread(download_file, self.workspace, link, fid)
            parsed = await self._pool().parse(
                item.parser_mime, item.content, priority=True, deadline=request_deadline.get(),
            )
            if not await asyncio.to_thread(
                self.permissions.check, link, fid, item.file.version, item.file.drive_id, fresh=True,
            ):
                return [], None
            chunks = []
            for part in parsed:
                score = len(query_words.intersection(part.normalized.split()))
                if score or file_id:
                    chunks.append({
                        "file_id": fid, "name": item.file.name, "page_or_section": part.page_or_section,
                        "content": part.content, "score": score,
                    })
            return chunks, {"file_id": fid, "version": item.file.version, "drive_id": item.file.drive_id}

        chunks = []
        verification = []
        for result in await asyncio.gather(*(read_file(fid) for fid in ids), return_exceptions=True):
            if isinstance(result, BaseException):
                partial = True
            else:
                parts, ref = result
                chunks.extend(parts)
                if ref:
                    verification.append(ref)
        if await asyncio.to_thread(self._link, user_id) != link:
            return response("link_changed")
        chunks.sort(key=lambda part: -part["score"])
        return response(
            "ok" if chunks else "no_evidence", link, chunks[:8],
            partial=partial, verification=verification,
        )
