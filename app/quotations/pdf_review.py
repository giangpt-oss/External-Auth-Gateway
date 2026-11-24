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
from app.reporting.context import bound_telegram_user
from app.reporting.schemas import TelegramScope
from app.telegram.client import TelegramClient

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
        {"text": "Xác nhận duyệt", "callback_data": f"qgrant:{token}"},
        {"text": "Cần chỉnh sửa", "callback_data": f"qfeed:{token}"},
    ]]


def _order_version(order: dict[str, Any]) -> str:
    return str(order.get("write_date") or order.get("id") or "v1")


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
    if order_id is None:
        if not name or not str(name).strip():
            raise QuotationValidationError("Cần cung cấp mã hoặc ID báo giá để gửi lại PDF.")
        found = svc.status_by_name(str(name).strip(), chosen_company)
        if found.get("status") != "OK":
            return {"status": "error", "message": found.get("message") or "Không tìm thấy báo giá trên Odoo."}
        order_id = found["result"]["id"]
    order = _read_order_for_token(ctx, _id(order_id, "order_id"), chosen_company)
    if getattr(settings, "ENABLE_QUOTATION_APPROVAL_FLOW", False):
        order["approval"] = read_approval_status(ctx, order)
    filename, content = render_quotation_pdf(ctx, order)
    token = issue_sale_pdf_token(
        sf, user_id=user_id, tenant=ctx.user.tenant, database=ctx.user.database,
        scope=scope, order=order, company_id=chosen_company,
    )
    keyboard = sale_pdf_keyboard(token, can_request_approval=_can_request_approval(settings, order)) if order.get("state") == "draft" else None
    client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
    with sf() as db:
        bound_telegram_user(db, user_id, scope)
    client.send_document(
        scope.chat_id,
        filename,
        content,
        caption=_pdf_caption(order),
        content_type="application/pdf",
        inline_keyboard=keyboard,
    )
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
        TelegramClient(settings.TELEGRAM_BOT_TOKEN).send_document(
            scope.chat_id,
            filename,
            content,
            caption=_pdf_caption(updated, prefix=f"Đã cập nhật báo giá {updated['name']}."),
            content_type="application/pdf",
            inline_keyboard=keyboard,
        )
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
    for rule in state.get("rules") or []:
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

    adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret, row.company_id)
    state = adapter.request(order["id"])
    if state.get("status") != "pending":
        return {"status": "error", "message": "Odoo chưa ghi nhận trạng thái chờ duyệt cho báo giá này."}

    refreshed = _read_order_for_token(ctx, order["id"], row.company_id)
    new_version = _order_version(refreshed)
    with sf() as db:
        db.query(QuotationApprovalReview).filter_by(key=key, operation_id=op_id).update({"verified": 1})
        db.query(QuotationPdfToken).filter_by(
            tenant=ctx.user.tenant, database=ctx.user.database, order_id=order["id"], active=1,
        ).update({"active": 0})
        db.commit()

    approver_uids = _find_approver_uids(adapter, state) - {ctx.user.odoo_uid}
    delivered_count = 0
    if settings.TELEGRAM_BOT_TOKEN and approver_uids:
        try:
            filename, pdf_bytes = render_quotation_pdf(ctx, refreshed)
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
                    )
                    .all()
                )
            for approver_user, tg_conn in targets:
                if not tg_conn.binding_id or not tg_conn.private_chat_id:
                    continue
                appr_token = secrets.token_hex(12)
                with sf() as db:
                    db.add(
                        QuotationPdfToken(
                            token=appr_token,
                            role="approver",
                            user_id=approver_user.id,
                            tenant=ctx.user.tenant,
                            database=ctx.user.database,
                            chat_id=tg_conn.private_chat_id,
                            telegram_binding_id=tg_conn.binding_id,
                            order_id=refreshed["id"],
                            order_name=refreshed["name"],
                            company_id=row.company_id,
                            order_version=new_version,
                            requester_user_id=user_id,
                            requester_chat_id=scope.chat_id,
                            requester_binding_id=scope.telegram_binding_id,
                            active=1,
                            created_at=time.time(),
                        )
                    )
                    db.commit()
                try:
                    caption = _pdf_caption(
                        refreshed,
                        prefix=f"📋 Yêu cầu phê duyệt báo giá {refreshed['name']} từ {ctx.user.name}.",
                    )
                    client.send_document(
                        tg_conn.private_chat_id,
                        filename,
                        pdf_bytes,
                        caption=caption,
                        content_type="application/pdf",
                        inline_keyboard=approver_pdf_keyboard(appr_token),
                    )
                    delivered_count += 1
                except Exception as exc:
                    logger.warning("Could not deliver approval PDF to approver %s: %s", approver_user.id, type(exc).__name__)
        except Exception as exc:
            logger.warning("Could not render PDF for approver delivery: %s", type(exc).__name__)

    suffix = (
        f" Đã gửi PDF cần duyệt tới {delivered_count} cấp trên qua Telegram."
        if delivered_count
        else " Yêu cầu đã lưu trên Odoo (cấp trên chưa liên kết Telegram sẽ thấy trên Odoo)."
    )
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
        if settings.TELEGRAM_BOT_TOKEN and row.requester_user_id and row.requester_chat_id and row.requester_binding_id:
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
                    TelegramClient(settings.TELEGRAM_BOT_TOKEN).send_message(
                        row.requester_chat_id,
                        f"✅ Cấp trên ({ctx.user.name}) đã phê duyệt báo giá {order['name']}.",
                    )
            except Exception as exc:
                logger.warning("Could not notify requester of approval: %s", type(exc).__name__)
        return {"status": "approved", "message": f"Đã phê duyệt báo giá {order['name']} trên Odoo."}

    clean_feedback = feedback.strip()[:2000]
    if not clean_feedback:
        return {"status": "error", "message": "Nội dung góp ý không được để trống."}

    # Post internal note to Odoo chatter if supported, and clear pending request so salesperson can edit & resubmit
    try:
        adapter.call(
            "sale.order",
            "message_post",
            [order["id"]],
            body=f"<p><b>Góp ý duyệt từ {html.escape(ctx.user.name or 'cấp trên')}:</b> {html.escape(clean_feedback)}</p>",
            subtype_xmlid="mail.mt_note",
        )
    except OdooError:
        pass
    req_ids = [r["id"] for r in state.get("requests") or [] if isinstance(r.get("id"), int)]
    if req_ids:
        try:
            adapter.call("studio.approval.request", "unlink", [req_ids])
        except OdooError:
            pass

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
                    client.send_document(
                        row.requester_chat_id,
                        filename,
                        pdf_bytes,
                        caption=f"💬 Góp ý chỉnh sửa báo giá {refreshed['name']} từ {ctx.user.name}:\n{clean_feedback}",
                        content_type="application/pdf",
                        inline_keyboard=sale_pdf_keyboard(sale_tok, can_request_approval=True),
                    )
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
