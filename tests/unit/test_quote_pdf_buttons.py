"""PDF controls carry a record and Odoo version, independent of chat history."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, TelegramConnection, User
from app.quotations import pdf_review
from app.quotations.pdf_review import QuotationPdfToken
from app.quotations.service import QuotationService
from app.quotations.tracking import QuotationRecordLink
from app.reporting.schemas import TelegramScope


def setup_db():
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    with sf() as db:
        db.add(User(id=1, tenant='tenant', database='db', odoo_uid=5,
                    login='sale', name='Sale', active=True))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7,
                                  private_chat_id=7, binding_id='bind', status='connected'))
        db.add(QuotationRecordLink(user_id=1, operation_id='op1', tenant='tenant',
            database='db', order_id=97, company_id=3))
        db.commit()
    return sf


def sale_context():
    user = SimpleNamespace(id=1, tenant='tenant', database='db', odoo_uid=5)
    scope = TelegramScope(telegram_user_id=7, chat_id=7, telegram_binding_id='bind')
    ctx = SimpleNamespace(user=user, service=SimpleNamespace(client=Mock()), secret='fixture')
    return ctx, scope


def sale_token(sf, scope, order):
    token = pdf_review.issue_sale_pdf_token(
        sf, user_id=1, tenant='tenant', database='db', scope=scope,
        order=order, company_id=3,
    )
    pdf_review.mark_pdf_token_delivered(sf, token, {'message_id': 44})
    return token


def edit_environment(monkeypatch, before, after):
    sf = setup_db()
    ctx, scope = sale_context()
    token = sale_token(sf, scope, before)
    read = Mock(side_effect=[before, after])
    monkeypatch.setattr(pdf_review, '_read_order_for_token', lambda *args: read(*args))
    monkeypatch.setattr(pdf_review, 'render_quotation_pdf',
                        Mock(return_value=('quote.pdf', b'%PDF-1.7\n')))
    send = Mock(return_value={'message_id': 45})
    monkeypatch.setattr(pdf_review, 'TelegramClient',
                        lambda *args: SimpleNamespace(send_document=send))
    writes = []

    def rpc(self, model, method, args, *other, **kwargs):
        if method == 'write':
            writes.append((model, args))
        return True

    monkeypatch.setattr(QuotationService, '_rpc', rpc)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_factory=sf, settings=SimpleNamespace(
            ENABLE_QUOTATION_APPROVAL_FLOW=False, TELEGRAM_BOT_TOKEN='fixture'))))
    return sf, request, ctx, scope, token, read, send, writes


def test_each_pdf_has_own_record_version_and_both_buttons():
    sf = setup_db()
    ctx, scope = sale_context()
    order = {'id': 97, 'name': 'S00097', 'company_id': [3, 'Company'],
             'write_date': '2026-11-16 12:00:00'}
    token = sale_token(sf, scope, order)
    buttons = pdf_review.sale_pdf_keyboard(token, can_request_approval=True)[0]
    assert [button['text'] for button in buttons] == ['Chỉnh sửa', 'Xác nhận gửi duyệt']
    assert buttons[0]['callback_data'] == 'qedit:' + token
    assert buttons[1]['callback_data'] == 'qappr:' + token
    with sf() as db:
        row = db.get(QuotationPdfToken, token)
        assert row.order_id == 97 and row.order_version == '2026-11-16 12:00:00'
        assert row.message_id == 44 and row.role == 'sale'
    assert pdf_review._load_valid_token(sf, token=token, role='sale', user_id=1,
        tenant='tenant', database='db', scope=scope)
    wrong_scope = TelegramScope(telegram_user_id=8, chat_id=8, telegram_binding_id='bind')
    assert pdf_review._load_valid_token(sf, token=token, role='sale', user_id=1,
        tenant='tenant', database='db', scope=wrong_scope) is None


def test_old_pdf_version_and_wrong_owner_are_rejected(monkeypatch):
    sf = setup_db()
    ctx, scope = sale_context()
    token = sale_token(sf, scope, {'id': 97, 'name': 'S00097',
                                   'company_id': [3, 'Company'], 'write_date': 'v1'})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf)))
    monkeypatch.setattr(pdf_review, '_read_order_for_token', lambda *args: {
        'id': 97, 'name': 'S00097', 'state': 'draft', 'write_date': 'v2',
        'company_id': [3, 'Company'], 'lines': []})
    assert pdf_review.open_sale_pdf(request, ctx, 1, scope, token)['status'] == 'stale'
    ctx.user = SimpleNamespace(id=2, tenant='tenant', database='db', odoo_uid=6)
    assert pdf_review.open_sale_pdf(request, ctx, 2, scope, token)['status'] == 'stale'


def test_edit_writes_only_selected_line_and_sends_new_pdf(monkeypatch):
    native_request = Mock(side_effect=AssertionError('Editing must not request approval'))
    monkeypatch.setattr('app.quotations.approval.QuotationApproval.request', native_request)
    before = {'id': 97, 'name': 'S00097', 'state': 'draft',
        'company_id': [3, 'Company'], 'write_date': 'v1',
        'validity_date': '2026-12-20', 'payment_term_id': [19, '30 Days'],
        'order_line': [31, 32],
        'lines': [{'id': 31, 'product_uom_qty': 2}, {'id': 32, 'product_uom_qty': 4}]}
    after = {**before, 'write_date': 'v2', 'lines': [
        {'id': 31, 'product_uom_qty': 3}, {'id': 32, 'product_uom_qty': 4}]}
    sf, request, ctx, scope, token, read, send, writes = edit_environment(monkeypatch, before, after)
    result = pdf_review.edit_sale_pdf(request, ctx, 1, scope,
                                      token=token, line_id=31, quantity=3)
    assert result == {'status': 'sent', 'order_id': 97, 'name': 'S00097'}
    assert writes == [('sale.order.line', [[31], {'product_uom_qty': 3.0}])]
    assert after['validity_date'] == before['validity_date']
    assert after['payment_term_id'] == before['payment_term_id']
    assert after['lines'][1] == before['lines'][1]
    send.assert_called_once()
    native_request.assert_not_called()
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='sale', active=1, message_id=45).count() == 1
    assert pdf_review.open_sale_pdf(request, ctx, 1, scope, token)['status'] == 'stale'


def test_edit_payment_term_keeps_same_order_and_never_requests_approval(monkeypatch):
    native_request = Mock(side_effect=AssertionError('Editing must not request approval'))
    monkeypatch.setattr('app.quotations.approval.QuotationApproval.request', native_request)
    before = {'id': 101, 'name': 'S00101', 'state': 'draft', 'company_id': [3, 'Company'],
        'write_date': 'v1', 'validity_date': '2026-12-20', 'payment_term_id': [4, '30 Days'],
        'order_line': [31], 'lines': [{'id': 31, 'product_uom_qty': 2}]}
    after = {**before, 'write_date': 'v2', 'payment_term_id': [1, 'Thanh toán ngay']}
    sf, request, ctx, scope, token, read, send, writes = edit_environment(monkeypatch, before, after)
    monkeypatch.setattr(QuotationService, '_record', lambda self, *args: {'id': 1})
    result = pdf_review.edit_sale_pdf(request, ctx, 1, scope,
                                      token=token, payment_term_id=1)
    assert result['status'] == 'sent' and result['order_id'] == 101
    assert writes == [('sale.order', [[101], {'payment_term_id': 1}])]
    assert after['lines'] == before['lines'] and after['validity_date'] == before['validity_date']
    send.assert_called_once()
    native_request.assert_not_called()
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='approver').count() == 0


def test_edit_optional_field_keeps_pdf_target_and_no_approval(monkeypatch):
    before = {'id': 101, 'name': 'S00101', 'state': 'draft', 'company_id': [3, 'Company'],
        'write_date': 'v1', 'validity_date': '2026-12-20', 'payment_term_id': [4, '30 Days'],
        'require_signature': True, 'order_line': [31],
        'lines': [{'id': 31, 'product_uom_qty': 2}]}
    after = {**before, 'write_date': 'v2', 'require_signature': False}
    sf, request, ctx, scope, token, read, send, writes = edit_environment(monkeypatch, before, after)
    monkeypatch.setattr(QuotationService, '_optional_values',
                        lambda self, *args: {'require_signature': False})
    result = pdf_review.edit_sale_pdf(request, ctx, 1, scope,
                                      token=token, optional_fields={'require_signature': False})
    assert result['status'] == 'sent' and result['order_id'] == 101
    assert writes == [('sale.order', [[101], {'require_signature': False}])]
    assert before['lines'] == after['lines']
    send.assert_called_once()
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='approver').count() == 0
