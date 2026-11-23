"""Internal Workspace API; credentials never leave the Gateway."""

import asyncio
import base64
import json
import mimetypes
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.auth.routes import get_current_user
from app.google.oauth import GoogleAuthRevokedError, GoogleOAuthError
from app.internal.security import verify_agent_secret
from app.models import User
from app.reporting.context import bound_telegram_user
from app.reporting.schemas import TelegramScope
from app.security import decrypt_secret, encrypt_secret
from app.workspace.activity_tracker import ActivityType, WorkspaceActivityTracker
from app.workspace.catalog import overview
from app.workspace.idempotency import compute_payload_hash
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus
from app.workspace.selection_manager import SelectionManager, SwitchLockError
from app.workspace.service import WorkspaceError

router = APIRouter(
    prefix="/internal/v1", tags=["Workspace"], dependencies=[Depends(verify_agent_secret)]
)
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


def _job_result(db, job, key):
    if job.service == "workspace" and job.operation == "plan.read":
        created = job.created_at
        if created and datetime.now(UTC) - created.replace(tzinfo=created.tzinfo or UTC) >= timedelta(hours=24):
            return None
    result = _decrypt_payload(job.result_encrypted, key)
    if isinstance(result, dict) and result.get("format") in {"indexed_drive_v1", "workspace_plan_v1"}:
        from app.workspace.drive_runtime import safe_result

        return safe_result(db, job.user_id, result)
    return result


def _job_payload_hash(payload):
    if isinstance(payload, dict):
        payload = {name: value for name, value in payload.items() if name != "drive_link"}
    return compute_payload_hash(payload)


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
        "drive",
        "index.fetch",
        False,
    ):
        raise HTTPException(422, "Tác vụ lập chỉ mục chỉ hỗ trợ đọc một tệp Drive.")
    ticket_val = payload.ticket or (
        payload.body.get("_ticket") if isinstance(payload.body, dict) else None
    )
    if payload.is_write and effective_action == "read":
        effective_action = "execute" if ticket_val else "prepare"
    tool_plan = payload.service == "workspace" and payload.operation == "plan.read"
    if tool_plan and (
        effective_action != "read" or payload.is_write or not key
        or not payload.body.get("steps") or not 1 <= len(payload.body.get("steps", [])) <= 8
        or len(json.dumps(payload.body).encode()) > 65536
    ):
        raise HTTPException(422, "Kế hoạch nền tối đa 8 bước.")

    with request.app.state.session_factory() as db:
        user = db.query(User).filter_by(id=user_id, active=True).first()
        if not user:
            raise HTTPException(404, detail="User not found or inactive")
        binding = None

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
        }
        current_hash = compute_payload_hash(payload_data)

        existing = (
            db.query(WorkspaceJob).filter_by(user_id=user_id, request_id=payload.request_id).first()
        )
        if existing:
            old_data = _decrypt_payload(existing.payload_encrypted, key)
            if _job_payload_hash(old_data) != current_hash:
                raise HTTPException(
                    409, detail="Conflict: request_id reused with different payload"
                )

            job_id_str = existing.job_id
            if existing.status == WorkspaceJobStatus.COMPLETED.value:
                return {
                    "status": "completed",
                    "job_id": job_id_str,
                    "result": _job_result(db, existing, key),
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
            try:
                pin = SelectionManager.admit(db, user_id)
            except SwitchLockError as exc:
                raise HTTPException(409, detail=f"ACCOUNT_BUSY: {exc.message}") from exc
            indexed = (
                getattr(settings, "DRIVE_INDEX_ENABLED", False)
                and payload.service == "drive"
                and (payload.operation in {"knowledge.search", "index.fetch"})
            )
            if indexed or tool_plan:
                from app.workspace.drive_permissions import current_link, link_payload

                payload_data["drive_link"] = link_payload(current_link(db, user_id, pin.account_id))
                if tool_plan and not payload_data["drive_link"]:
                    raise HTTPException(403, "Liên kết Google chưa hoạt động.")
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
                google_account_id=pin.account_id,
                selection_version=pin.selection_version,
                status=WorkspaceJobStatus.QUEUED.value,
                payload_encrypted=(
                    encrypt_secret(json.dumps(payload_data), key)
                    if indexed or tool_plan
                    else _encrypt_payload(payload_data, key)
                ),
                deadline_at=deadline,
                telegram_chat_id=payload.telegram_chat_id,
                telegram_user_id=payload.telegram_user_id,
            )
            db.add(new_job)
            try:
                WorkspaceActivityTracker.register_activity(
                    db,
                    user_id=user_id,
                    activity_type=ActivityType.TASK_QUEUED,
                    reference_id=job_id_str,
                    google_account_id=pin.account_id,
                    has_outbound_write=payload.is_write,
                    lease_expires_at=deadline,
                )
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = (
                    db.query(WorkspaceJob)
                    .filter_by(user_id=user_id, request_id=payload.request_id)
                    .first()
                )
                if existing is None:
                    raise
                if (
                    _job_payload_hash(_decrypt_payload(existing.payload_encrypted, key))
                    != current_hash
                ):
                    raise HTTPException(
                        409, detail="Conflict: request_id reused with different payload"
                    ) from None
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
                        "result": _job_result(db, j, key),
                    }
                if j.status == WorkspaceJobStatus.WAITING_CONFIRMATION.value:
                    return {
                        "status": "waiting_confirmation",
                        "job_id": j.job_id,
                        "result": _job_result(db, j, key),
                    }
                if j.status == WorkspaceJobStatus.FAILED.value:
                    return {
                        "status": "failed",
                        "job_id": j.job_id,
                        "error_code": j.error_code,
                        "error_message": j.error_message,
                    }
                if j.status in {"unknown_result", "expired", "cancelled"}:
                    return {
                        "status": j.status,
                        "job_id": j.job_id,
                        "error_code": j.error_code,
                        "error_message": j.error_message,
                    }
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
        if job.status == WorkspaceJobStatus.UNKNOWN_RESULT.value and job.operation_id:
            from app.workspace.models import WorkspaceIdempotencyRecord

            evidence = (
                db.query(WorkspaceIdempotencyRecord)
                .filter_by(
                    user_id=user_id,
                    operation_id=job.operation_id,
                    status="completed",
                )
                .first()
            )
            original_payload = _decrypt_payload(job.payload_encrypted, key)
            if (
                evidence
                and evidence.response_json
                and isinstance(original_payload, dict)
                and evidence.payload_hash == compute_payload_hash(original_payload)
            ):
                confirmed_result = json.loads(evidence.response_json)
                job.status = WorkspaceJobStatus.COMPLETED.value
                job.result_encrypted = _encrypt_payload(confirmed_result, key)
                job.error_code = None
                job.error_message = None
                job.callback_delivered_at = None
                job.callback_retry_at = datetime.now(UTC)
                WorkspaceActivityTracker.finish_activity(db, user_id, job.job_id)
                db.commit()
        result = _job_result(db, job, key)
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
        updated = (
            db.query(WorkspaceJob)
            .filter_by(
                job_id=job_id,
                user_id=user_id,
                status=WorkspaceJobStatus.QUEUED.value,
            )
            .update(
                {"status": WorkspaceJobStatus.CANCELLED.value, "completed_at": datetime.now(UTC)}
            )
        )
        if updated:
            WorkspaceActivityTracker.finish_activity(
                db, user_id, job_id, status=WorkspaceJobStatus.CANCELLED.value,
            )
            db.commit()
            return {"status": "cancelled", "job_id": job_id}
        job = db.query(WorkspaceJob).filter_by(job_id=job_id, user_id=user_id).first()
        if not job:
            raise HTTPException(404, detail="Workspace job not found")
        return {
            "status": job.status,
            "job_id": job.job_id,
            "message": f"Job cannot be cancelled in state '{job.status}'",
        }


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


class IndexedDriveSearch(TelegramScope):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=12000)
    file_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")
    terms: list[str] = Field(default_factory=list, max_length=8)


class DriveLinkCheck(BaseModel):
    google_sub: str
    link_version: int


class DriveEvidenceRef(BaseModel):
    file_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    version: str = Field(min_length=1, max_length=64)
    drive_id: str | None = Field(default=None, max_length=255)


class DriveEvidenceCheck(DriveLinkCheck):
    verification: list[DriveEvidenceRef] = Field(default_factory=list, max_length=4)


def _drive_runtime(request):
    settings = request.app.state.settings
    if not settings.DRIVE_INDEX_ENABLED:
        raise HTTPException(404, "Drive index chưa bật.")
    from app.internal.routes import _get_workspace_service
    from app.workspace.drive_runtime import DriveRuntime

    return DriveRuntime(
        settings,
        request.app.state.session_factory,
        _get_workspace_service(request),
        request.app.state.drive_http_client,
    )


@router.post("/users/{user_id}/workspace/drive/search")
async def search_indexed_drive(user_id: int, payload: IndexedDriveSearch, request: Request):
    from time import monotonic

    from app.workspace.drive_runtime import response
    from app.workspace.network_slots import request_deadline

    with request.app.state.session_factory() as db:
        bound_telegram_user(db, user_id, payload)
    runtime = _drive_runtime(request)
    if not payload.query.strip():
        raise HTTPException(422, "Cần câu hỏi để tìm tài liệu Drive.")
    token = request_deadline.set(monotonic() + 26)
    try:
        async with asyncio.timeout(26):
            return await runtime.search(
                user_id,
                payload.query,
                file_id=payload.file_id,
                terms=[term[:80] for term in payload.terms],
            )
    except TimeoutError:
        return response("connection_error", partial=True)
    finally:
        request_deadline.reset(token)


@router.post("/users/{user_id}/workspace/drive/link-check")
async def check_indexed_drive_link(user_id: int, payload: DriveEvidenceCheck, request: Request):
    from app.workspace.drive_permissions import current_link, link_payload

    with request.app.state.session_factory() as db:
        link = current_link(db, user_id)
    if not link or link_payload(link) != payload.model_dump(exclude={"verification"}):
        return {"valid": False}
    if not payload.verification:
        return {"valid": True}
    runtime = _drive_runtime(request)
    try:
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    runtime.permissions.check, link, ref.file_id, ref.version, ref.drive_id
                )
                for ref in payload.verification
            )
        )
    except WorkspaceError as exc:
        raise HTTPException(503, "Chưa thể xác minh quyền Drive.") from exc
    return {"valid": all(results) and await asyncio.to_thread(runtime._link, user_id) == link}


@router.get("/users/{user_id}/workspace/drive/sync-status")
def get_drive_sync_status(user_id: int, request: Request):
    return _drive_runtime(request).sync_status(user_id)


@router.post("/users/{user_id}/workspace/drive/resync", status_code=202)
def resync_drive(user_id: int, request: Request):
    from app.workspace.drive_runtime import RESYNC

    runtime = _drive_runtime(request)
    link = runtime._link(user_id)
    job_id = runtime.enqueue(link, RESYNC, {}) if link else None
    if not job_id:
        raise HTTPException(409, "Liên kết Google đã thay đổi.")
    return {"status": "queued", "job_id": job_id}


@router.post("/users/{user_id}/workspace/drive-knowledge")
def read_drive_knowledge(user_id: int, payload: DriveKnowledgeRequest, request: Request):
    from app.workspace.telemetry import (
        ExecutionTelemetry,
        active_execution,
        measure_stage,
        telemetry_store,
    )

    correlation = request.headers.get("X-Request-ID", "")
    if (
        not correlation
        or len(correlation) > 128
        or not correlation.isascii()
        or not correlation.isprintable()
    ):
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
    from app.internal.routes import _get_odoo_service, _get_workspace_service, _verify_odoo_account

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
    return _get_workspace_service(request)


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
                result["data"]["download_url"] = (
                    request.app.state.settings.PUBLIC_BASE_URL.rstrip("/")
                    + "/workspace/download/"
                    + ticket
                )
            return result
        return instance.prepare(user_id, payload.service, values)
    except WorkspaceError as exc:
        return {"status": exc.code, "message": str(exc)}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.post("/users/{user_id}/workspace/query")
def workspace_query(user_id: int, payload: WorkspaceRequest, request: Request):
    from time import monotonic

    from app.workspace.network_slots import request_deadline

    budget_ms = request.headers.get("X-Workspace-Budget-Ms")
    if budget_ms is None:
        return run_request(user_id, payload, request, "read")
    if not budget_ms.isascii() or not budget_ms.isdecimal() or len(budget_ms) > 5:
        raise HTTPException(400, detail="Invalid Workspace request budget")
    budget = min(30000, int(budget_ms))
    if budget <= 0:
        raise HTTPException(400, detail="Invalid Workspace request budget")
    deadline_token = request_deadline.set(monotonic() + budget / 1000)
    try:
        return run_request(user_id, payload, request, "read")
    finally:
        request_deadline.reset(deadline_token)


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
        return Response(
            base64.b64decode(data["content_base64"]),
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="workspace{extension}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except WorkspaceError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
