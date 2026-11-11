from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TelegramScope(BaseModel):
    telegram_user_id: int
    chat_id: int
    telegram_binding_id: str = Field(min_length=1)


class ReportRequest(TelegramScope):
    model_config = ConfigDict(extra='forbid')
    report_type: Literal["revenue", "sales", "commercial_summary", "inventory", "tasks", "quotations", "purchases", "purchase_rfqs", "crm", "contacts", "transfers"]
    date_from: date | None = None
    date_to: date | None = None
    company_ids: list[int] = Field(default_factory=list, max_length=30)
    partner_id: int | None = Field(default=None, gt=0)
    warehouse_id: int | None = Field(default=None, gt=0)
    project_id: int | None = Field(default=None, gt=0)
    assignee_id: int | None = Field(default=None, gt=0)
    product_id: int | None = Field(default=None, gt=0)
    stage_id: int | None = Field(default=None, gt=0)
    record_scope: Literal["visible", "mine"] = "visible"
    crm_type: Literal["lead", "opportunity", "all"] = "opportunity"
    crm_status: Literal["active", "won", "archived", "all"] = "active"
    contact_kind: Literal["all", "companies", "individuals", "customers", "suppliers"] = "all"
    transfer_type: Literal["all", "incoming", "outgoing", "internal"] = "all"
    task_date_field: Literal["date_deadline", "create_date"] = "date_deadline"
    group_by: Literal["customer", "supplier", "salesperson", "project", "location", "stage", "company", "operation"] | None = None
    timezone: str = "Asia/Bangkok"
    output_format: Literal["auto", "docx", "xlsx", "both", "chat_and_xlsx"] = "auto"
    report_purpose: Literal["auto", "narrative", "data_analysis", "presentation", "combined"] = "auto"

    @model_validator(mode="after")
    def validate_period(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Múi giờ báo cáo không hợp lệ.") from exc
        if self.record_scope == "mine" and self.assignee_id:
            raise ValueError("Chọn phạm vi của tôi hoặc người phụ trách cụ thể, không dùng đồng thời.")
        if self.record_scope == "mine" and self.report_type in ("inventory", "revenue", "commercial_summary", "tasks"):
            raise ValueError("Phạm vi của tôi chưa được định nghĩa cho loại báo cáo này.")
        if bool(self.date_from) != bool(self.date_to):
            raise ValueError("Cần cung cấp cả ngày bắt đầu và ngày kết thúc.")
        if self.report_type not in ("inventory", "crm", "contacts"):
            if not self.date_from or not self.date_to:
                raise ValueError("Cần ngày bắt đầu và ngày kết thúc báo cáo.")
            if self.date_from > self.date_to:
                raise ValueError("Ngày kết thúc phải từ ngày bắt đầu trở đi.")
        elif self.report_type == "inventory" and (self.date_from or self.date_to):
            raise ValueError("Chỉ hỗ trợ tồn kho hiện tại, không hỗ trợ tồn kho lịch sử.")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Ngày kết thúc phải từ ngày bắt đầu trở đi.")
        if self.warehouse_id and self.report_type not in ("inventory", "transfers"):
            raise ValueError("Bộ lọc kho chỉ dùng cho báo cáo tồn kho hoặc phiếu kho.")
        if self.project_id and self.report_type != "tasks":
            raise ValueError("Bộ lọc dự án chỉ dùng cho báo cáo công việc.")
        if self.partner_id and self.report_type not in ("revenue", "sales", "commercial_summary", "quotations", "purchases", "purchase_rfqs", "crm", "contacts", "transfers"):
            raise ValueError("Bộ lọc đối tác không phù hợp với loại báo cáo này.")
        if self.assignee_id and self.report_type not in ("sales", "tasks", "quotations", "purchases", "purchase_rfqs", "crm", "contacts", "transfers"):
            raise ValueError("Bộ lọc người phụ trách không phù hợp với loại báo cáo này.")
        if self.product_id and self.report_type not in ("inventory", "transfers"):
            raise ValueError("Bộ lọc sản phẩm chỉ dùng cho tồn kho hoặc phiếu kho.")
        if self.stage_id and self.report_type != "crm":
            raise ValueError("Bộ lọc giai đoạn chỉ dùng cho CRM.")
        if self.crm_type != "opportunity" and self.report_type != "crm":
            raise ValueError("Phân loại lead/cơ hội chỉ dùng cho CRM.")
        if self.crm_status != "active" and self.report_type != "crm":
            raise ValueError("Trạng thái CRM chỉ dùng cho báo cáo CRM.")
        if self.contact_kind != "all" and self.report_type != "contacts":
            raise ValueError("Phân loại liên hệ chỉ dùng cho báo cáo liên hệ.")
        if self.transfer_type != "all" and self.report_type != "transfers":
            raise ValueError("Loại phiếu kho chỉ dùng cho báo cáo phiếu kho.")
        valid_groups = {"revenue": {"customer"}, "sales": {"customer", "salesperson"},
                        "commercial_summary": {"customer"}, "inventory": {"location"}, "tasks": {"project"},
                        "quotations": {"customer", "salesperson"}, "purchases": {"supplier", "salesperson"},
                        "purchase_rfqs": {"supplier", "salesperson"}, "crm": {"stage"}, "contacts": {"company"},
                        "transfers": {"operation"}}
        if self.group_by and self.group_by not in valid_groups[self.report_type]:
            raise ValueError("Cách nhóm không phù hợp với loại báo cáo.")
        return self


class ReportDeliveryRequest(TelegramScope):
    model_config = ConfigDict(extra='forbid')
    output_format: Literal["auto", "docx", "xlsx", "both"] | None = None
    report_purpose: Literal["auto", "narrative", "data_analysis", "presentation", "combined"] = "auto"
