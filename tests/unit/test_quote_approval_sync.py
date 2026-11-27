"""Odoo decisions are reconciled without writing to Odoo or resending Telegram."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.business.models import QuotationApprovalReview
from app.models import Base, TelegramConnection, User
from app.quotations import approval_sync
from app.quotations.pdf_review import QuotationPdfToken
from app.telegram.client import TelegramDeliveryUnknownError


@pytest.mark.parametrize("approved,expected", [
    (True, "✅ Manager đã phê duyệt báo giá S00097 trên Odoo."),
    (False, "❌ Manager đã từ chối duyệt báo giá S00097 trên Odoo."),
])
def test_direct_odoo_decision_notifies_sale_once(monkeypatch, approved, expected):
    sf = sessionmaker(bind=create_engine("sqlite://"))
    Base.metadata.create_all(sf.kw["bind"])
    row = QuotationPdfToken(
        token="a" * 24, role="sale", user_id=1, tenant="tenant", database="db",
        chat_id=123, telegram_binding_id="binding", order_id=97,
        order_name="S00097", company_id=3, order_version="v1",
        message_id=44, active=1, created_at=100,
    )
    with sf() as db:
        db.add(User(id=1, tenant="tenant", database="db", odoo_uid=5,
                    login="sale", name="Sale"))
        db.add(TelegramConnection(user_id=1, telegram_user_id=123,
                                  private_chat_id=123, binding_id="binding",
                                  status="connected"))
        db.add(row)
        db.add(QuotationApprovalReview(
            key=approval_sync._review_key(row), digest="digest",
            operation_id="op", requester_id=1, created_at=100,
            verified=1,
        ))
        db.commit()
    state = {"status": "approved" if approved else "rejected", "entries": [{
        "id": 55, "rule_id": [14, "Step 1"], "approved": approved,
        "user_id": [11, "Manager"],
    }]}
    adapter = Mock()
    adapter.inspect.return_value = state
    monkeypatch.setattr(approval_sync, "QuotationApproval", lambda *args: adapter)
    monkeypatch.setattr(approval_sync, "_read_order_for_token", lambda *args: {
        "id": 97, "name": "S00097", "state": "draft", "write_date": "v1",
    })

    @contextmanager
    def context_factory(request, user_id, scope):
        yield SimpleNamespace(user=SimpleNamespace(odoo_uid=5, name="Sale"),
                              service=SimpleNamespace(client=Mock()), secret="mock")

    telegram = Mock()
    telegram.send_message.return_value = {"message_id": 80}
    settings = SimpleNamespace(TELEGRAM_BOT_TOKEN="mock")
    for _ in range(2):
        approval_sync.reconcile_once(settings, sf, telegram_client=telegram,
                                     context_factory=context_factory)
    telegram.send_message.assert_called_once_with(123, expected)
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role="sync", message_id=80).count() == 1


def test_unknown_delivery_is_not_retried_blindly(monkeypatch):
    sf = sessionmaker(bind=create_engine("sqlite://"))
    Base.metadata.create_all(sf.kw["bind"])
    row = QuotationPdfToken(token="b" * 24, role="sale", user_id=1,
                            tenant="tenant", database="db", chat_id=123,
                            telegram_binding_id="binding", order_id=97,
                            order_name="S00097", company_id=3,
                            order_version="v1", message_id=44, active=1,
                            created_at=100)
    with sf() as db:
        db.add(User(id=1, tenant="tenant", database="db", odoo_uid=5,
                    login="sale", name="Sale"))
        db.add(TelegramConnection(user_id=1, telegram_user_id=123,
                                  private_chat_id=123, binding_id="binding",
                                  status="connected"))
        db.add(row)
        db.add(QuotationApprovalReview(key=approval_sync._review_key(row),
                    digest="digest", operation_id="op", requester_id=1,
                    created_at=100, verified=1))
        db.commit()
    adapter = Mock()
    adapter.inspect.return_value = {"status": "approved", "entries": [{
        "id": 55, "rule_id": [14, "Step 1"], "approved": True,
        "user_id": [11, "Manager"],
    }]}
    monkeypatch.setattr(approval_sync, "QuotationApproval", lambda *args: adapter)
    monkeypatch.setattr(approval_sync, "_read_order_for_token", lambda *args: {
        "id": 97, "state": "draft", "write_date": "v1",
    })

    @contextmanager
    def context_factory(request, user_id, scope):
        yield SimpleNamespace(user=SimpleNamespace(odoo_uid=5, name="Sale"),
                              service=SimpleNamespace(client=Mock()), secret="mock")

    telegram = Mock()
    telegram.send_message.side_effect = TelegramDeliveryUnknownError("timeout")
    settings = SimpleNamespace(TELEGRAM_BOT_TOKEN="mock")
    for _ in range(2):
        approval_sync.reconcile_once(settings, sf, telegram_client=telegram,
                                     context_factory=context_factory)
    telegram.send_message.assert_called_once()
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role="sync", message_id=None).count() == 1
