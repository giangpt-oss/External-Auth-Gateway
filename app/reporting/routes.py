import json
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.internal.security import verify_agent_secret
from app.reporting.artifacts import load_artifact, revalidate_for_delivery, validate_result_access
from app.reporting.context import authenticated_context
from app.reporting.engine import capability
from app.reporting.export import chat_summary, has_data
from app.reporting.formats import (
    prepare_artifacts,
    select_formats,
    selected_artifacts,
    selection_text,
)
from app.reporting.models import ReportJob
from app.reporting.schemas import ReportDeliveryRequest, ReportRequest, TelegramScope

router = APIRouter(prefix="/internal/v1", tags=["Business Reports"], dependencies=[Depends(verify_agent_secret)])


def enabled(request):
    if not getattr(request.app.state.settings, "ENABLE_REPORTING", False):
        raise HTTPException(503, "Chức năng báo cáo chưa được bật.")


@router.get("/users/{user_id}/odoo/capabilities")
def capabilities(user_id: int, request: Request, scope: TelegramScope = Depends()):
    with authenticated_context(request, user_id, scope) as ctx:
        response = {"status": "OK", "reports": capability(ctx), "company_ids": ctx.company_ids, "default_company_id": ctx.default_company_id,
                "reporting_enabled": getattr(request.app.state.settings, "ENABLE_REPORTING", False),
                "quotation_enabled": getattr(request.app.state.settings, "ENABLE_QUOTATIONS", False),
                "output_formats": ["auto", "docx", "xlsx", "both"]}
        if response["quotation_enabled"]:
            from app.quotations.service import QuotationService
            try:
                response["quotation"] = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).metadata()
            except Exception as exc:
                response["quotation"] = {"available": False, "reason": type(exc).__name__}
        return response


@router.get("/users/{user_id}/reports/lookup")
def lookup_report_filter(user_id: int, request: Request, kind: str, query: str = Query(default="", max_length=100),
                         company_id: int | None = None, scope: TelegramScope = Depends()):
    enabled(request)
    mapping = {"warehouses": "stock.warehouse", "projects": "project.project", "assignees": "res.users", "partners": "res.partner", "products": "product.product", "stages": "crm.stage"}
    if kind not in mapping:
        raise HTTPException(400, "Loại bộ lọc không được hỗ trợ.")
    with authenticated_context(request, user_id, scope) as ctx:
        companies = ctx.selected_companies([company_id] if company_id else [])
        model = mapping[kind]
        fields = ctx.read_fields(model)
        domain = [("name", "ilike", query)] if query else []
        if "company_id" in fields:
            domain += ["|", ("company_id", "=", False), ("company_id", "in", companies)]
        if kind == "assignees" and "company_ids" in fields:
            domain += [("company_ids", "in", companies)]
        return {"status": "OK", "items": ctx.call(model, "search_read", [domain], {"fields": ["id", "name"], "limit": 20, "order": "name asc, id asc"})}


@router.post("/users/{user_id}/reports")
def create_report(user_id: int, body: ReportRequest, request: Request):
    enabled(request)
    with authenticated_context(request, user_id, body) as ctx:
        body.company_ids = ctx.selected_companies(body.company_ids)
        available = capability(ctx, [body.report_type])
        if not available[body.report_type]["available"]:
            raise HTTPException(403, {"code": "REPORT_UNAVAILABLE", "message": "Model chưa cài, thiếu trường hoặc không có quyền đọc.", "capability": available[body.report_type]})
        params = body.model_dump(mode="json")
        now = time.time()
        report_id = "rpt_" + secrets.token_hex(12)
        with request.app.state.session_factory() as db:
            db.add(ReportJob(id=report_id, user_id=user_id, tenant=ctx.user.tenant, database=ctx.user.database,
                             binding_id=body.telegram_binding_id, request_json=json.dumps(params, ensure_ascii=False),
                             status="queued", created_at=now, updated_at=now, delivery_status="pending", delivery_index=0))
            db.commit()
    selection = select_formats(params)
    return {"status": "queued", "report_id": report_id, "export": selection,
            "message": f"Đã nhận báo cáo {report_id}. " + selection_text(selection) + " File được gửi khi có dữ liệu phù hợp."}


def owned_job(db, user_id, report_id, ctx):
    job = db.query(ReportJob).filter_by(id=report_id, user_id=user_id, binding_id=ctx.user.telegram_connection.binding_id,
                                      tenant=ctx.user.tenant, database=ctx.user.database).first()
    if not job:
        raise HTTPException(404, "Không tìm thấy báo cáo trong phiên liên kết hiện tại.")
    ctx.selected_companies(json.loads(job.request_json).get("company_ids"))
    return job


@router.get("/users/{user_id}/reports/{report_id}")
def get_report(user_id: int, report_id: str, request: Request, scope: TelegramScope = Depends()):
    enabled(request)
    with authenticated_context(request, user_id, scope) as ctx, request.app.state.session_factory() as db:
        job = owned_job(db, user_id, report_id, ctx)
        result = json.loads(job.result_json) if job.result_json else None
        if result:
            validate_result_access(ctx, json.loads(job.request_json), result)
            for section in result["sections"]:
                if not ctx.call(section["model"], "check_access_rights", ["read", False]):
                    raise HTTPException(403, "Quyền đọc báo cáo đã bị thu hồi.")
                section.pop("rows", None)
        artifacts = selected_artifacts(db, report_id, result or {})
        message = chat_summary(result) if result else job.error or f"Trạng thái: {job.status}"
        if result and has_data(result):
            message += "\n" + selection_text(result.get("export") or select_formats(result["parameters"]))
        return {"status": job.status, "report_id": job.id, "progress": job.progress, "result": result,
                "message": message,
                "delivery_status": job.delivery_status,
                "attachments": [{"artifact_id": a.id, "filename": a.filename, "content_type": a.content_type} for a in artifacts]}


@router.post("/users/{user_id}/reports/{report_id}/cancel")
def cancel_report(user_id: int, report_id: str, body: TelegramScope, request: Request):
    enabled(request)
    with authenticated_context(request, user_id, body) as ctx, request.app.state.session_factory() as db:
        job = owned_job(db, user_id, report_id, ctx)
        if job.status in ("queued", "running"):
            db.query(ReportJob).filter(ReportJob.id == job.id, ReportJob.status.in_(["queued", "running"])).update({"status": "cancelled", "updated_at": time.time(), "lease_token": None})
            db.commit()
        db.refresh(job)
        return {"status": job.status, "report_id": job.id, "message": f"Trạng thái báo cáo: {job.status}"}


@router.post("/users/{user_id}/reports/{report_id}/deliver")
def redeliver_report(user_id: int, report_id: str, body: ReportDeliveryRequest, request: Request):
    """Explicit resend or format conversion from the same authorized saved dataset."""
    enabled(request)
    with authenticated_context(request, user_id, body) as ctx, request.app.state.session_factory() as db:
        job = owned_job(db, user_id, report_id, ctx)
        if job.status != "completed" or not job.result_json:
            raise HTTPException(409, "Báo cáo chưa hoàn tất; chưa thể gửi file.")
        result = json.loads(job.result_json)
        validate_result_access(ctx, json.loads(job.request_json), result)
        if not has_data(result):
            return {"status": "no_data", "report_id": report_id, "message": chat_summary(result)}
        if job.delivery_status in ("pending", "sending", "preparing"):
            return {"status": "in_progress", "report_id": report_id,
                    "message": "Báo cáo đang được chuẩn bị hoặc gửi. Hãy chờ hoàn tất trước khi đổi định dạng."}
        if not selected_artifacts(db, report_id, result):
            raise HTTPException(410, "File đã hết hạn sau 24 giờ; hãy tạo báo cáo mới.")
        previous_delivery = job.delivery_status
        token = secrets.token_hex(16)
        changed = db.query(ReportJob).filter(ReportJob.id == report_id,
            ReportJob.delivery_status.notin_(["pending", "sending", "preparing"])).update({
                "delivery_status": "preparing", "lease_token": token, "lease_until": time.time() + 120,
            }, synchronize_session=False)
        db.commit()
        if not changed:
            return {"status": "in_progress", "report_id": report_id, "message": "Báo cáo đang được xử lý ở một yêu cầu khác."}
        try:
            artifacts = []
            new_request = json.loads(job.request_json)
            if body.output_format is not None or body.report_purpose != "auto":
                new_request.update(output_format=body.output_format or "auto", report_purpose=body.report_purpose)
                result["parameters"].update(output_format=new_request["output_format"], report_purpose=body.report_purpose)
                artifacts = prepare_artifacts(result, report_id, user_id, body.telegram_binding_id)
            changed = db.query(ReportJob).filter_by(id=report_id, delivery_status="preparing", lease_token=token).update({
                "delivery_status": "pending", "delivery_index": 0, "lease_token": None,
                "updated_at": time.time(), "request_json": json.dumps(new_request, ensure_ascii=False),
                "result_json": json.dumps(result, ensure_ascii=False),
            }, synchronize_session=False)
            if not changed:
                db.rollback()
                raise HTTPException(409, "Phiên chuẩn bị file đã hết hạn. Vui lòng kiểm tra lại trạng thái báo cáo.")
            db.add_all(artifacts)
            db.commit()
        except Exception as exc:
            db.rollback()
            db.query(ReportJob).filter_by(id=report_id, delivery_status="preparing", lease_token=token).update({
                "delivery_status": previous_delivery, "lease_token": None,
            }, synchronize_session=False)
            db.commit()
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(503, "Không thể chuẩn bị định dạng yêu cầu; chưa gửi file khác thay thế.") from exc
        selection = result.get("export") or select_formats(new_request)
        return {"status": "queued", "report_id": report_id, "export": selection,
                "message": "Đã nhận yêu cầu gửi lại báo cáo. " + selection_text(selection)}


@router.get("/users/{user_id}/artifacts/{artifact_id}")
def download_artifact(user_id: int, artifact_id: str, request: Request, scope: TelegramScope = Depends()):
    enabled(request)
    state = request.app.state
    revalidate_for_delivery(state.settings, state.session_factory, user_id, scope.telegram_user_id, scope.chat_id,
                            scope.telegram_binding_id, artifact_id=artifact_id)
    artifact = load_artifact(state.session_factory, user_id, artifact_id, scope.telegram_binding_id)
    return Response(content=artifact["content"], media_type=artifact["content_type"],
                    headers={"Content-Disposition": f'attachment; filename="{artifact["filename"]}"'})
