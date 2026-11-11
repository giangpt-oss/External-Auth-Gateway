from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.quotations.tracking import begin_tracking, reconcile_tracked, record_created


def test_unknown_dispatch_never_uses_business_fields_or_retries_create():
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    begin_tracking(sf, 1, 'op_fixture', 'https://odoo.invalid', 'fixture')
    service = SimpleNamespace(reconcile=Mock(), _readback=Mock(), _companies=lambda company=None: (1, [1]))
    result = reconcile_tracked(sf, 1, 'op_fixture', 'https://odoo.invalid', 'fixture', service)
    assert result['status'] == 'unknown_result'
    service.reconcile.assert_not_called()
    record_created(sf, 1, 'op_fixture', 84, 1)
    service._readback.return_value = {'id': 84, 'name': 'S00085', 'state': 'draft'}
    result = reconcile_tracked(sf, 1, 'op_fixture', 'https://odoo.invalid', 'fixture', service)
    assert result['result']['name'] == 'S00085'
    service._readback.assert_called_once_with(84, 1)


def test_named_status_reads_exact_visible_number_and_current_state():
    from app.quotations.service import QuotationService
    service = QuotationService(None, 1, 'fixture')
    service._companies = Mock(return_value=(2, [2]))
    service._rpc = Mock(return_value=[{'id': 84, 'name': 'S00086', 'state': 'draft'}])
    service._readback = Mock(side_effect=AssertionError('Status must stay a lightweight read'))
    assert service.status_by_name('S00086')['result']['state'] == 'draft'
    service._rpc.return_value = [{'id': 84, 'name': 'S00086', 'state': 'sent'}]
    assert service.status_by_name('S00086')['result']['state'] == 'sent'
    assert service._rpc.call_args.args[2] == [[('name', '=', 'S00086'), ('company_id', '=', 2)]]
    service._rpc.return_value = []
    assert service.status_by_name('S00086')['status'] == 'not_found'
    service._rpc.return_value = [{'id': 84}, {'id': 85}]
    assert service.status_by_name('S00086')['status'] == 'needs_selection'
