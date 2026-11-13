"""Background worker loop processing Workspace jobs with PostgreSQL row locks and heartbeat leases."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any, Dict, Optional
import httpx
from sqlalchemy.orm import Session

from app.models import TelegramConnection, TelegramUpdate, User
from app.security import decrypt_secret, encrypt_secret
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus

logger = logging.getLogger(__name__)


def valid_recipient(db, user_id, telegram_user_id, chat_id, binding):
    return bool(binding and db.query(TelegramConnection).join(User).filter(
        User.id == user_id, User.active.is_(True), TelegramConnection.status == "connected",
        TelegramConnection.telegram_user_id == telegram_user_id,
        TelegramConnection.private_chat_id == chat_id, TelegramConnection.binding_id == binding,
    ).first())


def _encrypt_data(data: Any, key: Optional[str]) -> str:
    raw = json.dumps(data)
    if key:
        try:
            return encrypt_secret(raw, key)
        except Exception:
            pass
    return raw


def _decrypt_data(raw: Optional[str], key: Optional[str]) -> Any:
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
        http_client: Optional[httpx.AsyncClient] = None,
        background: bool = False,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.background = background
        self._http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        from app.workspace.network_slots import NetworkSlots
        with session_factory() as db:
            self.network_slots = NetworkSlots(db.get_bind(), background=background)

    def claim_next_job(self, db: Session) -> Optional[WorkspaceJob]:
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
            if db.query(WorkspaceJob.id).filter(
                WorkspaceJob.action != "index", WorkspaceJob.status == WorkspaceJobStatus.QUEUED.value,
                WorkspaceJob.deadline_at > now,
            ).first():
                return None
        else:
            query = query.filter(WorkspaceJob.action != "index")
        
        # Check if database dialect supports FOR UPDATE SKIP LOCKED (PostgreSQL)
        bind = db.get_bind()
        if bind and bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)

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
            job = db.query(WorkspaceJob).filter_by(
                job_id=job_id, worker_id=self.worker_id, retry_count=attempt,
                status=WorkspaceJobStatus.RUNNING.value,
            ).filter(WorkspaceJob.lease_expires_at > now).with_for_update().first()
            if job is None:
                return False
            job.status = status
            job.completed_at = now
            job.result_encrypted = _encrypt_data(result, key)
            job.callback_retry_at = now
            if error is not None:
                job.error_code = getattr(error, "code", type(error).__name__)
                job.error_message = str(error)
            db.commit()
            return True

    async def deliver_pending_callback(self, job_id=None):
        now = datetime.now(UTC)
        key = self.settings.ENCRYPTION_KEY
        with self.session_factory() as db:
            query = db.query(WorkspaceJob).filter(
                WorkspaceJob.callback_delivered_at.is_(None), WorkspaceJob.callback_retry_at <= now,
            )
            if job_id is not None:
                query = query.filter_by(job_id=job_id)
            job = query.order_by(WorkspaceJob.callback_retry_at).with_for_update(skip_locked=True).first()
            if job is None:
                return False
            job_id = job.job_id
            job.callback_retry_at = now + timedelta(seconds=30)
            db.flush()
            db.expunge(job)
            db.commit()
        if await self.send_callback(job, _decrypt_data(job.result_encrypted, key)):
            with self.session_factory() as db:
                db.query(WorkspaceJob).filter_by(job_id=job_id).update({
                    "callback_delivered_at": datetime.now(UTC), "callback_retry_at": None,
                })
                db.commit()
        return True

    async def execute_job_payload(
        self,
        user_id: int,
        service: str,
        operation: str,
        is_write: bool,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute workspace operation with strict prepare vs execute separation."""
        from app.google.client import GoogleClient
        from app.google.oauth import GoogleOAuthService
        from app.workspace.idempotency import IdempotencyManager
        from app.workspace.service import WorkspaceError, WorkspaceService

        oauth_service = GoogleOAuthService(
            session_factory=self.session_factory,
            client_id=getattr(self.settings, "GOOGLE_CLIENT_ID", ""),
            client_secret=getattr(self.settings, "GOOGLE_CLIENT_SECRET", ""),
            redirect_uri=getattr(self.settings, "google_redirect_uri", ""),
            encryption_key=getattr(self.settings, "ENCRYPTION_KEY", "") or "",
            allowed_domains=getattr(self.settings, "GOOGLE_WORKSPACE_DOMAINS", []),
        )
        google_client = GoogleClient(oauth_service=oauth_service)
        workspace_svc = WorkspaceService(
            google_client=google_client,
            signing_key=getattr(self.settings, "ENCRYPTION_KEY", "") or "default_key_32bytes_min_12345678",
            use_mcp=getattr(self.settings, "GOOGLE_WORKSPACE_MCP_ENABLED", True),
        )

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

            if action != "index" or service != "drive" or operation != "index.fetch":
                raise WorkspaceError("INVALID_BACKGROUND_JOB", "Tác vụ Drive nền không hợp lệ.")
            workspace_svc.use_mcp = False
            file_id = req_values["parameters"].get("fileId")
            from app.workspace.drive_knowledge import FILE_ID
            if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
                raise WorkspaceError("INVALID_BACKGROUND_JOB", "Cần ID tệp Drive hợp lệ.")
            result = await asyncio.to_thread(drive_documents, workspace_svc, user_id, [], file_id)
            return {"status": "completed", "result": result}

        if action == "prepare":
            # PREPARE: Generates preview and signed _ticket, emits 0 mutation requests
            res = await asyncio.to_thread(workspace_svc.prepare, user_id, service, req_values)
            return {"action": "prepare", "status": "waiting_confirmation", "result": res}

        elif action == "execute":
            # EXECUTE: Requires valid _ticket and operation_id
            if not ticket:
                raise WorkspaceError("MISSING_TICKET", "Execute operation requires a valid _ticket from prepare step")
            if not op_id:
                raise WorkspaceError("MISSING_OPERATION_ID", "Execute operation requires operation_id for idempotency")

            idemp_mgr = IdempotencyManager()
            with self.session_factory() as idemp_db:
                rec, claimed = idemp_mgr.claim(idemp_db, user_id, op_id, payload)
                if rec.status == "completed":
                    import json
                    cached_data = json.loads(rec.response_json) if rec.response_json else {}
                    return {"action": "execute", "status": "completed", "result": cached_data, "cached": True}
                if not claimed:
                    raise WorkspaceError("UNKNOWN_RESULT", "Thao tác đang xử lý hoặc cần đối soát; không gửi lại.")

            exec_payload = dict(req_values)
            exec_payload["_ticket"] = ticket
            # Run real mutation call
            res = await asyncio.to_thread(workspace_svc.execute, user_id, service, exec_payload)

            with self.session_factory() as idemp_db:
                res_id = str(res.get("data", {}).get("id") or "")
                idemp_mgr.mark_completed(idemp_db, op_id, res_id, res)

            return {"action": "execute", "status": "completed", "result": res, "cached": False}

        else:
            # READ
            res = await asyncio.to_thread(workspace_svc.read, user_id, service, req_values)
            return {"action": "read", "status": "completed", "result": res}

    async def send_callback(self, job: WorkspaceJob, result_data: Any) -> bool:
        """Notify AI Agent via webhook callback of job completion."""
        agent_url = getattr(self.settings, "AI_AGENT_BASE_URL", "").rstrip("/")
        agent_secret = getattr(self.settings, "GATEWAY_TO_AGENT_SECRET", "")
        if not agent_url or not agent_secret:
            return False

        url = f"{agent_url}/internal/v1/workspace/job-completed"
        headers = {
            "Authorization": f"Bearer {agent_secret}",
            "Content-Type": "application/json",
        }
        saved = _decrypt_data(job.payload_encrypted, self.settings.ENCRYPTION_KEY) or {}
        binding = saved.get("telegram_binding_id")
        if job.telegram_chat_id is not None:
            with self.session_factory() as db:
                if not valid_recipient(db, job.user_id, job.telegram_user_id, job.telegram_chat_id, binding):
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
        }
        try:
            resp = await self._http_client.post(url, json=callback_body, headers=headers)
            if resp.status_code != 200:
                return False
            if binding:
                self.queue_telegram_result(job, binding, resp.json().get("messages", []))
            return True
        except Exception as exc:
            logger.warning(f"Failed to deliver callback to Agent for job {job.job_id}: {exc}")
            return False

    def queue_telegram_result(self, job, binding, messages, *, progress=False):
        from app.telegram.client import normalize_agent_response
        from sqlalchemy.exc import IntegrityError
        import hashlib

        delivery = normalize_agent_response(messages)
        if not delivery["messages"]:
            return
        delivery["telegram_binding_id"] = binding
        delivery_key = f"{job.job_id}:progress" if progress else job.job_id
        update_id = -int.from_bytes(hashlib.sha256(delivery_key.encode()).digest()[:7], "big") - 1
        with self.session_factory() as db:
            if not valid_recipient(db, job.user_id, job.telegram_user_id, job.telegram_chat_id, binding):
                return
            if progress and not db.query(WorkspaceJob.id).filter_by(job_id=job.job_id).filter(
                WorkspaceJob.status.in_(["queued", "running"]),
            ).with_for_update().first():
                return
            if db.query(TelegramUpdate.id).filter_by(update_id=update_id).first():
                return
            db.add(TelegramUpdate(update_id=update_id, user_id=job.user_id, status="ai_received",
                                 delivery_status="ready", ai_response=json.dumps(delivery, ensure_ascii=False),
                                 payload=json.dumps({"message": {"chat": {"id": job.telegram_chat_id},
                                                                  "from": {"id": job.telegram_user_id}}})))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if not db.query(TelegramUpdate.id).filter_by(update_id=update_id).first():
                    raise

    def notify_slow_jobs(self):
        now = datetime.now(UTC)
        with self.session_factory() as db:
            jobs = db.query(WorkspaceJob).filter(
                WorkspaceJob.status.in_(["queued", "running"]), WorkspaceJob.deadline_at > now,
                WorkspaceJob.created_at <= now - timedelta(seconds=30),
                WorkspaceJob.telegram_chat_id.is_not(None),
                WorkspaceJob.phase.is_distinct_from("progress_queued"),
            ).limit(20).all()
        for job in jobs:
            payload = _decrypt_data(job.payload_encrypted, self.settings.ENCRYPTION_KEY) or {}
            binding = payload.get("telegram_binding_id")
            if binding:
                self.queue_telegram_result(job, binding, [f"Tác vụ Workspace (#{job.job_id}) vẫn đang xử lý."],
                                           progress=True)
            with self.session_factory() as db:
                db.query(WorkspaceJob).filter_by(id=job.id).update({"phase": "progress_queued"})
                db.commit()

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

        payload_dict = _decrypt_data(raw_payload, key) or {}
        from app.workspace.telemetry import ExecutionTelemetry, active_execution, telemetry_store
        import time

        queue_wait = 0.0
        if job.created_at:
            created_dt = job.created_at
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
            queue_wait = max(0.0, (datetime.now(UTC) - created_dt).total_seconds() * 1000.0)

        trace = ExecutionTelemetry(request_id=job.request_id, job_id=job_id,
                                   operation_id=payload_dict.get("operation_id"), service=service,
                                   operation=operation, queue_wait_ms=queue_wait)
        trace_token = active_execution.set(trace)
        from app.workspace.network_slots import network_slots
        slot_token = network_slots.set(self.network_slots)
        t_start = time.perf_counter()
        heartbeat = asyncio.create_task(self.heartbeat(job_id, attempt))
        try:
            from app.workspace.service import WorkspaceError

            deadline = deadline if deadline.tzinfo else deadline.replace(tzinfo=UTC)
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            mutation = payload_dict.get("action") == "execute" or (is_write and bool(payload_dict.get("_ticket")))
            if job.telegram_chat_id is not None:
                with self.session_factory() as db:
                    if not valid_recipient(db, user_id, job.telegram_user_id, job.telegram_chat_id,
                                           payload_dict.get("telegram_binding_id")):
                        raise WorkspaceError("RECONNECT_REQUIRED", "Liên kết Telegram đã thay đổi.")
            else:
                with self.session_factory() as db:
                    if not db.query(User.id).filter_by(id=user_id, active=True).first():
                        raise WorkspaceError("RECONNECT_REQUIRED", "Tài khoản không còn hoạt động.")
            if attempt > 1 and mutation and payload_dict.get("operation_id"):
                from app.workspace.idempotency import IdempotencyManager, compute_payload_hash

                with self.session_factory() as db:
                    record = IdempotencyManager().get_record(db, payload_dict["operation_id"])
                    if record is not None and (record.user_id != user_id
                                               or record.payload_hash != compute_payload_hash(payload_dict)):
                        raise WorkspaceError("OPERATION_CONFLICT", "operation_id đã thuộc thao tác khác.")
                    if record is not None and record.status != "completed":
                        raise WorkspaceError("UNKNOWN_RESULT", "Worker trước có thể đã gửi thao tác ghi. Cần đối soát.")
            elif attempt > 1 and mutation:
                raise WorkspaceError("UNKNOWN_RESULT", "Thiếu mã để đối soát thao tác ghi.")
            if remaining <= 0:
                raise WorkspaceError("DEADLINE_EXCEEDED", "Tác vụ đã hết hạn trước khi thực thi.")
            try:
                async with asyncio.timeout(remaining):
                    exec_outcome = await self.execute_job_payload(user_id, service, operation, is_write, payload_dict)
            except TimeoutError as exc:
                code = "UNKNOWN_RESULT" if mutation else "DEADLINE_EXCEEDED"
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
        self._running = True
        while self._running:
            try:
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
        await self._http_client.aclose()

