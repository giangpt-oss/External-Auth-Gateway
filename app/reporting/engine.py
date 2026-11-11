"""Deterministic calculations over complete Odoo record sets."""
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.reporting.schemas import ReportRequest

MODELS = {
    "revenue": ("account.move", ["id", "name", "partner_id", "invoice_date", "move_type", "state", "company_id", "currency_id", "amount_untaxed", "amount_tax", "amount_total"]),
    "sales": ("sale.order", ["id", "name", "partner_id", "date_order", "state", "user_id", "company_id", "currency_id", "amount_untaxed", "amount_tax", "amount_total"]),
    "inventory": ("stock.quant", ["id", "product_id", "location_id", "company_id", "quantity", "reserved_quantity"]),
    "tasks": ("project.task", ["id", "name", "project_id", "stage_id", "company_id", "date_deadline", "create_date"]),
}
FINANCIAL_PROFILES = {"quotations", "purchases", "purchase_rfqs"}
NEW_PROFILES = FINANCIAL_PROFILES | {"crm", "contacts", "transfers"}
ORDER_FIELDS = ["id", "name", "partner_id", "state", "user_id", "company_id", "currency_id", "amount_untaxed", "amount_tax", "amount_total"]
MODELS.update({
    "quotations": ("sale.order", ORDER_FIELDS + ["create_date", "validity_date"]),
    "purchases": ("purchase.order", ORDER_FIELDS + ["date_approve"]),
    "purchase_rfqs": ("purchase.order", ORDER_FIELDS + ["create_date"]),
    "crm": ("crm.lead", ["id", "name", "partner_id", "type", "active", "stage_id", "user_id", "company_id", "expected_revenue", "probability", "date_deadline", "create_date"]),
    "contacts": ("res.partner", ["id", "name", "company_id", "active", "is_company", "parent_id", "email", "phone", "customer_rank", "supplier_rank", "create_date"]),
    "transfers": ("stock.picking", ["id", "name", "company_id", "partner_id", "state", "picking_type_code", "date_done", "scheduled_date", "location_id", "location_dest_id", "origin"]),
})

LABELS = {"revenue": "Doanh thu theo hóa đơn", "sales": "Doanh số theo đơn bán", "inventory": "Tồn kho hiện tại", "tasks": "Công việc Odoo Project"}
LABELS.update({"quotations": "Báo giá bán hàng", "purchases": "Đơn mua đã xác nhận",
               "purchase_rfqs": "Yêu cầu báo giá mua hàng", "crm": "Cơ hội và khách hàng tiềm năng CRM",
               "contacts": "Liên hệ", "transfers": "Phiếu kho đã hoàn tất",
               "employee_weekly": "Báo cáo công việc tuần của nhân viên"})



def identity(value):
    return value[0] if isinstance(value, tuple | list) and value else value


def label(value):
    return str(value[1]) if isinstance(value, tuple | list) and len(value) > 1 else str(value or "Chưa xác định")


def decimal(value):
    return Decimal(str(value or 0))


def capability(ctx, report_types=None):
    requested = set(report_types or MODELS)
    kinds = requested - {"commercial_summary", "employee_weekly"}
    if "commercial_summary" in requested:
        kinds |= {"revenue", "sales"}
    if "employee_weekly" in requested:
        kinds |= {"tasks"}
    result = {}
    for kind in sorted(kinds):
        if kind not in MODELS:
            result[kind] = {"available": False, "reason": "unsupported_report_type"}
            continue
        model, required = MODELS[kind]
        try:
            fields = ctx.read_fields(model)
            can_read = ctx.call(model, "check_access_rights", ["read", False])
            missing = sorted(set(required) - set(fields))
            if kind == "crm":
                if not crm_currency_field(fields):
                    missing.append("company_currency|company_currency_id|currency_id")
                stage_fields = ctx.read_fields("crm.stage")
                if "is_won" not in stage_fields or not ctx.call("crm.stage", "check_access_rights", ["read", False]):
                    missing.append("crm.stage.is_won")
            result[kind] = {"available": bool(can_read) and not missing, "model": model, "missing_fields": missing}
        except Exception as exc:
            result[kind] = {"available": False, "model": model, "reason": type(exc).__name__}
    if report_types is None or "commercial_summary" in requested:
        result["commercial_summary"] = {"available": all(result[k]["available"] for k in ("revenue", "sales"))}
    if report_types is None or "employee_weekly" in requested:
        result["employee_weekly"] = {"available": result.get("tasks", {}).get("available", True)}
    return result


def fetch_all(ctx, model, fields, domain, checkpoint, rpc_context=None):
    """Keyset pagination, bounded by cancellation/lease rather than a silent row cap."""
    baseline = ctx.call(model, "search_count", [domain], {"context": rpc_context or {}})
    if not isinstance(baseline, int):
        raise ValueError("Odoo không trả số lượng bản ghi hợp lệ.")
    rows, last_id = [], 0
    while True:
        checkpoint(len(rows))
        page = ctx.call(model, "search_read", [domain + [("id", ">", last_id)]], {
            "fields": fields, "limit": 500, "order": "id asc", "context": rpc_context or {},
        })
        if not isinstance(page, list):
            raise ValueError("Odoo trả dữ liệu không hợp lệ.")
        if not page:
            break
        ids = [r["id"] for r in page]
        if ids != sorted(set(ids)) or ids[0] <= last_id:
            raise ValueError("Phân trang Odoo không ổn định; hãy lấy lại báo cáo.")
        rows.extend(page)
        last_id = ids[-1]
    if len(rows) != baseline or ctx.call(model, "search_count", [domain], {"context": rpc_context or {}}) != baseline:
        raise ValueError("Dữ liệu thay đổi trong lúc đọc; hãy chạy lại báo cáo.")
    return rows


def period_domain(req, field, field_type):
    start = req.date_from
    end = req.date_to + timedelta(days=1)
    if field_type == "datetime":
        tz = ZoneInfo(req.timezone)
        start = datetime.combine(start, time.min, tz).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.combine(end, time.min, tz).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    else:
        start, end = start.isoformat(), end.isoformat()
    return [(field, ">=", start), (field, "<", end)]


def generate_employee_weekly(ctx, req: ReportRequest, checkpoint, started, companies, warnings):
    target_uid = None
    emp_profile = {}
    has_employee = False
    emp_fields = {}
    try:
        emp_fields = ctx.read_fields("hr.employee")
        has_employee = "name" in emp_fields
    except Exception:
        has_employee = False

    if has_employee:
        emp_domain = []
        if req.employee_id:
            emp_domain = [("id", "=", req.employee_id)]
        elif req.assignee_id:
            emp_domain = [("user_id", "=", req.assignee_id)]
        elif req.employee_name:
            emp_domain = [("name", "ilike", req.employee_name.strip())]
        else:
            emp_domain = [("user_id", "=", ctx.user.odoo_uid)]

        req_fields = [f for f in ["id", "name", "job_title", "department_id", "work_email", "work_phone", "parent_id", "user_id"] if f in emp_fields]
        emp_rows = ctx.call("hr.employee", "search_read", [emp_domain], {"fields": req_fields, "limit": 1})
        if emp_rows:
            emp = emp_rows[0]
            target_uid = identity(emp.get("user_id")) or ctx.user.odoo_uid
            emp_profile = {
                "id": emp.get("id"),
                "name": emp.get("name") or "Chưa rõ",
                "job_title": label(emp.get("job_title")),
                "department": label(emp.get("department_id")),
                "manager": label(emp.get("parent_id")),
                "email": emp.get("work_email") or "Chưa có email",
                "phone": emp.get("work_phone") or "Chưa có SĐT",
                "user_id": target_uid,
            }

    if not target_uid:
        target_uid = req.assignee_id or ctx.user.odoo_uid
        emp_profile = {
            "id": None,
            "name": req.employee_name or ctx.user.name or ctx.user.login,
            "job_title": "Nhân viên",
            "department": "Chưa phân bổ",
            "manager": "Chưa xác định",
            "email": ctx.user.login,
            "phone": "Chưa có SĐT",
            "user_id": target_uid,
        }

    date_from_str = req.date_from.strftime("%Y-%m-%d 00:00:00") if req.date_from else None
    date_to_str = req.date_to.strftime("%Y-%m-%d 23:59:59") if req.date_to else None
    sections = []

    sections.append({
        "type": "employee_profile",
        "title": "Thông tin nhân sự",
        "model": "hr.employee",
        "count": 1,
        "summary": [emp_profile],
        "rows": [emp_profile],
    })

    try:
        task_fields = ctx.read_fields("project.task")
        assignee_f = "user_ids" if "user_ids" in task_fields else ("user_id" if "user_id" in task_fields else None)
        if assignee_f:
            t_domain = [(assignee_f, "in", [target_uid])]
            if companies:
                t_domain.append(("company_id", "in", companies))
            req_t_fields = [f for f in ["id", "name", "project_id", "stage_id", "state", "is_closed", "date_deadline", "create_date", "write_date"] if f in task_fields]
            task_rows = fetch_all(ctx, "project.task", req_t_fields, t_domain, checkpoint)

            today_iso = date.today().isoformat()
            done_tasks = []
            overdue_tasks = []
            inprogress_tasks = []
            for t in task_rows:
                is_done = bool(t.get("is_closed")) or t.get("state") in ("1_done", "done")
                deadline = str(t.get("date_deadline") or "")[:10]
                if is_done:
                    done_tasks.append(t)
                elif deadline and deadline < today_iso:
                    overdue_tasks.append(t)
                else:
                    inprogress_tasks.append(t)

            task_summary_data = {
                "total_tasks": len(task_rows),
                "done_count": len(done_tasks),
                "overdue_count": len(overdue_tasks),
                "inprogress_count": len(inprogress_tasks),
                "done_rate": f"{round(len(done_tasks) / len(task_rows) * 100, 1)}%" if task_rows else "0%",
            }
            sections.append({
                "type": "tasks",
                "title": "Công việc & Dự án",
                "model": "project.task",
                "count": len(task_rows),
                "summary": [task_summary_data],
                "rows": task_rows,
            })
    except Exception as exc:
        warnings.append(f"Không thể đọc công việc Project: {exc}")

    try:
        act_fields = ctx.read_fields("mail.activity")
        if "user_id" in act_fields:
            act_domain = [("user_id", "=", target_uid)]
            req_act_f = [f for f in ["id", "res_model", "res_name", "activity_type_id", "summary", "date_deadline", "state", "create_date"] if f in act_fields]
            act_rows = ctx.call("mail.activity", "search_read", [act_domain], {"fields": req_act_f, "limit": 100, "order": "date_deadline asc"})
            act_summary = {"total_activities": len(act_rows), "types": {}}
            for a in act_rows:
                t_name = label(a.get("activity_type_id"))
                act_summary["types"][t_name] = act_summary["types"].get(t_name, 0) + 1
            sections.append({
                "type": "activities",
                "title": "Hoạt động & Lịch làm việc",
                "model": "mail.activity",
                "count": len(act_rows),
                "summary": [act_summary],
                "rows": act_rows,
            })
    except Exception:
        pass

    try:
        sale_fields = ctx.read_fields("sale.order")
        if "user_id" in sale_fields and "amount_total" in sale_fields:
            s_domain = [("user_id", "=", target_uid)]
            if date_from_str and date_to_str:
                s_domain += [("date_order", ">=", date_from_str), ("date_order", "<=", date_to_str)]
            if companies:
                s_domain.append(("company_id", "in", companies))
            req_s_f = [f for f in ["id", "name", "partner_id", "date_order", "state", "amount_total", "currency_id"] if f in sale_fields]
            sale_rows = fetch_all(ctx, "sale.order", req_s_f, s_domain, checkpoint)

            quotes = [s for s in sale_rows if s.get("state") in ("draft", "sent")]
            confirmed = [s for s in sale_rows if s.get("state") in ("sale", "done")]
            total_rev = sum(decimal(s.get("amount_total")) for s in confirmed)
            quote_val = sum(decimal(s.get("amount_total")) for s in quotes)

            sale_summary = {
                "total_orders": len(sale_rows),
                "quotes_count": len(quotes),
                "quotes_value": str(quote_val),
                "confirmed_count": len(confirmed),
                "confirmed_value": str(total_rev),
            }
            sections.append({
                "type": "sales",
                "title": "Đơn hàng & Báo giá",
                "model": "sale.order",
                "count": len(sale_rows),
                "summary": [sale_summary],
                "rows": sale_rows,
            })
    except Exception:
        pass

    try:
        crm_fields = ctx.read_fields("crm.lead")
        if "user_id" in crm_fields:
            c_domain = [("user_id", "=", target_uid)]
            if date_from_str and date_to_str:
                c_domain += [("create_date", ">=", date_from_str), ("create_date", "<=", date_to_str)]
            if companies:
                c_domain.append(("company_id", "in", companies))
            req_c_f = [f for f in ["id", "name", "partner_id", "stage_id", "expected_revenue", "probability"] if f in crm_fields]
            crm_rows = fetch_all(ctx, "crm.lead", req_c_f, c_domain, checkpoint)
            crm_summary = {
                "total_leads": len(crm_rows),
                "expected_revenue": str(sum(decimal(c.get("expected_revenue")) for c in crm_rows)),
            }
            sections.append({
                "type": "crm",
                "title": "Cơ hội khách hàng CRM",
                "model": "crm.lead",
                "count": len(crm_rows),
                "summary": [crm_summary],
                "rows": crm_rows,
            })
    except Exception:
        pass

    return {
        "report_type": "employee_weekly",
        "parameters": req.model_dump(mode="json", exclude={"telegram_user_id", "chat_id", "telegram_binding_id"}),
        "company_ids": companies,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "warnings": list(dict.fromkeys(warnings)),
        "sections": sections,
    }


def generate(ctx, req: ReportRequest, checkpoint):
    started = datetime.now(UTC)
    companies = ctx.selected_companies(req.company_ids)
    sections, warnings = [], ["Dữ liệu là trạng thái khi truy vấn, không phải snapshot giao dịch."]
    if req.report_type == "employee_weekly":
        return generate_employee_weekly(ctx, req, checkpoint, started, companies, warnings)
    kinds = ["revenue", "sales"] if req.report_type == "commercial_summary" else [req.report_type]
    for kind in kinds:
        if kind in NEW_PROFILES:
            section, profile_warnings = profile_section(ctx, req, kind, companies, checkpoint, started)
            sections.append(section)
            warnings.extend(profile_warnings)
            continue
        model, required = MODELS[kind]
        metadata = ctx.read_fields(model)
        missing = set(required) - set(metadata)
        if missing:
            raise ValueError(f"{model} thiếu trường: {', '.join(sorted(missing))}")
        fields = list(required)
        if "write_date" in metadata:
            fields.append("write_date")
        domain = [("company_id", "in", companies)]
        if kind == "revenue":
            domain += [("state", "=", "posted"), ("move_type", "in", ["out_invoice", "out_refund"])]
            date_field = "invoice_date"
        elif kind == "sales":
            valid_states = {v[0] for v in metadata["state"].get("selection", [])}
            if "sale" not in valid_states:
                raise ValueError("Không xác minh được trạng thái đơn bán đã xác nhận của Odoo.")
            domain += [("state", "in", [s for s in ("sale", "done") if s in valid_states])]
            date_field = "date_order"
        elif kind == "inventory":
            domain += [("location_id.usage", "=", "internal")]
            if req.warehouse_id:
                warehouses = ctx.call("stock.warehouse", "search_read", [[("id", "=", req.warehouse_id), ("company_id", "in", companies)]], {"fields": ["view_location_id"], "limit": 1})
                if not warehouses:
                    raise ValueError("Không tìm thấy kho trong phạm vi công ty.")
                domain += [("location_id", "child_of", identity(warehouses[0]["view_location_id"]))]
            if req.product_id:
                domain += [("product_id", "=", req.product_id)]
            date_field = None
        else:
            date_field = req.task_date_field
            if "user_ids" in metadata:
                fields.append("user_ids")
                assignee_field = "user_ids"
            elif "user_id" in metadata:
                fields.append("user_id")
                assignee_field = "user_id"
            else:
                assignee_field = None
            for optional in ("is_closed", "state"):
                if optional in metadata:
                    fields.append(optional)
            if req.project_id:
                domain += [("project_id", "=", req.project_id)]
            if req.assignee_id:
                if not assignee_field:
                    raise ValueError("Odoo không có trường người phụ trách được hỗ trợ.")
                domain += [(assignee_field, "in", [req.assignee_id])]
        if date_field:
            domain += period_domain(req, date_field, metadata[date_field]["type"])
        if kind in ("revenue", "sales") and req.partner_id:
            domain += [("partner_id", "=", req.partner_id)]
        if kind == "sales" and req.assignee_id:
            domain += [("user_id", "=", req.assignee_id)]
        if kind == "sales" and req.record_scope == "mine":
            domain += [("user_id", "=", ctx.user.odoo_uid)]
        rows = fetch_all(ctx, model, fields, domain, checkpoint)
        # Detect updates of records still in the report scope. Deletions/moves out of
        # scope are covered by count checks; a database snapshot is not promised.
        if "write_date" in metadata and ctx.call(model, "search_count", [domain + [("write_date", ">=", started.strftime("%Y-%m-%d %H:%M:%S"))]]):
            warnings.append(f"{LABELS[kind]}: có chứng từ thay đổi trong lúc đọc, nên lấy lại để đối chiếu.")
        if kind in ("revenue", "sales"):
            summary = money_summary(rows, kind, req.group_by)
            if kind == "sales":
                warnings.append("Doanh số không tự giảm theo hóa đơn hoàn tiền.")
        elif kind == "inventory":
            rows, summary = inventory_summary(ctx, rows, checkpoint)
            warnings.append("Tồn khả dụng = tồn thực tế − đã giữ; không bao gồm dự báo hoặc định giá tồn kho.")
        else:
            rows, summary, task_warnings = tasks_summary(ctx, rows, req)
            warnings.extend(task_warnings)
        sections.append({"type": kind, "title": LABELS[kind], "model": model, "count": len(rows), "summary": summary, "rows": rows})
    return {"report_type": req.report_type, "parameters": req.model_dump(mode="json", exclude={"telegram_user_id", "chat_id", "telegram_binding_id"}), "company_ids": companies, "started_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(), "warnings": list(dict.fromkeys(warnings)), "sections": sections}


def money_summary(rows, kind, group_by):
    totals = defaultdict(lambda: {"count": 0, "refund_count": 0, "untaxed": Decimal(0), "tax": Decimal(0), "total": Decimal(0)})
    group_field = "user_id" if group_by == "salesperson" else "partner_id"
    descriptions = {}
    for row in rows:
        sign = -1 if kind == "revenue" and row["move_type"] == "out_refund" else 1
        # Standard Odoo invoice amounts are unsigned; normalize once by move type.
        for group_id, group_name in (("__total__", "Tổng"), (identity(row.get(group_field)), label(row.get(group_field)))):
            key = (identity(row["company_id"]), identity(row["currency_id"]), group_id)
            descriptions[key] = {"company": label(row["company_id"]), "company_id": identity(row["company_id"]),
                                 "currency": label(row["currency_id"]), "currency_id": identity(row["currency_id"]),
                                 "group": group_name, "is_total": group_id == "__total__"}
            item = totals[key]
            item["count"] += 1
            item["refund_count"] += int(sign == -1)
            for source, target in (("amount_untaxed", "untaxed"), ("amount_tax", "tax"), ("amount_total", "total")):
                item[target] += decimal(row[source]) * sign
        row["report_sign"] = sign
    return [{**descriptions[key], **{k: str(v) if isinstance(v, Decimal) else v for k, v in item.items()}} for key, item in totals.items()]


def inventory_summary(ctx, rows, checkpoint):
    product_ids = sorted({identity(r["product_id"]) for r in rows})
    products = {}
    for offset in range(0, len(product_ids), 500):
        checkpoint(len(rows))
        for product in ctx.call("product.product", "read", [product_ids[offset:offset + 500]], {"fields": ["default_code", "uom_id"]}):
            products[product["id"]] = product
    totals = {}
    for row in rows:
        product = products.get(identity(row["product_id"]))
        if not product:
            raise ValueError("Không đọc đủ thông tin sản phẩm để lập tồn kho.")
        key = (identity(row["company_id"]), identity(row["product_id"]), identity(row["location_id"]), identity(product["uom_id"]))
        item = totals.setdefault(key, {"company": label(row["company_id"]), "product": label(row["product_id"]), "sku": product.get("default_code") or "", "location": label(row["location_id"]), "uom": label(product["uom_id"]), "quantity": Decimal(0), "reserved": Decimal(0), "source_ids": []})
        item["quantity"] += decimal(row["quantity"])
        item["reserved"] += decimal(row["reserved_quantity"])
        item["source_ids"].append(row["id"])
    detailed = [{**v, "available": v["quantity"] - v["reserved"]} for v in totals.values()]
    detailed = [{k: str(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in detailed]
    return detailed, [{"product_location_count": len(detailed), "source_quant_count": len(rows)}]


def tasks_summary(ctx, rows, req):
    stages = defaultdict(int)
    warnings = ["Công việc được lọc theo hạn chót/ngày tạo; trạng thái là trạng thái hiện tại, không phải số hoàn thành trong kỳ."]
    can_closed = "is_closed" in ctx.read_fields("project.task")
    state_meta = ctx.read_fields("project.task").get("state", {})
    terminals = {x[0] for x in state_meta.get("selection", [])} & {"1_done", "1_canceled"}
    has_completion = can_closed or bool(terminals)
    overdue = done = 0
    today = datetime.now(ZoneInfo(req.timezone)).date().isoformat()
    user_ids = set()
    for row in rows:
        user_ids.update(row.get("user_ids") or [])
        if row.get("user_id"):
            user_ids.add(identity(row["user_id"]))
    names = {}
    for offset in range(0, len(user_ids), 500):
        batch = sorted(user_ids)[offset:offset + 500]
        # Missing user names are explicit IDs rather than fabricated display names.
        for user in ctx.call("res.users", "search_read", [[("id", "in", batch)]], {"fields": ["id", "name"], "limit": 500}):
            names[user["id"]] = user["name"]
    for row in rows:
        assigned = row.get("user_ids") or ([identity(row["user_id"])] if row.get("user_id") else [])
        row["assignees"] = [names.get(uid, f"ID {uid}") for uid in assigned]
        closed = bool(row.get("is_closed")) if can_closed else row.get("state") in terminals
        row["completion_known"] = has_completion
        row["report_closed"] = closed if has_completion else None
        deadline = row.get("date_deadline")
        if deadline and len(str(deadline)) > 10:
            deadline = datetime.fromisoformat(str(deadline)).replace(tzinfo=UTC).astimezone(ZoneInfo(req.timezone)).date().isoformat()
        # When closure is unknown, avoid incorrectly calling a completed task overdue.
        row["report_overdue"] = bool(deadline and str(deadline)[:10] < today and has_completion and not closed)
        overdue += int(row["report_overdue"])
        done += int(closed)
        stages[(label(row.get("project_id")), label(row.get("stage_id")))] += 1
    if not has_completion:
        warnings.append("Chưa xác định được trạng thái hoàn thành; không tính tỷ lệ hoàn thành hoặc số quá hạn.")
    summary = [{"project": p, "stage": s, "count": n} for (p, s), n in stages.items()]
    summary.append({"total_tasks": len(rows), "closed_current": done if has_completion else None, "overdue_current": overdue if has_completion else None})
    return rows, summary, warnings



def crm_currency_field(fields):
    return next((name for name in ("company_currency", "company_currency_id", "currency_id")
                 if fields.get(name, {}).get("type") == "many2one"), None)


def company_domain(kind, companies):
    if kind in {"crm", "contacts"}:
        return ["|", ("company_id", "=", False), ("company_id", "in", companies)]
    return [("company_id", "in", companies)]


def selected_states(metadata, wanted, required):
    available = {value[0] for value in metadata["state"].get("selection", [])}
    if not set(required).issubset(available):
        raise ValueError("Không xác minh được trạng thái nghiệp vụ yêu cầu trên Odoo.")
    return [value for value in wanted if value in available]


def profile_section(ctx, req, kind, companies, checkpoint, started):
    model, required = MODELS[kind]
    metadata = ctx.read_fields(model)
    missing = set(required) - set(metadata)
    if missing:
        raise ValueError(f"{model} thiếu trường: {', '.join(sorted(missing))}")
    fields = list(required)
    for optional in ("write_date", "user_id", "team_id", "date_closed", "date_open"):
        if optional in metadata and optional not in fields:
            fields.append(optional)
    domain = company_domain(kind, companies)
    warnings = ["Phạm vi là bản ghi tài khoản được quyền đọc, không mặc định bằng toàn bộ cơ sở dữ liệu."]
    rpc_context = {}
    if kind == "quotations":
        domain += [("state", "in", selected_states(metadata, ("draft", "sent"), ("draft", "sent")))]
        date_field = "create_date"
        warnings.append("Báo giá chỉ gồm nháp và đã gửi; loại đơn đã xác nhận và chứng từ hủy. Kỳ lọc theo ngày tạo.")
    elif kind == "purchases":
        domain += [("state", "in", selected_states(metadata, ("purchase", "done"), ("purchase",)))]
        date_field = "date_approve"
        warnings.append("Đơn mua đã xác nhận được lọc theo ngày xác nhận, không dùng hạn RFQ date_order làm ngày mua.")
    elif kind == "purchase_rfqs":
        domain += [("state", "in", selected_states(metadata, ("draft", "sent", "to approve"), ("draft", "sent")))]
        date_field = "create_date"
        warnings.append("RFQ gồm nháp, đã gửi và chờ phê duyệt nếu có; loại đơn mua đã xác nhận và chứng từ hủy. Kỳ lọc theo ngày tạo.")
    elif kind == "crm":
        if req.stage_id:
            if ctx.call("crm.stage", "search_count", [[("id", "=", req.stage_id)]]) != 1:
                raise ValueError("Không tìm thấy giai đoạn CRM trong phạm vi được quyền đọc.")
            domain += [("stage_id", "=", req.stage_id)]
        if req.crm_type != "all":
            available_types = {entry[0] for entry in metadata["type"].get("selection", [])}
            if req.crm_type not in available_types:
                raise ValueError("Phân loại lead/cơ hội không được Odoo hỗ trợ.")
            domain += [("type", "=", req.crm_type)]
        if req.crm_status in {"all", "archived"}:
            rpc_context["active_test"] = False
        if req.crm_status == "archived":
            domain += [("active", "=", False)]
        elif req.crm_status != "all":
            domain += [("active", "=", True)]
        if req.crm_status == "won":
            stage_meta = ctx.read_fields("crm.stage")
            if "is_won" not in stage_meta:
                raise ValueError("Không xác minh được giai đoạn thành công của CRM.")
            won_ids = ctx.call("crm.stage", "search", [[("is_won", "=", True)]])
            domain += [("stage_id", "in", won_ids)]
        currency_field = crm_currency_field(metadata)
        if not currency_field:
            raise ValueError("Không xác minh được tiền tệ của doanh thu dự kiến CRM.")
        fields.append(currency_field)
        date_field = "create_date" if req.date_from else None
        warnings.extend(["CRM là trạng thái hiện tại; nếu có kỳ thì lọc theo ngày tạo, không suy ra số thắng/thua trong kỳ.",
                         "Doanh thu dự kiến CRM không phải doanh thu đã ghi nhận. Bản ghi lưu trữ không tự được coi là cơ hội thua."])
    elif kind == "contacts":
        domain += [("active", "=", True)]
        if req.contact_kind in {"companies", "individuals"}:
            domain += [("is_company", "=", req.contact_kind == "companies")]
        elif req.contact_kind == "customers":
            domain += [("customer_rank", ">", 0)]
        elif req.contact_kind == "suppliers":
            domain += [("supplier_rank", ">", 0)]
        if req.partner_id:
            domain += [("id", "=", req.partner_id)]
        date_field = "create_date" if req.date_from else None
        warnings.append("Liên hệ gồm bản ghi đang hoạt động theo phân loại đã chọn. Một liên hệ có thể đồng thời là khách hàng và nhà cung cấp; không cộng hai nhóm thành tổng duy nhất.")
    else:
        domain += [("state", "in", selected_states(metadata, ("done",), ("done",)))]
        if req.transfer_type != "all":
            options = {entry[0] for entry in metadata["picking_type_code"].get("selection", [])}
            if req.transfer_type not in options:
                raise ValueError("Loại nghiệp vụ nhập/xuất/nội bộ không được Odoo hỗ trợ.")
            domain += [("picking_type_code", "=", req.transfer_type)]
        if req.warehouse_id:
            warehouse = ctx.call("stock.warehouse", "search_read", [[("id", "=", req.warehouse_id), ("company_id", "in", companies)]], {"fields": ["view_location_id"], "limit": 1})
            if not warehouse:
                raise ValueError("Không tìm thấy kho trong phạm vi đã chọn.")
            location = identity(warehouse[0]["view_location_id"])
            if req.transfer_type == "incoming":
                domain += [("location_dest_id", "child_of", location)]
            elif req.transfer_type == "outgoing":
                domain += [("location_id", "child_of", location)]
            else:
                domain += ["|", ("location_id", "child_of", location), ("location_dest_id", "child_of", location)]
        if req.product_id:
            if "product_id" not in metadata:
                raise ValueError("Odoo không hỗ trợ bộ lọc sản phẩm trên phiếu kho.")
            domain += [("product_id", "=", req.product_id)]
        date_field = "date_done"
        warnings.append("Phiếu kho chỉ gồm hoàn tất và lọc theo ngày hoàn tất thực tế, không theo ngày dự kiến. Không cộng số lượng khác đơn vị tính.")
    if req.record_scope == "mine" or req.assignee_id:
        if "user_id" not in metadata:
            raise ValueError("Model không có trường người phụ trách cho phạm vi yêu cầu.")
        domain += [("user_id", "=", ctx.user.odoo_uid if req.record_scope == "mine" else req.assignee_id)]
    if req.partner_id and kind != "contacts":
        domain += [("partner_id", "=", req.partner_id)]
    if date_field:
        domain += period_domain(req, date_field, metadata[date_field]["type"])
    rows = fetch_all(ctx, model, fields, domain, checkpoint, rpc_context)
    if "write_date" in metadata and ctx.call(model, "search_count", [domain + [("write_date", ">=", started.strftime("%Y-%m-%d %H:%M:%S"))]], {"context": rpc_context}):
        warnings.append("Có bản ghi thay đổi trong lúc đọc; nên lấy lại báo cáo để đối chiếu.")
    if kind in FINANCIAL_PROFILES:
        summary = money_summary(rows, kind, req.group_by)
    elif kind == "crm":
        rows, summary = crm_summary(ctx, rows, crm_currency_field(metadata))
    elif kind == "contacts":
        summary = [{"total_contacts": len(rows),
                    "companies": sum(row.get("is_company") is True for row in rows),
                    "individuals": sum(row.get("is_company") is False for row in rows),
                    "unknown_company_type": sum(row.get("is_company") is None for row in rows),
                    "missing_email": sum(not str(row.get("email") or "").strip() for row in rows),
                    "missing_phone": sum(not str(row.get("phone") or "").strip() for row in rows)}]
        if req.group_by == "company":
            groups = {}
            for row in rows:
                if row.get("is_company"):
                    group_id, group_name = row["id"], row["name"]
                else:
                    group_id = identity(row.get("parent_id")) or False
                    group_name = label(row["parent_id"]) if group_id else "Chưa gắn tổ chức"
                group = groups.setdefault(group_id, {"related_company_id": group_id, "related_company": group_name, "count": 0})
                group["count"] += 1
            summary = [*groups.values(), *summary]
            warnings.append("Nhóm liên hệ theo tổ chức liên quan parent_id hoặc bản ghi tổ chức; tổng nhóm là số bản ghi liên hệ, không phải số khách hàng duy nhất.")
    else:
        counts = defaultdict(int)
        for row in rows:
            operation = row["picking_type_code"]
            if operation not in {"incoming", "outgoing", "internal"}:
                raise ValueError("Có phiếu kho chưa xác định được loại nghiệp vụ; không tự phân loại nhập/xuất/nội bộ.")
            counts[operation] += 1
        summary = [{"transfer_type": key, "count": value} for key, value in sorted(counts.items())]
        summary.append({"total_transfers": len(rows)})
    return {"type": kind, "title": LABELS[kind], "model": model, "source_fields": fields, "count": len(rows), "summary": summary, "rows": rows}, warnings


def crm_summary(ctx, rows, currency_field):
    stage_ids = sorted({identity(row["stage_id"]) for row in rows if row.get("stage_id")})
    stage_meta = ctx.read_fields("crm.stage")
    if "is_won" not in stage_meta:
        raise ValueError("Không đọc được cờ thành công của giai đoạn CRM.")
    stages = {}
    for offset in range(0, len(stage_ids), 500):
        batch = stage_ids[offset:offset + 500]
        found = ctx.call("crm.stage", "search_read", [[("id", "in", batch)]], {"fields": ["id", "is_won"], "limit": 500})
        stages.update({stage["id"]: bool(stage["is_won"]) for stage in found})
        if not set(batch).issubset(stages):
            raise ValueError("Không đọc đủ giai đoạn CRM; chưa thể xác định cơ hội thành công.")
    totals, descriptions = {}, {}
    for row in rows:
        currency = row.get(currency_field)
        if not currency or not isinstance(currency, list | tuple) or len(currency) < 2:
            raise ValueError("CRM thiếu tiền tệ rõ ràng; không tổng hợp doanh thu dự kiến bằng giả định.")
        row["currency_id"] = currency
        row["report_won"] = stages.get(identity(row.get("stage_id")), False)
        company_id = identity(row.get("company_id")) or False
        company_name = label(row["company_id"]) if company_id else "Dùng chung không gắn công ty"
        for group in (None, identity(row.get("stage_id")) or False):
            key = (company_id, identity(currency), group)
            item = totals.setdefault(key, {"count": 0, "won_count": 0, "archived_count": 0, "lead_count": 0, "opportunity_count": 0, "expected_revenue": Decimal(0)})
            descriptions[key] = {"company_id": company_id, "company": company_name,
                                 "currency_id": identity(currency), "currency": label(currency),
                                 "stage_id": group, "stage": "Tổng" if group is None else label(row.get("stage_id")),
                                 "is_crm_total": group is None}
            item["count"] += 1
            item["lead_count"] += int(row.get("type") == "lead")
            item["opportunity_count"] += int(row.get("type") == "opportunity")
            item["won_count"] += int(row["report_won"])
            item["archived_count"] += int(row.get("active") is False)
            item["expected_revenue"] += decimal(row.get("expected_revenue"))
    summary = []
    for key, item in totals.items():
        output = {**descriptions[key], **item, "expected_revenue": str(item["expected_revenue"])}
        if descriptions[key]["is_crm_total"]:
            output.update(total_crm_records=item["count"], total_opportunities=item["opportunity_count"], total_leads=item["lead_count"], total_won=item["won_count"], total_archived=item["archived_count"])
        summary.append(output)
    return rows, summary
