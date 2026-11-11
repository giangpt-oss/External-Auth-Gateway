"""Narrative Word reports grounded in the reporting engine's complete aggregates."""
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

from app.reporting.engine import LABELS, label
from app.reporting.export import STATES, details, has_data, period_text, scope_filters

DETAIL_LIMIT = 40
FINANCIAL_TYPES = {"revenue", "sales", "quotations", "purchases", "purchase_rfqs"}


def _text(value):
    if value is None:
        return "Chưa xác định"
    if isinstance(value, bool):
        return "Có" if value else "Không"
    if isinstance(value, list | tuple):
        return ", ".join(map(str, value)) or "Chưa phân công"
    return str(value)


def _money(value):
    return f"{Decimal(str(value or 0)):,.2f}"


def _stamp(value, timezone):
    if not value:
        return "Chưa xác định"
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(ZoneInfo(timezone)).strftime("%d/%m/%Y %H:%M")


def _paragraph(document, text, style=None):
    from docx.shared import Pt

    paragraph = document.add_paragraph(text, style)
    paragraph.paragraph_format.space_after = Pt(6)
    paragraph.paragraph_format.widow_control = True
    return paragraph


def _table(document, headers, rows, widths, numeric=()):
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    table = document.add_table(rows=1, cols=len(headers))
    table.autofit = False
    properties = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement(f"w:{edge}")
        for name, value in (("val", "single"), ("sz", "4"), ("color", "D9D9D9")):
            border.set(qn(f"w:{name}"), value)
        borders.append(border)
    properties.append(borders)
    margins = OxmlElement("w:tblCellMar")
    for edge in ("top", "left", "bottom", "right"):
        margin = OxmlElement(f"w:{edge}")
        margin.set(qn("w:w"), "90")
        margin.set(qn("w:type"), "dxa")
        margins.append(margin)
    properties.append(margins)
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for index, width in enumerate(widths):
        table.columns[index].width = Cm(width)
    all_rows = [headers, *rows]
    for row_index, values in enumerate(all_rows):
        row = table.rows[0] if row_index == 0 else table.add_row()
        # Keep individual short records together while allowing the table to span pages.
        no_split = OxmlElement("w:cantSplit")
        row._tr.get_or_add_trPr().append(no_split)
        for column_index, value in enumerate(values):
            cell = row.cells[column_index]
            cell.width = Cm(widths[column_index])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = _text(value)
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "244761" if row_index == 0 else ("F0F4F7" if row_index % 2 == 0 else "FFFFFF"))
            cell._tc.get_or_add_tcPr().append(shade)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(2)
                paragraph.paragraph_format.space_before = Pt(2)
                paragraph.paragraph_format.line_spacing = 1.05
                paragraph.paragraph_format.keep_with_next = row_index == 0
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT if column_index in numeric else WD_ALIGN_PARAGRAPH.LEFT
                for run in paragraph.runs:
                    run.font.name = "Calibri"
                    run.font.size = Pt(10)
                    run.font.bold = row_index == 0
                    run.font.color.rgb = RGBColor.from_string("FFFFFF" if row_index == 0 else "000000")
    document.add_paragraph().paragraph_format.space_after = Pt(1)
    return table


def _financial(document, section):
    totals = [item for item in section["summary"] if item.get("is_total")]
    for item in totals:
        _paragraph(document,
            f"{item['company']} có {item['count']} chứng từ phù hợp phạm vi báo cáo, "
            f"với giá trị trước thuế {_money(item['untaxed'])} {item['currency']}. "
            f"Thuế là {_money(item['tax'])} và tổng sau thuế là {_money(item['total'])} {item['currency']}.")
    _table(document, ["Công ty", "Tiền tệ", "Chứng từ", "Trước thuế", "Thuế", "Sau thuế"],
           [[s["company"], s["currency"], s["count"], _money(s["untaxed"]), _money(s["tax"]), _money(s["total"])] for s in totals],
           [3.5, 1.5, 1.6, 3.5, 3.1, 3.8], numeric=(2, 3, 4, 5))
    document.add_heading("Nhận xét từ số liệu", 3)
    for total in totals:
        currency = total["currency"]
        groups = [s for s in section["summary"] if not s.get("is_total")
                  and s.get("company_id") == total.get("company_id")
                  and s.get("currency_id") == total.get("currency_id")]
        if groups:
            leading = max(groups, key=lambda item: Decimal(str(item["untaxed"])))
            value = Decimal(str(leading["untaxed"]))
            overall = Decimal(str(total["untaxed"]))
            message = f"Trong {total['company']}, nhóm {leading['group']} có giá trị trước thuế cao nhất, {_money(value)} {currency}."
            # A meaningful share needs a positive denominator and a nonnegative component.
            if overall > 0 and 0 <= value <= overall:
                message += f" Nhóm này chiếm {value / overall * 100:.1f}% tổng trước thuế cùng công ty và tiền tệ."
            _paragraph(document, message)
        if section["type"] == "revenue":
            refunds = total.get("refund_count", 0)
            _paragraph(document, f"Có {refunds} chứng từ hoàn tiền trong {total['company']} ({currency}); giá trị hoàn tiền đã được trừ trong tổng doanh thu theo hóa đơn.")
    if section["type"] == "revenue":
        _paragraph(document, "Chỉ tiêu này dựa trên hóa đơn khách hàng đã ghi nhận và hoàn tiền trong kỳ. Các số liệu không thể hiện doanh thu theo sổ cái hoặc số tiền thực thu.")
    elif section["type"] == "quotations":
        _paragraph(document, "Báo cáo chỉ gồm báo giá nháp và đã gửi, theo ngày tạo. Giá trị báo giá chưa phải doanh số đơn bán đã xác nhận hoặc doanh thu theo hóa đơn.")
    elif section["type"] == "purchases":
        _paragraph(document, "Báo cáo gồm đơn mua đã xác nhận hoặc hoàn tất, theo ngày xác nhận. Giá trị đơn mua không phải số tiền đã thanh toán hoặc chi phí đã ghi sổ.")
    elif section["type"] == "purchase_rfqs":
        _paragraph(document, "Báo cáo gồm yêu cầu báo giá mua nháp, đã gửi và chờ duyệt, theo ngày tạo. Giá trị này chưa phải giá trị đơn mua đã xác nhận.")
    else:
        _paragraph(document, "Doanh số phản ánh đơn bán đã xác nhận hoặc hoàn tất theo ngày đơn. Chỉ tiêu không tự giảm theo hóa đơn hoàn tiền và có thể khác doanh thu theo hóa đơn.")
    _paragraph(document, "Chưa có dữ liệu kỳ so sánh trong báo cáo này nên không kết luận tăng giảm hoặc nguyên nhân biến động.")


def _inventory(document, section):
    rows = section["rows"]
    negative = sum(Decimal(str(row["quantity"])) < 0 for row in rows)
    unavailable = sum(Decimal(str(row["available"])) <= 0 for row in rows)
    _paragraph(document,
        f"Báo cáo có {section['count']} dòng sản phẩm theo vị trí và đơn vị tính. "
        f"Trong đó {negative} dòng tồn thực tế âm và {unavailable} dòng có tồn khả dụng không dương. "
        "Số lượng của các sản phẩm hoặc đơn vị tính khác nhau không được cộng thành một tổng chung.")
    _table(document, ["Chỉ tiêu", "Số dòng"],
           [["Sản phẩm theo vị trí và đơn vị tính", section["count"]], ["Tồn thực tế âm", negative], ["Khả dụng không dương", unavailable]],
           [12.5, 4.5], numeric=(1,))
    _paragraph(document, "Tồn khả dụng được tính bằng tồn thực tế trừ số đã giữ tại địa điểm nội bộ. Các dòng âm hoặc hết khả dụng cần được đối chiếu với giao dịch kho trước khi quyết định bổ sung hàng.")


def _tasks(document, section):
    total = next((item for item in section["summary"] if "total_tasks" in item), {})
    closed = total.get("closed_current", total.get("done_count"))
    overdue = total.get("overdue_current", total.get("overdue_count"))
    inprogress = total.get("inprogress_count")
    _paragraph(document, f"Có {section['count']} công việc phù hợp với bộ lọc. Trạng thái và người phụ trách thể hiện thời điểm truy vấn, không phải lịch sử xử lý trong kỳ.")
    table_rows = [["Tổng công việc", section["count"]], ["Đã hoàn thành", closed], ["Đang thực hiện", inprogress], ["Quá hạn hiện tại", overdue]]
    _table(document, ["Chỉ tiêu", "Số công việc"], [r for r in table_rows if r[1] is not None], [12.5, 4.5], numeric=(1,))
    if overdue:
        _paragraph(document, f"Có {overdue} công việc đang quá hạn theo trạng thái hiện tại. Cần xem hạn chót và người phụ trách trong phụ lục để xác định việc cần theo dõi.")
    groups = [s for s in section["summary"] if "project" in s]
    if groups:
        _table(document, ["Dự án", "Giai đoạn", "Số công việc"],
               [[g["project"], g["stage"], g["count"]] for g in groups[:DETAIL_LIMIT]], [6.5, 7.5, 3], numeric=(2,))
        if len(groups) > DETAIL_LIMIT:
            _paragraph(document, f"Bảng trên trình bày {DETAIL_LIMIT}/{len(groups)} nhóm dự án và giai đoạn. Yêu cầu Excel để xem đủ các nhóm.")


def _employee_profile(document, section):
    profile = section["summary"][0] if section.get("summary") else {}
    _paragraph(document, f"Nhân viên: {profile.get('name', 'Chưa rõ')}")
    _table(document, ["Thông tin", "Giá trị"],
           [
               ["Họ và tên", profile.get("name") or "Chưa rõ"],
               ["Chức danh", profile.get("job_title") or "Chưa rõ"],
               ["Phòng ban", profile.get("department") or "Chưa rõ"],
               ["Quản lý trực tiếp", profile.get("manager") or "Chưa rõ"],
               ["Email làm việc", profile.get("email") or "Chưa rõ"],
               ["Điện thoại", profile.get("phone") or "Chưa rõ"],
           ],
           [6.0, 11.0])


def _activities(document, section):
    summary = section["summary"][0] if section.get("summary") else {}
    total = summary.get("total_activities", section.get("count", 0))
    types = summary.get("types", {})
    _paragraph(document, f"Có {total} hoạt động được ghi nhận. Chi tiết các loại hoạt động:")
    rows = [[k, str(v)] for k, v in types.items()] or [["Tất cả", str(total)]]
    _table(document, ["Loại hoạt động", "Số lượng"], rows, [12.0, 5.0], numeric=(1,))


def _employee_sales(document, section):
    summary = section["summary"][0] if section.get("summary") else {}
    _paragraph(document, f"Tổng số đơn và báo giá phụ trách: {summary.get('total_orders', 0)}")
    _table(document, ["Chỉ tiêu", "Số lượng", "Giá trị"],
           [
               ["Báo giá đang xử lý (Draft / Sent)", str(summary.get("quotes_count", 0)), _money(summary.get("quotes_value", "0"))],
               ["Đơn hàng đã chốt (Confirmed / Done)", str(summary.get("confirmed_count", 0)), _money(summary.get("confirmed_value", "0"))],
           ],
           [7.0, 4.0, 6.0], numeric=(1, 2))


def _employee_crm(document, section):
    summary = section["summary"][0] if section.get("summary") else {}
    _paragraph(document, f"Có {section['count']} cơ hội CRM đang phụ trách.")
    _table(document, ["Chỉ tiêu", "Giá trị"],
           [
               ["Tổng số cơ hội / lead", str(summary.get("total_leads", section.get("count", 0)))],
               ["Tổng giá trị kỳ vọng", _money(summary.get("expected_revenue", "0"))],
           ],
           [8.0, 9.0], numeric=(1,))


def _crm(document, section):
    totals = [item for item in section["summary"] if item.get("is_crm_total")]
    _paragraph(document, f"Có {section['count']} lead hoặc cơ hội phù hợp bộ lọc. Giá trị kỳ vọng dưới đây phản ánh pipeline hiện tại, không phải doanh số đã xác nhận, doanh thu hay số tiền thực thu.")
    _table(document, ["Công ty", "Tiền tệ", "Lead / cơ hội", "Kỳ vọng", "Đã thắng"],
           [[r["company"], r["currency"], r["count"], _money(r["expected_revenue"]), r.get("won_count", 0)] for r in totals],
           [4.3, 1.8, 2.4, 5.5, 3], numeric=(2, 3, 4))
    groups = [r for r in section["summary"] if not r.get("is_crm_total")]
    if groups:
        _table(document, ["Công ty", "Giai đoạn", "Tiền tệ", "Số lượng", "Kỳ vọng"],
               [[r["company"], r.get("stage"), r["currency"], r["count"], _money(r["expected_revenue"])] for r in groups[:DETAIL_LIMIT]],
               [3.5, 5, 1.6, 1.8, 5.1], numeric=(3, 4))
        if len(groups) > DETAIL_LIMIT:
            _paragraph(document, f"Bảng giai đoạn trình bày {DETAIL_LIMIT}/{len(groups)} nhóm; các tổng số vẫn tính trên toàn bộ dữ liệu. Yêu cầu Excel để xem đủ.")
    _paragraph(document, "Cơ hội thắng được xác định theo cờ thắng của giai đoạn trong Odoo. Bản ghi lưu trữ không tự được coi là cơ hội thua. Nếu có kỳ lọc, kỳ dùng ngày tạo và trạng thái vẫn là trạng thái hiện tại.")


def _contacts(document, section):
    summary = next((s for s in section["summary"] if "total_contacts" in s), {})
    _paragraph(document, f"Có {summary.get('total_contacts', section['count'])} liên hệ phù hợp bộ lọc. Mỗi bản ghi được tính một lần trong tổng liên hệ; phân loại khách hàng hoặc nhà cung cấp theo cấu hình Odoo.")
    _table(document, ["Chỉ tiêu", "Số liên hệ"],
           [["Tổng liên hệ", summary.get("total_contacts", section["count"])],
            ["Doanh nghiệp", summary.get("companies")], ["Cá nhân", summary.get("individuals")],
            ["Thiếu email", summary.get("missing_email")], ["Thiếu điện thoại", summary.get("missing_phone")]],
           [12.5, 4.5], numeric=(1,))
    if summary.get("unknown_company_type"):
        _paragraph(document, f"Có {summary['unknown_company_type']} liên hệ chưa xác định được loại doanh nghiệp/cá nhân; không tự gán chúng vào hai nhóm trên.")
    groups = [row for row in section["summary"] if "related_company" in row]
    if groups:
        _table(document, ["Tổ chức liên quan", "Số bản ghi liên hệ"],
               [[row["related_company"], row["count"]] for row in groups[:40]],
               [12.5, 4.5], numeric=(1,))
        if len(groups) > 40:
            _paragraph(document, f"Hiển thị 40/{len(groups)} tổ chức liên quan; chọn Excel để xem đầy đủ các nhóm.")
    _paragraph(document, "Một liên hệ có thể đồng thời là khách hàng và nhà cung cấp. Số liên hệ thiếu email hoặc điện thoại là chỉ báo dữ liệu cần rà soát, không chứng minh liên hệ không hợp lệ.")


def _transfers(document, section):
    _paragraph(document, f"Có {section['count']} phiếu kho đã hoàn tất phù hợp bộ lọc ngày hoàn tất. Đây là số phiếu nghiệp vụ, không phải tổng số sản phẩm hoặc giá trị hàng tồn.")
    groups = [r for r in section["summary"] if "transfer_type" in r]
    _table(document, ["Nghiệp vụ", "Số phiếu đã hoàn tất"],
           [[STATES.get(r["transfer_type"], r["transfer_type"]), r["count"]] for r in groups],
           [11, 6], numeric=(1,))
    _paragraph(document, "Phiếu dự thảo, chờ xử lý, sẵn sàng và đã hủy không thuộc số hoàn tất này. Vị trí nguồn, vị trí đích và chứng từ nguồn trong phụ lục giúp đối chiếu từng phiếu.")


def _day(value, timezone):
    if not value:
        return "Chưa có"
    if len(str(value)) > 10:
        return _stamp(value, timezone).split(" ")[0]
    return datetime.fromisoformat(str(value)).strftime("%d/%m/%Y")


def _appendix(document, section, timezone, report_type=None):
    rows = details(section)[:DETAIL_LIMIT]
    kind = section["type"]
    if kind == "employee_profile":
        return
    document.add_heading("Chi tiết đối chiếu", 3)
    _paragraph(document,
        f"Phụ lục trình bày {len(rows)}/{section['count']} dòng. "
        "Các chỉ tiêu tổng hợp và nhận xét ở trên được tính từ toàn bộ dữ liệu, không chỉ các dòng trong phụ lục.")
    if section["count"] > DETAIL_LIMIT:
        _paragraph(document, "Phụ lục là phần trích đầu danh sách. Yêu cầu thêm Excel để nhận toàn bộ dữ liệu chi tiết.")
    if kind == "activities":
        _table(document, ["Hoạt động", "Tài liệu liên quan", "Hạn chót", "Nội dung tóm tắt"],
               [[label(r.get("activity_type_id")), _text(r.get("res_name")), _day(r.get("date_deadline"), timezone), _text(r.get("summary"))] for r in rows],
               [4.0, 4.5, 3.0, 5.5])
    elif kind == "sales" and report_type == "employee_weekly":
        _table(document, ["Đơn hàng / Báo giá", "Khách hàng", "Ngày tạo / đặt", "Tổng tiền", "Trạng thái"],
               [[r.get("name"), label(r.get("partner_id")), _day(r.get("date_order"), timezone), _money(r.get("amount_total")), STATES.get(r.get("state"), r.get("state"))] for r in rows],
               [3.5, 5.0, 3.0, 3.5, 2.0], numeric=(3,))
    elif kind in FINANCIAL_TYPES:
        date_field = {"revenue": "invoice_date", "sales": "date_order", "quotations": "create_date", "purchases": "date_approve", "purchase_rfqs": "create_date"}[kind]
        partner_label = "Nhà cung cấp" if kind in {"purchases", "purchase_rfqs"} else "Khách hàng"
        _table(document, ["Chứng từ", partner_label, "Ngày", "Tiền tệ", "Trước thuế", "Loại"],
               [[r.get("name"), label(r.get("partner_id")), _day(r.get(date_field), timezone),
                 label(r.get("currency_id")), _money(r.get("net_untaxed", r.get("amount_untaxed"))),
                 STATES.get(r.get("move_type") or r.get("state"), r.get("move_type") or r.get("state"))] for r in rows],
               [2.7, 4, 2.2, 1.6, 3, 3.5], numeric=(4,))
    elif kind == "inventory":
        _table(document, ["Sản phẩm", "Vị trí", "Đơn vị", "Thực tế", "Đã giữ", "Khả dụng"],
               [[r["product"], r["location"], r["uom"], _money(r["quantity"]), _money(r["reserved"]), _money(r["available"])] for r in rows],
               [4.8, 3.4, 1.9, 2.3, 2.3, 2.3], numeric=(3, 4, 5))
    elif kind == "crm":
        _table(document, ["Lead / Cơ hội", "Giai đoạn", "Nhân viên", "Tiền tệ", "Kỳ vọng"],
               [[r.get("name"), label(r.get("stage_id")), label(r.get("user_id")), label(r.get("currency_id")), _money(r.get("expected_revenue"))] for r in rows],
               [5.3, 4, 3.2, 1.5, 3], numeric=(4,))
    elif kind == "contacts":
        _table(document, ["Liên hệ", "Phân loại", "Công ty liên quan", "Email", "Điện thoại"],
               [[r.get("name"), "Doanh nghiệp" if r.get("is_company") else "Cá nhân", label(r.get("parent_id")), r.get("email") or "Chưa có", r.get("phone") or "Chưa có"] for r in rows],
               [4.4, 2.4, 3.4, 4, 2.8])
    elif kind == "transfers":
        _table(document, ["Phiếu", "Nghiệp vụ", "Hoàn tất", "Vị trí nguồn", "Vị trí đích", "Chứng từ nguồn"],
               [[r.get("name"), STATES.get(r.get("picking_type_code"), r.get("picking_type_code")), _day(r.get("date_done"), timezone), label(r.get("location_id")), label(r.get("location_dest_id")), r.get("origin") or "Chưa có"] for r in rows],
               [2.8, 2.3, 2.3, 3, 3, 3.6])
    else:
        _table(document, ["Công việc", "Dự án", "Người phụ trách", "Giai đoạn", "Hạn chót"],
               [[r.get("name"), label(r.get("project_id")), _text(r.get("assignees")), label(r.get("stage_id")), _day(r.get("date_deadline"), timezone)] for r in rows],
               [5, 3.3, 3.4, 3, 2.3])


def export_docx(result) -> bytes:
    """Build a self-contained Word report without provider calls or invented analysis."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    if not has_data(result):
        raise ValueError("Không có dữ liệu phù hợp; không xuất Word trống.")
    document = Document()
    # The default Word template may carry a decorative border in an inherited style.
    for border in document.styles.element.xpath(".//w:pBdr"):
        border.getparent().remove(border)
    page = document.sections[0]
    page.page_width, page.page_height = Cm(21), Cm(29.7)
    page.top_margin = page.bottom_margin = page.left_margin = page.right_margin = Cm(2)
    for name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"):
        style = document.styles[name]
        style.font.name = "Calibri"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.widow_control = True
    document.styles["Normal"].font.size = Pt(11)
    document.styles["Normal"].paragraph_format.line_spacing = 1.12
    document.styles["Title"].font.size = Pt(22)
    document.styles["Heading 1"].font.size = Pt(15)
    document.styles["Heading 2"].font.size = Pt(12)
    params = result["parameters"]
    timezone = params.get("timezone", "Asia/Bangkok")
    title = LABELS.get(result["report_type"], "Báo cáo doanh thu và doanh số")
    document.add_paragraph(title, "Title")
    _paragraph(document, f"Kỳ báo cáo {period_text(params)}")
    companies = list(dict.fromkeys(s.get("company") for section in result["sections"] for s in section["summary"] if s.get("company")))
    _paragraph(document, "Phạm vi công ty " + (", ".join(companies) or ", ".join(f"ID {c}" for c in result["company_ids"])))
    _paragraph(document, f"Lấy dữ liệu lúc {_stamp(result['finished_at'], timezone)} theo múi giờ {timezone}.")
    _paragraph(document, "Báo cáo trình bày số liệu Odoo, nhận xét và dữ liệu đối chiếu theo phạm vi đã chọn.")
    _paragraph(document, "Phạm vi bản ghi: của tôi." if params.get("record_scope") == "mine" else "Phạm vi bản ghi: tài khoản được phép đọc.")
    selected = [f"{caption}: {value}" for caption, value in scope_filters(params) if caption != "Phạm vi bản ghi"]
    if selected:
        document.add_heading("Phạm vi và bộ lọc", 1)
        _paragraph(document, "; ".join(selected))
    if result["report_type"] == "tasks":
        _paragraph(document, "Công việc được chọn theo " + ("ngày tạo." if params.get("task_date_field") == "create_date" else "hạn chót."))
    document.add_heading("Kết quả và nhận xét", 1)
    for section in result["sections"]:
        document.add_heading(section["title"], 2)
        if not section["count"]:
            _paragraph(document, "Không tìm thấy bản ghi phù hợp cho phần báo cáo này trong phạm vi đã chọn.")
            continue
        if section["type"] == "employee_profile":
            _employee_profile(document, section)
        elif section["type"] == "activities":
            _activities(document, section)
        elif section["type"] == "sales" and result.get("report_type") == "employee_weekly":
            _employee_sales(document, section)
        elif section["type"] == "crm" and result.get("report_type") == "employee_weekly":
            _employee_crm(document, section)
        elif section["type"] in FINANCIAL_TYPES:
            _financial(document, section)
        elif section["type"] == "inventory":
            _inventory(document, section)
        elif section["type"] == "crm":
            _crm(document, section)
        elif section["type"] == "contacts":
            _contacts(document, section)
        elif section["type"] == "transfers":
            _transfers(document, section)
        else:
            _tasks(document, section)
        _appendix(document, section, timezone, report_type=result.get("report_type"))
    document.add_heading("Nguồn và giới hạn dữ liệu", 1)
    for warning in result.get("warnings", []):
        _paragraph(document, warning)
    _paragraph(document, "Nguồn dữ liệu là Odoo theo quyền tài khoản đã liên kết. Tổng tiền được trình bày riêng theo công ty và tiền tệ; báo cáo không tự quy đổi tiền tệ.")
    footer = page.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("Trang ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    for run in footer.runs:
        run.font.name = "Calibri"
        run.font.size = Pt(9)
    for border in document.element.xpath(".//w:pPr/w:pBdr"):
        border.getparent().remove(border)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()
