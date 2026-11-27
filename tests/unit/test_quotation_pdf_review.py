"""Unit tests for Gateway quotation optional fields, versioned PDF edits, and Studio approval callbacks."""
from __future__ import annotations

from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.business import models as _business_models  # noqa: F401
from app.models import Base, OdooCredential, TelegramConnection, User
from app.quotations import pdf_review
from app.quotations.approval import APPROVAL_METHOD
from app.quotations.service import QuotationService, QuotationValidationError
from app.quotations.tracking import QuotationRecordLink
from app.reporting.context import VerifiedContext
from app.reporting.schemas import TelegramScope
from app.telegram.service import TelegramService


class FakeOdooClient:
    def __init__(self):
        self.writes = []
        self.activities = []
        self.order = {
            "id": 97,
            "name": "S00097",
            "state": "draft",
            "write_date": "2026-11-24 10:00:00",
            "company_id": [2, "Công ty Demo"],
            "currency_id": [1, "VND"],
            "amount_total": 500000.0,
            "order_line": [31],
            "partner_id": [11, "Khách hàng A"],
            "pricelist_id": [3, "Thương mại"],
            "payment_term_id": [5, "30 ngày"],
        }
        self.lines = {
            31: {
                "id": 31,
                "product_id": [22, "Bao tay"],
                "name": "Bao tay y tế",
                "product_uom_qty": 2.0,
                "price_unit": 250000.0,
                "company_id": [2, "Công ty Demo"],
            }
        }
        self.rule = {
            "id": 14,
            "method": APPROVAL_METHOD,
            "action_id": False,
            "can_validate": False,
            "approver_ids": [24],
            "approval_group_id": False,
        }
        self.entries = []
        self.requests = []

    def studio_approval_spec(self, uid, secret, record_id, method, company):
        rule_copy = deepcopy(self.rule)
        rule_copy["can_validate"] = uid == 24
        return {
            "all_rules": {"14": rule_copy},
            "sale.order": [[[record_id, method, False], {"rules": [14], "entries": deepcopy(self.entries)}]],
        }

    def execute(self, uid, secret, model, method, args, kwargs):
        if model == "res.users" and method == "read":
            return [{"company_id": [2, "Công ty Demo"], "company_ids": [2]}]
        if model == "mail.activity" and method == "search_read":
            return deepcopy(self.activities)
        if model == "mail.activity" and method == "create":
            values = args[0]
            self.activities.append({"id": len(self.activities) + 1,
                                    "user_id": [values["user_id"], "Approver"],
                                    "summary": values["summary"]})
            return len(self.activities)
        if model == "ir.model" and method == "search_read":
            return [{"id": 1}]
        if model == "ir.model.data" and method == "search_read":
            return [{"res_id": 1}]
        if method == "check_access_rights":
            return True
        if method == "fields_get":
            if model == "sale.order":
                return {
                    "id": {"type": "integer", "string": "ID"},
                    "name": {"type": "char", "string": "Số báo giá", "readonly": True},
                    "state": {"type": "selection", "string": "Trạng thái", "readonly": True},
                    "write_date": {"type": "datetime", "string": "Cập nhật", "readonly": True},
                    "company_id": {"type": "many2one", "relation": "res.company", "string": "Công ty"},
                    "partner_id": {"type": "many2one", "relation": "res.partner", "string": "Khách hàng"},
                    "pricelist_id": {"type": "many2one", "relation": "product.pricelist", "string": "Bảng giá"},
                    "currency_id": {"type": "many2one", "relation": "res.currency", "string": "Tiền tệ"},
                    "payment_term_id": {"type": "many2one", "relation": "account.payment.term", "string": "Điều khoản"},
                    "validity_date": {"type": "date", "string": "Ngày hết hạn"},
                    "order_line": {"type": "one2many", "relation": "sale.order.line", "string": "Dòng hàng"},
                    "amount_total": {"type": "monetary", "string": "Tổng"},
                    "require_signature": {"type": "boolean", "string": "Chữ ký online"},
                    "require_payment": {"type": "boolean", "string": "Thanh toán online"},
                    "commitment_date": {"type": "datetime", "string": "Ngày giao hàng"},
                    "team_id": {"type": "many2one", "relation": "crm.team", "string": "Bộ phận sales"},
                    "tag_ids": {"type": "many2many", "relation": "crm.tag", "string": "Thẻ"},
                    "client_order_ref": {"type": "char", "string": "Tham chiếu KH"},
                    "x_studio_thoi_gian_bao_tri": {
                        "type": "selection",
                        "string": "Thời gian bảo trì",
                        "selection": [("3m", "3 tháng"), ("12m", "12 tháng")],
                    },
                    "x_approved_flag": {"type": "boolean", "string": "Approved"},
                }
            if model == "sale.order.line":
                return {
                    "id": {"type": "integer", "string": "ID"},
                    "product_id": {"type": "many2one", "relation": "product.product", "string": "Sản phẩm"},
                    "name": {"type": "text", "string": "Mô tả"},
                    "product_uom_qty": {"type": "float", "string": "Số lượng"},
                    "product_uom_id": {"type": "many2one", "relation": "uom.uom", "string": "ĐVT"},
                    "price_unit": {"type": "float", "string": "Đơn giá"},
                    "tax_ids": {"type": "many2many", "relation": "account.tax", "string": "Thuế"},
                    "company_id": {"type": "many2one", "relation": "res.company", "string": "Công ty"},
                }
            return {
                "id": {"type": "integer"},
                "display_name": {"type": "char"},
                "name": {"type": "char"},
                "company_id": {"type": "many2one", "relation": "res.company"},
                "active": {"type": "boolean"},
            }
        if method == "default_get":
            return {}
        if model == "sale.order" and method in ("search_read", "read"):
            return [deepcopy(self.order)]
        if model == "sale.order.line" and method in ("read", "search_read"):
            return [deepcopy(self.lines[31])]
        if model == "sale.order.line" and method == "write":
            ids, vals = args
            self.writes.append(("sale.order.line", ids, vals))
            self.lines[ids[0]].update(vals)
            self.order["write_date"] = "2026-11-24 10:05:00"
            return True
        if model == "sale.order" and method == "write":
            ids, vals = args
            self.writes.append(("sale.order", ids, vals))
            self.order.update(vals)
            self.order["write_date"] = "2026-11-24 10:06:00"
            return True
        if model == "sale.order" and method == "message_post":
            self.writes.append(("message_post", args, kwargs))
            return 501
        if model == "mail.message" and method == "search_read":
            return [{"id": 501}]
        if model == "crm.team" and method == "search_read":
            return [{"id": 52, "display_name": "Miền Nam", "company_id": [2, "Công ty Demo"], "active": True}]
        if model == "crm.tag" and method == "search_read":
            return [{"id": 7, "display_name": "VIP", "active": True}]
        if model == "account.payment.term" and method == "search_read":
            return [{"id": 9, "display_name": "Thanh toán ngay", "company_id": [2, "Công ty Demo"], "active": True}]
        if model == "res.company" and method == "search_read":
            return [{"id": 2, "display_name": "Công ty Demo", "currency_id": [1, "VND"], "active": True}]
        if model == "res.currency" and method == "search_read":
            return [{"id": 1, "display_name": "VND", "active": True}]
        if model == "res.partner" and method == "search_read":
            return [{"id": 11, "display_name": "Khách A", "company_id": [2, "Công ty Demo"], "active": True}]
        if model == "product.pricelist" and method == "search_read":
            return [{"id": 3, "display_name": "Thương mại", "currency_id": [1, "VND"], "company_id": [2, "Công ty Demo"], "active": True}]
        if model == "product.product" and method == "search_read":
            return [{"id": 22, "display_name": "Bao tay", "uom_id": [1, "Hộp"], "sale_ok": True, "list_price": 250000.0, "currency_id": [1, "VND"], "active": True}]
        if model == "uom.uom" and method == "search_read":
            return [{"id": 1, "display_name": "Hộp", "category_id": [1, "Đơn vị"], "active": True}]
        if model == "studio.approval.rule" and method == "check_approval":
            self.requests = [{"id": 901, "rule_id": [14, "Manager Approval"], "res_id": 97}]
            return True
        if model == "studio.approval.rule" and method == "set_approval":
            self.entries = [{"id": 801, "rule_id": [14, "Manager Approval"], "res_id": 97, "approved": kwargs["approved"]}]
            return True
        if model == "studio.approval.request" and method == "search_read":
            return deepcopy(self.requests)
        if model == "studio.approval.request" and method == "unlink":
            self.requests = []
            return True
        if model in ("studio.approval.rule", "studio.approval.entry") and method == "read":
            return [{"id": args[0][0]}]
        return []


def test_optional_metadata_candidates_and_prepare_validation():
    odoo = FakeOdooClient()
    svc = QuotationService(odoo, 10, "secret")

    sel_meta = svc.optional_metadata("x_studio_thoi_gian_bao_tri")
    assert sel_meta == {
        "available": True,
        "type": "selection",
        "options": [{"value": "3m", "label": "3 tháng"}, {"value": "12m", "label": "12 tháng"}],
    }
    assert svc.optional_metadata("require_signature") == {"available": True, "type": "boolean", "options": []}
    assert svc.optional_metadata("x_approved_flag")["available"] is False

    cand = svc.optional_candidates("team_id", "Miền")
    assert cand["items"][0]["id"] == 52

    prepared = svc.prepare({
        "partner_id": 11,
        "pricelist_id": 3,
        "payment_term_id": 9,
        "validity_date": "2026-12-20",
        "lines": [{"product_id": 22, "quantity": 2}],
        "optional_fields": {
            "require_signature": False,
            "team_id": 52,
            "tag_ids": [7],
            "x_studio_thoi_gian_bao_tri": "12m",
            "commitment_date": "2026-12-20 07:30:00",
            "client_order_ref": "REF-99",
        },
    })
    assert prepared["status"] == "ready"
    vals = prepared["values"]
    assert vals["require_signature"] is False
    assert vals["team_id"] == 52
    assert vals["tag_ids"] == [[6, 0, [7]]]
    assert vals["x_studio_thoi_gian_bao_tri"] == "12m"
    assert vals["commitment_date"] == "2026-12-20 07:30:00"
    assert vals["client_order_ref"] == "REF-99"

    with pytest.raises(QuotationValidationError):
        svc.prepare({
            "partner_id": 11,
            "pricelist_id": 3,
            "lines": [{"product_id": 22, "quantity": 1}],
            "optional_fields": {"x_approved_flag": True},
        })


def test_telegram_service_maps_pdf_callbacks_without_clearing_edit_buttons():
    sf = sessionmaker(bind=create_engine("sqlite://"))
    Base.metadata.create_all(sf.kw["bind"])
    tok = "a" * 24
    with sf() as db:
        db.add(User(id=1, tenant="https://odoo.invalid", database="db", odoo_uid=10,
                    login="sale", name="Sale", active=True))
        db.add(TelegramConnection(user_id=1, telegram_user_id=55, private_chat_id=55,
                                  binding_id="bind_sale", status="connected"))
        db.add(pdf_review.QuotationPdfToken(
            token=tok, role="sale", user_id=1, tenant="https://odoo.invalid",
            database="db", chat_id=55, telegram_binding_id="bind_sale",
            order_id=97, order_name="S00097", company_id=2,
            order_version="2026-11-24 10:00:00", message_id=700,
            active=1, created_at=time.time(),
        ))
        db.commit()
    tg_client = SimpleNamespace(
        answer_callback_query=Mock(),
        clear_inline_keyboard=Mock(),
        send_message=Mock(),
    )
    svc = TelegramService(
        session_factory=sf,
        telegram_client=tg_client,
        bot_username="demo_bot",
        webhook_secret="sec",
        enable_ai_forwarding=True,
    )
    res_edit = svc.ingest_update({
        "update_id": 1001,
        "callback_query": {
            "id": "cb_1",
            "data": f"qedit:{tok}",
            "from": {"id": 55, "is_bot": False},
            "message": {"message_id": 700, "chat": {"id": 55, "type": "private"}},
        },
    })
    assert res_edit["status"] == "ok"
    tg_client.clear_inline_keyboard.assert_not_called()

    res_appr = svc.ingest_update({
        "update_id": 1002,
        "callback_query": {
            "id": "cb_2",
            "data": f"qappr:{tok}",
            "from": {"id": 55, "is_bot": False},
            "message": {"message_id": 700, "chat": {"id": 55, "type": "private"}},
        },
    })
    assert res_appr["status"] == "ok"
    tg_client.clear_inline_keyboard.assert_called_once_with(55, 700)

    forged = svc.ingest_update({
        "update_id": 1003,
        "callback_query": {"id": "cb_3", "data": f"qappr:{tok}",
                           "from": {"id": 55, "is_bot": False},
                           "message": {"message_id": 701, "chat": {"id": 55, "type": "private"}}},
    })
    assert forged["status"] == "ignored"


def test_approver_delivery_checks_recipient_odoo_identity(monkeypatch):
    sf = sessionmaker(bind=create_engine("sqlite://"))
    Base.metadata.create_all(sf.kw["bind"])
    with sf() as db:
        db.add(User(id=2, tenant="https://odoo.invalid", database="db", odoo_uid=24,
                    login="mgr", name="Manager", active=True))
        db.add(OdooCredential(user_id=2, credential_kind="api_key", encrypted_secret="fixture"))
        db.commit()
    monkeypatch.setattr("app.odoo.client.OdooClient", lambda *args: object())
    monkeypatch.setattr("app.security.decrypt_secret", lambda *args: "fixture-secret")
    observed = []

    def inspect(self, order_id):
        observed.append((self.uid, order_id))
        return {"status": "pending", "requests": [{"id": 901}],
                "rules": [{"id": 14, "can_validate": self.uid == 24}]}

    monkeypatch.setattr(pdf_review.QuotationApproval, "inspect", inspect)
    state = {"rules": [{"id": 14, "notification_order": 1, "approver_ids": [24]}],
             "entries": []}
    with sf() as db:
        person = db.get(User, 2)
        assert pdf_review._can_receive_approval(
            SimpleNamespace(ENCRYPTION_KEY="fixture"), sf, person, 97, 2, state
        ) is True
    assert observed == [(24, 97)]


def test_pdf_lifecycle_edit_submit_feedback_and_grant(monkeypatch):
    sf = sessionmaker(bind=create_engine("sqlite://"))
    Base.metadata.create_all(sf.kw["bind"])
    with sf() as db:
        db.add(User(id=1, tenant="https://odoo.invalid", database="db", odoo_uid=10, login="sale", name="Nhân viên Sale", active=True))
        db.add(User(id=2, tenant="https://odoo.invalid", database="db", odoo_uid=24, login="mgr", name="Trưởng phòng", active=True))
        db.add(TelegramConnection(user_id=1, telegram_user_id=101, private_chat_id=101, binding_id="bind_sale", status="connected"))
        db.add(TelegramConnection(user_id=2, telegram_user_id=202, private_chat_id=202, binding_id="bind_mgr", status="connected"))
        db.add(QuotationRecordLink(user_id=1, operation_id="op-97", tenant="https://odoo.invalid",
                                   database="db", order_id=97, company_id=2))
        db.commit()

    odoo = FakeOdooClient()
    sent_docs = []
    sent_msgs = []
    monkeypatch.setattr(pdf_review, "render_quotation_pdf", lambda ctx, order: (f"{order['name']}.pdf", b"%PDF-1.7\n%%EOF"))
    monkeypatch.setattr(pdf_review, "_can_receive_approval", lambda *args: True)

    def send_document(chat_id, filename, content, **kwargs):
        sent_docs.append((chat_id, filename, kwargs))
        return {"message_id": 700 + len(sent_docs)}

    monkeypatch.setattr(
        pdf_review,
        "TelegramClient",
        lambda token: SimpleNamespace(
            send_document=send_document,
            send_message=lambda chat_id, text, **kw: sent_msgs.append((chat_id, text)),
        ),
    )

    settings = SimpleNamespace(TELEGRAM_BOT_TOKEN="bot-token", ENABLE_QUOTATION_APPROVAL_FLOW=True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=sf)))

    def make_ctx(user_id: int, odoo_uid: int, name: str):
        return VerifiedContext(
            user=SimpleNamespace(id=user_id, tenant="https://odoo.invalid", database="db", odoo_uid=odoo_uid, name=name),
            service=SimpleNamespace(client=odoo),
            secret="sec",
            company_ids=[2],
            default_company_id=2,
            session_factory=sf,
        )

    sale_ctx = make_ctx(1, 10, "Nhân viên Sale")
    mgr_ctx = make_ctx(2, 24, "Trưởng phòng")
    sale_scope = TelegramScope(telegram_user_id=101, chat_id=101, telegram_binding_id="bind_sale")
    mgr_scope = TelegramScope(telegram_user_id=202, chat_id=202, telegram_binding_id="bind_mgr")

    # 1. Retry PDF issues a versioned sale token with inline buttons
    retried = pdf_review.retry_sale_pdf(request, sale_ctx, 1, sale_scope, name="S00097")
    assert retried == {"status": "sent", "order_id": 97, "name": "S00097"}
    first_kb = sent_docs[-1][2]["inline_keyboard"][0]
    first_token = first_kb[0]["callback_data"].split(":")[1]
    assert len(first_token) == 24

    # 2. Open & edit quantity on same sale.order; old token becomes stale
    opened = pdf_review.open_sale_pdf(request, sale_ctx, 1, sale_scope, first_token)
    assert opened["status"] == "OK" and opened["order_id"] == 97
    edited = pdf_review.edit_sale_pdf(request, sale_ctx, 1, sale_scope, token=first_token, line_id=31, quantity=5)
    assert edited == {"status": "sent", "order_id": 97, "name": "S00097"}
    assert odoo.lines[31]["product_uom_qty"] == 5.0
    assert pdf_review.open_sale_pdf(request, sale_ctx, 1, sale_scope, first_token)["status"] == "stale"

    second_token = sent_docs[-1][2]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    assert second_token != first_token

    # 3. Submit approval -> delivers approver PDF with qgrant/qfeed buttons to manager chat 202
    submitted = pdf_review.submit_pdf_approval(request, sale_ctx, 1, sale_scope, second_token)
    assert submitted["status"] == "sent"
    assert sent_docs[-1][0] == 202
    appr_kb = sent_docs[-1][2]["inline_keyboard"][0]
    appr_token = appr_kb[0]["callback_data"].split(":")[1]
    sent_before_retry = len(sent_docs)
    retried_request = pdf_review.submit_pdf_approval(request, sale_ctx, 1, sale_scope, second_token)
    assert retried_request["status"] == "sent"
    assert len(sent_docs) == sent_before_retry
    assert len(odoo.requests) == 1

    # 4. Manager opens feedback & sends revision comment -> delivered back to sale chat 101 with fresh sale PDF
    assert pdf_review.open_approval_pdf(request, mgr_ctx, 2, mgr_scope, appr_token)["status"] == "OK"
    fb_res = pdf_review.decide_approval_pdf(request, mgr_ctx, 2, mgr_scope, token=appr_token, feedback="Giảm số lượng xuống 4")
    assert fb_res["status"] == "recorded"
    assert sent_docs[-1][0] == 101
    third_token = sent_docs[-1][2]["inline_keyboard"][0][0]["callback_data"].split(":")[1]

    # 5. Sale user resubmits approval -> Manager clicks Grant Approval -> approved on Odoo & notified
    resub = pdf_review.submit_pdf_approval(request, sale_ctx, 1, sale_scope, third_token)
    assert resub["status"] == "sent"
    appr_token_2 = sent_docs[-1][2]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    granted = pdf_review.decide_approval_pdf(request, mgr_ctx, 2, mgr_scope, token=appr_token_2)
    assert granted["status"] == "approved"
    assert any(chat_id == 101 and "phê duyệt" in msg for chat_id, msg in sent_msgs)
