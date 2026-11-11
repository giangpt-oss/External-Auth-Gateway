"""User-facing report presentation; calculations remain in the reporting engine."""
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

from app.reporting.engine import LABELS, label

HEADERS = {
    "id": "ID chứng từ", "name": "Chứng từ / Công việc", "partner_id": "Khách hàng",
    "invoice_date": "Ngày hóa đơn", "date_order": "Ngày đơn bán", "move_type": "Loại chứng từ",
    "state": "Trạng thái", "user_id": "Nhân viên phụ trách", "company_id": "Công ty",
    "currency_id": "Tiền tệ", "amount_untaxed": "Giá trị chứng từ trước thuế",
    "amount_tax": "Thuế chứng từ", "amount_total": "Tổng chứng từ",
    "report_sign": "Hệ số cộng / trừ", "write_date": "Cập nhật lúc",
    "net_untaxed": "Giá trị thuần trước thuế", "net_tax": "Thuế thuần", "net_total": "Tổng thuần",
    "company": "Công ty", "currency": "Tiền tệ", "group": "Nhóm khách hàng / Nhân viên",
    "count": "Số chứng từ", "refund_count": "Số hoàn tiền", "untaxed": "Trước thuế",
    "tax": "Thuế", "total": "Tổng tiền", "product": "Sản phẩm", "sku": "Mã sản phẩm",
    "location": "Vị trí kho", "uom": "Đơn vị tính", "quantity": "Tồn thực tế",
    "reserved": "Đã giữ", "available": "Khả dụng", "source_ids": "ID tồn kho nguồn",
    "project_id": "Dự án", "stage_id": "Giai đoạn", "date_deadline": "Hạn chót",
    "create_date": "Ngày tạo", "assignees": "Người phụ trách", "report_closed": "Đã đóng",
    "report_overdue": "Quá hạn", "project": "Dự án", "stage": "Giai đoạn",
    "total_tasks": "Tổng công việc", "closed_current": "Đã đóng hiện tại",
    "overdue_current": "Quá hạn hiện tại", "product_location_count": "Số dòng sản phẩm / vị trí",
    "source_quant_count": "Số bản ghi tồn kho nguồn",
    "date_approve": "Ngày xác nhận đơn mua", "date_done": "Ngày hoàn tất điều chuyển",
    "scheduled_date": "Ngày dự kiến", "origin": "Chứng từ nguồn", "validity_date": "Hết hiệu lực",
    "picking_type_code": "Nghiệp vụ kho", "operation_type": "Nghiệp vụ kho", "transfer_type": "Nghiệp vụ kho",
    "location_id": "Vị trí nguồn", "location_dest_id": "Vị trí đích",
    "expected_revenue": "Giá trị kỳ vọng", "probability": "Xác suất (%)",
    "team_id": "Nhóm bán hàng", "type": "Loại", "active": "Đang hoạt động",
    "report_won": "Đã thắng theo giai đoạn", "won_count": "Số đã thắng", "archived_count": "Số đã lưu trữ",
    "unknown_company_type": "Chưa xác định doanh nghiệp / cá nhân",
    "total_opportunities": "Tổng cơ hội", "total_leads": "Tổng lead", "total_crm_records": "Tổng bản ghi CRM",
    "lead_count": "Số lead", "opportunity_count": "Số cơ hội",
    "total_won": "Tổng đã thắng", "total_archived": "Tổng đã lưu trữ",
    "total_contacts": "Tổng liên hệ", "companies": "Doanh nghiệp", "individuals": "Cá nhân",
    "missing_email": "Thiếu email", "missing_phone": "Thiếu điện thoại",
    "is_company": "Là doanh nghiệp", "parent_id": "Công ty liên quan",
    "email": "Email", "phone": "Điện thoại", "customer_rank": "Hạng khách hàng",
    "related_company": "Tổ chức liên quan", "related_company_id": "ID tổ chức liên quan",
    "supplier_rank": "Hạng nhà cung cấp", "total_transfers": "Tổng phiếu hoàn tất",
    "date_closed": "Ngày đóng", "date_open": "Ngày phân công",
    "job_title": "Chức danh", "department": "Phòng ban", "manager": "Quản lý trực tiếp",
    "done_count": "Đã hoàn thành", "overdue_count": "Quá hạn", "inprogress_count": "Đang thực hiện",
    "done_rate": "Tỷ lệ hoàn thành", "quotes_count": "Số báo giá", "quotes_value": "Giá trị báo giá",
    "confirmed_count": "Số đơn hoàn tất", "confirmed_value": "Doanh số thực tế",
    "res_name": "Tài liệu liên quan", "activity_type_id": "Loại hoạt động", "summary": "Tóm tắt / Nội dung",
}
NUMBERS = {"untaxed", "tax", "total", "quantity", "reserved", "available", "net_untaxed", "net_tax", "net_total", "expected_revenue", "probability"}
DATES = {"invoice_date", "date_deadline", "date_order", "create_date", "write_date", "date_approve", "date_done", "scheduled_date", "validity_date", "date_closed", "date_open"}
HIDDEN = {"is_total", "is_crm_total", "completion_known", "is_closed", "user_ids"}
M2O = {"partner_id", "company_id", "currency_id", "user_id", "project_id", "stage_id", "team_id", "parent_id", "location_id", "location_dest_id"}
STATES = {"posted": "Đã ghi nhận", "draft": "Nháp", "sale": "Đã xác nhận", "done": "Hoàn tất",
          "cancel": "Đã hủy", "sent": "Đã gửi", "purchase": "Đơn mua đã xác nhận", "to approve": "Chờ duyệt", "incoming": "Nhập kho", "outgoing": "Xuất kho", "internal": "Nội bộ", "lead": "Lead", "opportunity": "Cơ hội", "out_invoice": "Hóa đơn khách hàng", "out_refund": "Hoàn tiền khách hàng"}


def has_data(result):
    return any(section.get("count", 0) > 0 for section in result.get("sections", []))


def period_text(params):
    if params.get("date_from"):
        return f"{params['date_from']} đến {params['date_to']}"
    return "Tồn kho hiện tại" if params.get("report_type") == "inventory" else "Hiện trạng tại thời điểm truy vấn"


def scope_filters(params):
    """Only expose filters meaningful to this report rather than technical defaults."""
    kind = params.get("report_type")
    pairs = [("Phạm vi bản ghi", "Của tôi" if params.get("record_scope") == "mine" else "Tài khoản được phép đọc")]
    for key, title in (("partner_id", "Đối tác ID"), ("warehouse_id", "Kho ID"), ("product_id", "Sản phẩm ID"), ("stage_id", "Giai đoạn ID"), ("project_id", "Dự án ID"), ("assignee_id", "Người phụ trách ID")):
        if params.get(key):
            pairs.append((title, params[key]))
    if kind == "crm":
        pairs.append(("Loại CRM", {"lead": "Lead", "opportunity": "Cơ hội", "all": "Lead và cơ hội"}.get(params.get("crm_type"), "Theo bộ lọc đã chọn")))
        pairs.append(("Trạng thái CRM", {"active": "Đang hoạt động", "won": "Đã thắng theo giai đoạn", "archived": "Đã lưu trữ", "all": "Hoạt động và lưu trữ"}.get(params.get("crm_status"), "Theo bộ lọc đã chọn")))
    if kind == "contacts":
        pairs.append(("Phân loại liên hệ", {"all": "Tất cả liên hệ", "companies": "Doanh nghiệp", "individuals": "Cá nhân", "customers": "Khách hàng", "suppliers": "Nhà cung cấp"}.get(params.get("contact_kind"), "Theo bộ lọc đã chọn")))
    if kind == "transfers":
        pairs.append(("Nghiệp vụ kho", STATES.get(params.get("transfer_type"), "Tất cả nghiệp vụ")))
    return pairs


def cell_value(value):
    if value is None:
        return "Chưa xác định"
    if isinstance(value, dict):
        return "; ".join(f"{k}: {cell_value(v)}" for k, v in value.items())
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return "Có" if value else "Không"
    return value


def append_row(sheet, values):
    sheet.append([cell_value(v) for v in values])
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"  # Treat untrusted source text as text, never as a formula.


def table(sheet, records, timezone="Asia/Bangkok", kind=None):
    from openpyxl.styles import Alignment, Font, PatternFill

    keys = [key for key in dict.fromkeys(k for row in records for k in row) if key not in HIDDEN]
    # Group summaries already contain display names; omit duplicate technical IDs.
    if records and "company" in records[0]:
        keys = [k for k in keys if k not in {"company_id", "currency_id"}]
    headers = dict(HEADERS)
    if kind in {"purchases", "purchase_rfqs"}:
        headers.update(partner_id="Nhà cung cấp", date_order="Hạn đặt hàng / RFQ", group="Nhà cung cấp / Nhân viên mua")
    elif kind == "contacts":
        headers.update(id="ID liên hệ", name="Tên liên hệ", count="Số liên hệ")
    elif kind == "crm":
        headers.update(id="ID lead / cơ hội", name="Lead / Cơ hội", count="Số lead / cơ hội")
    elif kind == "transfers":
        headers.update(name="Số phiếu kho", id="ID phiếu kho", count="Số phiếu")
    append_row(sheet, [headers.get(k, k) for k in keys] or ["Không có dữ liệu phù hợp"])
    for row in records:
        values = []
        for key in keys:
            value = row.get(key)
            if key in M2O:
                if isinstance(value, list | tuple) and len(value) > 1:
                    value = value[1]
                elif value is False or value is None:
                    value = "Chưa có"
            elif key in {"email", "phone"} and not value:
                value = "Chưa có"
            elif key in NUMBERS and value is not None:
                value = float(Decimal(str(value)))
            elif key in DATES:
                if not value:
                    values.append("Chưa có")
                    continue
                if len(str(value)) > 10:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
                    value = parsed.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
                else:
                    value = date.fromisoformat(str(value))
            elif key in {"state", "move_type", "picking_type_code", "operation_type", "transfer_type", "type"}:
                value = STATES.get(value, value)
            values.append(value)
        append_row(sheet, values)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    for cell in sheet[1]:
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="24555A")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[1].height = 34
    for index, column in enumerate(sheet.columns):
        key = keys[index] if index < len(keys) else ""
        is_number = key in NUMBERS or key.startswith("amount_")
        width = 24 if is_number or key in DATES else 36
        if key in {"id", "report_sign", "count", "refund_count"}:
            width = 18
        sheet.column_dimensions[column[0].column_letter].width = width
        for cell in column[1:]:
            cell.font = Font(name="Calibri", size=11)
            cell.alignment = Alignment(vertical="top", wrap_text=not is_number)
            if is_number:
                cell.number_format = "#,##0.00;[Red]-#,##0.00"
            elif key in DATES:
                cell.number_format = "dd/mm/yyyy hh:mm" if isinstance(cell.value, datetime) else "dd/mm/yyyy"
            if cell.row % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F0F6F5")
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_title_rows = "1:1"


def details(section):
    rows = []
    for source in section["rows"]:
        row = dict(source)
        if section["type"] == "revenue":
            for field in ("untaxed", "tax", "total"):
                row[f"net_{field}"] = Decimal(str(source[f"amount_{field}"])) * source.get("report_sign", 1)
        rows.append(row)
    return rows


def export_xlsx(result):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    if not has_data(result):
        raise ValueError("Không có dữ liệu phù hợp; chỉ trả thông báo, không xuất Excel trống.")
    workbook = Workbook()
    overview = workbook.active
    overview.title = "Tong_quan"
    params = result["parameters"]
    append_row(overview, [LABELS.get(result["report_type"], "Báo cáo doanh thu và doanh số")])
    append_row(overview, ["Kỳ báo cáo", period_text(params)])
    companies = list(dict.fromkeys(s.get("company") for section in result["sections"] for s in section["summary"] if s.get("company")))
    append_row(overview, ["Công ty", ", ".join(companies) or ", ".join(f"ID {c}" for c in result["company_ids"])])
    zone = ZoneInfo(params.get("timezone", "Asia/Bangkok"))
    fetched = datetime.fromisoformat(result["finished_at"]).astimezone(zone).replace(tzinfo=None)
    append_row(overview, ["Lấy dữ liệu lúc", fetched, params.get("timezone", "Asia/Bangkok")])
    overview["B4"].number_format = "dd/mm/yyyy hh:mm:ss"
    for caption, value in scope_filters(params):
        append_row(overview, [caption, value])
    append_row(overview, [])
    kind = result["report_type"]
    headings = {
        "crm": ["Báo cáo", "Công ty", "Tiền tệ", "Số lead / cơ hội", "Giá trị kỳ vọng", "Đã thắng", "Lưu ý"],
        "contacts": ["Báo cáo", "Tổng liên hệ", "Doanh nghiệp", "Cá nhân", "Thiếu email", "Thiếu điện thoại", ""],
        "transfers": ["Báo cáo", "Nghiệp vụ", "Số phiếu", "Trạng thái", "", "", ""],
        "employee_weekly": ["Hạng mục", "Thông tin / Nội dung", "Chỉ số / Số lượng", "Chi tiết", "", "", ""],
    }
    append_row(overview, headings.get(kind, ["Báo cáo", "Công ty", "Tiền tệ", "Số chứng từ", "Trước thuế", "Thuế", "Tổng tiền"]))
    header_row = overview.max_row
    for section in result["sections"]:
        totals = [s for s in section["summary"] if s.get("is_total") or s.get("is_crm_total")]
        if kind == "employee_weekly":
            if section["type"] == "employee_profile":
                p = section["summary"][0] if section.get("summary") else {}
                append_row(overview, [section["title"], f"{p.get('name')} - {p.get('job_title')}", p.get("department", ""), p.get("email", ""), "", "", ""])
            elif section["type"] == "tasks":
                t = section["summary"][0] if section.get("summary") else {}
                append_row(overview, [section["title"], f"Tỷ lệ xong: {t.get('done_rate')}", f"Tổng: {t.get('total_tasks')}", f"Xong: {t.get('done_count')} | Quá hạn: {t.get('overdue_count')}", "", "", ""])
            elif section["type"] == "activities":
                a = section["summary"][0] if section.get("summary") else {}
                append_row(overview, [section["title"], "Hoạt động & Lịch làm việc", a.get("total_activities", 0), "", "", "", ""])
            elif section["type"] == "sales":
                s = section["summary"][0] if section.get("summary") else {}
                append_row(overview, [section["title"], f"Báo giá: {s.get('quotes_count')} | Đơn bán: {s.get('confirmed_count')}", s.get("total_orders", 0), f"Doanh số: {s.get('confirmed_value', 0)}", "", "", ""])
            elif section["type"] == "crm":
                c = section["summary"][0] if section.get("summary") else {}
                append_row(overview, [section["title"], "Cơ hội khách hàng", c.get("total_leads", 0), f"Kỳ vọng: {c.get('expected_revenue', 0)}", "", "", ""])
            else:
                append_row(overview, [section["title"], "", section.get("count", 0), "", "", "", ""])
        elif section["type"] == "crm":
            for item in totals:
                append_row(overview, [section["title"], item["company"], item["currency"], item["count"],
                    float(Decimal(item["expected_revenue"])), item.get("won_count", 0), "Kỳ vọng, không phải doanh thu đã thực hiện"])
        elif section["type"] == "contacts":
            item = next((s for s in section["summary"] if "total_contacts" in s), {})
            append_row(overview, [section["title"], item.get("total_contacts", section["count"]), item.get("companies"),
                item.get("individuals"), item.get("missing_email"), item.get("missing_phone"), ""])
        elif section["type"] == "transfers":
            for item in section["summary"]:
                if "transfer_type" in item:
                    append_row(overview, [section["title"], STATES.get(item["transfer_type"], item["transfer_type"]), item["count"], "Đã hoàn tất", "", "", ""])
        elif totals:
            for item in totals:
                append_row(overview, [section["title"], item["company"], item["currency"], item["count"],
                    float(Decimal(item["untaxed"])), float(Decimal(item["tax"])), float(Decimal(item["total"]))])
        else:
            append_row(overview, [section["title"], "", "", section["count"], "Không áp dụng", "Không áp dụng", "Không áp dụng"])
        name_map = {
            "revenue": "Chi_tiet_hoa_don", "sales": "Chi_tiet_don_ban", "inventory": "Chi_tiet_ton_kho", "tasks": "Chi_tiet_cong_viec",
            "quotations": "Chi_tiet_bao_gia", "purchases": "Chi_tiet_don_mua", "purchase_rfqs": "Chi_tiet_RFQ",
            "crm": "Chi_tiet_CRM", "contacts": "Chi_tiet_lien_he", "transfers": "Chi_tiet_nhap_xuat",
            "employee_profile": "Ho_so_nhan_vien", "activities": "Chi_tiet_hoat_dong",
        }
        name = name_map.get(section["type"], f"Chi_tiet_{section['type']}")
        if name in workbook.sheetnames:
            name = f"{name}_{section.get('type')}"
        table(workbook.create_sheet(name), details(section), params.get("timezone", "Asia/Bangkok"), section["type"])
        if kind != "employee_weekly":
            groups = [s for s in section["summary"] if not s.get("is_total") and not s.get("is_crm_total")]
            if groups:
                table(workbook.create_sheet("TH_" + section["type"]), groups, params.get("timezone", "Asia/Bangkok"), section["type"])
    overview.sheet_view.showGridLines = False
    overview.freeze_panes = f"D{header_row + 1}"
    for row in overview:
        for cell in row:
            cell.font = Font(name="Calibri", size=11)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    overview["A1"].font = Font(name="Calibri", size=18, bold=True, color="24555A")
    overview.merge_cells("A1:G1")
    overview.row_dimensions[1].height = 32
    for cell in overview[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="24555A")
    overview.row_dimensions[header_row].height = 30
    for column in "ABCDEFG":
        overview.column_dimensions[column].width = 25 if column not in "AB" else 36
    for row in overview.iter_rows(min_row=header_row + 1, min_col=5, max_col=7):
        for cell in row:
            if isinstance(cell.value, int | float):
                cell.number_format = "#,##0" if kind in {"contacts", "transfers"} or kind == "crm" and cell.column == 6 else "#,##0.00;[Red]-#,##0.00"
    definitions = workbook.create_sheet("Dinh_nghia")
    rules = [
        {"Nội dung": "Nguồn", "Giải thích": "Dữ liệu Odoo theo quyền tài khoản và bộ lọc đã chọn."},
        {"Nội dung": "Doanh thu", "Giải thích": "Hóa đơn khách hàng đã ghi nhận trừ hoàn tiền theo ngày hóa đơn. Không phải báo cáo sổ cái."},
        {"Nội dung": "Doanh số", "Giải thích": "Đơn đã xác nhận/hoàn tất theo ngày đơn. Không tự giảm theo hóa đơn hoàn tiền."},
        {"Nội dung": "Tồn khả dụng", "Giải thích": "Tồn thực tế trừ đã giữ, tại địa điểm nội bộ khi truy vấn."},
        {"Nội dung": "Công việc", "Giải thích": "Lọc theo hạn chót hoặc ngày tạo. Trạng thái là trạng thái hiện tại."},
    ]
    rules += [
        {"Nội dung": "Báo giá bán", "Giải thích": "Chỉ báo giá nháp và đã gửi, lọc theo ngày tạo. Không gồm đơn bán đã xác nhận hoặc đã hủy."},
        {"Nội dung": "Mua hàng", "Giải thích": "Đơn mua đã xác nhận lọc theo ngày xác nhận; yêu cầu báo giá mua gồm RFQ nháp, đã gửi và chờ duyệt, lọc theo ngày tạo."},
        {"Nội dung": "CRM", "Giải thích": "Giá trị kỳ vọng không phải doanh thu đã thực hiện. Giai đoạn thắng theo cấu hình Odoo; lưu trữ không đồng nghĩa thất bại."},
        {"Nội dung": "Liên hệ", "Giải thích": "Đếm các liên hệ phù hợp bộ lọc; doanh nghiệp, cá nhân, khách hàng và nhà cung cấp là các cách phân loại khác nhau."},
        {"Nội dung": "Nhập xuất kho", "Giải thích": "Chỉ phiếu đã hoàn tất theo ngày hoàn tất; không phải số lượng sản phẩm hoặc giá trị tồn kho."},
    ]
    rules += [{"Nội dung": "Lưu ý", "Giải thích": warning} for warning in result["warnings"]]
    table(definitions, rules)
    definitions.column_dimensions["B"].width = 100
    for row in range(2, definitions.max_row + 1):
        definitions.row_dimensions[row].height = 34
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def employee_weekly_chat_summary(result):
    params = result["parameters"]
    sections_by_type = {s["type"]: s for s in result["sections"]}
    profile = (sections_by_type.get("employee_profile", {}).get("summary") or [{}])[0]
    tasks_sec = sections_by_type.get("tasks", {})
    task_stat = (tasks_sec.get("summary") or [{}])[0]
    acts_sec = sections_by_type.get("activities", {})
    act_stat = (acts_sec.get("summary") or [{}])[0]
    sales_sec = sections_by_type.get("sales", {})
    sale_stat = (sales_sec.get("summary") or [{}])[0]
    crm_sec = sections_by_type.get("crm", {})
    crm_stat = (crm_sec.get("summary") or [{}])[0]

    lines = [
        "📋 BÁO CÁO CÔNG VIỆC TUẦN CỦA NHÂN VIÊN",
        f"👤 Nhân viên: {profile.get('name', 'Chưa rõ')} | Chức danh: {profile.get('job_title', 'Chưa rõ')}",
        f"🏢 Phòng ban: {profile.get('department', 'Chưa rõ')} | Quản lý trực tiếp: {profile.get('manager', 'Chưa xác định')}",
        f"📅 Kỳ báo cáo: {period_text(params)}",
        "",
        "📊 TỔNG HỢP CHỈ SỐ NHANH (KPIS):",
        f"• Công việc dự án: {task_stat.get('done_count', 0)}/{task_stat.get('total_tasks', 0)} hoàn thành ({task_stat.get('done_rate', '0%')}) • Quá hạn: {task_stat.get('overdue_count', 0)} • Đang làm: {task_stat.get('inprogress_count', 0)}",
    ]
    if sale_stat.get("total_orders", 0) > 0:
        lines.append(f"• Doanh số đơn bán: {Decimal(sale_stat.get('confirmed_value', 0)):,.2f} VNĐ ({sale_stat.get('confirmed_count', 0)} đơn chốt) • Báo giá nháp: {sale_stat.get('quotes_count', 0)}")
    if crm_stat.get("total_leads", 0) > 0:
        lines.append(f"• Cơ hội CRM: {crm_stat.get('total_leads', 0)} lead mới • Giá trị kỳ vọng: {Decimal(crm_stat.get('expected_revenue', 0)):,.2f} VNĐ")
    if act_stat.get("total_activities", 0) > 0:
        types_str = ", ".join(f"{k}: {v}" for k, v in act_stat.get("types", {}).items())
        lines.append(f"• Hoạt động lịch Odoo: {act_stat.get('total_activities', 0)} hoạt động ({types_str})")

    lines.append("")
    lines.append("1. CHI TIẾT CÔNG VIỆC & DỰ ÁN (project.task):")
    task_rows = tasks_sec.get("rows", [])
    if task_rows:
        done_list = [t for t in task_rows if t.get("is_closed") or t.get("state") in ("1_done", "done")]
        overdue_list = [t for t in task_rows if not (t.get("is_closed") or t.get("state") in ("1_done", "done")) and str(t.get("date_deadline") or "")[:10] and str(t.get("date_deadline") or "")[:10] < date.today().isoformat()]
        inprog_list = [t for t in task_rows if t not in done_list and t not in overdue_list]

        if done_list:
            lines.append(f"✅ Đã hoàn thành ({len(done_list)}):")
            for t in done_list[:5]:
                tid = f"[{t['id']}] " if t.get("id") else ""
                lines.append(f"  - {tid}{t.get('name', '')}")
            if len(done_list) > 5:
                lines.append(f"  ... và {len(done_list) - 5} công việc khác.")
        if inprog_list:
            lines.append(f"⏳ Đang thực hiện ({len(inprog_list)}):")
            for t in inprog_list[:5]:
                tid = f"[{t['id']}] " if t.get("id") else ""
                dl = f" (Hạn: {t['date_deadline']})" if t.get("date_deadline") else ""
                lines.append(f"  - {tid}{t.get('name', '')}{dl}")
            if len(inprog_list) > 5:
                lines.append(f"  ... và {len(inprog_list) - 5} công việc khác.")
        if overdue_list:
            lines.append(f"⚠️ Quá hạn / Cần chú ý ({len(overdue_list)}):")
            for t in overdue_list[:5]:
                tid = f"[{t['id']}] " if t.get("id") else ""
                lines.append(f"  - {tid}{t.get('name', '')} (Hạn: {t.get('date_deadline', 'Chưa rõ')})")
            if len(overdue_list) > 5:
                lines.append(f"  ... và {len(overdue_list) - 5} công việc khác.")
    else:
        lines.append("  Không có công việc nào được phân công trong kỳ.")

    if sale_stat.get("total_orders", 0) > 0:
        lines.append("")
        lines.append("2. ĐƠN HÀNG & BÁO GIÁ (sale.order):")
        for s in sales_sec.get("rows", [])[:5]:
            st = STATES.get(s.get("state"), s.get("state"))
            lines.append(f"  - [{s.get('name')}] {label(s.get('partner_id'))}: {Decimal(str(s.get('amount_total', 0))):,.2f} ({st})")

    lines.append("")
    lines.append("💡 NHẬN XÉT CỦA HỆ THỐNG DÀNH CHO QUẢN LÝ:")
    done_rate_num = float(task_stat.get('done_rate', '0%').rstrip('%') or 0)
    if done_rate_num >= 80:
        lines.append(f"• Tiến độ công việc tốt: Hoàn thành {done_rate_num}% khối lượng công việc được giao.")
    else:
        lines.append(f"• Cần lưu ý: Tỷ lệ hoàn thành công việc đạt {done_rate_num}%, có {task_stat.get('overdue_count', 0)} việc quá hạn.")
    if task_stat.get("overdue_count", 0) > 0:
        lines.append(f"• Quản lý nên kiểm tra các task quá hạn để hỗ trợ tháo gỡ vướng mắc.")
    if sale_stat.get("confirmed_count", 0) > 0:
        lines.append(f"• Nhân viên có đóng góp doanh số thực tế: {Decimal(sale_stat.get('confirmed_value', 0)):,.2f} VNĐ trong kỳ.")

    lines += ["", "Thời điểm lấy dữ liệu: " + result["finished_at"]]
    return "\n".join(lines)


def chat_summary(result):
    params = result["parameters"]
    if params.get("report_type") == "employee_weekly":
        return employee_weekly_chat_summary(result)
    title = LABELS.get(params["report_type"], "Báo cáo doanh thu và doanh số")
    lines = [title, f"Kỳ: {period_text(params)}", "Phạm vi công ty: " + ", ".join(f"ID {c}" for c in result["company_ids"])]
    for section in result["sections"]:
        if not section["count"]:
            subject = {"revenue": "hóa đơn khách hàng hoặc hoàn tiền đã ghi nhận", "sales": "đơn bán đã xác nhận/hoàn tất",
                       "inventory": "bản ghi tồn kho nội bộ", "tasks": "công việc theo bộ lọc đã chọn", "quotations": "báo giá nháp hoặc đã gửi", "purchases": "đơn mua đã xác nhận", "purchase_rfqs": "yêu cầu báo giá mua", "crm": "lead hoặc cơ hội CRM", "contacts": "liên hệ", "transfers": "phiếu kho đã hoàn tất"}[section["type"]]
            lines.append(f"{section['title']}: không tìm thấy {subject} phù hợp trong phạm vi tài khoản.")
            continue
        lines.append(f"{section['title']}: {section['count']} bản ghi.")
        totals = [s for s in section["summary"] if s.get("is_total") or s.get("is_crm_total")]
        for item in totals:
            if section["type"] == "crm":
                lines.append(f"{item['company']} / {item['currency']}: {item['count']} lead/cơ hội, giá trị kỳ vọng {Decimal(item['expected_revenue']):,.2f}; {item.get('won_count', 0)} đã thắng theo giai đoạn. Đây không phải doanh thu thực hiện.")
            else:
                lines.append(f"{item['company']} / {item['currency']}: trước thuế {Decimal(item['untaxed']):,.2f}; thuế {Decimal(item['tax']):,.2f}; tổng {Decimal(item['total']):,.2f}.")
        if section["type"] == "contacts" and section["summary"]:
            item = next((s for s in section["summary"] if "total_contacts" in s), {})
            lines.append(f"Doanh nghiệp: {item.get('companies', 0)}; cá nhân: {item.get('individuals', 0)}; thiếu email: {item.get('missing_email', 0)}; thiếu điện thoại: {item.get('missing_phone', 0)}.")
        if section["type"] == "transfers":
            for item in section["summary"]:
                if "transfer_type" in item:
                    lines.append(f"{STATES.get(item['transfer_type'], item['transfer_type'])}: {item['count']} phiếu đã hoàn tất.")
        if not totals and section["type"] == "tasks":
            total = next((s for s in section["summary"] if "total_tasks" in s), {})
            lines.append(f"Tổng công việc: {total.get('total_tasks', section['count'])}. Quá hạn: {total.get('overdue_current') if total.get('overdue_current') is not None else 'chưa xác định' }.")
    if not has_data(result):
        lines += ["Không tạo file báo cáo trống. Bạn có thể đổi kỳ báo cáo hoặc phạm vi công ty."]
        if params["report_type"] in {"revenue", "commercial_summary"}:
            lines.append("Doanh thu dựa trên hóa đơn đã ghi nhận; nếu cần số liệu đơn bán, hãy yêu cầu báo cáo doanh số.")
    lines.append("Phạm vi bản ghi: của tôi." if params.get("record_scope") == "mine" else "Phạm vi bản ghi: các bản ghi tài khoản được phép đọc.")
    lines += ["Thời điểm lấy dữ liệu: " + result["finished_at"], *result["warnings"]]
    return "\n".join(lines)
