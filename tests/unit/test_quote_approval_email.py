"""No provider calls: verify approval and exact-recipient fences before email dispatch."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.business import quotation_approval
from app.business.adapters import execute
from app.business.catalog import ACTIONS
from app.business.service import Unsupported


class Context:
    def __init__(self, altered=False):
        self.calls, self.values = [], {}
        self.altered = altered

    def call(self, model, method, args, kwargs=None):
        self.calls.append((model, method, args, kwargs or {}))
        if method == 'action_quotation_send':
            return {'res_model': 'mail.compose.message'}
        if method == 'create':
            self.values = deepcopy(args[0])
            return 12
        if method == 'read':
            row = deepcopy(self.values)
            row['partner_ids'] = [55, 99] if self.altered else [55]
            row['attachment_ids'] = []
            return [row]


@pytest.mark.parametrize('altered', [False, True])
def test_send_uses_native_action_and_only_reviewed_recipients(monkeypatch, altered):
    ctx = Context(altered)
    service = SimpleNamespace(ctx=ctx, readback=lambda *args: {'id': 85, 'name': 'S00086', 'state': 'sent'})
    checks = []
    monkeypatch.setattr(quotation_approval, 'native', lambda service: SimpleNamespace(
        require_approved=lambda record_id: checks.append(('approved', record_id))))
    monkeypatch.setattr(quotation_approval, 'require_review', lambda service, row: checks.append(('reviewed', row['id'])))
    payload = {'options': {'recipient_ids': [55], 'subject': 'Quotation S00086', 'body': 'Please review.', 'attachment_ids': []}}
    if altered:
        with pytest.raises(Unsupported):
            execute(service, ACTIONS['business.sales.send'], payload, {'id': 85}, 'operation', lambda *args: None)
        assert not any(method == 'action_send_mail' for _, method, _, _ in ctx.calls)
    else:
        result = execute(service, ACTIONS['business.sales.send'], payload, {'id': 85}, 'operation', lambda *args: None)
        assert result['delivery_status'] == 'submitted'
        assert checks == [('approved', 85), ('reviewed', 85)]
        send = next(call for call in ctx.calls if call[1] == 'action_send_mail')
        assert send[3]['context']['mark_so_as_sent'] is True
        assert ctx.values['notify_skip_followers'] is True
        assert ctx.values['force_send'] is False
        assert not any(method == 'action_confirm' for _, method, _, _ in ctx.calls)
