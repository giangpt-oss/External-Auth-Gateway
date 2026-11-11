import json
import time
from types import SimpleNamespace

from fastapi import HTTPException

from app.reporting.context import authenticated_context
from app.reporting.engine import MODELS, capability
from app.reporting.models import ReportArtifact, ReportJob
from app.reporting.schemas import TelegramScope


def load_artifact(session_factory, user_id, artifact_id, binding_id):
    with session_factory() as db:
        artifact = db.query(ReportArtifact).filter_by(id=artifact_id, user_id=user_id, binding_id=binding_id).first()
        if not artifact or artifact.expires_at <= time.time():
            raise HTTPException(404, "File không tồn tại hoặc đã hết hạn sau 24 giờ.")
        return {"filename": artifact.filename, "content_type": artifact.content_type, "content": artifact.content}


def revalidate_for_delivery(settings, session_factory, user_id, telegram_user_id, chat_id, binding_id, artifact_id=None, report_id=None):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=session_factory)))
    binding = TelegramScope(telegram_user_id=telegram_user_id, chat_id=chat_id, telegram_binding_id=binding_id)
    with authenticated_context(request, user_id, binding) as ctx:
        if not artifact_id and not report_id:
            return
        with session_factory() as db:
            if artifact_id:
                artifact = db.query(ReportArtifact).filter_by(id=artifact_id, user_id=user_id, binding_id=binding_id).first()
                if not artifact or artifact.expires_at <= time.time():
                    raise HTTPException(404, "File không tồn tại hoặc đã hết hạn sau 24 giờ.")
                report_id = artifact.report_id
            job = db.query(ReportJob).filter_by(id=report_id, user_id=user_id, binding_id=binding_id).first()
            if not job or job.tenant != ctx.user.tenant or job.database != ctx.user.database:
                raise HTTPException(403, "Phạm vi dữ liệu đã thay đổi.")
            params = json.loads(job.request_json)
            result = json.loads(job.result_json) if job.result_json else None
        validate_result_access(ctx, params, result)


def validate_result_access(ctx, params, result):
    companies = ctx.selected_companies(params.get("company_ids"))
    kinds = ["revenue", "sales"] if params["report_type"] == "commercial_summary" else [params["report_type"]]
    for kind in kinds:
        if not ctx.call(MODELS[kind][0], "check_access_rights", ["read", False]):
            raise HTTPException(403, "Quyền đọc báo cáo đã bị thu hồi.")
        if not set(MODELS[kind][1]).issubset(ctx.read_fields(MODELS[kind][0])):
            raise HTTPException(403, "Quyền đọc trường dữ liệu đã thay đổi; hãy lấy lại báo cáo.")
    if "crm" in kinds and not capability(ctx, ["crm"])["crm"]["available"]:
        raise HTTPException(403, "Quyền đọc tiền tệ hoặc định nghĩa giai đoạn CRM đã thay đổi.")
    for section in (result or {}).get("sections", []):
        source_fields = section.get("source_fields", MODELS[section["type"]][1])
        if not set(source_fields).issubset(ctx.read_fields(section["model"])):
            raise HTTPException(403, "Quyền đọc trường nguồn đã thay đổi; hãy lấy lại báo cáo.")
        if section["type"] == "crm":
            stage_ids = sorted({row["stage_id"][0] for row in section["rows"] if row.get("stage_id")})
            for offset in range(0, len(stage_ids), 500):
                batch = stage_ids[offset:offset + 500]
                if ctx.call("crm.stage", "search_count", [[("id", "in", batch)]]) != len(batch):
                    raise HTTPException(403, "Phạm vi truy cập giai đoạn CRM đã thay đổi.")
        ids = sorted({source_id for row in section["rows"] for source_id in
                      (row.get("source_ids") or [row["id"]])})
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            domain = [("id", "in", batch)]
            if section["type"] in {"crm", "contacts"}:
                domain += ["|", ("company_id", "=", False), ("company_id", "in", companies)]
            else:
                domain += [("company_id", "in", companies)]
            options = {"context": {"active_test": False}} if section["type"] == "crm" and params.get("crm_status") in {"archived", "all"} else {}
            if ctx.call(section["model"], "search_count", [domain], options) != len(batch):
                raise HTTPException(403, "Quyền đọc chứng từ đã thay đổi hoặc chứng từ đã bị xóa; hãy lấy lại báo cáo.")
