import json
import logging
import secrets
import threading
import time
from types import SimpleNamespace

from fastapi import HTTPException

from app.reporting.artifacts import load_artifact, revalidate_for_delivery
from app.reporting.context import authenticated_context
from app.reporting.engine import generate
from app.reporting.export import chat_summary, has_data
from app.reporting.formats import prepare_artifacts, selected_artifacts, selection_text
from app.reporting.models import ReportArtifact, ReportJob
from app.reporting.schemas import ReportRequest
from app.telegram.client import TelegramClient
from app.telegram.text import plain_text

log = logging.getLogger(__name__)


class LeaseLost(Exception):
    pass


class ReportWorker:
    def __init__(self, settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self.stop_event = threading.Event()
        self.thread = None
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=session_factory)))

    def start(self):
        if self.thread or not getattr(self.settings, "ENABLE_REPORTING", False):
            return
        self.thread = threading.Thread(target=self.run, daemon=True, name="ReportWorker")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Report worker iteration failed")
            self.stop_event.wait(2)

    def tick(self):
        now = time.time()
        with self.session_factory() as db:
            db.query(ReportArtifact).filter(ReportArtifact.expires_at <= now).delete(synchronize_session=False)
            # Reports are read-only, so an expired calculation can safely be restarted.
            db.query(ReportJob).filter(ReportJob.status == "running", ReportJob.lease_until < now).update({"status": "queued", "lease_token": None}, synchronize_session=False)
            # A crashed Telegram send is ambiguous and must never be automatically replayed.
            db.query(ReportJob).filter(ReportJob.delivery_status.in_(["sending", "preparing"]), ReportJob.lease_until < now).update({"delivery_status": "unknown"}, synchronize_session=False)
            db.commit()
            candidate = db.query(ReportJob).filter_by(status="queued").order_by(ReportJob.created_at).first()
            if candidate:
                report_id = candidate.id
                token = secrets.token_hex(16)
                changed = db.query(ReportJob).filter_by(id=report_id, status="queued").update({"status": "running", "lease_token": token, "lease_until": now + 120, "updated_at": now})
                db.commit()
                if changed:
                    self.calculate(report_id, token)
            deliveries = [j.id for j in db.query(ReportJob).filter(ReportJob.status.in_(["completed", "failed"]), ReportJob.delivery_status == "pending").order_by(ReportJob.created_at).limit(5)]
        for report_id in deliveries:
            self.deliver(report_id)

    def checkpoint(self, report_id, token, progress):
        if self.stop_event.is_set():
            raise LeaseLost()
        with self.session_factory() as db:
            changed = db.query(ReportJob).filter_by(id=report_id, status="running", lease_token=token).update({"progress": progress, "lease_until": time.time() + 120, "updated_at": time.time()})
            db.commit()
            if not changed:
                raise LeaseLost()

    def calculate(self, report_id, token):
        try:
            with self.session_factory() as db:
                job = db.get(ReportJob, report_id)
                user_id, tenant, database = job.user_id, job.tenant, job.database
                req = ReportRequest.model_validate_json(job.request_json)
            with authenticated_context(self.request, user_id, req) as ctx:
                if ctx.user.tenant != tenant or ctx.user.database != database:
                    raise ValueError("Tenant/database đã thay đổi; hãy yêu cầu báo cáo mới.")
                result = generate(ctx, req, lambda n: self.checkpoint(report_id, token, n))
                self.checkpoint(report_id, token, sum(s["count"] for s in result["sections"]))
                artifacts = prepare_artifacts(result, report_id, user_id, req.telegram_binding_id)
                now = time.time()
                with self.session_factory() as db:
                    changed = db.query(ReportJob).filter_by(id=report_id, status="running", lease_token=token).update({"status": "completed", "result_json": json.dumps(result, ensure_ascii=False), "updated_at": now})
                    if not changed:
                        db.rollback()
                        return
                    db.add_all(artifacts)
                    db.commit()
                log.info("Report completed report_id=%s", report_id)
        except LeaseLost:
            return
        except Exception as exc:
            log.warning("Report failed report_id=%s error_type=%s", report_id, type(exc).__name__)
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            # Do not persist raw provider faults, which may contain sensitive data.
            if not isinstance(exc, ValueError | HTTPException):
                message = f"Không thể lấy đầy đủ dữ liệu Odoo ({type(exc).__name__}); kiểm tra kết nối và quyền truy cập."
            with self.session_factory() as db:
                db.query(ReportJob).filter_by(id=report_id, status="running", lease_token=token).update({"status": "failed", "error": str(message), "updated_at": time.time()})
                db.commit()

    def deliver(self, report_id):
        token = secrets.token_hex(16)
        with self.session_factory() as db:
            changed = db.query(ReportJob).filter_by(id=report_id, delivery_status="pending").update({"delivery_status": "sending", "lease_token": token, "lease_until": time.time() + 120})
            db.commit()
            if not changed:
                return
            job = db.get(ReportJob, report_id)
            req = ReportRequest.model_validate_json(job.request_json)
            user_id, cursor = job.user_id, job.delivery_index
            if job.result_json:
                result = json.loads(job.result_json)
                text = f"Báo cáo {report_id} hoàn tất.\n" + chat_summary(result)
                if has_data(result) and result.get("export"):
                    text += "\n" + selection_text(result["export"])
            else:
                text = f"Báo cáo {report_id} thất bại: {job.error}"
            artifacts = selected_artifacts(db, report_id, result) if job.result_json and has_data(result) else []
            artifact_ids = [artifact.id for artifact in artifacts]
            if job.result_json and has_data(result) and not artifact_ids:
                # A cached financial summary must not bypass source authorization
                # just because the file needed to locate its owner has expired.
                job.delivery_status = "expired"
                job.updated_at = time.time()
                db.commit()
                return
        text = plain_text(text)
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)]
        chunks.extend({"artifact_id": artifact_id} for artifact_id in artifact_ids)
        client = TelegramClient(self.settings.TELEGRAM_BOT_TOKEN)
        try:
            if not self.settings.TELEGRAM_BOT_TOKEN:
                raise ValueError("Telegram bot chưa được cấu hình.")
            for index in range(cursor, len(chunks)):
                chunk = chunks[index]
                artifact_id = chunk["artifact_id"] if isinstance(chunk, dict) else None
                revalidate_for_delivery(self.settings, self.session_factory, user_id, req.telegram_user_id,
                                        req.chat_id, req.telegram_binding_id, artifact_id=artifact_id, report_id=report_id if job.result_json else None)
                with self.session_factory() as db:
                    owned = db.query(ReportJob).filter_by(id=report_id, delivery_status="sending", lease_token=token).update({"lease_until": time.time() + 120})
                    db.commit()
                    if not owned:
                        return
                if artifact_id:
                    file = load_artifact(self.session_factory, user_id, artifact_id, req.telegram_binding_id)
                    client.send_document(req.chat_id, file["filename"], file["content"], content_type=file["content_type"])
                else:
                    client.send_message(req.chat_id, chunks[index], parse_mode="")
                with self.session_factory() as db:
                    changed = db.query(ReportJob).filter_by(id=report_id, delivery_status="sending", lease_token=token).update({"delivery_index": index + 1})
                    db.commit()
                    if not changed:
                        return
            state = "delivered"
        except Exception:
            # Includes a crash/timeout around provider acceptance: never resend automatically.
            state = "unknown"
            log.warning("Report delivery withheld/unknown report_id=%s", report_id)
        with self.session_factory() as db:
            db.query(ReportJob).filter_by(id=report_id, delivery_status="sending", lease_token=token).update({"delivery_status": state, "updated_at": time.time()})
            db.commit()
