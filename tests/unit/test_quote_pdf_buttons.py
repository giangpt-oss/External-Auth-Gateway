"""PDF controls carry a record and Odoo version, independent of chat history."""
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.quotations import routes
from app.quotations.interaction import QuoteApprovalDelivery, QuotePdfMessage, find_pdf_message, send_pdf_with_buttons
from app.quotations.tracking import QuotationRecordLink


def setup_db():
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    with sf() as db:
        db.add(QuotationRecordLink(user_id=1, operation_id='op1', tenant='tenant',
            database='db', order_id=97, company_id=3))
        db.commit()
    return sf


def test_each_pdf_has_own_record_version_and_both_buttons():
    sf = setup_db()
    user = SimpleNamespace(id=1, tenant='tenant', database='db')
    scope = SimpleNamespace(telegram_user_id=7, chat_id=7, telegram_binding_id='bind')
    send = Mock(return_value={'message_id': 44})
    client = SimpleNamespace(send_document=send)
    send_pdf_with_buttons(client, sf, user, scope, {'id': 97, 'name': 'S00097',
        'company_id': [3, 'Company'], 'write_date': '2026-11-16 12:00:00'},
        'S00097.pdf', b'%PDF-1.7\n', 'Preview')
    buttons = send.call_args.kwargs['reply_markup']['inline_keyboard'][0]
    assert [button['text'] for button in buttons] == ['Chỉnh sửa', 'Xác nhận gửi duyệt']
    token = buttons[0]['callback_data'].split(':')[1]
    assert buttons[1]['callback_data'] == 'qs:' + token
    assert find_pdf_message(sf, token, 7, 7, 44)['order_id'] == 97
    assert find_pdf_message(sf, token, 7, 7, 45) is None
    assert find_pdf_message(sf, token, 8, 7, 44) is None
    with sf() as db:
        assert db.get(QuotePdfMessage, token).version == '2026-11-16 12:00:00'


def test_old_pdf_version_and_wrong_owner_are_rejected(monkeypatch):
    sf = setup_db()
    user = SimpleNamespace(id=1, tenant='tenant', database='db', odoo_uid=5,
        telegram_connection=SimpleNamespace(telegram_user_id=7,
            private_chat_id=7, binding_id='bind'))
    scope = SimpleNamespace(telegram_user_id=7, chat_id=7, telegram_binding_id='bind')
    send = Mock(return_value={'message_id': 44})
    send_pdf_with_buttons(SimpleNamespace(send_document=send), sf, user, scope,
        {'id': 97, 'company_id': [3, 'Company'], 'write_date': 'v1'},
        'test.pdf', b'%PDF-1.7\n', 'Preview')
    token = send.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data'].split(':')[1]
    ctx = SimpleNamespace(user=user, service=SimpleNamespace(client=Mock()), secret='fixture')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf)))
    monkeypatch.setattr(routes.QuotationService, '_readback', lambda self, *args: {
        'id': 97, 'state': 'draft', 'write_date': 'v2', 'company_id': [3, 'Company']})
    with pytest.raises(HTTPException) as stale:
        routes._pdf_target(request, ctx, token)
    assert stale.value.status_code == 409
    ctx.user = SimpleNamespace(id=2, tenant='tenant', database='db', odoo_uid=6,
        telegram_connection=user.telegram_connection)
    with pytest.raises(HTTPException) as other:
        routes._pdf_target(request, ctx, token)
    assert other.value.status_code == 403


def test_edit_writes_only_selected_line_and_sends_new_pdf(monkeypatch):
    native_request = Mock(side_effect=AssertionError('Editing must not request approval'))
    monkeypatch.setattr('app.quotations.approval.QuotationApproval.request', native_request)
    sf = setup_db()
    before = {'id': 97, 'name': 'S00097', 'state': 'draft',
        'company_id': [3, 'Company'], 'write_date': 'v1',
        'validity_date': '2026-12-20', 'payment_term_id': [19, '30 Days'],
        'lines': [{'id': 31, 'product_uom_qty': 2}, {'id': 32, 'product_uom_qty': 4}]}
    after = {**before, 'write_date': 'v2', 'lines': [
        {'id': 31, 'product_uom_qty': 3}, {'id': 32, 'product_uom_qty': 4}]}
    ctx = SimpleNamespace(user=SimpleNamespace(id=1, odoo_uid=5),
        service=SimpleNamespace(client=Mock()), secret='fixture',
        selected_companies=Mock(), call=Mock(return_value=True))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf,
        settings=SimpleNamespace(ENABLE_QUOTATIONS=True, TELEGRAM_BOT_TOKEN='fixture'))))
    monkeypatch.setattr(routes, 'authenticated_context', lambda *args: nullcontext(ctx))
    monkeypatch.setattr(routes, '_pdf_target', lambda *args: before)
    monkeypatch.setattr(routes.QuotationService, '_readback', lambda self, *args: after)
    monkeypatch.setattr('app.quotations.pdf.render_quotation_pdf',
                        lambda *args: ('S00097.pdf', b'%PDF-1.7\n'))
    send = Mock()
    monkeypatch.setattr('app.quotations.interaction.send_pdf_with_buttons', send)
    body = routes.PdfEditRequest(telegram_user_id=7, chat_id=7,
        telegram_binding_id='bind', token='a' * 24, line_id=31, quantity=3)
    result = routes.edit_pdf_quotation(1, body, request)
    assert result == {'status': 'sent', 'order_id': 97, 'name': 'S00097', 'version': 'v2'}
    ctx.call.assert_called_once_with('sale.order.line', 'write', [[31], {'product_uom_qty': 3}])
    assert after['validity_date'] == before['validity_date']
    assert after['payment_term_id'] == before['payment_term_id']
    assert after['lines'][1] == before['lines'][1]
    send.assert_called_once()
    native_request.assert_not_called()
    with sf() as db:
        assert db.query(QuoteApprovalDelivery).count() == 0


def test_edit_payment_term_keeps_same_order_and_never_requests_approval(monkeypatch):
    native_request = Mock(side_effect=AssertionError('Editing must not request approval'))
    monkeypatch.setattr('app.quotations.approval.QuotationApproval.request', native_request)
    sf = setup_db()
    before = {'id': 101, 'name': 'S00101', 'state': 'draft', 'company_id': [3, 'Company'],
        'write_date': 'v1', 'validity_date': '2026-12-20', 'payment_term_id': [4, '30 Days'],
        'lines': [{'id': 31, 'product_uom_qty': 2}]}
    after = {**before, 'write_date': 'v2', 'payment_term_id': [1, 'Thanh toán ngay']}
    ctx = SimpleNamespace(user=SimpleNamespace(id=1, odoo_uid=5),
        service=SimpleNamespace(client=Mock()), secret='fixture', selected_companies=Mock(),
        call=Mock(return_value=True))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf,
        settings=SimpleNamespace(ENABLE_QUOTATIONS=True, TELEGRAM_BOT_TOKEN='fixture'))))
    monkeypatch.setattr(routes, 'authenticated_context', lambda *args: nullcontext(ctx))
    monkeypatch.setattr(routes, '_pdf_target', lambda *args: before)
    monkeypatch.setattr(routes.QuotationService, '_record', lambda self, *args: {'id': 1})
    monkeypatch.setattr(routes.QuotationService, '_readback', lambda self, *args: after)
    monkeypatch.setattr('app.quotations.pdf.render_quotation_pdf',
                        lambda *args: ('S00101.pdf', b'%PDF-1.7\n'))
    send = Mock()
    monkeypatch.setattr('app.quotations.interaction.send_pdf_with_buttons', send)
    body = routes.PdfEditRequest(telegram_user_id=7, chat_id=7,
        telegram_binding_id='bind', token='a' * 24, payment_term_id=1)
    result = routes.edit_pdf_quotation(1, body, request)
    assert result['status'] == 'sent' and result['order_id'] == 101
    ctx.call.assert_called_once_with('sale.order', 'write', [[101], {'payment_term_id': 1}])
    assert after['lines'] == before['lines'] and after['validity_date'] == before['validity_date']
    send.assert_called_once()
    native_request.assert_not_called()
    with sf() as db:
        assert db.query(QuoteApprovalDelivery).count() == 0


def test_edit_optional_field_keeps_pdf_target_and_no_approval(monkeypatch):
    sf = setup_db()
    before = {'id': 101, 'name': 'S00101', 'state': 'draft', 'company_id': [3, 'Company'],
        'write_date': 'v1', 'validity_date': '2026-12-20', 'payment_term_id': [4, '30 Days'],
        'require_signature': True, 'lines': [{'id': 31, 'product_uom_qty': 2}]}
    after = {**before, 'write_date': 'v2', 'require_signature': False}
    ctx = SimpleNamespace(user=SimpleNamespace(id=1, odoo_uid=5),
        service=SimpleNamespace(client=Mock()), secret='fixture', selected_companies=Mock(),
        call=Mock(return_value=True))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf,
        settings=SimpleNamespace(ENABLE_QUOTATIONS=True, TELEGRAM_BOT_TOKEN='fixture'))))
    monkeypatch.setattr(routes, 'authenticated_context', lambda *args: nullcontext(ctx))
    monkeypatch.setattr(routes, '_pdf_target', lambda *args: before)
    monkeypatch.setattr(routes.QuotationService, 'optional_values', lambda self, *args: {'require_signature': False})
    monkeypatch.setattr(routes.QuotationService, 'optional_spec', lambda self, *args: {'type': 'boolean'})
    monkeypatch.setattr(routes.QuotationService, '_readback', lambda self, *args: after)
    monkeypatch.setattr('app.quotations.pdf.render_quotation_pdf',
                        lambda *args: ('S00101.pdf', b'%PDF-1.7\n'))
    send = Mock()
    monkeypatch.setattr('app.quotations.interaction.send_pdf_with_buttons', send)
    body = routes.PdfEditRequest(telegram_user_id=7, chat_id=7,
        telegram_binding_id='bind', token='a' * 24, optional_fields={'require_signature': False})
    result = routes.edit_pdf_quotation(1, body, request)
    assert result['status'] == 'sent' and result['order_id'] == 101
    ctx.call.assert_called_once_with('sale.order', 'write', [[101], {'require_signature': False}])
    assert before['lines'] == after['lines']
    send.assert_called_once()
    with sf() as db:
        assert db.query(QuoteApprovalDelivery).count() == 0
