"""Studio rule recipients are checked before any native approval request or Telegram send."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, TelegramConnection, User
from app.quotations import routes
from app.quotations.interaction import QuoteApprovalDelivery
from app.quotations.interaction import QuotePdfMessage


def environment(monkeypatch, linked):
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    with sf() as db:
        db.add(User(id=1, tenant='tenant', database='db', odoo_uid=5,
                    login='sale', name='Sale'))
        db.add(QuotePdfMessage(token='a' * 24, user_id=1, tenant='tenant',
            database='db', order_id=97, company_id=3,
            version='2026-11-16 12:00:00', telegram_user_id=7,
            chat_id=7, binding_id='sale', message_id=44))
        for uid in linked:
            gateway_id = uid + 100
            db.add(User(id=gateway_id, tenant='tenant', database='db', odoo_uid=uid,
                        login=str(uid), name=str(uid)))
            db.add(TelegramConnection(user_id=gateway_id, telegram_user_id=gateway_id,
                private_chat_id=gateway_id, binding_id=f'bind-{uid}', status='connected'))
        db.commit()
    result = {'id': 97, 'name': 'S00097', 'state': 'draft',
              'company_id': [3, 'Company'], 'write_date': '2026-11-16 12:00:00'}
    ctx = SimpleNamespace(user=SimpleNamespace(id=1, tenant='tenant', database='db', odoo_uid=5, name='Sale'),
        service=SimpleNamespace(client=Mock()), secret='fixture')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf,
        settings=SimpleNamespace(ENABLE_QUOTATIONS=True, ENABLE_QUOTATION_APPROVAL_FLOW=True,
                                 TELEGRAM_BOT_TOKEN='fixture'))))
    monkeypatch.setattr(routes, 'authenticated_context', lambda *args: nullcontext(ctx))
    monkeypatch.setattr(routes, '_pdf_target', lambda *args: result)
    monkeypatch.setattr(routes, '_can_receive_approval', lambda *args: True)
    monkeypatch.setattr(routes, '_record_native_review', lambda *args: None)
    monkeypatch.setattr(routes.QuotationService, '_readback', lambda self, *args: result)
    rule = {'id': 14, 'approver_ids': [11], 'notification_order': 1,
            'exclusive_user': False}
    adapter = Mock()
    adapter.inspect.return_value = {'status': 'not_requested', 'rules': [rule], 'entries': []}
    adapter.request.return_value = {**adapter.inspect.return_value, 'status': 'pending'}
    adapter.call.return_value = [{'id': 11, 'name': 'Approver 11'}]
    monkeypatch.setattr('app.quotations.approval.QuotationApproval', lambda *args: adapter)
    monkeypatch.setattr('app.quotations.pdf.render_quotation_pdf',
                        lambda *args: ('S00097.pdf', b'%PDF-1.7\n'))
    send = Mock(return_value={'message_id': 90})
    monkeypatch.setattr('app.telegram.client.TelegramClient',
                        lambda *args: SimpleNamespace(send_document=send))
    body = routes.PdfInteractionRequest(telegram_user_id=7, chat_id=7,
        telegram_binding_id='sale', token='a' * 24)
    return sf, request, body, adapter, send


def test_missing_link_records_odoo_request_without_claiming_telegram_delivery(monkeypatch):
    sf, request, body, adapter, send = environment(monkeypatch, linked=[])
    result = routes.submit_quotation_approval(1, body, request)
    assert result['status'] == 'partial'
    assert 'Approver 11' in result['message']
    adapter.request.assert_called_once()
    send.assert_not_called()
    with sf() as db:
        assert db.query(QuoteApprovalDelivery).count() == 0


def test_existing_approval_entry_cannot_be_resent_as_new_request(monkeypatch):
    sf, request, body, adapter, send = environment(monkeypatch, linked=[11])
    adapter.inspect.return_value = {**adapter.inspect.return_value,
        'status': 'approved', 'entries': [
            {'rule_id': [14, 'Approval'], 'approved': True,
             'user_id': [5, 'Creator']}]}
    with pytest.raises(HTTPException) as error:
        routes.submit_quotation_approval(1, body, request)
    assert error.value.status_code == 409
    adapter.request.assert_not_called()
    send.assert_not_called()
    with sf() as db:
        assert db.query(QuoteApprovalDelivery).count() == 0


def test_one_studio_step_sends_to_only_configured_candidate_once(monkeypatch):
    # A previously linked user must not receive a PDF after removal from Odoo's rule.
    sf, request, body, adapter, send = environment(monkeypatch, linked=[11, 12])
    result = routes.submit_quotation_approval(1, body, request)
    assert result['status'] == 'sent'
    assert send.call_count == 1
    assert send.call_args.args[0] == 111
    assert adapter.request.call_count == 1
    for call in send.call_args_list:
        buttons = call.kwargs['reply_markup']['inline_keyboard'][0]
        assert [button['text'] for button in buttons] == ['Phê duyệt', 'Yêu cầu chỉnh sửa']
    with sf() as db:
        rows = db.query(QuoteApprovalDelivery).all()
        assert len(rows) == 1
        assert {row.order_id for row in rows} == {97}
        assert {row.rule_id for row in rows} == {14}
        assert {row.version for row in rows} == {'2026-11-16 12:00:00'}
    routes.submit_quotation_approval(1, body, request)
    assert send.call_count == 1


def test_one_missing_link_does_not_block_linked_approver(monkeypatch):
    sf, request, body, adapter, send = environment(monkeypatch, linked=[11])
    adapter.inspect.return_value['rules'][0]['approver_ids'] = [11, 12]
    adapter.call.return_value = [{'id': 12, 'name': 'Missing approver'}]
    result = routes.submit_quotation_approval(1, body, request)
    assert result['status'] == 'partial'
    assert 'Missing approver' in result['message']
    assert send.call_count == 1
    with sf() as db:
        assert db.query(QuoteApprovalDelivery).count() == 1


def test_known_telegram_failure_retries_without_second_odoo_request(monkeypatch):
    from app.telegram.client import BotBlockedError

    sf, request, body, adapter, send = environment(monkeypatch, linked=[11])
    state = adapter.inspect.return_value
    adapter.inspect.side_effect = [state, {**state, 'status': 'pending'}]
    send.side_effect = [BotBlockedError('blocked'), {'message_id': 91}]
    first = routes.submit_quotation_approval(1, body, request)
    assert first['status'] == 'partial'
    with sf() as db:
        row = db.query(QuoteApprovalDelivery).one()
        assert row.status == 'failed'
        token = row.token
    second = routes.submit_quotation_approval(1, body, request)
    assert second['status'] == 'sent'
    assert adapter.request.call_count == 2  # retry reconciles without another create
    with sf() as db:
        row = db.query(QuoteApprovalDelivery).one()
        assert row.token == token and row.message_id == 91 and row.status == 'pending'


@pytest.mark.parametrize('feedback,odoo_status', [
    (None, 'approved'), (None, 'pending'),
    ('Xin đổi lại còn 2 Bao tay, giữ nguyên giá và hạn.', 'approved')])
def test_decision_notifies_exact_creator_and_records_feedback(monkeypatch, feedback, odoo_status):
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    with sf() as db:
        db.add(User(id=1, tenant='tenant', database='db', odoo_uid=5,
                    login='sale', name='Sale'))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7, private_chat_id=7,
                                  binding_id='sale-bind', status='connected'))
        db.add(QuotePdfMessage(token='a' * 24, user_id=1, tenant='tenant', database='db',
            order_id=97, company_id=3, version='v1', telegram_user_id=7,
            chat_id=7, binding_id='sale-bind', message_id=44))
        db.add(QuoteApprovalDelivery(token='b' * 24, pdf_token='a' * 24, rule_id=14,
            approver_user_id=2, order_id=97, company_id=3, version='v1',
            chat_id=8, telegram_user_id=8, binding_id='approver-bind', message_id=55))
        db.commit()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf,
        settings=SimpleNamespace(ENABLE_QUOTATIONS=True,
            ENABLE_QUOTATION_APPROVAL_FLOW=True, TELEGRAM_BOT_TOKEN='fixture'))))
    ctx = SimpleNamespace(user=SimpleNamespace(id=2, odoo_uid=12, name='Approver'),
        service=SimpleNamespace(client=Mock()), secret='fixture')
    monkeypatch.setattr(routes, 'authenticated_context', lambda *args: nullcontext(ctx))
    monkeypatch.setattr(routes, '_approval_delivery', lambda *args: (
        {'pdf_token': 'a' * 24, 'rule_id': 14, 'order_id': 97, 'company_id': 3,
         'version': 'v1'}, {'id': 97, 'name': 'S00097', 'state': 'draft',
                            'write_date': 'v1'}))
    monkeypatch.setattr(routes.QuotationService, '_readback', lambda self, *args:
        {'id': 97, 'name': 'S00097', 'state': 'draft', 'write_date': 'v1'})
    adapter = Mock()
    adapter.decide.return_value = {'status': odoo_status, 'entries': [
        {'rule_id': [14, 'Approval'], 'approved': True, 'user_id': [12, 'Approver']}]}
    adapter.inspect.return_value = {'status': 'pending', 'rules': [
        {'id': 14, 'can_validate': True}], 'entries': []}
    adapter.call.side_effect = [101, [{'id': 101}]]
    monkeypatch.setattr('app.quotations.approval.QuotationApproval', lambda *args: adapter)
    next_stage = Mock(return_value={'status': 'sent', 'message': 'Đã gửi bước kế tiếp.'})
    monkeypatch.setattr(routes, 'submit_quotation_approval', next_stage)
    send = Mock(return_value={'message_id': 100})
    monkeypatch.setattr('app.telegram.client.TelegramClient',
                        lambda *args: SimpleNamespace(send_message=send))
    body = routes.ApprovalReplyRequest(telegram_user_id=8, chat_id=8,
        telegram_binding_id='approver-bind', token='b' * 24, feedback=feedback)
    outcome = routes.decide_quotation_approval(2, body, request)
    assert outcome['status'] == 'recorded'
    assert send.call_args.args[0] == 7
    assert 'S00097' in send.call_args.args[1]
    with sf() as db:
        row = db.get(QuoteApprovalDelivery, 'b' * 24)
        assert row.status == ('feedback' if feedback else 'approved')
        assert row.feedback_text == feedback
    if feedback:
        assert feedback in send.call_args.args[1]
        assert send.call_args.kwargs['preserve_text'] is True
        adapter.decide.assert_not_called()
    else:
        adapter.decide.assert_called_once_with(97, 14, True)
        if odoo_status == 'pending':
            next_stage.assert_called_once()
            assert 'Đã gửi bước kế tiếp' in send.call_args.args[1]
        else:
            next_stage.assert_not_called()
