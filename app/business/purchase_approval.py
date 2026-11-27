"""Versioned Telegram approval buttons & notifications for Odoo Purchase Orders (RFQ -> to approve -> purchase)."""
from __future__ import annotations

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



def _ensure_table(sf) -> None:
    try:
        with sf() as db:
            PurchaseApprovalToken.__table__.create(bind=db.get_bind(), checkfirst=True)
    except Exception:
        logger.exception("Could not initialize purchase approval storage")
        raise


def requester_po_keyboard(token: str) -> list[list[dict[str, str]]]:
    return [[{"text": "Xác nhận gửi duyệt", "callback_data": f"pappr:{token}"}]]


def approver_po_keyboard(token: str) -> list[list[dict[str, str]]]:
    return [[
        {"text": "Xác nhận duyệt", "callback_data": f"pgrant:{token}"},
        {"text": "Từ chối", "callback_data": f"prej:{token}"},
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


def submit_purchase_approval(
    request,
    ctx,
    *,
    user_id: int,
    scope: TelegramScope,
    token: str,
) -> dict[str, Any]:
    """Requester clicks [Xác nhận gửi duyệt]: runs button_confirm and notifies Purchase Managers via Telegram."""
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
        po = svc.record("purchase.order", row.order_id, ["name", "state", "partner_id", "amount_total", "currency_id"], track=False)
    except (BusinessError, OdooError) as exc:
        return {"status": "error", "message": str(exc)}

    if po.get("state") not in ("draft", "sent"):
        return {"status": "stale", "message": f"Đơn mua {po.get('name')} hiện ở trạng thái “{po.get('state')}”, không thể gửi duyệt lại."}

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
        target_uids = other_managers or manager_uids
        delivered_count = 0
        if settings.TELEGRAM_BOT_TOKEN and target_uids:
            client = TelegramClient(settings.TELEGRAM_BOT_TOKEN)
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
                            company_id=row.company_id,
                            requester_user_id=user_id,
                            requester_chat_id=scope.chat_id,
                            requester_binding_id=scope.telegram_binding_id,
                            active=1,
                            created_at=time.time(),
                        )
                    )
                    _save_review(db, ctx, token=appr_token, user_id=approver_user.id,
                                 scope=TelegramScope(telegram_user_id=tg_conn.telegram_user_id,
                                                     chat_id=tg_conn.private_chat_id,
                                                     telegram_binding_id=tg_conn.binding_id),
                                 order_id=refreshed["id"], company_id=row.company_id)
                    db.commit()
                try:
                    text = _format_po_summary(
                        refreshed,
                        prefix=f"📋 Yêu cầu phê duyệt đơn mua {po_name} từ {ctx.user.name}.",
                    )
                    client.send_message(
                        tg_conn.private_chat_id,
                        text,
                        inline_keyboard=approver_po_keyboard(appr_token),
                    )
                    delivered_count += 1
                except Exception as exc:
                    logger.warning("Could not notify PO approver %s: %s", approver_user.id, type(exc).__name__)
        suffix = (
            f" Đã gửi yêu cầu duyệt tới {delivered_count} cấp trên qua Telegram."
            if delivered_count
            else " Đơn đã chuyển sang Chờ phê duyệt trên Odoo (cấp trên chưa liên kết Telegram sẽ duyệt trực tiếp trên Odoo)."
        )
        return {"status": "awaiting_approval", "message": f"Đã gửi yêu cầu phê duyệt đơn mua {po_name}.{suffix}", "record": refreshed}

    if new_state == "purchase":
        return {
            "status": "completed",
            "message": f"Đơn mua {po_name} đã được Odoo xác nhận trực tiếp thành Đơn mua (không vượt ngưỡng cần duyệt hai bước trên Odoo).\n{refreshed.get('url', '')}".strip(),
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
    """Approver clicks [Xác nhận duyệt] or [Từ chối]: runs button_approve/button_cancel and notifies requester."""
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
