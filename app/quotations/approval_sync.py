"""Reconcile Studio decisions made outside Telegram for reviewed quotations.

The existing PDF-token table stores a deterministic delivery marker. Reserving
it before sending prevents a timeout or process restart from duplicating a
notification. An unconfirmed marker requires manual reconciliation.
"""
import hashlib
import json
import logging
import time
from threading import Event, Thread
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.business.models import QuotationApprovalReview
from app.models import TelegramConnection, User
from app.quotations.approval import QuotationApproval
from app.quotations.pdf_review import (
    QuotationPdfToken, _deliver_pending_approval_pdfs, _read_order_for_token,
)
from app.reporting.context import authenticated_context
from app.reporting.schemas import TelegramScope
from app.telegram.client import TelegramClient, TelegramDeliveryUnknownError, TelegramError

logger = logging.getLogger(__name__)


def _review_key(row):
    return hashlib.sha256(json.dumps([
        row.tenant, row.database, row.company_id, row.order_id,
    ]).encode()).hexdigest()


def _event_token(row, entry):
    return hashlib.sha256(json.dumps([
        "odoo-approval-notice-v1", row.tenant, row.database,
        row.order_id, row.order_version, entry["id"], row.user_id,
    ]).encode()).hexdigest()[:24]


def _reserve(sf, row, token):
    with sf() as db:
        if db.get(QuotationPdfToken, token):
            return False
        db.add(QuotationPdfToken(
            token=token, role="sync", user_id=row.user_id,
            tenant=row.tenant, database=row.database,
            chat_id=row.chat_id, telegram_binding_id=row.telegram_binding_id,
            order_id=row.order_id, order_name=row.order_name,
            company_id=row.company_id, order_version=row.order_version,
            active=1, created_at=time.time(),
        ))
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False


def _mark_delivery(sf, token, sent):
    message_id = sent.get("message_id") if isinstance(sent, dict) else None
    if type(message_id) is not int:
        return
    with sf() as db:
        db.query(QuotationPdfToken).filter_by(token=token).update({"message_id": message_id})
        db.commit()


def reconcile_once(settings, sf, *, telegram_client=None, context_factory=None):
    """Poll only requests verified by Gateway; never write to Odoo."""
    client = telegram_client or TelegramClient(settings.TELEGRAM_BOT_TOKEN)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=settings, session_factory=sf,
    )))
    context_factory = context_factory or authenticated_context
    with sf() as db:
        sale_rows = db.query(QuotationPdfToken).filter_by(role="sale").order_by(
            QuotationPdfToken.created_at.desc()).all()
        candidates = []
        seen = set()
        for row in sale_rows:
            key = (row.tenant, row.database, row.order_id, row.company_id, row.user_id)
            if key in seen:
                continue
            seen.add(key)
            review = db.get(QuotationApprovalReview, _review_key(row))
            if review and review.verified and review.requester_id == row.user_id:
                candidates.append({column.name: getattr(row, column.name)
                                   for column in QuotationPdfToken.__table__.columns})

    for values in candidates:
        row = SimpleNamespace(**values)
        with sf() as db:
            owner = db.get(User, row.user_id)
            conn = db.query(TelegramConnection).filter_by(
                user_id=row.user_id, private_chat_id=row.chat_id,
                binding_id=row.telegram_binding_id, status="connected",
            ).first()
            if not owner or not owner.active or not conn or (
                owner.tenant != row.tenant or owner.database != row.database
            ):
                continue
            scope = TelegramScope(telegram_user_id=conn.telegram_user_id,
                                  chat_id=row.chat_id,
                                  telegram_binding_id=row.telegram_binding_id)
        try:
            with context_factory(request, row.user_id, scope) as ctx:
                order = _read_order_for_token(ctx, row.order_id, row.company_id)
                if order.get("state") != "draft" or order.get("write_date") != row.order_version:
                    continue
                adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid,
                                            ctx.secret, row.company_id)
                state = adapter.inspect(row.order_id)
                if not state.get("entries"):
                    continue
                entries = sorted(state["entries"], key=lambda item: item.get("id", 0))
                for entry in entries:
                    if type(entry.get("id")) is not int or type(entry.get("approved")) is not bool:
                        continue
                    token = _event_token(row, entry)
                    if not _reserve(sf, row, token):
                        continue
                    actor = entry.get("user_id") or []
                    actor_name = actor[1] if isinstance(actor, (list, tuple)) and len(actor) > 1 else "Người duyệt"
                    final = state["status"] == "approved" and entry is entries[-1]
                    if entry["approved"] is False:
                        message = f"❌ {actor_name} đã từ chối duyệt báo giá {row.order_name} trên Odoo."
                    elif final:
                        message = f"✅ {actor_name} đã phê duyệt báo giá {row.order_name} trên Odoo."
                    else:
                        message = f"✅ {actor_name} đã phê duyệt một bước của báo giá {row.order_name} trên Odoo; còn bước tiếp theo."
                    try:
                        sent = client.send_message(row.chat_id, message)
                        _mark_delivery(sf, token, sent)
                    except TelegramDeliveryUnknownError:
                        logger.warning("Approval notification delivery uncertain for order %s", row.order_id)
                    except TelegramError as exc:
                        with sf() as db:
                            db.query(QuotationPdfToken).filter_by(token=token).delete()
                            db.commit()
                        logger.warning("Approval notification failed for order %s: %s",
                                       row.order_id, type(exc).__name__)
                with sf() as db:
                    decided = {entry["rule_id"][0] for entry in state["entries"]
                               if entry.get("rule_id")}
                    if state["status"] in ("approved", "rejected"):
                        db.query(QuotationPdfToken).filter_by(
                            role="approver", tenant=row.tenant, database=row.database,
                            order_id=row.order_id, active=1,
                        ).update({"active": 0})
                    db.commit()
                if state["status"] == "pending" and decided:
                    _deliver_pending_approval_pdfs(
                        request, ctx, adapter, state, order, row.user_id,
                        row.chat_id, row.telegram_binding_id, ctx.user.name,
                    )
        except Exception as exc:
            logger.warning("Approval reconciliation failed for order %s: %s",
                           row.order_id, type(exc).__name__)


class ApprovalSyncWorker:
    def __init__(self, settings, sf):
        self.settings, self.sf = settings, sf
        self.stop_event = Event()
        self.thread = Thread(target=self._run, daemon=True, name="ApprovalSync")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self):
        while not self.stop_event.wait(60):
            reconcile_once(self.settings, self.sf)
            try:
                from app.business.purchase_approval import reconcile_purchase_approvals
                reconcile_purchase_approvals(self.settings, self.sf)
            except Exception as exc:
                logger.warning("Purchase approval sync failed: %s", type(exc).__name__)
