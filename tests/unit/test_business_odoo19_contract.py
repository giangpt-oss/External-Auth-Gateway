"""Odoo 19 field contracts at the RPC boundary; no provider writes."""
from types import SimpleNamespace

import pytest

from app.business.catalog import ACTIONS
from app.business.service import BusinessService, Conflict


class Fixture(BusinessService):
    def __init__(self):
        super().__init__(SimpleNamespace(default_company_id=1))

    def access(self, *args):
        pass

    def schema(self, model):
        return {name: {'required': name == 'customer_lead'} for name in ('tax_ids', 'product_uom_id', 'customer_lead', 'date_planned')}

    def record(self, model, record_id, fields=None, track=True):
        return {'id': record_id, 'display_name': 'Demo', 'uom_id': [3, 'Unit'], 'active': True}

    def values(self, model, incoming, allowed, creating=False):
        return incoming, []


@pytest.mark.parametrize('module', ['sales', 'purchases'])
def test_odoo19_lines_use_current_tax_field_without_mandatory_lead(module):
    service = Fixture()
    line = {'product_id': 2, 'quantity': 1, 'price_unit': 100, 'price_confirmed': True, 'tax_ids': []}
    if module == 'purchases':
        line['date_planned'] = '2026-11-04 00:00:00'
    commands, _ = service.order_lines(ACTIONS[f'business.{module}.create'], [line])
    values = commands[0][2]
    assert values['tax_ids'] == [(6, 0, [])]
    assert 'tax_id' not in values and 'taxes_id' not in values
    assert 'customer_lead' not in values


def test_snapshot_tracks_tax_changes_even_without_write_date():
    current = {'id': 9, 'product_id': [2, 'Demo'], 'tax_ids': [4]}
    class Context:
        default_company_id = 1
        def read_fields(self, model):
            if model == 'sale.order':
                return {'id': {}, 'order_line': {}}
            return {k: {} for k in current}
        def call(self, model, method, args=None, kwargs=None):
            if method == 'check_access_rights':
                return True
            assert method == 'search_read'
            if model == 'sale.order':
                return [{'id': 1, 'order_line': [9]}]
            return [{k: v for k, v in current.items() if k in kwargs['fields']}]
    service = BusinessService(Context())
    service.track_children('sale.order', {'id': 1, 'order_line': [9]})
    snapshot = list(service.snapshot.values())
    assert any(item['values'].get('tax_ids') == [4] for item in snapshot)
    current['tax_ids'] = [5]
    with pytest.raises(Conflict):
        service.verify_snapshot(snapshot)
