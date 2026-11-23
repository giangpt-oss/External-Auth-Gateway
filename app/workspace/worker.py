"""Background worker loop processing Workspace jobs with PostgreSQL row locks and heartbeat leases."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import TelegramConnection, TelegramUpdate, User
from app.security import decrypt_secret, encrypt_secret
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus
from app.workspace.selection_manager import WorkspaceAccountPin, active_account_pin

logger = logging.getLogger(__name__)


def valid_recipient(db, user_id, telegram_user_id, chat_id, binding):
    return bool(
        binding
        and db.query(TelegramConnection)
        .join(User)
        .filter(
            User.id == user_id,
            User.active.is_(True),
            TelegramConnection.status.in_(["connected", "pending_qr"]),
            TelegramConnection.telegram_user_id == telegram_user_id,
            TelegramConnection.private_chat_id == chat_id,
            TelegramConnection.binding_id == binding,
        )
        .first()
    )


def _encrypt_data(data: Any, key: str | None) -> str:
    raw = json.dumps(data)
    if key:
        try:
            return encrypt_secret(raw, key)
        except Exception:
            pass
    return raw


def _decrypt_data(raw: str | None, key: str | None) -> Any:
    if not raw:
        return None
    if key:
        try:
            return json.loads(decrypt_secret(raw, key))
        except Exception:
            pass
    try:
        return json.loads(raw)
    except Exception:
        return raw


class WorkspaceWorker:
    """Worker claiming and processing WorkspaceJobs in the background."""

    def __init__(
        self,
        settings: Any,
        session_factory: Any,
        worker_id: str = "workspace-worker-1",
        poll_interval: float = 1.0,
        http_client: httpx.AsyncClient | None = None,
        background: bool = False,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.background = background
        self._http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._running = False
        self._task: asyncio.Task | None = None
        from app.workspace.network_slots import NetworkSlots

        with session_factory() as db:
            self.network_slots = NetworkSlots(db.get_bind(), background=background)

        from app.google.client import GoogleClient
        from app.google.oauth import GoogleOAuthService

        self.oauth_service = GoogleOAuthService(
            session_factory=self.session_factory,
            client_id=getattr(self.settings, "GOOGLE_CLIENT_ID", ""),
            client_secret=getattr(self.settings, "GOOGLE_CLIENT_SECRET", ""),
            redirect_uri=getattr(self.settings, "google_redirect_uri", ""),
            encryption_key=getattr(self.settings, "ENCRYPTION_KEY", "") or "",
            allowed_domains=getattr(self.settings, "GOOGLE_WORKSPACE_DOMAINS", []),
        )
        self.google_client = GoogleClient(oauth_service=self.oauth_service)
        self._workspace_svc = None
        self._drive_runtime = None

    @property
    def drive_runtime(self):
        if self._drive_runtime is None:
            from app.workspace.drive_runtime import DriveRuntime

            self._drive_runtime = DriveRuntime(
                self.settings,
                self.session_factory,
                self.workspace_svc,
                self._http_client,
            )
        return self._drive_runtime

    @property
    def workspace_svc(self):
        if getattr(self, "_workspace_svc", None) is None:
            from app.workspace.service import WorkspaceService

            self._workspace_svc = WorkspaceService(
                google_client=self.google_client,
                signing_key=getattr(self.settings, "ENCRYPTION_KEY", "")
                or "default_key_32bytes_min_12345678",
                use_mcp=getattr(self.settings, "GOOGLE_WORKSPACE_MCP_ENABLED", True),
            )
        return self._workspace_svc

    @workspace_svc.setter
    def workspace_svc(self, value):
        self._workspace_svc = value

    def claim_next_job(self, db: Session) -> WorkspaceJob | None:
        """Claim next queued job or expired running job using row locks."""
        now = datetime.now(UTC)
        query = db.query(WorkspaceJob).filter(
            (WorkspaceJob.status == WorkspaceJobStatus.QUEUED.value)
            | (
                (WorkspaceJob.status == WorkspaceJobStatus.RUNNING.value)
                & (WorkspaceJob.lease_expires_at < now)
            )
        )
        if self.background:
            query = query.filter(WorkspaceJob.action == "index")
            if (
                not getattr(self.settings, "DRIVE_INDEX_ENABLED", False)
                and db.query(WorkspaceJob.id)
                .filter(
                    WorkspaceJob.action != "index",
                    WorkspaceJob.status == WorkspaceJobStatus.QUEUED.value,
                    WorkspaceJob.deadline_at > now,
                )
                .first()
            ):
                return None
        else:
            query = query.filter(WorkspaceJob.action != "index")

        # Check if database dialect supports FOR UPDATE SKIP LOCKED (PostgreSQL)
        bind = db.get_bind()
        if bind and bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)

        if self.background:
            from sqlalchemy import case

            query = query.order_by(case((WorkspaceJob.operation == "index.preview", 0), else_=1))
        job = query.order_by(WorkspaceJob.created_at.asc()).first()
        if not job:
            return None

        job.status = WorkspaceJobStatus.RUNNING.value
        job.worker_id = self.worker_id
        job.lease_expires_at = now + timedelta(seconds=60)
        job.retry_count = (job.retry_count or 0) + 1
        db.commit()
        db.refresh(job)
        return job

    def renew_lease(self, db: Session, job_id: str, attempt: int | None = None) -> bool:
        """Renew worker lease for an ongoing job."""
        query = (
            db.query(WorkspaceJob)
            .filter(
                WorkspaceJob.job_id == job_id,
                WorkspaceJob.worker_id == self.worker_id,
                WorkspaceJob.status == WorkspaceJobStatus.RUNNING.value,
            )
            .filter(WorkspaceJob.lease_expires_at > datetime.now(UTC))
            .with_for_update()
        )
        if attempt is not None:
            query = query.filter_by(retry_count=attempt)
        job = query.first()
        if not job:
            return False
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
        db.commit()
        return True

    async def heartbeat(self, job_id: str, attempt: int) -> None:
        while True:
            await asyncio.sleep(15)
            with self.session_factory() as db:
                if not self.renew_lease(db, job_id, attempt):
                    return

    def finish_job(self, job_id, attempt, status, result, key, error=None):
        now = datetime.now(UTC)
        with self.session_factory() as db:
            job = (
                db.query(WorkspaceJob)
                .filter_by(
                    job_id=job_id,
                    worker_id=self.worker_id,
                    retry_count=attempt,
                    status=WorkspaceJobStatus.RUNNING.value,
                )
                .filter(WorkspaceJob.lease_expires_at > now)
                .with_for_update()
                .first()
            )
            if job is None:
                return False
            if status == WorkspaceJobStatus.UNKNOWN_RESULT.value and job.operation_id:
                from app.workspace.idempotency import IdempotencyManager, compute_payload_hash

                evidence = IdempotencyManager().get_record(db, job.operation_id)
                payload = _decrypt_data(job.payload_encrypted, key)
                if (
                    evidence
                    and evidence.status == "completed"
                    and evidence.user_id == job.user_id
                    and evidence.payload_hash == compute_payload_hash(payload)
                    and evidence.response_json
                ):
                    status = WorkspaceJobStatus.COMPLETED.value
                    result = json.loads(evidence.response_json)
                    error = None
            job.status = status
            scoped_formats = {"indexed_drive_v1", "workspace_plan_v1"}
            if isinstance(result, dict) and result.get("format") in scoped_formats:
                from app.workspace.drive_runtime import safe_result

                result = safe_result(db, job.user_id, result)
            job.completed_at = now
            indexed = isinstance(result, dict) and result.get("format") in scoped_formats
            job.result_encrypted = (
                encrypt_secret(json.dumps(result), key) if indexed else _encrypt_data(result, key)
            )
            job.callback_retry_at = (
                None
                if job.action == "index" and getattr(self.settings, "DRIVE_INDEX_ENABLED", False)
                else now
            )
            if error is not None:
                job.error_code = getattr(error, "code", type(error).__name__)
                job.error_message = str(error)

            from app.workspace.activity_tracker import ActivityType, WorkspaceActivityTracker
            if status == WorkspaceJobStatus.WAITING_CONFIRMATION.value:
                WorkspaceActivityTracker.register_activity(
                    db,
                    user_id=job.user_id,
                    activity_type=ActivityType.PREVIEW_PENDING,
                    reference_id=job.job_id,
                    google_account_id=job.google_account_id,
                )
            elif status == WorkspaceJobStatus.UNKNOWN_RESULT.value:
                WorkspaceActivityTracker.mark_reconciling(db, user_id=job.user_id, reference_id=job.job_id)
            else:
                WorkspaceActivityTracker.finish_activity(db, user_id=job.user_id, reference_id=job.job_id, status=status)

            db.commit()
            return True

    async def deliver_pending_callback(self, job_id=None):
        now = datetime.now(UTC)
        key = self.settings.ENCRYPTION_KEY
        with self.session_factory() as db:
            query = db.query(WorkspaceJob).filter(
                WorkspaceJob.callback_delivered_at.is_(None),
                WorkspaceJob.callback_retry_at <= now,
            )
            if job_id is not None:
                query = query.filter_by(job_id=job_id)
            job = (
                query.order_by(WorkspaceJob.callback_retry_at)
                .with_for_update(skip_locked=True)
                .first()
            )
            if job is None:
                return False
            job_id = job.job_id
            job.callback_retry_at = now + timedelta(seconds=30)
            db.flush()
            db.expunge(job)
            db.commit()
        if await self.send_callback(job, _decrypt_data(job.result_encrypted, key)):
            with self.session_factory() as db:
                db.query(WorkspaceJob).filter_by(job_id=job_id, status=job.status).update(
                    {
                        "callback_delivered_at": datetime.now(UTC),
                        "callback_retry_at": None,
                    }
                )
                db.commit()
        return True

    async def execute_job_payload(
        self,
        user_id: int,
        service: str,
        operation: str,
        is_write: bool,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute workspace operation with strict prepare vs execute separation."""
        from app.workspace.idempotency import IdempotencyManager
        from app.workspace.service import WorkspaceError

        workspace_svc = self.workspace_svc

        action = payload.get("action", "read")
        ticket = payload.get("_ticket")
        op_id = payload.get("operation_id")

        if is_write and action == "read":
            action = "execute" if ticket else "prepare"

        req_values = {
            "method": operation,
            "parameters": payload.get("parameters", {}),
            "body": payload.get("body", {}),
            "media": payload.get("media"),
        }

        if self.background:
            from app.workspace.drive_knowledge import drive_documents

            if (
                getattr(self.settings, "DRIVE_INDEX_ENABLED", False)
                and action == "index"
                and service == "drive"
            ):
                from app.workspace.drive_permissions import link_payload
                from app.workspace.drive_runtime import PREVIEW, RESYNC

                if operation == PREVIEW:
                    return {
                        "status": "completed",
                        "result": await self.drive_runtime.process_preview(user_id, payload),
                    }
                link = await asyncio.to_thread(self.drive_runtime._link, user_id)
                if link is None or link_payload(link) != payload.get("drive_link"):
                    raise WorkspaceError("RECONNECT_REQUIRED", "Liên kết Google đã thay đổi.")
                if operation == RESYNC:
                    from app.workspace.drive_scanner import DriveScanner

                    scanner = DriveScanner(self.session_factory, workspace_svc)
                    complete = await asyncio.to_thread(
                        scanner.daily_reconcile, link, max_pages=4, force=True
                    )
                    return {
                        "status": "completed",
                        "result": {"status": "ok", "scan_complete": complete},
                    }
                if operation == "index.fetch":
                    file_id = req_values["parameters"].get("fileId")
                    from app.workspace.drive_knowledge import FILE_ID

                    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
                        raise WorkspaceError("INVALID_BACKGROUND_JOB", "Cần ID tệp Drive hợp lệ.")
                    await self.drive_runtime.index_file(link, file_id)
                    return {"status": "completed", "result": {"status": "ok"}}

            if action != "index" or service != "drive" or operation != "index.fetch":
                raise WorkspaceError("INVALID_BACKGROUND_JOB", "Tác vụ Drive nền không hợp lệ.")
            file_id = req_values["parameters"].get("fileId")
            from app.workspace.drive_knowledge import FILE_ID

            if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
                raise WorkspaceError("INVALID_BACKGROUND_JOB", "Cần ID tệp Drive hợp lệ.")
            result = await asyncio.to_thread(drive_documents, workspace_svc, user_id, [], file_id)
            return {"status": "completed", "result": result}

        if action == "read" and service == "workspace" and operation == "plan.read":
            return {"status": "completed", "result": await self.run_read_plan(user_id, payload)}

        if action == "read" and service == "drive" and operation == "knowledge.search":
            from app.workspace.drive_knowledge import FILE_ID, drive_documents

            terms = req_values["parameters"].get("terms", [])
            file_id = req_values["parameters"].get("file_id")
            question = req_values["body"].get("question")
            if (
                not isinstance(question, str)
                or not question.strip()
                or len(question) > 12000
                or not isinstance(terms, list)
                or len(terms) > 8
                or any(not isinstance(term, str) or len(term) > 80 for term in terms)
                or (
                    file_id is not None
                    and (not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id))
                )
            ):
                raise WorkspaceError("INVALID_REQUEST", "Yêu cầu tìm tài liệu không hợp lệ.")
            if getattr(self.settings, "DRIVE_INDEX_ENABLED", False):
                expected = payload.get("drive_link")
                if not expected:
                    from app.workspace.drive_runtime import response

                    result = response("connection_error")
                else:
                    result = await self.drive_runtime.search(
                        user_id,
                        question,
                        file_id=file_id,
                        terms=terms,
                        expected=expected,
                    )
            else:
                result = await asyncio.to_thread(
                    drive_documents, workspace_svc, user_id, terms, file_id
                )
            result["question"] = question
            return {"status": "completed", "result": result}

        if action == "prepare":
            # PREPARE: Generates preview and signed _ticket, emits 0 mutation requests
            res = await asyncio.to_thread(workspace_svc.prepare, user_id, service, req_values)
            return {"action": "prepare", "status": "waiting_confirmation", "result": res}

        elif action == "execute":
            # EXECUTE: Requires valid _ticket and operation_id
            if not ticket:
                raise WorkspaceError(
                    "MISSING_TICKET", "Execute operation requires a valid _ticket from prepare step"
                )
            if not op_id:
                raise WorkspaceError(
                    "MISSING_OPERATION_ID",
                    "Execute operation requires operation_id for idempotency",
                )

            idemp_mgr = IdempotencyManager()
            with self.session_factory() as idemp_db:
                rec, claimed = idemp_mgr.claim(idemp_db, user_id, op_id, payload)
                if rec.status == "completed":
                    import json

                    cached_data = json.loads(rec.response_json) if rec.response_json else {}
                    return {
                        "action": "execute",
                        "status": "completed",
                        "result": cached_data,
                        "cached": True,
                    }
                if not claimed:
                    raise WorkspaceError(
                        "UNKNOWN_RESULT", "Thao tác đang xử lý hoặc cần đối soát; không gửi lại."
                    )

            exec_payload = dict(req_values)
            exec_payload["_ticket"] = ticket

            # Run real mutation call
            def execute_and_record():
                # The thread can outlive the async deadline. Persist a confirmed response
                # inside the thread so a later status check can reconcile it.
                try:
                    result = workspace_svc.execute(user_id, service, exec_payload)
                except WorkspaceError as exc:
                    if exc.code == "UNKNOWN_RESULT":
                        with self.session_factory() as idemp_db:
                            idemp_mgr.mark_unknown_result(idemp_db, op_id)
                    raise
                with self.session_factory() as idemp_db:
                    resource_id = str(result.get("data", {}).get("id") or "")
                    idemp_mgr.mark_completed(idemp_db, op_id, resource_id, result)
                    # A timed-out worker may have already committed the unknown status.
                    from app.workspace.telemetry import active_execution

                    trace = active_execution.get()
                    job_key = trace.job_id if trace else None
                    if job_key:
                        reconciled = idemp_db.query(WorkspaceJob).filter_by(
                            job_id=job_key,
                            user_id=user_id,
                            operation_id=op_id,
                            status=WorkspaceJobStatus.UNKNOWN_RESULT.value,
                        ).update(
                            {
                                "status": WorkspaceJobStatus.COMPLETED.value,
                                "result_encrypted": _encrypt_data(
                                    result, self.settings.ENCRYPTION_KEY
                                ),
                                "error_code": None,
                                "error_message": None,
                                "callback_delivered_at": None,
                                "callback_retry_at": datetime.now(UTC),
                            }
                        )
                        if reconciled:
                            from app.workspace.activity_tracker import WorkspaceActivityTracker

                            WorkspaceActivityTracker.finish_activity(idemp_db, user_id, job_key)
                        idemp_db.commit()
                return result

            res = await asyncio.to_thread(execute_and_record)

            return {"action": "execute", "status": "completed", "result": res, "cached": False}

        else:
            # READ
            res = await asyncio.to_thread(workspace_svc.read, user_id, service, req_values)
            return {"action": "read", "status": "completed", "result": res}

    async def run_read_plan(self, user_id, payload):
        from time import monotonic

        from app.workspace.drive_permissions import matches_link
        from app.workspace.network_slots import request_deadline
        from app.workspace.service import WorkspaceError

        link = payload.get("drive_link")
        with self.session_factory() as db:
            if not matches_link(db, user_id, link):
                raise WorkspaceError("RECONNECT_REQUIRED", "Liên kết Google đã thay đổi.")
        settings = self.settings
        base_url = settings.AI_AGENT_BASE_URL.rstrip("/")
        if not base_url or not settings.GATEWAY_TO_AGENT_SECRET:
            raise WorkspaceError("SERVICE_ERROR", "Agent chưa được cấu hình.")
        deadline = request_deadline.get()
        remaining = deadline - monotonic() if deadline is not None else 0
        if remaining <= 0:
            raise WorkspaceError("DEADLINE_EXCEEDED", "Kế hoạch đã hết hạn.")
        try:
            result = await self._http_client.post(
                f"{base_url}/internal/v1/workspace/read-plan",
                headers={"Authorization": f"Bearer {settings.GATEWAY_TO_AGENT_SECRET}"},
                json={
                    "user_id": user_id, "steps": payload["body"]["steps"],
                    "telegram_user_id": payload["telegram_user_id"],
                    "chat_id": payload["telegram_chat_id"],
                    "binding_id": payload["telegram_binding_id"],
                    "link": link, "remaining_seconds": remaining,
                },
                timeout=remaining,
            )
            if result.status_code == 403:
                raise WorkspaceError("PERMISSION_DENIED", "Liên kết hoặc quyền đọc đã thay đổi.")
            if result.status_code == 422:
                raise WorkspaceError("INVALID_REQUEST", "Agent từ chối kế hoạch đọc.")
            result.raise_for_status()
            data = result.json()
            if (not isinstance(data, dict) or not isinstance(data.get("steps"), list)
                    or type(data.get("partial")) is not bool):
                raise ValueError("Invalid read plan response")
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkspaceError("SERVICE_ERROR", "Không thể hoàn tất kế hoạch đọc qua Agent.") from exc
        return {"format": "workspace_plan_v1", "link": link, "steps": data["steps"],
                "partial": data["partial"]}

    async def send_callback(self, job: WorkspaceJob, result_data: Any) -> bool:
        """Notify AI Agent via webhook callback of job completion."""
        if job.service == "workspace" and job.operation == "plan.read":
            created = job.created_at
            if created and datetime.now(UTC) - created.replace(tzinfo=created.tzinfo or UTC) >= timedelta(hours=24):
                return True
        agent_url = getattr(self.settings, "AI_AGENT_BASE_URL", "").rstrip("/")
        agent_secret = getattr(self.settings, "GATEWAY_TO_AGENT_SECRET", "")
        if not agent_url or not agent_secret:
            return True

        url = f"{agent_url}/internal/v1/workspace/job-completed"
        headers = {
            "Authorization": f"Bearer {agent_secret}",
            "Content-Type": "application/json",
        }
        saved = _decrypt_data(job.payload_encrypted, self.settings.ENCRYPTION_KEY) or {}
        binding = saved.get("telegram_binding_id")
        indexed = isinstance(result_data, dict) and result_data.get("format") in {
            "indexed_drive_v1", "workspace_plan_v1",
        }
        drive_link = result_data.get("link") if indexed else None
        drive_verification = result_data.get("verification", []) if indexed else []
        with self.session_factory() as db:
            if drive_link:
                from app.workspace.drive_runtime import safe_result

                result_data = safe_result(db, job.user_id, result_data)
            if job.telegram_chat_id is not None:
                if not valid_recipient(
                    db, job.user_id, job.telegram_user_id, job.telegram_chat_id, binding
                ):
                    return True
        callback_body = {
            "job_id": job.job_id,
            "user_id": job.user_id,
            "request_id": job.request_id,
            "status": job.status,
            "service": job.service,
            "operation": job.operation,
            "operation_id": job.operation_id,
            "result": result_data,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "telegram_chat_id": job.telegram_chat_id,
            "telegram_user_id": job.telegram_user_id,
            "telegram_binding_id": binding,
            "google_account_id": job.google_account_id,
            "google_selection_version": job.selection_version,
        }
        try:
            resp = await self._http_client.post(url, json=callback_body, headers=headers)
            if resp.status_code != 200:
                return False
            if binding:
                self.queue_telegram_result(
                    job,
                    binding,
                    resp.json().get("messages", []),
                    drive_link=drive_link,
                    drive_verification=drive_verification,
                )
            return True
        except Exception as exc:
            logger.warning(f"Failed to deliver callback to Agent for job {job.job_id}: {exc}")
            return False

    def queue_telegram_result(
        self, job, binding, messages, *, progress=False, drive_link=None, drive_verification=()
    ):
        import hashlib

        from sqlalchemy.exc import IntegrityError

        from app.telegram.client import normalize_agent_response

        delivery = normalize_agent_response(messages)
        if not delivery["messages"]:
            return
        delivery["telegram_binding_id"] = binding
        delivery_key = job.job_id
        if progress:
            delivery_key += ":progress"
        elif job.status == "unknown_result":
            delivery_key += ":unknown_result"
        update_id = -int.from_bytes(hashlib.sha256(delivery_key.encode()).digest()[:7], "big") - 1
        with self.session_factory() as db:
            if not valid_recipient(
                db, job.user_id, job.telegram_user_id, job.telegram_chat_id, binding
            ):
                return
            if drive_link:
                from app.workspace.drive_permissions import delivery_evidence_current

                if not delivery_evidence_current(db, job.user_id, drive_link, drive_verification):
                    return
                delivery["drive_link"] = drive_link
                delivery["drive_verification"] = drive_verification
                if job.service == "workspace" and job.operation == "plan.read":
                    created = job.created_at.replace(tzinfo=job.created_at.tzinfo or UTC)
                    delivery["expires_at_unix"] = (created + timedelta(hours=24)).timestamp()
            if (
                progress
                and not db.query(WorkspaceJob.id)
                .filter_by(job_id=job.job_id)
                .filter(
                    WorkspaceJob.status.in_(["queued", "running"]),
                )
                .with_for_update()
                .first()
            ):
                return
            if db.query(TelegramUpdate.id).filter_by(update_id=update_id).first():
                return
            stored_delivery = json.dumps(delivery, ensure_ascii=False)
            if drive_link:
                stored_delivery = "drive1:" + encrypt_secret(
                    stored_delivery, self.settings.ENCRYPTION_KEY
                )
            db.add(
                TelegramUpdate(
                    update_id=update_id,
                    user_id=job.user_id,
                    google_account_id=job.google_account_id,
                    status="ai_received",
                    delivery_status="ready",
                    ai_response=stored_delivery,
                    payload=json.dumps(
                        {
                            "message": {
                                "chat": {"id": job.telegram_chat_id},
                                "from": {"id": job.telegram_user_id},
                            }
                        }
                    ),
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if not db.query(TelegramUpdate.id).filter_by(update_id=update_id).first():
                    raise

    def notify_slow_jobs(self):
        now = datetime.now(UTC)
        with self.session_factory() as db:
            jobs = (
                db.query(WorkspaceJob)
                .filter(
                    WorkspaceJob.status.in_(["queued", "running"]),
                    WorkspaceJob.deadline_at > now,
                    WorkspaceJob.created_at <= now - timedelta(seconds=30),
                    WorkspaceJob.telegram_chat_id.is_not(None),
                    WorkspaceJob.phase.is_distinct_from("progress_queued"),
                )
                .limit(20)
                .all()
            )
        for job in jobs:
            payload = _decrypt_data(job.payload_encrypted, self.settings.ENCRYPTION_KEY) or {}
            binding = payload.get("telegram_binding_id")
            if binding:
                self.queue_telegram_result(
                    job,
                    binding,
                    [f"Tác vụ Workspace (#{job.job_id}) vẫn đang xử lý."],
                    progress=True,
                )
            with self.session_factory() as db:
                db.query(WorkspaceJob).filter_by(id=job.id).update({"phase": "progress_queued"})
                db.commit()

    def purge_expired_read_plans(self):
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        with self.session_factory.begin() as db:
            expired = (
                db.query(WorkspaceJob)
                .filter_by(service="workspace", operation="plan.read")
                .filter(WorkspaceJob.created_at <= cutoff, WorkspaceJob.payload_encrypted != "")
                .limit(100)
                .with_for_update(skip_locked=True)
                .all()
            )
            for job in expired:
                job.payload_encrypted = ""
                job.result_encrypted = None
                job.callback_retry_at = None
                if job.status in {"queued", "running"}:
                    job.status = WorkspaceJobStatus.EXPIRED.value
            deliveries = (
                db.query(TelegramUpdate)
                .filter(
                    TelegramUpdate.created_at <= cutoff,
                    TelegramUpdate.ai_response.like("drive1:%"),
                    TelegramUpdate.delivery_status != "sending",
                )
                .limit(100)
                .with_for_update(skip_locked=True)
                .all()
            )
            for delivery in deliveries:
                delivery.ai_response = None
                delivery.delivery_chunks = None
                if delivery.status == "ai_received":
                    delivery.status = "failed"
                    delivery.delivery_status = "failed"

    async def process_one_cycle(self) -> bool:
        """Claim and execute a single job."""
        key = getattr(self.settings, "ENCRYPTION_KEY", None)
        with self.session_factory() as db:
            job = self.claim_next_job(db)
            if not job:
                return False
            job_id = job.job_id
            user_id = job.user_id
            service = job.service
            operation = job.operation
            is_write = job.is_write
            raw_payload = job.payload_encrypted
            deadline = job.deadline_at
            attempt = job.retry_count
            pin = (
                WorkspaceAccountPin(user_id, job.google_account_id, job.selection_version)
                if job.google_account_id is not None else None
            )

        payload_dict = _decrypt_data(raw_payload, key) or {}
        import time

        from app.workspace.telemetry import ExecutionTelemetry, active_execution, telemetry_store

        queue_wait = 0.0
        if job.created_at:
            created_dt = job.created_at
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
            queue_wait = max(0.0, (datetime.now(UTC) - created_dt).total_seconds() * 1000.0)

        trace = ExecutionTelemetry(
            request_id=job.request_id,
            job_id=job_id,
            operation_id=payload_dict.get("operation_id"),
            service=service,
            operation=operation,
            queue_wait_ms=queue_wait,
        )
        trace_token = active_execution.set(trace)
        from app.workspace.network_slots import before_write, network_slots, request_deadline

        slot_token = network_slots.set(self.network_slots)
        t_start = time.perf_counter()
        heartbeat = asyncio.create_task(self.heartbeat(job_id, attempt))
        from threading import Lock

        dispatch_lock = Lock()
        dispatch_started = False
        dispatch_closed = False

        def guard_write():
            nonlocal dispatch_started
            with dispatch_lock:
                if dispatch_closed or datetime.now(UTC) >= deadline:
                    raise WorkspaceError(
                        "DEADLINE_EXCEEDED", "Tác vụ đã hết hạn trước khi gửi thao tác ghi."
                    )
                dispatch_started = True

        write_token = before_write.set(guard_write)
        account_token = active_account_pin.set(pin)
        deadline_token = None
        try:
            from app.workspace.service import WorkspaceError

            deadline = deadline if deadline.tzinfo else deadline.replace(tzinfo=UTC)
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            deadline_token = request_deadline.set(time.monotonic() + remaining)
            mutation = payload_dict.get("action") == "execute" or (
                is_write and bool(payload_dict.get("_ticket"))
            )
            if job.telegram_chat_id is not None:
                with self.session_factory() as db:
                    if not valid_recipient(
                        db,
                        user_id,
                        job.telegram_user_id,
                        job.telegram_chat_id,
                        payload_dict.get("telegram_binding_id"),
                    ):
                        raise WorkspaceError("RECONNECT_REQUIRED", "Liên kết Telegram đã thay đổi.")
            else:
                with self.session_factory() as db:
                    if not db.query(User.id).filter_by(id=user_id, active=True).first():
                        raise WorkspaceError("RECONNECT_REQUIRED", "Tài khoản không còn hoạt động.")
            if attempt > 1 and mutation and payload_dict.get("operation_id"):
                from app.workspace.idempotency import IdempotencyManager, compute_payload_hash

                with self.session_factory() as db:
                    record = IdempotencyManager().get_record(db, payload_dict["operation_id"])
                    if record is not None and (
                        record.user_id != user_id
                        or record.payload_hash != compute_payload_hash(payload_dict)
                    ):
                        raise WorkspaceError(
                            "OPERATION_CONFLICT", "operation_id đã thuộc thao tác khác."
                        )
                    if record is not None and record.status != "completed":
                        raise WorkspaceError(
                            "UNKNOWN_RESULT",
                            "Worker trước có thể đã gửi thao tác ghi. Cần đối soát.",
                        )
            elif attempt > 1 and mutation:
                raise WorkspaceError("UNKNOWN_RESULT", "Thiếu mã để đối soát thao tác ghi.")
            if remaining <= 0:
                raise WorkspaceError("DEADLINE_EXCEEDED", "Tác vụ đã hết hạn trước khi thực thi.")
            try:
                async with asyncio.timeout(remaining):
                    exec_outcome = await self.execute_job_payload(
                        user_id, service, operation, is_write, payload_dict
                    )
            except TimeoutError as exc:
                with dispatch_lock:
                    dispatch_closed = True
                    code = "UNKNOWN_RESULT" if dispatch_started else "DEADLINE_EXCEEDED"
                raise WorkspaceError(code, "Tác vụ đã hết thời gian thực thi.") from exc

            result_data = exec_outcome.get("result")
            final_status = (
                WorkspaceJobStatus.WAITING_CONFIRMATION.value
                if exec_outcome.get("status") == "waiting_confirmation"
                else WorkspaceJobStatus.COMPLETED.value
            )

            self.finish_job(job_id, attempt, final_status, result_data, key)
        except Exception as exc:
            trace.success = False
            trace.error_code = str(getattr(exc, "code", type(exc).__name__))
            logger.error(f"Error executing workspace job {job_id}: {exc}", exc_info=True)
            status = {
                "UNKNOWN_RESULT": WorkspaceJobStatus.UNKNOWN_RESULT.value,
                "DEADLINE_EXCEEDED": WorkspaceJobStatus.EXPIRED.value,
            }.get(trace.error_code, WorkspaceJobStatus.FAILED.value)
            self.finish_job(job_id, attempt, status, None, key, error=exc)

        finally:
            with dispatch_lock:
                dispatch_closed = True
            before_write.reset(write_token)
            active_account_pin.reset(account_token)
            if deadline_token is not None:
                request_deadline.reset(deadline_token)
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            active_execution.reset(trace_token)
            network_slots.reset(slot_token)
            trace.total_duration_ms = queue_wait + (time.perf_counter() - t_start) * 1000
            telemetry_store.record(trace)

        await self.deliver_pending_callback(job_id)
        return True

    async def run(self) -> None:
        """Continuous worker execution loop."""
        from time import monotonic

        self._running = True
        next_cleanup = 0.0
        while self._running:
            try:
                if not self.background and monotonic() >= next_cleanup:
                    next_cleanup = monotonic() + 300
                    await asyncio.to_thread(self.purge_expired_read_plans)
                processed = await self.process_one_cycle()
                processed = await self.deliver_pending_callback() or processed
                if not processed:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Unexpected error in WorkspaceWorker loop: {exc}", exc_info=True)
                await asyncio.sleep(self.poll_interval)

    def start(self) -> None:
        """Start worker in background asyncio task."""
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Stop worker task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if hasattr(self, "workspace_svc") and self.workspace_svc:
            self.workspace_svc.close()
        if getattr(self, "_drive_runtime", None) is not None:
            await self._drive_runtime.close()
        if hasattr(self, "google_client") and self.google_client:
            self.google_client.close()
            await self.google_client.unified_client.close()
        await self._http_client.aclose()
