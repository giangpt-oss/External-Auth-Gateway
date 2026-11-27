"""Native Studio approval requests and PDF delivery stay separate and idempotent."""
from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, TelegramConnection, User
from app.odoo.client import OdooError
from app.quotations import pdf_review, routes
from app.quotations.pdf_review import QuotationPdfToken
from app.quotations.approval import QuotationApproval
from app.reporting.schemas import TelegramScope


def environment(monkeypatch, linked):
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    with sf() as db:
        db.add(User(id=1, tenant='tenant', database='db', odoo_uid=5,
                    login='sale', name='Sale'))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7,
            private_chat_id=7, binding_id='sale', status='connected'))
        db.add(QuotationPdfToken(token='a' * 24, role='sale', user_id=1,
            tenant='tenant', database='db', chat_id=7, telegram_binding_id='sale',
            order_id=97, order_name='S00097', company_id=3,
            order_version='2026-11-16 12:00:00', message_id=44,
            active=1, created_at=time.time()))
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
    monkeypatch.setattr(pdf_review, '_read_order_for_token', lambda *args: result)
    monkeypatch.setattr(pdf_review, '_can_receive_approval', lambda *args: True)
    monkeypatch.setattr(pdf_review, 'BusinessService',
                        lambda context: SimpleNamespace(company_id=3))
    monkeypatch.setattr(pdf_review, 'review_key', lambda *args: 'fixture-review')
    monkeypatch.setattr(pdf_review, 'commercial_snapshot', lambda *args: 'fixture-digest')
    rule = {'id': 14, 'approver_ids': [11], 'notification_order': 1,
            'exclusive_user': False, 'approval_group_id': False}
    adapter = Mock()
    state = {'status': 'not_requested', 'rules': [rule], 'entries': [], 'requests': []}
    adapter.state = state
    adapter.native_creates = 0
    adapter.inspect.side_effect = lambda order_id: deepcopy(state)

    def request_approval(order_id):
        if state['status'] == 'not_requested':
            state['status'] = 'pending'
            state['requests'] = [{'id': 901}]
            adapter.native_creates += 1
        return deepcopy(state)

    adapter.request.side_effect = request_approval
    def odoo_call(model, method, args, **kwargs):
        if (model, method) == ('res.users', 'search_read'):
            return [{'id': uid, 'name': f'Approver {uid}'} for uid in args[0][0][2]]
        if (model, method) == ('sale.order', 'message_post'):
            return 501
        if (model, method) == ('mail.message', 'search_read'):
            return [{'id': 501}]
        if (model, method) == ('studio.approval.request', 'unlink'):
            state['requests'] = []
            return True
        raise AssertionError(f'Unexpected Odoo call: {model}.{method}')
    adapter.call.side_effect = odoo_call
    monkeypatch.setattr(pdf_review, 'QuotationApproval', lambda *args: adapter)
    monkeypatch.setattr(pdf_review, 'render_quotation_pdf',
                        lambda *args: ('S00097.pdf', b'%PDF-1.7\n'))
    send = Mock(return_value={'message_id': 90})
    notify = Mock(return_value={'message_id': 91})
    monkeypatch.setattr(pdf_review, 'TelegramClient',
                        lambda *args: SimpleNamespace(send_document=send, send_message=notify))
    body = routes.PdfTokenRequest(telegram_user_id=7, chat_id=7,
        telegram_binding_id='sale', token='a' * 24)
    request.app.state.ctx = ctx
    request.app.state.scope = TelegramScope(telegram_user_id=7, chat_id=7,
                                            telegram_binding_id='sale')
    request.app.state.notify = notify
    return sf, request, body, adapter, send


def test_sale_who_is_also_approver_does_not_auto_approve():
    adapter = QuotationApproval(Mock(), 5, 'mock', 3)
    state = {'status': 'not_requested', 'rules': [{
        'id': 14, 'can_validate': True, 'notification_order': 1,
    }], 'requests': [], 'entries': []}
    pending = deepcopy(state)
    pending['status'] = 'pending'
    pending['requests'] = [{'id': 901}]
    adapter.inspect = Mock(side_effect=[state, pending, pending])
    adapter.call = Mock(return_value=901)
    adapter.ensure_request_activities = Mock()
    assert adapter.request(97)['status'] == 'pending'
    adapter.call.assert_called_once_with(
        'studio.approval.request', 'create', [{'rule_id': 14, 'res_id': 97}],
    )


def test_missing_link_records_odoo_request_without_claiming_telegram_delivery(monkeypatch):
    sf, request, body, adapter, send = environment(monkeypatch, linked=[])
    result = routes.approval_submit(1, body, request)
    assert result['status'] == 'sent'
    assert 'chưa xác nhận gửi PDF' in result['message']
    adapter.request.assert_called_once()
    assert adapter.native_creates == 1
    send.assert_not_called()
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='approver').count() == 0


def test_existing_approval_entry_cannot_be_resent_as_new_request(monkeypatch):
    sf, request, body, adapter, send = environment(monkeypatch, linked=[11])
    adapter.state['status'] = 'approved'
    adapter.state['entries'] = [{'rule_id': [14, 'Approval'], 'approved': True}]
    result = routes.approval_submit(1, body, request)
    assert result['status'] == 'stale'
    adapter.request.assert_not_called()
    send.assert_not_called()
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='approver').count() == 0


def test_one_studio_step_sends_to_only_configured_candidate_once(monkeypatch):
    # A previously linked user must not receive a PDF after removal from Odoo's rule.
    sf, request, body, adapter, send = environment(monkeypatch, linked=[11, 12])
    result = routes.approval_submit(1, body, request)
    assert result['status'] == 'sent'
    assert send.call_count == 1
    assert send.call_args.args[0] == 111
    assert adapter.request.call_count == 1
    for call in send.call_args_list:
        buttons = call.kwargs['inline_keyboard'][0]
        assert [button['text'] for button in buttons] == ['Phê duyệt', 'Yêu cầu chỉnh sửa']
    with sf() as db:
        rows = db.query(QuotationPdfToken).filter_by(role='approver').all()
        assert len(rows) == 1
        assert {row.order_id for row in rows} == {97}
        assert {row.order_version for row in rows} == {'2026-11-16 12:00:00'}
        assert rows[0].message_id == 90
    routes.approval_submit(1, body, request)
    assert send.call_count == 1
    assert adapter.native_creates == 1


def test_one_missing_link_does_not_block_linked_approver(monkeypatch):
    sf, request, body, adapter, send = environment(monkeypatch, linked=[11])
    adapter.state['rules'][0]['approver_ids'] = [11, 12]
    result = routes.approval_submit(1, body, request)
    assert result['status'] == 'sent'
    assert '1' in result['message']
    assert send.call_count == 1
    assert send.call_args.args[0] == 111
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='approver').count() == 1


def test_known_telegram_failure_retries_without_second_odoo_request(monkeypatch):
    from app.telegram.client import BotBlockedError

    sf, request, body, adapter, send = environment(monkeypatch, linked=[11])
    send.side_effect = [BotBlockedError('blocked'), {'message_id': 91}]
    first = routes.approval_submit(1, body, request)
    assert first['status'] == 'sent'
    assert 'chưa xác nhận gửi PDF' in first['message']
    with sf() as db:
        row = db.query(QuotationPdfToken).filter_by(role='approver').one()
        assert row.active == 0 and row.message_id is None
    second = routes.approval_submit(1, body, request)
    assert second['status'] == 'sent'
    assert adapter.request.call_count == 2  # retry reconciles without another create
    assert adapter.native_creates == 1
    with sf() as db:
        active = db.query(QuotationPdfToken).filter_by(role='approver', active=1).one()
        assert active.message_id == 91


@pytest.mark.parametrize('feedback,odoo_status', [
    (None, 'approved'), (None, 'pending'),
    ('Xin đổi lại còn 2 Bao tay, giữ nguyên giá và hạn.', 'approved')])
def test_decision_notifies_exact_creator_and_records_feedback(monkeypatch, feedback, odoo_status):
    linked = [11, 12] if odoo_status == 'pending' else [11]
    sf, request, body, adapter, send = environment(monkeypatch, linked=linked)
    if odoo_status == 'pending':
        adapter.state['rules'].append({
            'id': 15, 'approver_ids': [12], 'notification_order': 2,
            'approval_group_id': False, 'can_validate': False,
        })
    assert routes.approval_submit(1, body, request)['status'] == 'sent'
    assert send.call_count == 1 and send.call_args.args[0] == 111
    with sf() as db:
        approval = db.query(QuotationPdfToken).filter_by(role='approver', user_id=111).one()
        approval_token = approval.token
    adapter.state['rules'][0]['can_validate'] = True

    def decide(order_id, rule_id, approved):
        assert (order_id, rule_id, approved) == (97, 14, True)
        adapter.state['status'] = odoo_status
        adapter.state['entries'] = [{'rule_id': [14, 'Approval'], 'approved': True}]
        return deepcopy(adapter.state)

    adapter.decide.side_effect = decide
    monkeypatch.setattr('app.business.quotation_approval.require_review', lambda *args: None)
    approver_ctx = SimpleNamespace(
        user=SimpleNamespace(id=111, tenant='tenant', database='db', odoo_uid=11,
                             name='Approver 11'),
        service=SimpleNamespace(client=Mock()), secret='fixture',
    )
    approver_scope = TelegramScope(telegram_user_id=111, chat_id=111,
                                    telegram_binding_id='bind-11')
    outcome = pdf_review.decide_approval_pdf(
        request, approver_ctx, 111, approver_scope,
        token=approval_token, feedback=feedback,
    )
    with sf() as db:
        assert db.get(QuotationPdfToken, approval_token).active == 0
    if feedback:
        assert outcome['status'] == 'recorded'
        assert send.call_args.args[0] == 7
        assert feedback in send.call_args.kwargs['caption']
        assert any(c.args[0:2] == ('sale.order', 'message_post') for c in adapter.call.call_args_list)
        adapter.decide.assert_not_called()
    else:
        assert outcome['status'] == ('approved_pending' if odoo_status == 'pending' else 'approved')
        adapter.decide.assert_called_once_with(97, 14, True)
        notify = request.app.state.notify
        assert notify.call_args.args[0] == 7
        assert 'S00097' in notify.call_args.args[1]
        if odoo_status == 'pending':
            assert send.call_count == 2 and send.call_args.args[0] == 112
            with sf() as db:
                assert db.query(QuotationPdfToken).filter_by(role='approver', user_id=112,
                                                              active=1).count() == 1
        else:
            assert send.call_count == 1


@pytest.mark.parametrize('failed_step', ['message_post', 'note_readback', 'request_unlink'])
def test_feedback_does_not_claim_recorded_when_odoo_has_not_confirmed(monkeypatch, failed_step):
    sf, request, body, adapter, send = environment(monkeypatch, linked=[11])
    assert routes.approval_submit(1, body, request)['status'] == 'sent'
    adapter.state['rules'][0]['can_validate'] = True
    with sf() as db:
        approval_token = db.query(QuotationPdfToken).filter_by(role='approver').one().token
    monkeypatch.setattr('app.business.quotation_approval.require_review', lambda *args: None)

    def failing_call(model, method, args, **kwargs):
        if (model, method) == ('sale.order', 'message_post'):
            if failed_step == 'message_post':
                raise OdooError('Odoo unavailable')
            return 501
        if (model, method) == ('mail.message', 'search_read'):
            return [] if failed_step == 'note_readback' else [{'id': 501}]
        if (model, method) == ('studio.approval.request', 'unlink'):
            return False if failed_step == 'request_unlink' else True
        raise AssertionError(f'Unexpected Odoo call: {model}.{method}')

    adapter.call.side_effect = failing_call
    approver_ctx = SimpleNamespace(
        user=SimpleNamespace(id=111, tenant='tenant', database='db', odoo_uid=11,
                             name='Approver 11'),
        service=SimpleNamespace(client=Mock()), secret='fixture',
    )
    approver_scope = TelegramScope(telegram_user_id=111, chat_id=111,
                                   telegram_binding_id='bind-11')
    with pytest.raises(OdooError):
        pdf_review.decide_approval_pdf(request, approver_ctx, 111, approver_scope,
                                       token=approval_token, feedback='Xin sửa số lượng')
    with sf() as db:
        assert db.get(QuotationPdfToken, approval_token).active == 1
    assert send.call_count == 1
    request.app.state.notify.assert_not_called()
