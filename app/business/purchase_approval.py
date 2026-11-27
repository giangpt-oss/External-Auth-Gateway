"""Versioned Telegram approval buttons & notifications for Odoo Purchase Orders (RFQ -> to approve -> purchase)."""
from __future__ import annotations

import base64
from datetime import date
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.business.models import BusinessPreparation, PurchaseApprovalToken
from app.business.service import BusinessError, BusinessService
from app.models import TelegramConnection, User
from app.odoo.client import OdooAccessError, OdooError
from app.reporting.schemas import TelegramScope
from app.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[0-9a-f]{24}$")
TOKEN_TTL_SECONDS = 7 * 24 * 3600
MAX_PDF_BYTES = 49 * 1024 * 1024
COMPOSER = "mail.compose.message"


def render_purchase_pdf(ctx, record: dict[str, Any]) -> tuple[str, bytes]:
    """Render Odoo RFQ / Purchase Order PDF via Odoo's transient mail composer attachment."""
    order_id = record["id"]
    company = record.get("company_id")
    company_id = company[0] if isinstance(company, (list, tuple)) and company else ctx.default_company_id
    ctx.selected_companies([company_id])
    orders = ctx.call(
        "purchase.order",
        "search_read",
        [[("id", "=", order_id), ("company_id", "=", company_id)]],
        {"fields": ["name", "state", "order_line"], "limit": 1},
    )
    if not orders:
        raise OdooAccessError("Không có quyền đọc đơn mua trong công ty này.")
    order = orders[0]
    lines = order.get("order_line") or []
    if lines and ctx.call("purchase.order.line", "search_count", [[("id", "in", lines)]]) != len(lines):
        raise OdooAccessError("Không có quyền đọc đầy đủ các dòng đơn mua.")

    is_confirmed = order.get("state") in ("purchase", "done")
    report_xml = ("purchase", "action_report_purchase_order") if is_confirmed else ("purchase", "report_purchase_quotation")
    tmpl_xml = ("purchase", "email_template_edi_purchase_done") if is_confirmed else ("purchase", "email_template_edi_purchase")

    model, report_id = ctx.call("ir.model.data", "check_object_reference", list(report_xml))
    if model != "ir.actions.report" or not report_id:
        raise OdooAccessError("Không tìm thấy mẫu in PDF đơn mua trên Odoo.")
    reports = ctx.call(
        model,
        "search_read",
        [[("id", "=", report_id), ("model", "=", "purchase.order"), ("report_type", "=", "qweb-pdf")]],
        {"fields": ["group_ids"]},
    )
    if not reports:
        raise OdooError("Mẫu in đơn mua không phải định dạng PDF.")
    groups = reports[0].get("group_ids") or []
    if groups:
        profile = ctx.call("res.users", "read", [[ctx.user.odoo_uid]], {"fields": ["all_group_ids"]})
        if not profile or not set(groups).intersection(profile[0].get("all_group_ids") or []):
            raise OdooAccessError("Tài khoản không thuộc nhóm được phép in đơn mua.")

    t_model, template_id = ctx.call("ir.model.data", "check_object_reference", list(tmpl_xml))
    if t_model != "mail.template" or not template_id:
        raise OdooAccessError("Không tìm thấy mẫu email đơn mua trên Odoo.")
    templates = ctx.call(t_model, "read", [[template_id]], {"fields": ["model", "report_template_ids", "attachment_ids"]})
    if not templates or templates[0].get("model") != "purchase.order" or report_id not in (templates[0].get("report_template_ids") or []):
        raise OdooError("Mẫu email đơn mua chưa gắn báo cáo PDF tương ứng.")

    composer_id = ctx.call(
        COMPOSER,
        "create",
        [{
            "model": "purchase.order",
            "res_ids": repr([order_id]),
            "composition_mode": "comment",
            "template_id": template_id,
            "body": False,
        }],
    )
    composers = ctx.call(COMPOSER, "read", [[composer_id]], {"fields": ["attachment_ids"]})
    static_ids = set(templates[0].get("attachment_ids") or [])
    generated_ids = [item for item in (composers[0].get("attachment_ids") or []) if item not in static_ids] if composers else []
    attachments = ctx.call(
        "ir.attachment",
        "search_read",
        [[
            ("id", "in", generated_ids),
            ("res_model", "=", COMPOSER),
            ("res_id", "=", 0),
            ("create_uid", "=", ctx.user.odoo_uid),
            ("type", "=", "binary"),
            ("mimetype", "=", "application/pdf"),
            ("file_size", "<=", MAX_PDF_BYTES),
        ]],
        {"fields": ["name", "datas"], "limit": 2, "context": {"bin_size": False}},
    )
    if len(attachments) != 1 or not attachments[0].get("datas"):
        raise OdooError("Odoo không trả về file PDF đơn mua hợp lệ.")
    attachment = attachments[0]
    content = base64.b64decode(attachment["datas"], validate=True)
    if not content.startswith(b"%PDF-") or len(content) > MAX_PDF_BYTES:
        raise OdooError("Dữ liệu trả về không phải file PDF đơn mua hợp lệ.")
    filename = attachment.get("name") or f"{order.get('name') or record.get('name') or order_id}.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename, content


class PurchaseStudioApproval:
    """Native Odoo Studio approval rules protecting purchase.order.button_confirm."""

    METHOD = "button_confirm"

    def __init__(self, ctx, company_id: int):
        self.ctx = ctx
        self.company_id = company_id
        self.client = ctx.service.client
        self.uid = ctx.user.odoo_uid
        self.secret = ctx.secret

    def _call(self, model: str, method: str, args: list, **kwargs):
        context = kwargs.get("context") or {}
        kwargs["context"] = {**context, "allowed_company_ids": [self.company_id], "force_company": self.company_id}
        return self.client.execute(self.uid, self.secret, model, method, args, kwargs)

    def inspect(self, record_id: int) -> dict[str, Any]:
        rows = self._call(
            "purchase.order",
            "search_read",
            [[("id", "=", record_id), ("company_id", "=", self.company_id)]],
            fields=["id", "name", "state"],
            limit=1,
        )
        if len(rows) != 1:
            raise OdooAccessError("Không có quyền xem đơn mua trong công ty đã chọn.")
        try:
            raw = self.client.studio_approval_spec(
                self.uid, self.secret, record_id, self.METHOD, self.company_id, model="purchase.order"
            )
        except OdooError:
            return {"status": "no_rules", "record": rows[0], "rules": [], "entries": [], "requests": []}
        matches = [val for key, val in raw.get("purchase.order", []) if key == [record_id, self.METHOD, False]]
        if not matches:
            return {"status": "no_rules", "record": rows[0], "rules": [], "entries": [], "requests": []}
        data = matches[0]
        rules = []
        for rule_id in data.get("rules", []):
            rule = raw.get("all_rules", {}).get(str(rule_id)) or raw.get("all_rules", {}).get(rule_id)
            if rule and rule.get("method") == self.METHOD and not rule.get("action_id"):
                rules.append(rule)
        if not rules:
            return {"status": "no_rules", "record": rows[0], "rules": [], "entries": [], "requests": []}
        entries = data.get("entries", [])
        approved = {item["rule_id"][0] for item in entries if item.get("approved") is True}
        rejected = {item["rule_id"][0] for item in entries if item.get("approved") is False}
        requests = self._call(
            "studio.approval.request",
            "search_read",
            [[("res_id", "=", record_id), ("rule_id", "in", [r["id"] for r in rules])]],
            fields=["id", "rule_id", "res_id"],
        )
        if all(r["id"] in approved for r in rules):
            status = "approved"
        elif any(r["id"] in rejected for r in rules):
            status = "rejected"
        elif requests:
            status = "pending"
        else:
            status = "not_requested"
        return {"status": status, "record": rows[0], "rules": rules, "entries": entries, "requests": requests}

    def require_approved(self, record_id: int) -> dict[str, Any]:
        state = self.inspect(record_id)
        if state["rules"] and state["status"] != "approved":
            raise OdooAccessError("Đơn mua chưa được phê duyệt đủ các bước trên Odoo.")
        return state

    def approver_uids(self, state: dict[str, Any]) -> list[int]:
        approved = {e["rule_id"][0] for e in state.get("entries") or [] if e.get("approved") is True}
        remaining = [r for r in state.get("rules") or [] if r["id"] not in approved] or (state.get("rules") or [])
        uids: set[int] = set()
        for rule in remaining:
            for uid in rule.get("approver_ids") or []:
                if type(uid) is int:
                    uids.add(uid)
            group = rule.get("approval_group_id")
            gid = group[0] if isinstance(group, (list, tuple)) and group else group
            if type(gid) is int:
                try:
                    groups = self._call("res.groups", "read", [[gid]], fields=["users"])
                    if groups:
                        uids.update(u for u in (groups[0].get("users") or []) if type(u) is int)
                except OdooError:
                    pass
        return sorted(uids)

    def request(self, record_id: int) -> dict[str, Any]:
        state = self.inspect(record_id)
        if not state["rules"]:
            return state
        if state["status"] in ("approved", "rejected"):
            raise OdooAccessError("Đơn mua đã có kết quả phê duyệt; không tạo lại yêu cầu.")
        if state["status"] == "not_requested":
            if any(rule.get("can_validate") is True for rule in state["rules"]):
                for rule in state["rules"]:
                    self._call("studio.approval.request", "create", [{"rule_id": rule["id"], "res_id": record_id}])
            else:
                self._call("studio.approval.rule", "check_approval", ["purchase.order", record_id, self.METHOD, False])
            state = self.inspect(record_id)
        if state["status"] != "pending" or not state["requests"]:
            raise OdooError("Odoo chưa xác nhận yêu cầu duyệt Studio đang chờ.")
        self._ensure_activities(record_id, state)
        return self.inspect(record_id)

    def _ensure_activities(self, record_id: int, state: dict[str, Any]) -> None:
        recipients = self.approver_uids(state)
        if not recipients:
            return
        try:
            existing = self._call(
                "mail.activity",
                "search_read",
                [[("res_model", "=", "purchase.order"), ("res_id", "=", record_id), ("user_id", "in", recipients)]],
                fields=["id", "user_id", "summary"],
            )
            notified = {r["user_id"][0] for r in existing if "grant" in str(r.get("summary") or "").casefold()}
            missing = set(recipients) - notified
            if not missing:
                return
            model = self._call("ir.model", "search_read", [[("model", "=", "purchase.order")]], fields=["id"], limit=1)
            todo = self._call(
                "ir.model.data",
                "search_read",
                [[("module", "=", "mail"), ("name", "=", "mail_activity_data_todo"), ("model", "=", "mail.activity.type")]],
                fields=["res_id"],
                limit=1,
            )
            if len(model) == 1 and len(todo) == 1:
                for uid in sorted(missing):
                    self._call("mail.activity", "create", [{
                        "res_model_id": model[0]["id"],
                        "res_id": record_id,
                        "activity_type_id": todo[0]["res_id"],
                        "user_id": uid,
                        "summary": "Grant Approval",
                        "date_deadline": date.today().isoformat(),
                    }])
        except OdooError as exc:
            logger.warning("Could not ensure PO approval activities: %s", type(exc).__name__)

    def decide(self, record_id: int, *, approved: bool) -> dict[str, Any]:
        state = self.inspect(record_id)
        if not state["rules"]:
            raise OdooAccessError("Đơn mua không có quy tắc duyệt Studio.")
        decided_ids = {e["rule_id"][0] for e in state["entries"] if e.get("rule_id")}
        eligible = [r for r in state["rules"] if r.get("can_validate") is True and r["id"] not in decided_ids]
        if not eligible:
            raise OdooAccessError("Tài khoản hiện tại không có bước phê duyệt nào đang chờ trên đơn mua này.")
        if not state["requests"]:
            raise OdooAccessError("Đơn mua chưa được gửi yêu cầu phê duyệt.")
        for rule in eligible:
            self._call("studio.approval.rule", "set_approval", [[rule["id"]]], res_id=record_id, approved=approved)
        return self.inspect(record_id)



def _ensure_table(sf) -> None:
    try:
        with sf() as db:
            PurchaseApprovalToken.__table__.create(bind=db.get_bind(), checkfirst=True)
    except Exception:
        logger.exception("Could not initialize purchase approval storage")
        raise


def requester_po_keyboard(token: str) -> list[list[dict[str, str]]]:
    return [[
        {"text": "Chỉnh sửa", "callback_data": f"pedit:{token}"},
        {"text": "Xác nhận gửi duyệt", "callback_data": f"pappr:{token}"},
    ]]


def approver_po_keyboard(token: str) -> list[list[dict[str, str]]]:
    return [[
        {"text": "Xác nhận duyệt", "callback_data": f"pgrant:{token}"},
        {"text": "Yêu cầu chỉnh sửa", "callback_data": f"pfeed:{token}"},
    ]]


def _load_valid_token(
    sf,
    *,
    token: str,
    role: str,
    user_id: int,
    tenant: str,
    database: str,
    scope: TelegramScope,
) -> PurchaseApprovalToken | None:
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    _ensure_table(sf)
    with sf() as db:
        row = db.get(PurchaseApprovalToken, token)
        if not row or row.role != role or row.active != 1:
            return None
        if (
            row.user_id != user_id
            or row.tenant != tenant
            or row.database != database
            or row.chat_id != scope.chat_id
            or row.telegram_binding_id != scope.telegram_binding_id
        ):
            return None
        if time.time() - (row.created_at or 0) > TOKEN_TTL_SECONDS:
            return None
        return row


def _find_purchase_manager_uids(ctx) -> set[int]:
    """Resolve the membership field from the schema for the deployed Odoo version."""
    try:
        ref = ctx.call("ir.model.data", "search_read",
                       [[("module", "=", "purchase"), ("name", "=", "group_purchase_manager")]],
                       {"fields": ["res_id"], "limit": 1})
        if not ref or not ref[0].get("res_id"):
            return set()
        fields = ctx.read_fields("res.groups")
        relation = next((name for name in ("user_ids", "users") if name in fields), None)
        if not relation:
            return set()
        groups = ctx.call("res.groups", "read", [[ref[0]["res_id"]]], {"fields": [relation]})
        return {uid for uid in groups[0].get(relation, []) if type(uid) is int} if groups else set()
    except OdooError as exc:
        logger.warning("Could not query purchase managers: %s", type(exc).__name__)
        return set()


def _save_review(db, ctx, *, token, user_id, scope, order_id, company_id):
    service = BusinessService(ctx)
    service.company_id = ctx.selected_companies([company_id])[0]
    record = service.record("purchase.order", order_id,
                            ["partner_id", "currency_id", "amount_total", "order_line"])
    service.track_children("purchase.order", record)
    now = time.time()
    db.add(BusinessPreparation(
        id="pr_" + token, user_id=user_id, tenant=ctx.user.tenant, database=ctx.user.database,
        binding_id=scope.telegram_binding_id, chat_id=str(scope.chat_id), action="purchase.review",
        data_json=json.dumps(list(service.snapshot.values()), ensure_ascii=False),
        created_at=now, expires_at=now + TOKEN_TTL_SECONDS,
    ))


def _execute_review(request, ctx, row, scope, action):
    """Use the same permission checks, document lock and durable ledger as Business actions."""
    from app.business.routes import execute_business_action, gate, principal
    from app.internal.actions import UserActionRequest

    gate(request.app.state.settings, action)
    sf = request.app.state.session_factory
    service = BusinessService(ctx)
    service.company_id = ctx.selected_companies([row.company_id])[0]
    prepared_id = "bp_" + row.token + "_" + action.rsplit(".", 1)[1]
    with sf() as db:
        prepared = db.get(BusinessPreparation, prepared_id)
        if not prepared:
            review = db.get(BusinessPreparation, "pr_" + row.token)
            if not review or review.expires_at <= time.time():
                return {"status": "stale", "message": "Bản xem trước đã hết hiệu lực. Hãy lấy lại thông tin đơn mua."}
            principal(review, ctx, scope)
            snapshot = json.loads(review.data_json)
            service.verify_snapshot(snapshot)
            payload = {"company_id": row.company_id, "target_id": row.order_id}
            result = service.prepare(action, payload)
            service.verify_snapshot(snapshot)
            if result["status"] != "ready":
                return result
            result.pop("_values", None)
            now = time.time()
            db.add(BusinessPreparation(
                id=prepared_id, user_id=row.user_id, tenant=row.tenant, database=row.database,
                binding_id=row.telegram_binding_id, chat_id=str(row.chat_id), action=action,
                data_json=json.dumps(result, ensure_ascii=False), created_at=now, expires_at=now + 300,
            ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # A simultaneous click prepared the same deterministic operation.
    response = execute_business_action(row.user_id, UserActionRequest(
        operation_id="purchase_" + row.token, action=action,
        telegram_user_id=scope.telegram_user_id, chat_id=scope.chat_id,
        telegram_binding_id=scope.telegram_binding_id, arguments={"prepared_id": prepared_id},
    ), request)
    return response.get("result") or response


def _format_po_summary(record: dict[str, Any], prefix: str | None = None) -> str:
    state_map = {
        "draft": "RFQ nháp",
        "sent": "RFQ đã gửi",
        "to approve": "Chờ phê duyệt",
        "purchase": "Đơn mua đã xác nhận",
        "done": "Đơn mua đã khóa",
        "cancel": "Đã hủy",
    }
    lines = [prefix] if prefix else []
    name = record.get("name") or f"PO #{record.get('id')}"
    st = state_map.get(record.get("state"), str(record.get("state") or ""))
    lines.append(f"Đơn mua {name} · {st}")
    partner = record.get("partner_id")
    if isinstance(partner, list | tuple) and len(partner) > 1:
        lines.append(f"Nhà cung cấp: {partner[1]}")
    if record.get("amount_total") is not None:
        cur = record.get("currency_id")
        cur_lbl = cur[1] if isinstance(cur, list | tuple) and len(cur) > 1 else ""
        lines.append(f"Tổng tiền: {record['amount_total']} {cur_lbl}".strip())
    if record.get("url"):
        lines.append(record["url"])
    return "\n".join(lines)


def send_requester_approval_prompt(
    request,
    ctx,
    *,
    user_id: int,
    scope: TelegramScope,
    record: dict[str, Any],
    company_id: int,
) -> str | None:
    """Issue a requester approval-submit token and send an inline-button prompt on Telegram."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    if not record or not record.get("id") or record.get("state") not in ("draft", "sent"):
        return None
    if not settings.TELEGRAM_BOT_TOKEN or not scope.chat_id:
        return None
    _ensure_table(sf)
    token = secrets.token_hex(12)
    order_name = str(record.get("name") or record["id"])
    with sf() as db:
        db.query(PurchaseApprovalToken).filter_by(
            role="requester",
            user_id=user_id,
            tenant=ctx.user.tenant,
            database=ctx.user.database,
            order_id=record["id"],
            active=1,
        ).update({"active": 0})
        db.add(
            PurchaseApprovalToken(
                token=token,
                role="requester",
                user_id=user_id,
                tenant=ctx.user.tenant,
                database=ctx.user.database,
                chat_id=scope.chat_id,
                telegram_binding_id=scope.telegram_binding_id,
                order_id=record["id"],
                order_name=order_name,
                company_id=company_id,
                active=1,
                created_at=time.time(),
            )
        )
        _save_review(db, ctx, token=token, user_id=user_id, scope=scope,
                     order_id=record["id"], company_id=company_id)
        db.commit()

    if settings.TELEGRAM_BOT_TOKEN and scope.chat_id:
        try:
            client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
            caption = (
                f"📄 Bản xem trước PDF đơn mua {order_name}.\n"
                f"Vui lòng kiểm tra lại nội dung, sau đó bấm nút bên dưới để gửi yêu cầu phê duyệt lên cấp trên."
            )
            try:
                filename, pdf_bytes = render_purchase_pdf(ctx, record)
                client.send_document(
                    scope.chat_id,
                    filename,
                    pdf_bytes,
                    caption=caption,
                    inline_keyboard=requester_po_keyboard(token),
                )
            except Exception as pdf_exc:
                logger.warning("Could not render/send PO PDF for %s (%s); falling back to text prompt", order_name, type(pdf_exc).__name__)
                msg = (
                    f"📋 Bạn có muốn xác nhận & gửi phê duyệt cho đơn mua {order_name} không?\n"
                    f"Bấm nút bên dưới để chuyển đơn mua lên bước phê duyệt trên Odoo."
                )
                client.send_message(
                    scope.chat_id,
                    msg,
                    inline_keyboard=requester_po_keyboard(token),
                )
            return token
        except Exception as exc:
            logger.warning("Could not send PO requester approval prompt: %s", type(exc).__name__)
    return None


def _notify_po_approvers(
    sf,
    ctx,
    *,
    settings,
    user_id: int,
    scope: TelegramScope,
    refreshed: dict[str, Any],
    company_id: int,
    target_uids: list[int],
) -> int:
    """Issue approver tokens and deliver PDF + [Xác nhận duyệt]/[Từ chối] buttons via Telegram."""
    delivered_count = 0
    if not settings.TELEGRAM_BOT_TOKEN or not target_uids:
        return 0
    po_name = str(refreshed.get("name") or refreshed.get("id"))
    client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
    pdf_pair: tuple[str, bytes] | None = None
    try:
        pdf_pair = render_purchase_pdf(ctx, refreshed)
    except Exception as exc:
        logger.warning("Could not render PO PDF for approvers (%s): %s", po_name, type(exc).__name__)

    with sf() as db:
        targets = (
            db.query(User, TelegramConnection)
            .join(TelegramConnection, TelegramConnection.user_id == User.id)
            .filter(
                User.active.is_(True),
                User.tenant == ctx.user.tenant,
                User.database == ctx.user.database,
                User.odoo_uid.in_(list(target_uids)),
                TelegramConnection.status == "connected",
            )
            .all()
        )
    for approver_user, tg_conn in targets:
        if not tg_conn.binding_id or not tg_conn.private_chat_id:
            continue
        appr_token = secrets.token_hex(12)
        with sf() as db:
            db.add(
                PurchaseApprovalToken(
                    token=appr_token,
                    role="approver",
                    user_id=approver_user.id,
                    tenant=ctx.user.tenant,
                    database=ctx.user.database,
                    chat_id=tg_conn.private_chat_id,
                    telegram_binding_id=tg_conn.binding_id,
                    order_id=refreshed["id"],
                    order_name=po_name,
                    company_id=company_id,
                    requester_user_id=user_id,
                    requester_chat_id=scope.chat_id,
                    requester_binding_id=scope.telegram_binding_id,
                    active=1,
                    created_at=time.time(),
                )
            )
            _save_review(
                db, ctx, token=appr_token, user_id=approver_user.id,
                scope=TelegramScope(
                    telegram_user_id=tg_conn.telegram_user_id,
                    chat_id=tg_conn.private_chat_id,
                    telegram_binding_id=tg_conn.binding_id,
                ),
                order_id=refreshed["id"], company_id=company_id,
            )
            db.commit()
        try:
            text = _format_po_summary(
                refreshed,
                prefix=f"📋 Yêu cầu phê duyệt đơn mua {po_name} từ {ctx.user.name}.",
            )
            if pdf_pair:
                client.send_document(
                    tg_conn.private_chat_id,
                    pdf_pair[0],
                    pdf_pair[1],
                    caption=text[:1024],
                    inline_keyboard=approver_po_keyboard(appr_token),
                )
            else:
                client.send_message(
                    tg_conn.private_chat_id,
                    text,
                    inline_keyboard=approver_po_keyboard(appr_token),
                )
            delivered_count += 1
        except Exception as exc:
            logger.warning("Could not notify PO approver %s: %s", approver_user.id, type(exc).__name__)
    return delivered_count


def submit_purchase_approval(
    request,
    ctx,
    *,
    user_id: int,
    scope: TelegramScope,
    token: str,
) -> dict[str, Any]:
    """Requester clicks [Xác nhận gửi duyệt]: submits Studio approval (if configured) or runs button_confirm."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="requester", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Nút gửi duyệt đơn mua này đã cũ hoặc đã được xử lý."}

    svc = BusinessService(ctx)
    svc.company_id = ctx.selected_companies([row.company_id])[0]
    try:
        po = svc.record("purchase.order", row.order_id, ["name", "state", "partner_id", "amount_total", "currency_id", "company_id"], track=False)
    except (BusinessError, OdooError) as exc:
        return {"status": "error", "message": str(exc)}

    if po.get("state") not in ("draft", "sent"):
        return {"status": "stale", "message": f"Đơn mua {po.get('name')} hiện ở trạng thái “{po.get('state')}”, không thể gửi duyệt lại."}

    # Check if Odoo Studio approval rules govern purchase.order.button_confirm
    studio_adapter = PurchaseStudioApproval(ctx, row.company_id)
    try:
        studio_state = studio_adapter.inspect(row.order_id)
    except (BusinessError, OdooError) as exc:
        return {"status": "error", "message": f"Không thể kiểm tra quy tắc duyệt đơn mua {po.get('name')}: {exc}"}

    if studio_state["rules"] and studio_state["status"] != "approved":
        try:
            requested_state = studio_adapter.request(row.order_id)
        except (BusinessError, OdooError) as exc:
            return {"status": "error", "message": f"Không thể gửi yêu cầu phê duyệt đơn mua {po.get('name')}: {exc}"}

        po_name = str(po.get("name") or row.order_name)
        po_with_url = {**po, "url": f"{ctx.user.tenant.rstrip('/')}/web#id={row.order_id}&model=purchase.order&view_type=form"}
        with sf() as db:
            db.query(PurchaseApprovalToken).filter_by(
                role="requester", tenant=ctx.user.tenant, database=ctx.user.database, order_id=row.order_id, active=1,
            ).update({"active": 0})
            db.commit()

        approver_uids = studio_adapter.approver_uids(requested_state)
        approver_names: list[str] = []
        if approver_uids:
            try:
                people = ctx.call("res.users", "search_read", [[("id", "in", approver_uids)]], {"fields": ["name"]})
                approver_names = [p["name"] for p in people if p.get("name")]
            except OdooError:
                pass
        delivered_count = _notify_po_approvers(
            sf, ctx, settings=settings, user_id=user_id, scope=scope,
            refreshed=po_with_url, company_id=row.company_id, target_uids=approver_uids,
        )
        names_str = f" ({', '.join(approver_names)})" if approver_names else ""
        suffix = (
            f" Đã gửi file PDF và nút duyệt tới {delivered_count} cấp trên qua Telegram."
            if delivered_count
            else " Đã tạo yêu cầu duyệt trên Odoo (cấp trên chưa liên kết Telegram sẽ duyệt trực tiếp trên Odoo)."
        )
        return {
            "status": "awaiting_approval",
            "message": f"Đã gửi yêu cầu phê duyệt đơn mua {po_name}{names_str}.{suffix}",
            "record": po_with_url,
        }

    try:
        result = _execute_review(request, ctx, row, scope, "business.purchases.confirm")
        if result.get("status") not in ("completed", "awaiting_approval"):
            return result
    except (BusinessError, OdooError) as exc:
        return {"status": "error", "message": f"Không thể xác nhận/gửi duyệt đơn mua {po.get('name')}: {exc}"}

    refreshed = result["record"]
    with sf() as db:
        db.query(PurchaseApprovalToken).filter_by(
            tenant=ctx.user.tenant, database=ctx.user.database, order_id=row.order_id, active=1,
        ).update({"active": 0})
        db.commit()

    new_state = refreshed.get("state")
    po_name = str(refreshed.get("name") or row.order_name)

    if new_state == "to approve":
        manager_uids = _find_purchase_manager_uids(ctx)
        other_managers = manager_uids - {ctx.user.odoo_uid}
        target_uids = sorted(other_managers or manager_uids)
        delivered_count = _notify_po_approvers(
            sf, ctx, settings=settings, user_id=user_id, scope=scope,
            refreshed=refreshed, company_id=row.company_id, target_uids=target_uids,
        )
        suffix = (
            f" Đã gửi yêu cầu duyệt tới {delivered_count} cấp trên qua Telegram."
            if delivered_count
            else " Đơn đã chuyển sang Chờ phê duyệt trên Odoo (cấp trên chưa liên kết Telegram sẽ duyệt trực tiếp trên Odoo)."
        )
        return {"status": "awaiting_approval", "message": f"Đã gửi yêu cầu phê duyệt đơn mua {po_name}.{suffix}", "record": refreshed}

    if new_state == "purchase":
        return {
            "status": "completed",
            "message": f"Đơn mua {po_name} đã được Odoo xác nhận trực tiếp thành Đơn mua.\n{refreshed.get('url', '')}".strip(),
            "record": refreshed,
        }

    return {
        "status": "completed",
        "message": f"Đơn mua {po_name} đã chuyển sang trạng thái “{new_state}”.",
        "record": refreshed,
    }


def decide_purchase_approval(
    request,
    ctx,
    *,
    user_id: int,
    scope: TelegramScope,
    token: str,
    decision: str,
) -> dict[str, Any]:
    """Approver clicks [Xác nhận duyệt] or [Từ chối]: runs Studio set_approval or native button_approve/button_cancel."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    if decision not in ("approve", "reject"):
        return {"status": "error", "message": "Quyết định phê duyệt không hợp lệ."}
    row = _load_valid_token(
        sf, token=token, role="approver", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Yêu cầu duyệt đơn mua này đã cũ hoặc đã được xử lý."}

    svc = BusinessService(ctx)
    svc.company_id = ctx.selected_companies([row.company_id])[0]
    studio_adapter = PurchaseStudioApproval(ctx, row.company_id)
    try:
        studio_state = studio_adapter.inspect(row.order_id)
    except (BusinessError, OdooError) as exc:
        return {"status": "error", "message": str(exc)}

    if studio_state["rules"]:
        try:
            updated_state = studio_adapter.decide(row.order_id, approved=(decision == "approve"))
        except (BusinessError, OdooError) as exc:
            return {"status": "error", "message": str(exc)}

        po_name = str(updated_state["record"].get("name") or row.order_name)
        po_url = f"{ctx.user.tenant.rstrip('/')}/web#id={row.order_id}&model=purchase.order&view_type=form"
        refreshed = {**updated_state["record"], "url": po_url}
        with sf() as db:
            db.query(PurchaseApprovalToken).filter_by(token=row.token).update({"active": 0})
            if updated_state["status"] in ("approved", "rejected"):
                db.query(PurchaseApprovalToken).filter_by(
                    tenant=ctx.user.tenant, database=ctx.user.database, order_id=row.order_id, active=1,
                ).update({"active": 0})
            db.commit()

        if settings.TELEGRAM_BOT_TOKEN and row.requester_chat_id and row.requester_chat_id != scope.chat_id:
            try:
                with sf() as db:
                    connection = db.query(TelegramConnection).filter_by(
                        user_id=row.requester_user_id, status="connected",
                        private_chat_id=row.requester_chat_id, binding_id=row.requester_binding_id,
                    ).first()
                if connection:
                    client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
                    verb = "✅ đã phê duyệt" if decision == "approve" else "❌ đã từ chối"
                    extra = " (Đã đủ điều kiện xác nhận đơn mua trên Odoo)" if updated_state["status"] == "approved" else ""
                    client.send_message(
                        row.requester_chat_id,
                        f"Cấp trên {ctx.user.name} {verb} đơn mua {po_name}{extra}.\n{po_url}",
                    )
            except Exception as exc:
                logger.warning("Could not notify PO requester: %s", type(exc).__name__)

        if decision == "reject":
            return {"status": "completed", "message": f"Đã từ chối phê duyệt đơn mua {po_name}.", "record": refreshed}
        if updated_state["status"] == "approved":
            return {"status": "completed", "message": f"Đã phê duyệt đơn mua {po_name} (đủ các bước duyệt).", "record": refreshed}
        return {"status": "completed", "message": f"Đã ghi nhận phê duyệt của bạn cho đơn mua {po_name}. Đang chờ cấp duyệt còn lại.", "record": refreshed}

    try:
        if not svc.group("purchase.group_purchase_manager"):
            raise OdooAccessError("Tài khoản của bạn không có quyền Quản lý mua hàng để duyệt đơn này.")
        po = svc.record("purchase.order", row.order_id, ["name", "state"], track=False)
        if po.get("state") != "to approve":
            return {"status": "stale", "message": f"Đơn mua {po.get('name')} không còn ở trạng thái Chờ phê duyệt."}
        action = "business.purchases.approve" if decision == "approve" else "business.purchases.reject"
        result = _execute_review(request, ctx, row, scope, action)
        if result.get("status") != "completed":
            return result
    except (BusinessError, OdooError) as exc:
        return {"status": "error", "message": str(exc)}

    refreshed = result["record"]
    po_name = str(refreshed.get("name") or row.order_name)
    with sf() as db:
        db.query(PurchaseApprovalToken).filter_by(
            tenant=ctx.user.tenant, database=ctx.user.database, order_id=row.order_id, active=1,
        ).update({"active": 0})
        db.commit()

    if settings.TELEGRAM_BOT_TOKEN and row.requester_chat_id and row.requester_chat_id != scope.chat_id:
        try:
            with sf() as db:
                connection = db.query(TelegramConnection).filter_by(
                    user_id=row.requester_user_id, status="connected",
                    private_chat_id=row.requester_chat_id, binding_id=row.requester_binding_id,
                ).first()
            if not connection:
                return {"status": "completed", "message": "Odoo đã xử lý quyết định duyệt; liên kết người tạo đã thay đổi.", "record": refreshed}
            client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
            verb = "✅ đã phê duyệt" if decision == "approve" else "❌ đã từ chối"
            client.send_message(
                row.requester_chat_id,
                f"Cấp trên {ctx.user.name} {verb} đơn mua {po_name}.\n{refreshed.get('url', '')}".strip(),
            )
        except Exception as exc:
            logger.warning("Could not notify PO requester: %s", type(exc).__name__)

    if decision == "approve":
        return {"status": "completed", "message": f"Đã phê duyệt đơn mua {po_name}.", "record": refreshed}
    return {"status": "completed", "message": f"Đã từ chối đơn mua {po_name}.", "record": refreshed}


def start_purchase_edit(
    request,
    ctx,
    *,
    user_id: int,
    scope: TelegramScope,
    token: str,
) -> dict[str, Any]:
    """Requester clicks [Chỉnh sửa]: validate token and return PO context so Agent opens edit menu."""
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="requester", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Nút chỉnh sửa đơn mua này đã cũ hoặc đã hết hiệu lực."}

    svc = BusinessService(ctx)
    svc.company_id = ctx.selected_companies([row.company_id])[0]
    try:
        po = svc.record(
            "purchase.order",
            row.order_id,
            ["name", "state", "partner_id", "currency_id", "date_order", "date_planned", "picking_type_id", "payment_term_id", "partner_ref", "notes"],
            track=False,
        )
    except (BusinessError, OdooError) as exc:
        return {"status": "error", "message": str(exc)}

    if po.get("state") not in ("draft", "sent"):
        return {"status": "stale", "message": f"Đơn mua {po.get('name')} hiện ở trạng thái “{po.get('state')}”, không thể chỉnh sửa qua nút này."}

    return {
        "status": "ok",
        "order_id": row.order_id,
        "order_name": str(po.get("name") or row.order_name),
        "company_id": row.company_id,
        "record": po,
    }


def request_purchase_feedback_context(
    request,
    ctx,
    *,
    user_id: int,
    scope: TelegramScope,
    token: str,
) -> dict[str, Any]:
    """Approver clicks [Yêu cầu chỉnh sửa]: validate token and return PO context so Agent asks for feedback."""
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="approver", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Yêu cầu duyệt đơn mua này đã cũ hoặc đã được xử lý."}
    return {
        "status": "ok",
        "order_id": row.order_id,
        "order_name": row.order_name,
        "company_id": row.company_id,
        "token": row.token,
    }


def submit_purchase_feedback(
    request,
    ctx,
    *,
    user_id: int,
    scope: TelegramScope,
    token: str,
    feedback_text: str,
) -> dict[str, Any]:
    """Approver submits revision feedback: post note on Odoo (best-effort) and notify requester with [Chỉnh sửa]."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    clean_feedback = (feedback_text or "").strip()
    if not clean_feedback:
        return {"status": "error", "message": "Vui lòng nhập nội dung góp ý chỉnh sửa."}

    row = _load_valid_token(
        sf, token=token, role="approver", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Yêu cầu duyệt đơn mua này đã cũ hoặc đã được xử lý."}

    po_name = row.order_name
    po_url = f"{ctx.user.tenant.rstrip('/')}/web#id={row.order_id}&model=purchase.order&view_type=form"
    try:
        ctx.selected_companies([row.company_id])
        ctx.call(
            "purchase.order",
            "message_post",
            [[row.order_id]],
            {"body": f"Yêu cầu chỉnh sửa từ {ctx.user.name}: {clean_feedback}"},
        )
    except OdooError as exc:
        logger.warning("Could not post PO feedback chatter note: %s", type(exc).__name__)

    with sf() as db:
        db.query(PurchaseApprovalToken).filter_by(token=row.token).update({"active": 0})
        db.commit()

    delivered = False
    if settings.TELEGRAM_BOT_TOKEN and row.requester_user_id and row.requester_chat_id and row.requester_binding_id:
        try:
            with sf() as db:
                conn = db.query(TelegramConnection).filter_by(
                    user_id=row.requester_user_id,
                    status="connected",
                    private_chat_id=row.requester_chat_id,
                    binding_id=row.requester_binding_id,
                ).first()
            if conn:
                new_req_token = secrets.token_hex(12)
                with sf() as db:
                    db.query(PurchaseApprovalToken).filter_by(
                        role="requester",
                        user_id=row.requester_user_id,
                        tenant=row.tenant,
                        database=row.database,
                        order_id=row.order_id,
                        active=1,
                    ).update({"active": 0})
                    db.add(
                        PurchaseApprovalToken(
                            token=new_req_token,
                            role="requester",
                            user_id=row.requester_user_id,
                            tenant=row.tenant,
                            database=row.database,
                            chat_id=row.requester_chat_id,
                            telegram_binding_id=row.requester_binding_id,
                            order_id=row.order_id,
                            order_name=po_name,
                            company_id=row.company_id,
                            active=1,
                            created_at=time.time(),
                        )
                    )
                    _save_review(
                        db, ctx, token=new_req_token, user_id=row.requester_user_id,
                        scope=TelegramScope(
                            telegram_user_id=conn.telegram_user_id,
                            chat_id=row.requester_chat_id,
                            telegram_binding_id=row.requester_binding_id,
                        ),
                        order_id=row.order_id, company_id=row.company_id,
                    )
                    db.commit()
                client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
                client.send_message(
                    row.requester_chat_id,
                    f"📝 Cấp trên {ctx.user.name} yêu cầu chỉnh sửa đơn mua {po_name}:\n"
                    f"“{clean_feedback}”\n\n"
                    f"Bấm Chỉnh sửa bên dưới để cập nhật đơn mua và gửi duyệt lại.\n{po_url}",
                    inline_keyboard=requester_po_keyboard(new_req_token),
                )
                delivered = True
        except Exception as exc:
            logger.warning("Could not deliver PO feedback to requester: %s", type(exc).__name__)

    if delivered:
        return {"status": "completed", "message": f"Đã gửi yêu cầu chỉnh sửa đơn mua {po_name} tới người tạo qua Telegram."}
    return {"status": "completed", "message": f"Đã ghi nhận yêu cầu chỉnh sửa đơn mua {po_name} trên Odoo."}


def _po_sync_token(row: PurchaseApprovalToken, event_key: str) -> str:
    return hashlib.sha256(
        json.dumps([
            "po-approval-notice-v1", row.tenant, row.database,
            row.order_id, event_key, row.user_id,
        ]).encode()
    ).hexdigest()[:24]


def _reserve_po_sync_notice(sf, row: PurchaseApprovalToken, sync_token: str) -> bool:
    with sf() as db:
        if db.get(PurchaseApprovalToken, sync_token):
            return False
        db.add(
            PurchaseApprovalToken(
                token=sync_token,
                role="sync",
                user_id=row.user_id,
                tenant=row.tenant,
                database=row.database,
                chat_id=row.chat_id,
                telegram_binding_id=row.telegram_binding_id,
                order_id=row.order_id,
                order_name=row.order_name,
                company_id=row.company_id,
                active=1,
                created_at=time.time(),
            )
        )
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False


def reconcile_purchase_approvals(settings, sf, *, telegram_client=None, context_factory=None) -> None:
    """Poll Odoo Studio decisions made outside Telegram for submitted POs and notify requester."""
    from types import SimpleNamespace
    from app.reporting.context import authenticated_context

    if not settings.TELEGRAM_BOT_TOKEN:
        return
    client = telegram_client or TelegramClient(settings.TELEGRAM_BOT_TOKEN)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=sf)))
    context_factory = context_factory or authenticated_context
    cutoff = time.time() - TOKEN_TTL_SECONDS

    with sf() as db:
        _ensure_table(sf)
        rows = (
            db.query(PurchaseApprovalToken)
            .filter(
                PurchaseApprovalToken.role == "requester",
                PurchaseApprovalToken.active == 0,
                PurchaseApprovalToken.created_at >= cutoff,
            )
            .order_by(PurchaseApprovalToken.created_at.desc())
            .all()
        )
        candidates: list[PurchaseApprovalToken] = []
        seen: set[tuple] = set()
        for row in rows:
            key = (row.tenant, row.database, row.order_id, row.company_id, row.user_id)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(row)

    for row in candidates:
        with sf() as db:
            conn = db.query(TelegramConnection).filter_by(
                user_id=row.user_id,
                private_chat_id=row.chat_id,
                binding_id=row.telegram_binding_id,
                status="connected",
            ).first()
            user = db.get(User, row.user_id)
        if not conn or not user or not user.active:
            continue
        scope = TelegramScope(
            telegram_user_id=conn.telegram_user_id,
            chat_id=row.chat_id,
            telegram_binding_id=row.telegram_binding_id,
        )
        try:
            with context_factory(request, row.user_id, scope) as ctx:
                adapter = PurchaseStudioApproval(ctx, row.company_id)
                state = adapter.inspect(row.order_id)
                po = state["record"]
                po_name = str(po.get("name") or row.order_name)
                po_url = f"{row.tenant.rstrip('/')}/web#id={row.order_id}&model=purchase.order&view_type=form"
                for entry in state.get("entries") or []:
                    if entry.get("approved") not in (True, False):
                        continue
                    sync_tok = _po_sync_token(row, f"entry:{entry['id']}")
                    if not _reserve_po_sync_notice(sf, row, sync_tok):
                        continue
                    who = entry.get("user_id")
                    approver_name = who[1] if isinstance(who, (list, tuple)) and len(who) > 1 else "Cấp trên"
                    if entry["approved"]:
                        tail = " (Đã đủ tất cả các bước duyệt)" if state["status"] == "approved" else " (Đang chờ cấp duyệt còn lại)"
                        msg = f"✅ {approver_name} đã phê duyệt đơn mua {po_name} trên Odoo{tail}.\n{po_url}"
                    else:
                        msg = f"❌ {approver_name} đã từ chối phê duyệt đơn mua {po_name} trên Odoo.\n{po_url}"
                    client.send_message(row.chat_id, msg)
                if po.get("state") == "purchase":
                    sync_tok = _po_sync_token(row, "state:purchase")
                    if _reserve_po_sync_notice(sf, row, sync_tok):
                        client.send_message(
                            row.chat_id,
                            f"✅ Đơn mua {po_name} đã được xác nhận thành Đơn mua hàng trên Odoo.\n{po_url}",
                        )
        except Exception as exc:
            logger.warning("PO approval sync failed for order %s: %s", row.order_id, type(exc).__name__)
