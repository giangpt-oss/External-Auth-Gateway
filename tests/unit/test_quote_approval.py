from copy import deepcopy

import pytest

from app.odoo.client import OdooAccessError, OdooError
from app.quotations.approval import APPROVAL_METHOD, QuotationApproval, approval_control


class Client:
    def __init__(self):
        self.calls = []
        self.visible = True
        self.rule = {'id': 14, 'method': APPROVAL_METHOD, 'action_id': False, 'can_validate': False, 'approver_ids': [24]}
        self.entries, self.requests = [], []
        self.activities = []

    def studio_approval_spec(self, uid, secret, record_id, method, company):
        self.calls.append(('spec', method))
        return {'all_rules': {'14': deepcopy(self.rule)} if self.rule else {},
                'sale.order': [[[record_id, method, False], {'rules': [14] if self.rule else [], 'entries': deepcopy(self.entries)}]]}

    def execute(self, uid, secret, model, method, args, kwargs):
        self.calls.append((model, method, args, kwargs))
        assert kwargs['context']['allowed_company_ids'] == [2]
        if model == 'sale.order':
            return [{'id': 85, 'name': 'S00086', 'state': 'draft'}] if self.visible else []
        if model == 'studio.approval.request':
            if method == 'create':
                assert args == [{'rule_id': 14, 'res_id': 85}]
                self.requests = [{'id': 1, 'rule_id': [14, 'Approval'], 'res_id': 85}]
                return 1
            return self.requests
        if model == 'mail.activity':
            if method == 'create':
                values = args[0]
                self.activities.append({'id': len(self.activities) + 1,
                    'user_id': [values['user_id'], 'Approver'],
                    'summary': values['summary']})
                return len(self.activities)
            return self.activities
        if model == 'ir.model':
            return [{'id': 8}]
        if model == 'ir.model.data':
            return [{'res_id': 4}]
        if method == 'check_approval':
            assert args == ['sale.order', 85, APPROVAL_METHOD, False]
            self.requests = [{'id': 1, 'rule_id': [14, 'Approval'], 'res_id': 85}]
        if method == 'set_approval':
            assert args == [[14]] and kwargs['res_id'] == 85
            self.entries = [{'rule_id': [14, 'Approval'], 'approved': kwargs['approved']}]


@pytest.fixture
def setup():
    client = Client()
    return client, QuotationApproval(client, 27, 'secret', 2)


def test_discovery_never_requests_approval(setup):
    client, service = setup
    assert service.inspect(85)['status'] == 'not_requested'
    assert all(call[0] == 'spec' or call[1] not in ('check_approval', 'set_approval', 'action_confirm') for call in client.calls)


def test_source_access_checked_before_studio(setup):
    client, service = setup
    client.visible = False
    with pytest.raises(OdooAccessError):
        service.inspect(85)
    assert len(client.calls) == 1


def test_missing_send_rule_is_configuration_error(setup):
    client, service = setup
    client.rule = None
    assert service.inspect(85)['status'] == 'configuration_required'
    with pytest.raises(OdooAccessError):
        service.request(85)


def test_request_creates_pending_without_confirming_order(setup):
    client, service = setup
    assert service.request(85)['status'] == 'pending'
    assert service.request(85)['status'] == 'pending'
    assert sum(call[1] == 'check_approval' for call in client.calls) == 1
    assert not any(call[0] == 'sale.order' and call[1] == 'action_confirm' for call in client.calls)


def test_request_cannot_implicitly_grant_own_approval(setup):
    client, service = setup
    client.rule['approver_ids'] = [27]
    client.rule['can_validate'] = True
    assert service.request(85)['status'] == 'pending'
    assert any(call[:2] == ('studio.approval.request', 'create') for call in client.calls)
    assert not any(call[1] == 'check_approval' for call in client.calls)
    assert not any(call[1] == 'set_approval' for call in client.calls)
    assert {row['user_id'][0] for row in client.activities} == {27}
    assert service.decide(85, 14, True)['status'] == 'approved'


def test_request_readback_failure_recovers_existing_request_without_duplicate(setup):
    client, service = setup
    client.rule['approver_ids'] = [27]
    client.rule['can_validate'] = True
    original = client.studio_approval_spec
    failed = False

    def transient(*args):
        nonlocal failed
        if client.requests and not failed:
            failed = True
            raise OdooError('readback unavailable')
        return original(*args)

    client.studio_approval_spec = transient
    with pytest.raises(OdooError, match='readback unavailable'):
        service.request(85)
    assert client.requests
    assert service.request(85)['status'] == 'pending'
    assert sum(call[:2] == ('studio.approval.request', 'create')
               for call in client.calls) == 1
    assert not any(call[0] != 'spec' and call[1] in ('set_approval', 'action_confirm')
                   for call in client.calls)


def test_request_not_reported_pending_if_odoo_did_not_persist_it(setup):
    client, service = setup
    client.rule['approver_ids'] = [27]
    client.rule['can_validate'] = True
    original = client.execute

    def no_persist(uid, secret, model, method, args, kwargs):
        if (model, method) == ('studio.approval.request', 'create'):
            client.calls.append((model, method, args, kwargs))
            return 1
        return original(uid, secret, model, method, args, kwargs)

    client.execute = no_persist
    with pytest.raises(OdooError, match='chưa xác nhận'):
        service.request(85)
    assert not client.requests and not client.activities


def test_unauthorized_approver_cannot_grant(setup):
    client, service = setup
    service.request(85)
    with pytest.raises(OdooAccessError):
        service.decide(85, 14, True)
    assert not any(call[1] == 'set_approval' for call in client.calls)


@pytest.mark.parametrize('approved,expected', [(True, 'approved'), (False, 'rejected')])
def test_native_approver_decision_is_separate_from_sale_confirmation(setup, approved, expected):
    client, service = setup
    service.request(85)
    client.rule['can_validate'] = True
    assert service.decide(85, 14, approved)['status'] == expected
    with pytest.raises(OdooAccessError):
        service.decide(85, 14, approved)


def test_send_blocked_until_all_rules_approved(setup):
    client, service = setup
    with pytest.raises(OdooAccessError):
        service.require_approved(85)
    client.entries = [{'rule_id': [14, 'Approval'], 'approved': True}]
    assert service.require_approved(85)['status'] == 'approved'


@pytest.mark.parametrize('name', ['x_studio_a_phe_duyet', 'x_studio_approved', 'x_grant_approval'])
def test_approval_booleans_are_control_fields(name):
    assert approval_control(name)


def test_flow_flag_only_enables_quotation_lifecycle():
    from types import SimpleNamespace

    from app.business.catalog import ACTIONS
    from app.business.service import enabled
    settings = SimpleNamespace(ENABLE_BUSINESS_ACTIONS=False, ENABLE_QUOTATION_APPROVAL_FLOW=True)
    assert 'business.sales.request_approval' not in ACTIONS
    assert enabled(settings, ACTIONS['business.sales.send'])
    assert not enabled(settings, ACTIONS['business.sales.delete'])
    assert not enabled(settings, ACTIONS['business.purchases.confirm'])


def test_legacy_request_approval_cannot_prepare_or_execute_without_pdf_button():
    from types import SimpleNamespace

    from app.business.quotation_approval import execute, prepare
    from app.business.service import Unsupported

    definition = SimpleNamespace(name='business.sales.request_approval')
    with pytest.raises(Unsupported, match='PDF'):
        prepare(None, definition, {}, None)
    with pytest.raises(Unsupported, match='PDF'):
        execute(None, definition, {}, None, 'old-operation', lambda *args: None)


def test_quote_cannot_set_approval_checkbox():
    from app.quotations.service import QuotationService, QuotationValidationError
    service = QuotationService(None, 27, 'fixture')
    service._schemas['sale.order'] = {'x_studio_a_phe_duyet': {'type': 'boolean', 'readonly': False}}
    with pytest.raises(QuotationValidationError, match='phê duyệt'):
        service._custom('sale.order', {'x_studio_a_phe_duyet': True}, 2)
    assert 'x_studio_a_phe_duyet' not in service._editable('sale.order')


def test_commercial_digest_tracks_lines_and_ignores_status_and_chatter():
    from types import SimpleNamespace

    from app.business.quotation_approval import commercial_snapshot
    order = {'id': 85, 'partner_id': [1, 'Customer'], 'state': 'draft', 'write_date': 'old', 'order_line': [9]}
    line = {'id': 9, 'price_unit': 10, 'tax_ids': [], 'name': 'Product'}
    service = SimpleNamespace(schema=lambda model: {}, record=lambda *args: None,
        track_children=lambda *args: None, snapshot={
            1: {'model': 'sale.order', 'id': 85, 'values': order},
            2: {'model': 'sale.order.line', 'id': 9, 'values': line}})
    digest = commercial_snapshot(service, order)
    order.update(state='sent', write_date='new')
    assert commercial_snapshot(service, order) == digest
    line['tax_ids'] = [7]
    assert commercial_snapshot(service, order) != digest
    line['tax_ids'] = []
    line['name'] = 'Different description'
    assert commercial_snapshot(service, order) != digest


def test_review_scope_shared_by_approvers_but_isolated_by_tenant_company():
    from types import SimpleNamespace

    from app.business.quotation_approval import review_key
    user = SimpleNamespace(id=1, tenant='tenant-a', database='db')
    service = SimpleNamespace(ctx=SimpleNamespace(user=user), company_id=2)
    key = review_key(service, {'id': 85})
    user.id = 99
    assert review_key(service, {'id': 85}) == key
    service.company_id = 3
    assert review_key(service, {'id': 85}) != key
