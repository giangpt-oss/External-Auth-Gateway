"""Internal Workspace API; credentials never leave the Gateway."""
import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
import mimetypes
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.routes import get_current_user
from app.google.oauth import GoogleAuthRevokedError, GoogleOAuthError
from app.internal.security import verify_agent_secret
from app.models import User
from app.reporting.context import bound_telegram_user
from app.reporting.schemas import TelegramScope
from app.security import decrypt_secret, encrypt_secret
import uuid
from sqlalchemy.exc import IntegrityError
from app.workspace.catalog import overview
from app.workspace.idempotency import compute_payload_hash
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus
from app.workspace.service import WorkspaceError, WorkspaceService

router = APIRouter(prefix="/internal/v1", tags=["Workspace"], dependencies=[Depends(verify_agent_secret)])
portal_router = APIRouter(tags=["Workspace downloads"])


def _encrypt_payload(data: Any, key: str | None) -> str:
    raw = json.dumps(data)
    if key:
        try:
            return encrypt_secret(raw, key)
        except Exception:
            pass
    return raw


def _decrypt_payload(encrypted: str | None, key: str | None) -> Any:
    if not encrypted:
        return None
    if key:
        try:
            return json.loads(decrypt_secret(encrypted, key))
        except Exception:
            pass
    try:
        return json.loads(encrypted)
    except Exception:
        return encrypted


class WorkspaceJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    request_id: str
    operation_id: str | None = None
    action: str = Field(default="read")  # read, prepare, execute
    service: str
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    media: dict[str, Any] | None = None
    ticket: str | None = Field(default=None, alias="_ticket")
    is_write: bool = False
    protocol: str = "auto"
    deadline_seconds: int = Field(default=120, ge=1, le=300)
    wait_seconds: float = Field(default=2.0, ge=0, le=2.0)
    telegram_chat_id: int | None = None
    telegram_user_id: int | None = None
    telegram_binding_id: str | None = None


@router.post("/users/{user_id}/workspace/jobs")
async def create_workspace_job(user_id: int, payload: WorkspaceJobCreateRequest, request: Request):
    settings = request.app.state.settings
    key = getattr(settings, "ENCRYPTION_KEY", None)

    # Determine effective action
    effective_action = payload.action
    if effective_action not in {"read", "prepare", "execute", "index"}:
        raise HTTPException(422, "Workspace action không hợp lệ.")
    if effective_action == "index" and (payload.service, payload.operation, payload.is_write) != (
        "drive", "index.fetch", False,
    ):
        raise HTTPException(422, "Tác vụ lập chỉ mục chỉ hỗ trợ đọc một tệp Drive.")
    ticket_val = payload.ticket or (payload.body.get("_ticket") if isinstance(payload.body, dict) else None)
    if payload.is_write and effective_action == "read":
        effective_action = "execute" if ticket_val else "prepare"

    with request.app.state.session_factory() as db:
        user = db.query(User).filter_by(id=user_id, active=True).first()
        if not user:
            raise HTTPException(404, detail="User not found or inactive")
        binding = None
        if payload.telegram_user_id is not None or payload.telegram_chat_id is not None:
            connection = user.telegram_connection
            if not connection or connection.status != "connected" or not connection.binding_id or (
                connection.telegram_user_id != payload.telegram_user_id
                or connection.private_chat_id != payload.telegram_chat_id
                or (payload.telegram_binding_id is not None and connection.binding_id != payload.telegram_binding_id)
            ):
                raise HTTPException(403, "RECONNECT_REQUIRED: Liên kết Telegram đã thay đổi.")
            binding = connection.binding_id

        payload_data = {
            "action": effective_action,
            "service": payload.service,
            "operation": payload.operation,
            "parameters": payload.parameters,
            "body": payload.body,
            "media": payload.media,
            "is_write": payload.is_write,
            "_ticket": ticket_val,
            "operation_id": payload.operation_id,
            **({"telegram_binding_id": binding, "telegram_user_id": payload.telegram_user_id,
                "telegram_chat_id": payload.telegram_chat_id} if binding else {}),
        }
        current_hash = compute_payload_hash(payload_data)

        existing = db.query(WorkspaceJob).filter_by(user_id=user_id, request_id=payload.request_id).first()
        if existing:
            old_data = _decrypt_payload(existing.payload_encrypted, key)
            if compute_payload_hash(old_data) != current_hash:
                raise HTTPException(409, detail="Conflict: request_id reused with different payload")

            job_id_str = existing.job_id
            if existing.status == WorkspaceJobStatus.COMPLETED.value:
                return {
                    "status": "completed",
                    "job_id": job_id_str,
                    "result": _decrypt_payload(existing.result_encrypted, key),
                }
            if existing.status == WorkspaceJobStatus.WAITING_CONFIRMATION.value:
                return {
                    "status": "waiting_confirmation",
                    "job_id": job_id_str,
                    "result": _decrypt_payload(existing.result_encrypted, key),
                }
            if existing.status == WorkspaceJobStatus.FAILED.value:
                return {
                    "status": "failed",
                    "job_id": job_id_str,
                    "error_code": existing.error_code,
                    "error_message": existing.error_message,
                }
            target_status = existing.status
        else:
            deadline = datetime.now(UTC) + timedelta(seconds=payload.deadline_seconds)
            job_id_str = f"job_{uuid.uuid4().hex[:16]}"
            new_job = WorkspaceJob(
                user_id=user_id,
                job_id=job_id_str,
                request_id=payload.request_id,
                operation_id=payload.operation_id,
                action=effective_action,
                service=payload.service,
                operation=payload.operation,
                protocol=payload.protocol,
                is_write=payload.is_write,
                status=WorkspaceJobStatus.QUEUED.value,
                payload_encrypted=_encrypt_payload(payload_data, key),
                deadline_at=deadline,
                telegram_chat_id=payload.telegram_chat_id,
                telegram_user_id=payload.telegram_user_id,
            )
            db.add(new_job)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.query(WorkspaceJob).filter_by(user_id=user_id, request_id=payload.request_id).first()
                if existing is None:
                    raise
                if compute_payload_hash(_decrypt_payload(existing.payload_encrypted, key)) != current_hash:
                    raise HTTPException(409, detail="Conflict: request_id reused with different payload") from None
                job_id_str = existing.job_id
                target_status = existing.status
            else:
                target_status = WorkspaceJobStatus.QUEUED.value

    deadline_wait = asyncio.get_event_loop().time() + payload.wait_seconds
    while asyncio.get_event_loop().time() < deadline_wait:
        await asyncio.sleep(0.02)
        with request.app.state.session_factory() as db:
            j = db.query(WorkspaceJob).filter_by(job_id=job_id_str).first()
            if j:
                if j.status == WorkspaceJobStatus.COMPLETED.value:
                    return {
                        "status": "completed",
                        "job_id": j.job_id,
                        "result": _decrypt_payload(j.result_encrypted, key),
                    }
                if j.status == WorkspaceJobStatus.WAITING_CONFIRMATION.value:
                    return {
                        "status": "waiting_confirmation",
                        "job_id": j.job_id,
                        "result": _decrypt_payload(j.result_encrypted, key),
                    }
                if j.status == WorkspaceJobStatus.FAILED.value:
                    return {
                        "status": "failed",
                        "job_id": j.job_id,
                        "error_code": j.error_code,
                        "error_message": j.error_message,
                    }
                if j.status in {"unknown_result", "expired", "cancelled"}:
                    return {"status": j.status, "job_id": j.job_id,
                            "error_code": j.error_code, "error_message": j.error_message}
                target_status = j.status

    return JSONResponse(
        status_code=202,
        content={"status": target_status, "job_id": job_id_str, "retry_after_seconds": 2},
    )


@router.get("/users/{user_id}/workspace/jobs/{job_id}")
def get_workspace_job(user_id: int, job_id: str, request: Request):
    settings = request.app.state.settings
    key = getattr(settings, "ENCRYPTION_KEY", None)
    with request.app.state.session_factory() as db:
        job = db.query(WorkspaceJob).filter_by(job_id=job_id, user_id=user_id).first()
        if not job:
            raise HTTPException(404, detail="Workspace job not found")
        result = _decrypt_payload(job.result_encrypted, key) if job.result_encrypted else None
        return {
            "status": job.status,
            "job_id": job.job_id,
            "service": job.service,
            "operation": job.operation,
            "is_write": job.is_write,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "result": result,
        }


@router.post("/users/{user_id}/workspace/jobs/{job_id}/cancel")
def cancel_workspace_job(user_id: int, job_id: str, request: Request):
    with request.app.state.session_factory() as db:
        updated = db.query(WorkspaceJob).filter_by(
            job_id=job_id, user_id=user_id, status=WorkspaceJobStatus.QUEUED.value,
        ).update({"status": WorkspaceJobStatus.CANCELLED.value, "completed_at": datetime.now(UTC)})
        if updated:
            db.commit()
            return {"status": "cancelled", "job_id": job_id}
        job = db.query(WorkspaceJob).filter_by(job_id=job_id, user_id=user_id).first()
        if not job:
            raise HTTPException(404, detail="Workspace job not found")
        return {"status": job.status, "job_id": job.job_id, "message": f"Job cannot be cancelled in state '{job.status}'"}



class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str
    method: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    media: dict[str, Any] | None = None
    mail: dict[str, Any] | None = None


class DriveKnowledgeRequest(TelegramScope):
    model_config = ConfigDict(extra="forbid")
    terms: list[str] = Field(default_factory=list, max_length=8)
    file_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")


@router.post("/users/{user_id}/workspace/drive-knowledge")
def read_drive_knowledge(user_id: int, payload: DriveKnowledgeRequest, request: Request):
    from app.workspace.telemetry import ExecutionTelemetry, active_execution, measure_stage, telemetry_store

    correlation = request.headers.get("X-Request-ID", "")
    if not correlation or len(correlation) > 128 or not correlation.isascii() or not correlation.isprintable():
        correlation = uuid.uuid4().hex
    trace = ExecutionTelemetry(request_id=correlation, service="drive", operation="knowledge")
    token = active_execution.set(trace)
    try:
        with measure_stage("permission_check_ms"):
            with request.app.state.session_factory() as db:
                bound_telegram_user(db, user_id, payload)
        result = _read_drive_documents(user_id, payload, request)
        trace.success = result.get("status") == "OK"
        if not trace.success:
            trace.error_code = result.get("status")
        return result
    except Exception:
        trace.success = False
        raise
    finally:
        active_execution.reset(token)
        telemetry_store.record(trace)


def _read_drive_documents(user_id: int, payload: DriveKnowledgeRequest, request: Request):
    from app.workspace.drive_knowledge import drive_documents

    instance = service_for(request, user_id)
    # The REST adapter enforces a byte limit and supports binary exports.
    instance.use_mcp = False
    try:
        terms = [term.strip()[:80] for term in payload.terms if term.strip()]
        return drive_documents(instance, user_id, terms, payload.file_id)
    except WorkspaceError as exc:
        return {"status": exc.code, "message": str(exc)}
    except GoogleAuthRevokedError:
        return {"status": "RECONNECT_REQUIRED"}
    except GoogleOAuthError:
        return {"status": "SERVICE_ERROR"}


def service_for(request, user_id):
    from app.internal.routes import _get_google_client, _get_odoo_service, _verify_odoo_account
    settings = request.app.state.settings
    with request.app.state.session_factory() as db:
        user = db.query(User).filter_by(id=user_id, active=True).first()
        if not user:
            raise HTTPException(404, detail="User not found or inactive")
        secret = ""
        if user.credential and settings.ENCRYPTION_KEY:
            try:
                secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            except Exception:
                secret = ""
        _verify_odoo_account(_get_odoo_service(request, user), user.odoo_uid, secret)
    return WorkspaceService(
        _get_google_client(request), settings.ENCRYPTION_KEY,
        use_mcp=settings.GOOGLE_WORKSPACE_MCP_ENABLED,
    )


@router.get("/workspace/metrics")
def get_workspace_metrics(request: Request):
    from app.workspace.metrics import workspace_metrics
    from app.workspace.telemetry import telemetry_store
    with request.app.state.session_factory() as db:
        metrics_data = workspace_metrics.get_metrics(db)
        metrics_data["telemetry_stages"] = telemetry_store.get_summary()
        return {
            "status": "OK",
            "metrics": metrics_data,
        }


@router.get("/workspace/catalog")
def workspace_catalog(service: str | None = None, method: str | None = None):
    try:
        return {"status": "OK", "catalog": overview(service, method)}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


def run_request(user_id, payload, request, kind):
    instance = service_for(request, user_id)
    values = payload.model_dump(exclude={"service"}, exclude_none=True)
    try:
        if kind == "read":
            result = instance.read(user_id, payload.service, values)
            data = result.get("data")
            if isinstance(data, dict) and "content_base64" in data:
                ticket = instance.download_ticket(user_id, payload.service, values)
                result["data"] = {k: v for k, v in data.items() if k != "content_base64"}
                result["data"]["download_url"] = request.app.state.settings.PUBLIC_BASE_URL.rstrip("/") + "/workspace/download/" + ticket
            return result
        return instance.prepare(user_id, payload.service, values)
    except WorkspaceError as exc:
        return {"status": exc.code, "message": str(exc)}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.post("/users/{user_id}/workspace/query")
def workspace_query(user_id: int, payload: WorkspaceRequest, request: Request):
    return run_request(user_id, payload, request, "read")


@router.post("/users/{user_id}/workspace/preview")
def workspace_preview(user_id: int, payload: WorkspaceRequest, request: Request):
    return run_request(user_id, payload, request, "write")


@portal_router.get("/workspace/download/{ticket}")
def download_workspace_file(ticket: str, request: Request, user=Depends(get_current_user)):
    try:
        data = service_for(request, user.id).download(user.id, ticket)["data"]
        if not isinstance(data, dict) or "content_base64" not in data:
            raise HTTPException(400, detail="This request is not a binary download")
        mime = data.get("mime_type", "application/octet-stream").split(";")[0]
        extension = mimetypes.guess_extension(mime) or ".bin"
        return Response(base64.b64decode(data["content_base64"]), media_type=mime, headers={"Content-Disposition": f'attachment; filename="workspace{extension}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except WorkspaceError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
