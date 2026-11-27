"""Versioned Telegram PDF tokens for saved quotation edits and Studio approval."""
from __future__ import annotations

import html
import logging
import re
import secrets
import time
from typing import Any

from sqlalchemy import BigInteger, Column, Float, Integer, String

from app.business.models import QuotationApprovalReview
from app.business.quotation_approval import commercial_snapshot, review_key
from app.business.service import BusinessError, BusinessService
from app.models import Base, TelegramConnection, User
from app.odoo.client import OdooAccessError, OdooError
from app.quotations.approval import APPROVAL_LABELS, QuotationApproval, read_approval_status
from app.quotations.pdf import render_quotation_pdf
from app.quotations.service import QuotationService, QuotationValidationError, _id, _m2o, _number
from app.quotations.tracking import QuotationRecordLink
from app.reporting.context import bound_telegram_user
from app.reporting.schemas import TelegramScope
from app.telegram.client import TelegramClient, TelegramDeliveryUnknownError, TelegramError

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[0-9a-f]{24}$")
TOKEN_TTL_SECONDS = 7 * 24 * 3600


class QuotationPdfToken(Base):
    __tablename__ = "quotation_pdf_tokens"

    token = Column(String(24), primary_key=True)
    role = Column(String(16), nullable=False)  # 'sale' | 'approver'
    user_id = Column(Integer, nullable=False, index=True)
    tenant = Column(String(500), nullable=False)
    database = Column(String(250), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    telegram_binding_id = Column(String(128), nullable=False)
    order_id = Column(Integer, nullable=False, index=True)
    order_name = Column(String(128), nullable=False)
    company_id = Column(Integer, nullable=False)
    order_version = Column(String(64), nullable=False)
    message_id = Column(Integer, nullable=True)
    requester_user_id = Column(Integer, nullable=True)
    requester_chat_id = Column(BigInteger, nullable=True)
    requester_binding_id = Column(String(128), nullable=True)
    active = Column(Integer, nullable=False, default=1)
    created_at = Column(Float, nullable=False, default=time.time)


def sale_pdf_keyboard(token: str, *, can_request_approval: bool) -> list[list[dict[str, str]]]:
    row = [{"text": "Chỉnh sửa", "callback_data": f"qedit:{token}"}]
    if can_request_approval:
        row.append({"text": "Xác nhận gửi duyệt", "callback_data": f"qappr:{token}"})
    return [row]


def approver_pdf_keyboard(token: str) -> list[list[dict[str, str]]]:
    return [[
        {"text": "Phê duyệt", "callback_data": f"qgrant:{token}"},
        {"text": "Yêu cầu chỉnh sửa", "callback_data": f"qfeed:{token}"},
    ]]


def _order_version(order: dict[str, Any]) -> str:
    version = order.get("write_date")
    if not isinstance(version, str) or not version:
        raise QuotationValidationError("Odoo chưa trả phiên bản ghi của báo giá.")
    return version


def _can_request_approval(settings: Any, order: dict[str, Any]) -> bool:
    if order.get("state") != "draft":
        return False
    if not getattr(settings, "ENABLE_QUOTATION_APPROVAL_FLOW", False):
        return False
    approval = order.get("approval") or {}
    return approval.get("status") not in ("pending", "approved")


def issue_sale_pdf_token(
    sf,
    *,
    user_id: int,
    tenant: str,
    database: str,
    scope: TelegramScope,
    order: dict[str, Any],
    company_id: int,
) -> str:
    token = secrets.token_hex(12)
    version = _order_version(order)
    with sf() as db:
        db.query(QuotationPdfToken).filter_by(
            role="sale",
            user_id=user_id,
            tenant=tenant,
            database=database,
            order_id=order["id"],
            active=1,
        ).update({"active": 0})
        db.add(
            QuotationPdfToken(
                token=token,
                role="sale",
                user_id=user_id,
                tenant=tenant,
                database=database,
                chat_id=scope.chat_id,
                telegram_binding_id=scope.telegram_binding_id,
                order_id=order["id"],
                order_name=str(order.get("name") or order["id"]),
                company_id=company_id,
                order_version=version,
                active=1,
                created_at=time.time(),
            )
        )
        db.commit()
    return token


def mark_pdf_token_delivered(sf, token: str, sent: dict[str, Any]) -> None:
    message_id = sent.get("message_id") if isinstance(sent, dict) else None
    if type(message_id) is not int:
        raise ValueError("Telegram did not confirm the PDF message ID")
    with sf() as db:
        row = db.get(QuotationPdfToken, token)
        if row is None:
            raise ValueError("PDF token is no longer persisted")
        row.message_id = message_id
        db.commit()


def _load_valid_token(
    sf,
    *,
    token: str,
    role: str,
    user_id: int,
    tenant: str,
    database: str,
    scope: TelegramScope,
) -> QuotationPdfToken | None:
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    with sf() as db:
        row = db.get(QuotationPdfToken, token)
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


def _read_order_for_token(ctx, order_id: int, company_id: int) -> dict[str, Any]:
    svc = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
    chosen_company, _ = svc._companies(company_id)
    schema = svc._schema("sale.order")
    fields = [
        f for f in ("id", "name", "state", "write_date", "company_id", "currency_id", "amount_total", "order_line")
        if f in schema or f == "id"
    ]
    rows = svc._rpc(
        "sale.order",
        "search_read",
        [[("id", "=", order_id), ("company_id", "=", chosen_company)]],
        chosen_company,
        fields=fields,
        limit=1,
    )
    if len(rows) != 1:
        raise OdooAccessError("Không tìm thấy hoặc không có quyền đọc báo giá này trên Odoo.")
    order = dict(rows[0])
    line_schema = svc._schema("sale.order.line")
    line_fields = [
        f for f in ("id", "product_id", "name", "product_uom_qty", "price_unit")
        if f in line_schema or f == "id"
    ]
    order["lines"] = (
        svc._rpc("sale.order.line", "read", [order.get("order_line") or []], chosen_company, fields=line_fields)
        if order.get("order_line")
        else []
    )
    order["url"] = f"{ctx.user.tenant.rstrip('/')}/web#id={order_id}&model=sale.order&view_type=form"
    return order


def _pdf_caption(order: dict[str, Any], prefix: str | None = None) -> str:
    labels = {
        "draft": "Báo giá nháp",
        "sent": "Đã gửi tới khách hàng",
        "sale": "Đơn bán đã xác nhận",
        "cancel": "Đã hủy",
    }
    state_lbl = labels.get(order.get("state"), str(order.get("state") or "nháp"))
    lines = [prefix] if prefix else []
    lines.append(f"Báo giá {order.get('name')} · {state_lbl}")
    if order.get("amount_total") is not None:
        currency = order.get("currency_id") or []
        cur_lbl = currency[1] if isinstance(currency, (list, tuple)) and len(currency) > 1 else ""
        lines.append(f"Tổng tiền: {order['amount_total']} {cur_lbl}".strip())
    if order.get("approval"):
        st = order["approval"].get("status")
        lines.append("Duyệt nội bộ: " + APPROVAL_LABELS.get(st, APPROVAL_LABELS["unknown"]))
    if order.get("url"):
        lines.append(order["url"])
    return "\n".join(lines)


def open_sale_pdf(request, ctx, user_id: int, scope: TelegramScope, token: str) -> dict[str, Any]:
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="sale", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Nút PDF này đã cũ hoặc không thuộc tài khoản của bạn. Hãy gửi lại PDF mới."}
    order = _read_order_for_token(ctx, row.order_id, row.company_id)
    if _order_version(order) != row.order_version:
        return {"status": "stale", "message": f"Báo giá {order['name']} đã thay đổi kể từ bản PDF này. Hãy dùng bản PDF mới nhất."}
    if order.get("state") != "draft":
        return {"status": "locked", "message": f"Báo giá {order['name']} không còn ở trạng thái nháp nên không thể chỉnh sửa qua PDF."}
    return {
        "status": "OK",
        "order_id": order["id"],
        "name": order["name"],
        "version": _order_version(order),
        "company_id": row.company_id,
        "lines": order["lines"],
    }


def retry_sale_pdf(
    request,
    ctx,
    user_id: int,
    scope: TelegramScope,
    *,
    order_id: int | None = None,
    name: str | None = None,
    company_id: int | None = None,
) -> dict[str, Any]:
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    svc = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
    chosen_company, _ = svc._companies(company_id)
    with sf() as db:
        bound_telegram_user(db, user_id, scope)
    if order_id is None:
        if not name or not str(name).strip():
            raise QuotationValidationError("Cần cung cấp mã hoặc ID báo giá để gửi lại PDF.")
        found = svc.status_by_name(str(name).strip(), chosen_company)
        if found.get("status") != "OK":
            return {"status": "error", "message": found.get("message") or "Không tìm thấy báo giá trên Odoo."}
        order_id = found["result"]["id"]
    with sf() as db:
        linked = db.query(QuotationRecordLink).filter_by(
            user_id=user_id, tenant=ctx.user.tenant, database=ctx.user.database,
            order_id=order_id, company_id=chosen_company,
        ).first()
    if linked is None:
        return {"status": "stale", "message": "Báo giá không thuộc bản nháp đã tạo từ tài khoản này."}
    order = _read_order_for_token(ctx, _id(order_id, "order_id"), chosen_company)
    if order.get("state") != "draft":
        return {"status": "locked", "message": "Báo giá không còn ở trạng thái nháp."}
    if getattr(settings, "ENABLE_QUOTATION_APPROVAL_FLOW", False):
        order["approval"] = read_approval_status(ctx, order)
    filename, content = render_quotation_pdf(ctx, order)
    token = issue_sale_pdf_token(
        sf, user_id=user_id, tenant=ctx.user.tenant, database=ctx.user.database,
        scope=scope, order=order, company_id=chosen_company,
    )
    keyboard = sale_pdf_keyboard(token, can_request_approval=_can_request_approval(settings, order)) if order.get("state") == "draft" else None
    client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
    sent = client.send_document(
        scope.chat_id,
        filename,
        content,
        caption=_pdf_caption(order),
        content_type="application/pdf",
        inline_keyboard=keyboard,
    )
    mark_pdf_token_delivered(sf, token, sent)
    return {"status": "sent", "order_id": order["id"], "name": order["name"]}


def edit_sale_pdf(
    request,
    ctx,
    user_id: int,
    scope: TelegramScope,
    *,
    token: str,
    line_id: int | None = None,
    quantity: float | None = None,
    payment_term_id: int | None = None,
    optional_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="sale", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Nút PDF này đã cũ hoặc không còn hiệu lực. Hãy gửi lại PDF mới."}
    svc = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
    company_id = row.company_id
    order = _read_order_for_token(ctx, row.order_id, company_id)
    if _order_version(order) != row.order_version:
        return {"status": "stale", "message": f"Báo giá {order['name']} đã được cập nhật ở nơi khác. Hãy lấy PDF mới trước khi sửa."}
    if order.get("state") != "draft":
        return {"status": "locked", "message": f"Báo giá {order['name']} không còn là bản nháp nên không thể chỉnh sửa."}
    if getattr(settings, "ENABLE_QUOTATION_APPROVAL_FLOW", False):
        approval = read_approval_status(ctx, order)
        if approval.get("status") in ("pending", "approved"):
            return {
                "status": "locked",
                "message": f"Báo giá {order['name']} đang chờ duyệt hoặc đã được duyệt nội bộ nên không thể sửa trực tiếp.",
            }

    if not svc._rpc("sale.order", "check_access_rights", ["write", False]):
        raise OdooAccessError("Tài khoản không có quyền cập nhật báo giá trên Odoo.")

    if line_id is not None or quantity is not None:
        lid = _id(line_id, "line_id")
        qty = _number(quantity, "Số lượng", positive=True)
        if lid not in (order.get("order_line") or []):
            raise QuotationValidationError("Dòng sản phẩm không thuộc báo giá đã chọn.")
        if not svc._rpc("sale.order.line", "check_access_rights", ["write", False]):
            raise OdooAccessError("Tài khoản không có quyền cập nhật dòng báo giá.")
        svc._rpc("sale.order.line", "write", [[lid], {"product_uom_qty": qty}], company_id)
    elif payment_term_id is not None:
        ptid = _id(payment_term_id, "payment_term_id")
        svc._record("account.payment.term", ptid, company_id, ["id", "display_name"])
        svc._rpc("sale.order", "write", [[order["id"]], {"payment_term_id": ptid}], company_id)
    elif optional_fields:
        vals = svc._optional_values(optional_fields, company_id)
        if not vals:
            raise QuotationValidationError("Không có trường tùy chọn hợp lệ để cập nhật.")
        svc._rpc("sale.order", "write", [[order["id"]], vals], company_id)
    else:
        raise QuotationValidationError("Chưa xác định thông tin cần chỉnh sửa trên báo giá.")

    # Invalidate all old tokens for this quotation across tenant/database
    with sf() as db:
        db.query(QuotationPdfToken).filter_by(
            tenant=ctx.user.tenant, database=ctx.user.database, order_id=order["id"], active=1,
        ).update({"active": 0})
        db.commit()

    updated = _read_order_for_token(ctx, order["id"], company_id)
    if getattr(settings, "ENABLE_QUOTATION_APPROVAL_FLOW", False):
        updated["approval"] = read_approval_status(ctx, updated)
    try:
        filename, content = render_quotation_pdf(ctx, updated)
        new_token = issue_sale_pdf_token(
            sf, user_id=user_id, tenant=ctx.user.tenant, database=ctx.user.database,
            scope=scope, order=updated, company_id=company_id,
        )
        keyboard = sale_pdf_keyboard(new_token, can_request_approval=_can_request_approval(settings, updated))
        sent = TelegramClient(settings.TELEGRAM_BOT_TOKEN).send_document(
            scope.chat_id,
            filename,
            content,
            caption=_pdf_caption(updated, prefix=f"Đã cập nhật báo giá {updated['name']}."),
            content_type="application/pdf",
            inline_keyboard=keyboard,
        )
        mark_pdf_token_delivered(sf, new_token, sent)
        return {"status": "sent", "order_id": updated["id"], "name": updated["name"]}
    except Exception as exc:
        logger.warning("Saved quote edited on Odoo but PDF resend failed: %s", type(exc).__name__)
        return {
            "status": "saved_pdf_failed",
            "order_id": updated["id"],
            "name": updated["name"],
            "message": f"Đã lưu thay đổi trên báo giá {updated['name']} nhưng chưa gửi được PDF mới. Bạn có thể nhắn 'Gửi lại PDF {updated['name']}'.",
        }


def _find_approver_uids(adapter: QuotationApproval, state: dict[str, Any]) -> set[int]:
    uids: set[int] = set()
    approved = {entry["rule_id"][0] for entry in state.get("entries") or []
                if entry.get("approved") is True and entry.get("rule_id")}
    remaining = [rule for rule in state.get("rules") or [] if rule["id"] not in approved]
    if not remaining:
        return uids
    order = min(int(rule.get("notification_order") or 1) for rule in remaining)
    for rule in remaining:
        if int(rule.get("notification_order") or 1) != order:
            continue
        for uid in rule.get("approver_ids") or []:
            if isinstance(uid, int):
                uids.add(uid)
        grp = _m2o(rule.get("approval_group_id"))
        if grp:
            try:
                groups = adapter.call("res.groups", "read", [[grp]], fields=["users"])
                if groups and isinstance(groups[0].get("users"), list):
                    uids.update(u for u in groups[0]["users"] if isinstance(u, int))
            except OdooError:
                pass
    return uids


def _can_receive_approval(settings, sf, person, order_id: int, company_id: int, state: dict[str, Any]) -> bool:
    """Verify each recipient with that recipient's own Odoo credential."""
    from app.models import OdooCredential
    from app.odoo.client import OdooClient
    from app.security import decrypt_secret

    with sf() as db:
        credential = db.query(OdooCredential).filter_by(user_id=person.id).first()
        if credential is None:
            return False
        secret = decrypt_secret(credential.encrypted_secret, settings.ENCRYPTION_KEY)
    own = QuotationApproval(OdooClient(person.tenant, person.database), person.odoo_uid,
                            secret, company_id).inspect(order_id)
    approved = {entry["rule_id"][0] for entry in state.get("entries") or []
                if entry.get("approved") is True and entry.get("rule_id")}
    remaining = [rule for rule in state.get("rules") or [] if rule["id"] not in approved]
    if not remaining:
        return False
    order = min(int(rule.get("notification_order") or 1) for rule in remaining)
    pending_rules = {rule["id"] for rule in remaining
                     if int(rule.get("notification_order") or 1) == order}
    return bool(own["status"] == "pending" and own["requests"]
                and any(rule["id"] in pending_rules and rule.get("can_validate") is True
                        for rule in own["rules"]))


def _deliver_pending_approval_pdfs(request, ctx, adapter, state, order,
                                   requester_user_id, requester_chat_id,
                                   requester_binding_id, requester_name):
    """Send only the current Studio step, reusing confirmed deliveries on retries."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    approver_uids = _find_approver_uids(adapter, state)
    delivered_count = uncertain_count = 0
    missing_names: list[str] = []
    failed_names: list[str] = []
    if not settings.TELEGRAM_BOT_TOKEN or not approver_uids:
        return delivered_count, uncertain_count, missing_names, failed_names
    try:
        people = adapter.call("res.users", "search_read", [[("id", "in", sorted(approver_uids))]],
                              fields=["id", "name"], limit=len(approver_uids))
        names = {person["id"]: person["name"] for person in people}
    except OdooError:
        names = {}
    try:
        filename, pdf_bytes = render_quotation_pdf(ctx, order)
        client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
        with sf() as db:
            targets = (
                db.query(User, TelegramConnection)
                .join(TelegramConnection, TelegramConnection.user_id == User.id)
                .filter(
                    User.active.is_(True),
                    User.tenant == ctx.user.tenant,
                    User.database == ctx.user.database,
                    User.odoo_uid.in_(list(approver_uids)),
                    TelegramConnection.status == "connected",
                ).all()
            )
        linked_uids = {person.odoo_uid for person, connection in targets
                       if connection.binding_id and connection.private_chat_id}
        missing_names = [names.get(uid, f"Odoo #{uid}") for uid in sorted(approver_uids - linked_uids)]
        for approver_user, tg_conn in targets:
            if not tg_conn.binding_id or not tg_conn.private_chat_id:
                continue
            try:
                if not _can_receive_approval(settings, sf, approver_user,
                                             order["id"], _m2o(order["company_id"]), state):
                    continue
            except OdooError as exc:
                logger.warning("Could not verify approver %s: %s", approver_user.id, type(exc).__name__)
                failed_names.append(names.get(approver_user.odoo_uid, f"Odoo #{approver_user.odoo_uid}"))
                continue
            with sf() as db:
                previous = db.query(QuotationPdfToken).filter_by(
                    role="approver", user_id=approver_user.id,
                    tenant=ctx.user.tenant, database=ctx.user.database,
                    order_id=order["id"], order_version=_order_version(order),
                    active=1,
                ).order_by(QuotationPdfToken.created_at.desc()).first()
                if previous:
                    if previous.message_id is not None:
                        delivered_count += 1
                    else:
                        uncertain_count += 1
                    continue
                appr_token = secrets.token_hex(12)
                db.add(QuotationPdfToken(
                    token=appr_token, role="approver", user_id=approver_user.id,
                    tenant=ctx.user.tenant, database=ctx.user.database,
                    chat_id=tg_conn.private_chat_id,
                    telegram_binding_id=tg_conn.binding_id,
                    order_id=order["id"], order_name=order["name"],
                    company_id=_m2o(order["company_id"]),
                    order_version=_order_version(order),
                    requester_user_id=requester_user_id,
                    requester_chat_id=requester_chat_id,
                    requester_binding_id=requester_binding_id,
                    active=1, created_at=time.time(),
                ))
                db.commit()
            try:
                sent = client.send_document(
                    tg_conn.private_chat_id, filename, pdf_bytes,
                    caption=_pdf_caption(order, prefix=(
                        f"📋 Yêu cầu phê duyệt báo giá {order['name']} từ {requester_name}.")),
                    content_type="application/pdf",
                    inline_keyboard=approver_pdf_keyboard(appr_token),
                )
                mark_pdf_token_delivered(sf, appr_token, sent)
                delivered_count += 1
            except Exception as exc:
                if isinstance(exc, TelegramError) and not isinstance(exc, TelegramDeliveryUnknownError):
                    with sf() as db:
                        db.query(QuotationPdfToken).filter_by(token=appr_token).update({"active": 0})
                        db.commit()
                    failed_names.append(names.get(approver_user.odoo_uid, f"Odoo #{approver_user.odoo_uid}"))
                else:
                    uncertain_count += 1
                logger.warning("Could not deliver approval PDF to approver %s: %s",
                               approver_user.id, type(exc).__name__)
    except Exception as exc:
        logger.warning("Could not render PDF for approver delivery: %s", type(exc).__name__)
    return delivered_count, uncertain_count, missing_names, failed_names


def submit_pdf_approval(request, ctx, user_id: int, scope: TelegramScope, token: str) -> dict[str, Any]:
    settings = request.app.state.settings
    if not getattr(settings, "ENABLE_QUOTATION_APPROVAL_FLOW", False):
        return {"status": "error", "message": "Luồng phê duyệt báo giá nội bộ hiện chưa được bật trên Gateway."}
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="sale", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Nút gửi duyệt trên PDF này đã cũ. Hãy gửi lại PDF mới."}
    order = _read_order_for_token(ctx, row.order_id, row.company_id)
    if _order_version(order) != row.order_version:
        return {"status": "stale", "message": f"Báo giá {order['name']} đã thay đổi kể từ bản PDF này. Hãy kiểm tra bản PDF mới nhất."}
    if order.get("state") != "draft":
        return {"status": "error", "message": f"Báo giá {order['name']} không ở trạng thái nháp nên không thể gửi duyệt."}

    adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret, row.company_id)
    initial_state = adapter.inspect(order["id"])
    if initial_state.get("status") in ("approved", "rejected"):
        return {"status": "stale", "message": "Báo giá đã có kết quả duyệt trên Odoo."}

    business = BusinessService(ctx)
    business.company_id = row.company_id
    key = review_key(business, order)
    digest = commercial_snapshot(business, order)
    op_id = "qappr_" + secrets.token_hex(12)
    with sf() as db:
        existing = db.get(QuotationApprovalReview, key)
        if existing:
            db.delete(existing)
            db.flush()
        db.add(
            QuotationApprovalReview(
                key=key,
                digest=digest,
                operation_id=op_id,
                requester_id=user_id,
                created_at=time.time(),
                verified=0,
            )
        )
        db.commit()

    state = adapter.request(order["id"])
    if state.get("status") != "pending":
        return {"status": "error", "message": "Odoo chưa ghi nhận trạng thái chờ duyệt cho báo giá này."}

    refreshed = _read_order_for_token(ctx, order["id"], row.company_id)
    with sf() as db:
        db.query(QuotationApprovalReview).filter_by(key=key, operation_id=op_id).update({"verified": 1})
        db.commit()

    delivered_count, uncertain_count, missing_names, failed_names = _deliver_pending_approval_pdfs(
        request, ctx, adapter, state, refreshed, user_id, scope.chat_id,
        scope.telegram_binding_id, ctx.user.name,
    )

    suffix = (
        f" Đã gửi PDF cần duyệt tới {delivered_count} cấp trên qua Telegram."
        if delivered_count
        else " Yêu cầu đã lưu trên Odoo; chưa xác nhận gửi PDF cho người duyệt qua Telegram."
    )
    if uncertain_count:
        suffix += f" Có {uncertain_count} lượt gửi chưa xác định kết quả; không tự gửi trùng PDF."
    if missing_names:
        suffix += " Chưa liên kết Telegram: " + ", ".join(missing_names) + "."
    if failed_names:
        suffix += " Gửi Telegram thất bại: " + ", ".join(failed_names) + "."
    return {"status": "sent", "message": f"Đã gửi yêu cầu duyệt nội bộ cho báo giá {refreshed['name']}.{suffix}"}


def open_approval_pdf(request, ctx, user_id: int, scope: TelegramScope, token: str) -> dict[str, Any]:
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="approver", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Bản duyệt này đã cũ hoặc không còn hiệu lực."}
    order = _read_order_for_token(ctx, row.order_id, row.company_id)
    if _order_version(order) != row.order_version:
        return {"status": "stale", "message": f"Báo giá {order['name']} đã thay đổi. Hãy chờ bản PDF mới."}
    adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret, row.company_id)
    state = adapter.inspect(order["id"])
    candidates = [
        rule for rule in state["rules"]
        if rule.get("can_validate") is True
        and not any(e.get("rule_id", [None])[0] == rule["id"] for e in state["entries"])
    ]
    if state["status"] != "pending" or not candidates:
        return {"status": "stale", "message": f"Báo giá {order['name']} không còn bước duyệt đang chờ cho tài khoản của bạn."}
    return {"status": "OK", "order_id": order["id"], "name": order["name"]}


def decide_approval_pdf(
    request,
    ctx,
    user_id: int,
    scope: TelegramScope,
    *,
    token: str,
    feedback: str | None = None,
) -> dict[str, Any]:
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    row = _load_valid_token(
        sf, token=token, role="approver", user_id=user_id,
        tenant=ctx.user.tenant, database=ctx.user.database, scope=scope,
    )
    if not row:
        return {"status": "stale", "message": "Bản duyệt này đã cũ hoặc đã được xử lý."}
    order = _read_order_for_token(ctx, row.order_id, row.company_id)
    if _order_version(order) != row.order_version:
        return {"status": "stale", "message": f"Báo giá {order['name']} đã thay đổi kể từ lúc gửi duyệt."}

    business = BusinessService(ctx)
    business.company_id = row.company_id
    from app.business.quotation_approval import require_review
    try:
        require_review(business, order)
    except BusinessError as exc:
        return {"status": "stale", "message": str(exc)}

    adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret, row.company_id)
    state = adapter.inspect(order["id"])
    candidates = [
        rule for rule in state["rules"]
        if rule.get("can_validate") is True
        and not any(e.get("rule_id", [None])[0] == rule["id"] for e in state["entries"])
    ]
    if state["status"] != "pending" or not candidates:
        return {"status": "stale", "message": f"Báo giá {order['name']} không còn chờ duyệt ở bước của bạn."}

    rule_id = candidates[0]["id"]

    if feedback is None:
        result_state = adapter.decide(order["id"], rule_id, True)
        with sf() as db:
            db.query(QuotationPdfToken).filter_by(token=token).update({"active": 0})
            if result_state.get("status") == "approved":
                db.query(QuotationPdfToken).filter_by(
                    role="approver", tenant=ctx.user.tenant, database=ctx.user.database,
                    order_id=order["id"], active=1,
                ).update({"active": 0})
            db.commit()
        next_stage_count = 0
        if result_state.get("status") == "pending":
            refreshed = _read_order_for_token(ctx, order["id"], row.company_id)
            with sf() as db:
                requester = db.get(User, row.requester_user_id) if row.requester_user_id else None
                requester_name = requester.name if requester else "người gửi"
            next_stage_count, _, _, _ = _deliver_pending_approval_pdfs(
                request, ctx, adapter, result_state, refreshed,
                row.requester_user_id, row.requester_chat_id,
                row.requester_binding_id, requester_name,
            )
        if settings.TELEGRAM_BOT_TOKEN and row.requester_user_id and row.requester_chat_id and row.requester_binding_id:
            event_token = None
            try:
                req_scope = TelegramScope(
                    telegram_user_id=row.requester_chat_id,
                    chat_id=row.requester_chat_id,
                    telegram_binding_id=row.requester_binding_id,
                )
                with sf() as db:
                    tg = db.query(TelegramConnection).filter_by(
                        user_id=row.requester_user_id,
                        private_chat_id=row.requester_chat_id,
                        binding_id=row.requester_binding_id,
                        status="connected",
                    ).first()
                if tg:
                    step_note = (f" Đã gửi PDF cho {next_stage_count} người duyệt ở bước kế tiếp."
                                 if result_state.get("status") == "pending"
                                 else "")
                    from app.quotations.approval_sync import _event_token, _mark_delivery, _reserve
                    with sf() as db:
                        sale_pdf = db.query(QuotationPdfToken).filter_by(
                            role="sale", user_id=row.requester_user_id,
                            tenant=ctx.user.tenant, database=ctx.user.database,
                            order_id=order["id"], order_version=row.order_version,
                            chat_id=row.requester_chat_id,
                            telegram_binding_id=row.requester_binding_id,
                        ).order_by(QuotationPdfToken.created_at.desc()).first()
                    entries = [entry for entry in result_state.get("entries") or []
                               if entry.get("rule_id", [None])[0] == rule_id
                               and type(entry.get("id")) is int]
                    event_token = _event_token(sale_pdf, entries[-1]) if sale_pdf and entries else None
                    if event_token is None or _reserve(sf, sale_pdf, event_token):
                        sent = TelegramClient(settings.TELEGRAM_BOT_TOKEN).send_message(
                            row.requester_chat_id,
                            f"✅ {ctx.user.name} đã phê duyệt bước của mình cho báo giá {order['name']}.{step_note}",
                        )
                        if event_token:
                            _mark_delivery(sf, event_token, sent)
            except Exception as exc:
                if event_token and isinstance(exc, TelegramError) and not isinstance(exc, TelegramDeliveryUnknownError):
                    with sf() as db:
                        db.query(QuotationPdfToken).filter_by(token=event_token, role="sync",
                                                               message_id=None).delete()
                        db.commit()
                logger.warning("Could not notify requester of approval: %s", type(exc).__name__)
        if result_state.get("status") == "pending":
            return {"status": "approved_pending", "message":
                    f"Đã phê duyệt bước hiện tại của {order['name']}; Odoo còn bước duyệt tiếp theo. "
                    f"Đã xác nhận gửi PDF cho {next_stage_count} người duyệt ở bước này."}
        return {"status": "approved", "message": f"Đã phê duyệt báo giá {order['name']} trên Odoo."}

    clean_feedback = feedback.strip()[:2000]
    if not clean_feedback:
        return {"status": "error", "message": "Nội dung góp ý không được để trống."}

    # Verify the Odoo note before reporting feedback as recorded or notifying the sale.
    note_id = adapter.call(
        "sale.order", "message_post", [[order["id"]]],
        body=f"<p><b>Góp ý duyệt từ {html.escape(ctx.user.name or 'cấp trên')}:</b> {html.escape(clean_feedback)}</p>",
        message_type="comment", subtype_xmlid="mail.mt_note", partner_ids=[],
        context={"mail_notify_force_send": False, "mail_post_autofollow": False},
    )
    if type(note_id) is not int or note_id <= 0:
        raise OdooError("Odoo chưa trả về ID ghi chú góp ý.")
    recorded = adapter.call(
        "mail.message", "search_read", [[
            ("id", "=", note_id), ("model", "=", "sale.order"),
            ("res_id", "=", order["id"]),
        ]], fields=["id"], limit=1,
    )
    if len(recorded) != 1 or recorded[0].get("id") != note_id:
        raise OdooError("Odoo chưa xác minh ghi chú góp ý trên báo giá.")

    # Clear the pending request so the sale can edit and resubmit.
    req_ids = [r["id"] for r in state.get("requests") or [] if isinstance(r.get("id"), int)]
    if req_ids:
        if adapter.call("studio.approval.request", "unlink", [req_ids]) is not True:
            raise OdooError("Odoo chưa xác nhận đã đóng yêu cầu duyệt cũ.")
    remaining = adapter.inspect(order["id"])
    if remaining.get("requests"):
        raise OdooError("Odoo vẫn còn yêu cầu duyệt đang chờ; chưa thể gửi báo giá để sửa.")

    with sf() as db:
        db.query(QuotationPdfToken).filter_by(
            tenant=ctx.user.tenant, database=ctx.user.database, order_id=order["id"], active=1,
        ).update({"active": 0})
        db.commit()

    delivered = False
    if settings.TELEGRAM_BOT_TOKEN and row.requester_user_id and row.requester_chat_id and row.requester_binding_id:
        try:
            with sf() as db:
                tg = db.query(TelegramConnection).filter_by(
                    user_id=row.requester_user_id,
                    private_chat_id=row.requester_chat_id,
                    binding_id=row.requester_binding_id,
                    status="connected",
                ).first()
            if tg:
                refreshed = _read_order_for_token(ctx, order["id"], row.company_id)
                req_scope = TelegramScope(
                    telegram_user_id=tg.telegram_user_id,
                    chat_id=row.requester_chat_id,
                    telegram_binding_id=row.requester_binding_id,
                )
                sale_tok = issue_sale_pdf_token(
                    sf, user_id=row.requester_user_id, tenant=ctx.user.tenant,
                    database=ctx.user.database, scope=req_scope, order=refreshed,
                    company_id=row.company_id,
                )
                client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
                try:
                    filename, pdf_bytes = render_quotation_pdf(ctx, refreshed)
                    sent = client.send_document(
                        row.requester_chat_id,
                        filename,
                        pdf_bytes,
                        caption=f"💬 Góp ý chỉnh sửa báo giá {refreshed['name']} từ {ctx.user.name}:\n{clean_feedback}",
                        content_type="application/pdf",
                        inline_keyboard=sale_pdf_keyboard(sale_tok, can_request_approval=True),
                    )
                    mark_pdf_token_delivered(sf, sale_tok, sent)
                except Exception:
                    client.send_message(
                        row.requester_chat_id,
                        f"💬 Góp ý chỉnh sửa báo giá {refreshed['name']} từ {ctx.user.name}:\n{clean_feedback}",
                    )
                delivered = True
        except Exception as exc:
            logger.warning("Could not deliver feedback to requester: %s", type(exc).__name__)

    if delivered:
        return {
            "status": "recorded",
            "message": f"Đã ghi nhận và chuyển góp ý chỉnh sửa báo giá {order['name']} cho nhân viên phụ trách.",
        }
    return {
        "status": "recorded_not_delivered",
        "message": f"Đã ghi nhận góp ý trên báo giá {order['name']}, nhưng chưa gửi được thông báo Telegram cho nhân viên phụ trách.",
    }
