"""Optional quotation fields never infer IDs or write in these tests."""
from unittest.mock import Mock

import pytest

from app.odoo.client import OdooAccessError
from app.quotations.optional import OPTIONAL_FIELDS
from app.quotations.service import QuotationService, QuotationValidationError


def fixture_service(monkeypatch):
    service = QuotationService(None, 29, 'fixture')
    monkeypatch.setattr(service, '_companies', lambda company_id=None: (3, [3]))
    def schema(model):
        if model == 'sale.order':
            return {field: {'type': kind, 'relation': relation, 'readonly': False,
                    'selection': [('3 tháng', '3 tháng'), ('12 tháng', '12 tháng')]
                    if field == 'x_studio_thoi_gian_bao_tri' else
                    [('direct', 'Càng sớm càng tốt'), ('one', 'Khi tất cả sản phẩm đã sẵn sàng')]
                    if field == 'picking_policy' else []}
                    for field, (_, kind, relation) in OPTIONAL_FIELDS.items()}
        return {'id': {}, 'display_name': {}, 'name': {}, 'company_id': {}, 'active': {}}
    monkeypatch.setattr(service, '_schema', schema)
    monkeypatch.setattr(service, '_rpc', Mock(side_effect=lambda model, method, args, *a, **kw:
        True if method == 'check_access_rights' else [{'id': 8, 'display_name': 'Được phép'}]))
    return service


def test_all_nineteen_fields_match_live_verified_technical_types(monkeypatch):
    service = fixture_service(monkeypatch)
    assert len(OPTIONAL_FIELDS) == 19
    for field, (label, kind, relation) in OPTIONAL_FIELDS.items():
        spec = service.optional_spec(field, 3)
        assert spec['available'] is True
        assert (spec['label'], spec['type'], spec['relation']) == (label, kind, relation)


def test_boolean_false_selection_datetime_and_missing_field(monkeypatch):
    service = fixture_service(monkeypatch)
    result = service.optional_values({'require_signature': False, 'require_payment': True,
        'x_studio_thoi_gian_bao_tri': '12 tháng', 'picking_policy': 'one',
        'commitment_date': '2026-12-20 07:30:00'}, 3)
    assert result['require_signature'] is False and result['require_payment'] is True
    assert result['commitment_date'] == '2026-12-20 07:30:00'
    with pytest.raises(QuotationValidationError):
        service.optional_values({'x_studio_thoi_gian_bao_tri': '24 tháng'}, 3)
    with pytest.raises(QuotationValidationError):
        service.optional_values({'require_signature': None}, 3)
    service._schema = lambda model: {} if model == 'sale.order' else {'id': {}}
    assert service.optional_spec('require_signature', 3)['available'] is False
    with pytest.raises(QuotationValidationError):
        service.optional_values({'require_signature': True}, 3)


def test_relation_id_must_be_visible_in_selected_company(monkeypatch):
    service = fixture_service(monkeypatch)
    calls = []
    def rpc(model, method, args, *a, **kw):
        if method == 'check_access_rights':
            return True
        calls.append((model, args[0]))
        return [] if args[0][-1] == ('id', '=', 99) else [{'id': 8, 'display_name': 'Được phép'}]
    service._rpc = rpc
    assert service.optional_values({'team_id': 8, 'tag_ids': [8]}, 3) == {
        'team_id': 8, 'tag_ids': [(6, 0, [8])]}
    assert ('company_id', '=', 3) in calls[0][1]
    with pytest.raises(OdooAccessError):
        service.optional_values({'team_id': 99}, 3)
    with pytest.raises(QuotationValidationError):
        service.optional_values({'tag_ids': [8, 8]}, 3)
    with pytest.raises(QuotationValidationError):
        service.optional_values({'incoterm_location': 'Hà Nội'}, 3)


def test_optional_catalog_uses_sale_company_and_never_guesses_id(monkeypatch):
    service = fixture_service(monkeypatch)
    observed = []
    def rpc(model, method, args, *a, **kw):
        if method == 'check_access_rights':
            return True
        observed.append((model, args[0], kw.get('context')))
        return [{'id': 52, 'name': 'Miền Nam', 'display_name': 'Miền Nam'}]
    service._rpc = rpc
    result = service.optional_candidates('team_id', 'Miền Nam', 3)
    assert result['items'][0]['id'] == 52 and result['exact_match_unique'] is True
    assert ('company_id', '=', 3) in observed[0][1]
    assert observed[0][2]['lang'] == 'vi_VN'
