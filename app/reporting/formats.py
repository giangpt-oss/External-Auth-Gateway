"""Validated format selection and rendering independent of model interpretation."""
import secrets
import time

from app.reporting.export import export_xlsx, has_data
from app.reporting.models import ReportArtifact

MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
FORMAT_LABELS = {"docx": "Word (.docx)", "xlsx": "Excel (.xlsx)"}


def select_formats(parameters: dict) -> dict:
    requested = parameters.get("output_format", "auto")
    purpose = parameters.get("report_purpose", "auto")
    if requested == "chat_and_xlsx":  # Existing durable jobs retain their original contract.
        requested = "xlsx"
    if requested not in {"auto", "docx", "xlsx", "both"}:
        raise ValueError("Chỉ hỗ trợ tự chọn, Word, Excel hoặc cả hai định dạng.")
    if purpose not in {"auto", "narrative", "data_analysis", "presentation", "combined"}:
        raise ValueError("Mục đích báo cáo không hợp lệ.")
    if requested != "auto":
        formats = ["docx", "xlsx"] if requested == "both" else [requested]
        reason = "Theo định dạng bạn yêu cầu."
    elif purpose == "combined":
        formats = ["docx", "xlsx"]
        reason = "Word để trình bày kết quả, Excel để đối chiếu đầy đủ dữ liệu."
    elif purpose in {"narrative", "presentation"}:
        formats = ["docx"]
        reason = "Word phù hợp để đọc phần tổng quan, nhận xét và kết luận."
    elif purpose == "data_analysis" or parameters.get("report_type") in {"inventory", "contacts", "transfers"}:
        formats = ["xlsx"]
        reason = "Excel phù hợp cho bảng chi tiết cần lọc, tính toán và đối chiếu."
    else:
        formats = ["docx"]
        reason = "Word phù hợp cho báo cáo tổng hợp có số liệu và nhận xét."
    return {"formats": formats, "reason": reason}


def selection_text(selection: dict) -> str:
    labels = ", ".join(FORMAT_LABELS[f] for f in selection["formats"])
    return f"Định dạng: {labels}. {selection['reason']}"


def prepare_artifacts(result: dict, report_id: str, user_id: int, binding_id: str) -> list:
    selection = select_formats(result["parameters"])
    artifacts = []
    if has_data(result):
        for output_format in selection["formats"]:
            if output_format == "docx":
                from app.reporting.word_export import export_docx
                content = export_docx(result)
            else:
                content = export_xlsx(result)
            if not content or len(content) > 45 * 1024 * 1024:
                raise ValueError("Không thể gửi file rỗng hoặc vượt giới hạn 45 MB; hãy thu hẹp phạm vi báo cáo.")
            artifacts.append(ReportArtifact(
                id="art_" + secrets.token_hex(12), report_id=report_id, user_id=user_id,
                binding_id=binding_id, filename=f"{report_id}.{output_format}",
                content_type=MIME_TYPES[output_format], content=content, expires_at=time.time() + 86400,
            ))
    result["export"] = {**selection, "artifact_ids": [artifact.id for artifact in artifacts]}
    return artifacts


def selected_artifacts(db, report_id: str, result: dict) -> list:
    query = db.query(ReportArtifact).filter(ReportArtifact.report_id == report_id,
                                           ReportArtifact.expires_at > time.time())
    export = result.get("export") or {}
    if "artifact_ids" in export:
        ids = export["artifact_ids"]
        if not ids:
            return []
        found = {a.id: a for a in query.filter(ReportArtifact.id.in_(ids)).all()}
        # Never send an incomplete selection, e.g. Excel without the requested Word file.
        return [found[i] for i in ids] if all(i in found for i in ids) else []
    return query.order_by(ReportArtifact.id).all()  # Older jobs have a single XLSX artifact.
