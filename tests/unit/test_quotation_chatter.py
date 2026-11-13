from types import SimpleNamespace

import pytest

from app.business import quotation_chatter
from app.business.service import BusinessError


def test_message_preview_uses_customer_email():
    service = SimpleNamespace(record=lambda model, record_id, fields=None: {'id': record_id, 'partner_id': [7, 'Customer'], 'email': 'customer@example.com', 'display_name': 'Customer'},
                              access=lambda *args: None, schema=lambda model: {'outgoing_email_to': {}})
    row = {'id': 42, 'partner_id': [7, 'Customer']}
    payload = {'options': {'body': 'Xin chào', 'recipient': 'customer'}}
    result = quotation_chatter.prepare(service, 'message', payload, row)
    assert result['preview']['recipients'] == [{'email': 'customer@example.com'}]
    assert payload['options']['recipient'] == 'customer@example.com'
    repeated = quotation_chatter.prepare(service, 'message', payload, row)
    assert repeated['preview']['recipients'] == result['preview']['recipients']


def test_note_posts_on_quotation_without_email():
    calls = []
    ctx = SimpleNamespace(call=lambda *args: calls.append(args) or 91)
    service = SimpleNamespace(ctx=ctx, readback=lambda *args: {'id': 42}, record=lambda *args: {'id': 91, 'model': 'sale.order', 'res_id': 42})
    quotation_chatter.execute(service, 'note', {'options': {'body': 'Nội bộ'}}, {'id': 42}, lambda *args: None)
    assert calls[0][0:2] == ('sale.order', 'message_post')
    assert calls[0][3]['subtype_xmlid'] == 'mail.mt_note'


def test_note_accepts_singleton_odoo_recordset_result():
    seen = []
    ctx = SimpleNamespace(call=lambda *args: [91])
    def record(model, record_id, fields):
        seen.append(record_id)
        assert record_id == 91
        return {'id': 91, 'model': 'sale.order', 'res_id': 42}
    service = SimpleNamespace(ctx=ctx, readback=lambda *args: {'id': 42}, record=record)
    result = quotation_chatter.execute(service, 'note', {'options': {'body': 'Nội bộ'}}, {'id': 42}, lambda *args: None)
    assert result['status'] == 'completed'
    assert seen == [91]


def test_message_records_queued_delivery_on_the_quotation():
    calls = []

    def call(model, method, args, kwargs=None):
        calls.append((model, method, args, kwargs))
        if model == 'sale.order':
            return 91
        return [{'state': 'outgoing'}]

    ctx = SimpleNamespace(call=call)
    service = SimpleNamespace(ctx=ctx, readback=lambda *args: {'id': 42},
                              record=lambda *args: {'id': 91, 'model': 'sale.order', 'res_id': 42,
                                                    'outgoing_email_to': 'customer@example.com'})
    response = quotation_chatter.execute(service, 'message',
        {'options': {'body': 'Xin chào', 'recipient': 'customer@example.com'}}, {'id': 42}, lambda *args: None)
    assert calls[0][3]['outgoing_email_to'] == 'customer@example.com'
    assert calls[0][3]['notify_skip_followers'] is True
    assert response['delivery_status'] == 'queued'


def test_meeting_uses_native_calendar_on_quotation():
    calls = []
    ctx = SimpleNamespace(user=SimpleNamespace(odoo_uid=3), call=lambda *args: calls.append(args) or 55)
    service = SimpleNamespace(ctx=ctx, readback=lambda *args: {'id': 42}, record=lambda *args: {'id': 55})
    payload = {'options': {'summary': 'Họp', 'start': '2026-11-12 02:00:00',
                           'stop': '2026-11-12 03:00:00', 'model_id': 12, 'user_id': 3}}
    service.record = lambda *args: {'id': 55, 'res_id': 42, 'activity_ids': [77]}
    quotation_chatter.execute(service, 'meeting', payload, {'id': 42}, lambda *args: None)
    assert calls[0][0:2] == ('calendar.event', 'create')
    assert calls[0][2][0]['res_model_id'] == 12
    assert calls[0][2][0]['res_id'] == 42


def test_meeting_requires_an_explicit_time():
    with pytest.raises(BusinessError):
        quotation_chatter._time('2026-12-12', 'Asia/Bangkok')
