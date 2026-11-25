import base64
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import ActionOperation, Base, TelegramConnection, User
from app.quotations import execution
from app.quotations.pdf_review import QuotationPdfToken
from app.quotations.pdf import render_quotation_pdf


@pytest.mark.parametrize('details', [{}, {'missing_fields': ['partner_invoice_id', 'partner_shipping_id']}])
def test_missing_address_notification_uses_display_names(details):
    message = execution.quotation_failure_message(
        'Còn thiếu thông tin: partner_invoice_id, partner_shipping_id', details,
    )
    assert 'Địa chỉ xuất hóa đơn' in message
    assert 'Địa chỉ giao hàng' in message
    assert 'partner_' not in message


def test_render_uses_native_report_and_excludes_static_template_attachments():
    pdf = b'%PDF-1.7\n%%EOF'
    ctx = SimpleNamespace(user=SimpleNamespace(odoo_uid=5), selected_companies=Mock(), call=Mock(side_effect=[
        [{'order_line': [19]}], 1,
        ('ir.actions.report', 31), [{'group_ids': []}],
        ('mail.template', 41), [{'model': 'sale.order', 'report_template_ids': [31], 'attachment_ids': [99]}],
        60, [{'attachment_ids': [99, 102]}], [{'name': 'Quotation - S00084.pdf', 'datas': base64.b64encode(pdf).decode()}],
    ]))
    assert render_quotation_pdf(ctx, {'id': 83, 'company_id': [3, 'Company']}) == ('Quotation - S00084.pdf', pdf)
    ctx.selected_companies.assert_called_once_with([3])
    calls = [call.args for call in ctx.call.call_args_list]
    assert calls[6][:2] == ('mail.compose.message', 'create')
    assert calls[6][2][0]['res_ids'] == '[83]'
    assert calls[-1][2][0][0] == ('id', 'in', [102])
    assert all(method != 'action_send_mail' for _, method, *_ in calls)


@pytest.mark.parametrize('delivery_case', ['sent', 'ambiguous', 'scope_changed', 'pdf_error', 'binding_changed'])
def test_restart_delivers_saved_result_without_recreating_quote(monkeypatch, delivery_case):
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    scope = {'telegram_user_id': 7, 'chat_id': 7, 'telegram_binding_id': 'fixture'}
    with sf() as db:
        db.add(User(id=1, tenant='https://odoo.invalid', database='fixture', odoo_uid=1, login='user', name='User'))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7, private_chat_id=7, status='connected',
                                  binding_id='new-binding' if delivery_case == 'binding_changed' else 'fixture'))
        db.add(execution.QuoteExecutionJob(operation_id='op_fixture', user_id=1, tenant='https://odoo.invalid', database='fixture',
            scope_json=json.dumps(scope), payload_json='{}', status='completed', notified='pending'))
        db.add(ActionOperation(operation_id='op_fixture', user_id=1, action='sales.quotation.create', payload_hash='fixture', status='completed',
            result_json=json.dumps({'status': 'completed', 'result': {'id': 83, 'name': 'S00084',
                'state': 'draft', 'write_date': '2026-11-16 12:00:00', 'company_id': [3, 'Company']}})))
        db.commit()
    auth = Mock(side_effect=lambda *args: nullcontext(SimpleNamespace(user=SimpleNamespace(id=1, odoo_uid=1,
        tenant='https://other.invalid' if delivery_case == 'scope_changed' else 'https://odoo.invalid', database='fixture'),
        service=SimpleNamespace(client=Mock()), secret='fixture')))
    monkeypatch.setattr(execution, 'authenticated_context', auth)
    create = Mock(side_effect=AssertionError('Must never replay a saved write'))
    monkeypatch.setattr(execution.QuotationService, 'create', create)
    monkeypatch.setattr(execution.QuotationService, '_readback', lambda self, order_id, company_id: {
        'id': 83, 'name': 'S00084', 'state': 'draft',
        'write_date': '2026-11-16 12:00:00', 'company_id': [3, 'Company']})
    send = Mock()
    upload = Mock(side_effect=RuntimeError('ambiguous network send') if delivery_case == 'ambiguous' else None,
                  return_value={'message_id': 88})
    render = Mock(side_effect=ValueError('report unavailable') if delivery_case == 'pdf_error' else None,
                  return_value=('S00084.pdf', b'%PDF-1.7\n%%EOF'))
    monkeypatch.setattr(execution, 'render_quotation_pdf', render)
    monkeypatch.setattr(execution, 'read_approval_status', Mock(return_value={'status': 'pending'}))
    monkeypatch.setattr('app.telegram.client.TelegramClient', lambda token: SimpleNamespace(send_message=send, send_document=upload))
    worker = execution.QuoteExecutionWorker(SimpleNamespace(TELEGRAM_BOT_TOKEN='fixture', ENABLE_QUOTATION_APPROVAL_FLOW=True), sf)
    worker.tick()
    worker.tick()
    assert send.call_count == (1 if delivery_case == 'pdf_error' else 0)
    assert upload.call_count == (1 if delivery_case in ('sent', 'ambiguous') else 0)
    if delivery_case == 'sent':
        assert upload.call_args.args == (7, 'S00084.pdf', b'%PDF-1.7\n%%EOF')
        assert 'S00084' in upload.call_args.kwargs['caption']
        assert 'Đang chờ cấp trên duyệt' in upload.call_args.kwargs['caption']
        assert upload.call_args.kwargs['content_type'] == 'application/pdf'
    if send.call_count:
        assert 'S00084' in send.call_args.args[1]
        assert 'Chưa lấy được file PDF' in send.call_args.args[1]
        assert send.call_args.args[0] == 7
    create.assert_not_called()
    with sf() as db:
        row = db.query(execution.QuoteExecutionJob).one()
        assert row.status == 'completed'
        assert row.notified == {'sent': 'delivered', 'ambiguous': 'unknown', 'scope_changed': 'blocked',
                                'pdf_error': 'delivered', 'binding_changed': 'blocked'}[delivery_case]


def test_creation_only_sends_draft_pdf_to_sale_before_pdf_confirmation(monkeypatch):
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    scope = {'telegram_user_id': 7, 'chat_id': 7, 'telegram_binding_id': 'sale-bind'}
    with sf() as db:
        db.add(User(id=1, tenant='tenant', database='db', odoo_uid=5,
                    login='sale', name='Sale'))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7,
            private_chat_id=7, binding_id='sale-bind', status='connected'))
        db.add(User(id=2, tenant='tenant', database='db', odoo_uid=11,
                    login='approver', name='Approver'))
        db.add(TelegramConnection(user_id=2, telegram_user_id=8,
            private_chat_id=8, binding_id='approver-bind', status='connected'))
        db.add(execution.QuoteExecutionJob(operation_id='op-new', user_id=1,
            tenant='tenant', database='db', scope_json=json.dumps(scope),
            payload_json='{}', status='queued'))
        db.add(ActionOperation(operation_id='op-new', user_id=1,
            action='sales.quotation.create', payload_hash='fixture', status='in_progress'))
        db.commit()
    result = {'id': 201, 'name': 'S00201', 'state': 'draft',
              'write_date': 'v1', 'company_id': [3, 'Company']}
    ctx = SimpleNamespace(user=SimpleNamespace(id=1, tenant='tenant', database='db',
        odoo_uid=5), service=SimpleNamespace(client=Mock()), secret='fixture')
    monkeypatch.setattr(execution, 'authenticated_context',
                        lambda *args: nullcontext(ctx))
    create = Mock(return_value=result)
    monkeypatch.setattr(execution.QuotationService, 'create', create)
    monkeypatch.setattr(execution.QuotationService, '_readback', lambda self, *args: result)
    monkeypatch.setattr(execution, 'read_approval_status',
                        Mock(return_value={'status': 'not_requested'}))
    monkeypatch.setattr(execution, 'render_quotation_pdf',
                        Mock(return_value=('S00201.pdf', b'%PDF-1.7\n')))
    native_request = Mock(side_effect=AssertionError('No approval before sale PDF button'))
    monkeypatch.setattr('app.quotations.approval.QuotationApproval.request', native_request)
    send = Mock(return_value={'message_id': 44})
    monkeypatch.setattr('app.telegram.client.TelegramClient',
        lambda *args: SimpleNamespace(send_document=send, send_message=Mock()))
    worker = execution.QuoteExecutionWorker(SimpleNamespace(
        TELEGRAM_BOT_TOKEN='fixture', ENABLE_QUOTATION_APPROVAL_FLOW=True), sf)
    worker.tick()
    create.assert_called_once()
    native_request.assert_not_called()
    send.assert_called_once()
    assert send.call_args.args[0] == 7
    buttons = send.call_args.kwargs['inline_keyboard'][0]
    assert [button['text'] for button in buttons] == ['Chỉnh sửa', 'Xác nhận gửi duyệt']
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='sale', message_id=44).count() == 1
        assert db.query(QuotationPdfToken).filter_by(role='approver').count() == 0
