"""Regression coverage at the quotation RPC boundary; never contact live Odoo."""
import copy
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.db import create_sqlite_engine
from app.models import Base
from app.quotations import jobs
from app.quotations.service import QuotationService, QuotationValidationError
from app.reporting.schemas import TelegramScope


class QuotationFixture(QuotationService):
    def __init__(self, *args):
        super().__init__(None, 1, 'fixture')
        self.calls = []

    def _companies(self, company_id=None):
        return 1, [1]

    def _check_create(self):
        pass

    def _schema(self, model):
        fields = {
            'sale.order': {'company_id': 'many2one', 'partner_id': 'many2one', 'currency_id': 'many2one', 'origin': 'char'},
            'sale.order.line': {'product_id': 'many2one', 'name': 'char', 'product_uom_qty': 'float', 'product_uom_id': 'many2one', 'price_unit': 'float', 'tax_id': 'many2many', 'customer_lead': 'float'},
        }[model]
        relations = {'product_id': 'product.product', 'company_id': 'res.company', 'partner_id': 'res.partner', 'currency_id': 'res.currency', 'product_uom_id': 'uom.uom'}
        return {name: {'type': kind, 'required': name == 'customer_lead', 'readonly': False, 'string': name, 'relation': relations.get(name)} for name, kind in fields.items()}

    def _rpc(self, model, method, args, company_id=None, **kwargs):
        self.calls.append((model, method, copy.deepcopy(args)))
        if method == 'default_get':
            return {}
        if method == 'address_get':
            return {'invoice': 1, 'delivery': 1}
        if method == 'create' and model == 'sale.order':
            return 99
        raise AssertionError((model, method))

    def _record(self, model, record_id, company_id, fields=None):
        return {'res.partner': {'id': record_id, 'display_name': 'Customer'},
                'res.company': {'id': record_id, 'currency_id': [1, 'VND']},
                'product.product': {'id': record_id, 'display_name': 'Product', 'uom_id': [3, 'Unit'], 'sale_ok': True, 'list_price': 100},
                'uom.uom': {'id': record_id, 'category_id': [1, 'Unit']}}.get(model, {'id': record_id})

    def _readback(self, order_id, company_id):
        values = next(args[0] for model, method, args in self.calls if method == 'create')
        return {'id': order_id, 'state': 'draft', 'partner_id': [values['partner_id'], 'Customer'], 'company_id': [company_id, 'Company'],
                'lines': [dict(values['order_line'][0][2], product_id=[2, 'Product'], product_uom_id=[3, 'Unit'], tax_id=[])]}


@pytest.fixture
def payload():
    return {'partner_id': 1, 'lines': [{'product_id': 2, 'quantity': 1, 'price_unit': 100, 'price_confirmed': True, 'tax_ids': [], 'customer_lead': 10}]}


@pytest.mark.parametrize('days', [0, 1, 10, 2.5])
def test_delivery_duration_is_supported_and_frozen(payload, days):
    service = QuotationFixture()
    payload['lines'][0]['customer_lead'] = days
    result = service.prepare(payload)
    assert result['status'] == 'ready'
    assert result['values']['order_line'][0][2]['customer_lead'] == days
    assert result['payload']['lines'][0]['customer_lead'] == days
    assert result['preview']['lines'][0]['customer_lead'] == days
    assert not result['metadata']['models']['sale.order.line']['unsupported_required_fields']
    assert all(method in {'default_get', 'address_get'} for _, method, _ in service.calls)


@pytest.mark.parametrize('days', ['2026-11-03', '10 ngày', -1, True, float('nan'), float('inf')])
def test_calendar_or_invalid_duration_cannot_reach_create(payload, days):
    service = QuotationFixture()
    payload['lines'][0]['customer_lead'] = days
    with pytest.raises(QuotationValidationError):
        service.prepare(payload)
    assert not any(method == 'create' for _, method, _ in service.calls)


def test_computed_duration_is_not_a_required_user_input(payload):
    payload['lines'][0].pop('customer_lead')
    result = QuotationFixture().prepare(payload)
    assert result['status'] == 'ready'
    assert 'customer_lead' not in result['values']['order_line'][0][2]


def test_explicit_catalog_price_is_resolved_and_frozen(payload):
    payload['lines'][0].pop('price_unit')
    payload['lines'][0].pop('customer_lead')
    payload['lines'][0]['use_catalog_price'] = True
    result = QuotationFixture().prepare(payload)
    assert result['status'] == 'ready'
    assert result['payload']['lines'][0]['price_unit'] == 100
    assert result['payload']['lines'][0]['price_confirmed'] is True
    assert 'use_catalog_price' not in result['payload']['lines'][0]


def test_catalog_price_does_not_silently_convert_units(payload):
    payload['lines'][0]['use_catalog_price'] = True
    payload['lines'][0]['uom_id'] = 4
    with pytest.raises(QuotationValidationError, match='đơn vị'):
        QuotationFixture().prepare(payload)


def test_create_serializes_and_checks_duration(payload):
    service = QuotationFixture()
    result = service.create('op_fixture_123', payload)
    creates = [args for _, method, args in service.calls if method == 'create']
    assert len(creates) == 1
    assert creates[0][0]['order_line'][0][2]['customer_lead'] == 10
    assert result['verification'] == 'matched'


def test_odoo19_uses_tax_ids_in_create_and_readback(payload):
    class Odoo19(QuotationFixture):
        def _schema(self, model):
            schema = super()._schema(model)
            if model == 'sale.order.line':
                schema['tax_ids'] = schema.pop('tax_id')
            return schema

        def _readback(self, order_id, company_id):
            result = super()._readback(order_id, company_id)
            result['lines'][0].pop('tax_id', None)
            result['lines'][0]['tax_ids'] = []
            return result
    service = Odoo19()
    result = service.create('op_fixture_odoo19', payload)
    values = next(args[0] for _, method, args in service.calls if method == 'create')
    assert values['order_line'][0][2]['tax_ids'] == [[6, 0, []]]
    assert 'tax_id' not in values['order_line'][0][2]
    assert result['verification'] == 'matched'


def test_internal_operation_never_populates_business_origin(payload):
    service = QuotationFixture()
    service.create('op_internal_only', payload)
    values = next(args[0] for _, method, args in service.calls if method == 'create')
    assert not values.get('origin')


def test_record_identity_is_saved_before_readback_can_fail(payload):
    from app.odoo.client import OdooNetworkError

    class BrokenReadback(QuotationFixture):
        def _readback(self, order_id, company_id):
            raise OdooNetworkError('read timeout')

    saved = []
    service = BrokenReadback()
    with pytest.raises(OdooNetworkError):
        service.create('op_callback', payload, on_created=lambda order_id, company_id: saved.append((order_id, company_id)))
    assert saved == [(99, 1)]
    assert len([call for call in service.calls if call[1] == 'create']) == 1


def test_optional_fields_and_custom_defaults_are_not_silently_filled(payload):
    class Defaults(QuotationFixture):
        def _schema(self, model):
            schema = super()._schema(model)
            if model == 'sale.order':
                for name in ('note', 'validity_date', 'client_order_ref', 'x_comment'):
                    schema[name] = {'type': 'date' if name == 'validity_date' else 'char', 'string': name, 'required': False, 'readonly': False}
                schema['payment_term_id'] = {'type': 'many2one', 'relation': 'account.payment.term', 'string': 'Payment', 'required': False}
            return schema

        def _rpc(self, model, method, args, company_id=None, **kwargs):
            if method == 'default_get' and model == 'sale.order':
                return {'payment_term_id': 55, 'note': 'Unrequested terms', 'x_comment': 'Unrequested custom value'}
            return super()._rpc(model, method, args, company_id, **kwargs)

    prepared = Defaults().prepare(payload)
    for name in ('payment_term_id', 'note', 'validity_date', 'client_order_ref', 'x_comment'):
        assert not prepared['values'].get(name)
    fields = {row['name']: row for row in prepared['preview']['fields']}
    assert fields['payment_term_id']['value'] is None
    assert fields['validity_date']['value'] is None


def test_background_validation_is_readonly_and_scoped(payload, monkeypatch):
    engine = create_sqlite_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    settings = SimpleNamespace(TELEGRAM_BOT_TOKEN='')
    scope = TelegramScope(telegram_user_id=7, chat_id=7, telegram_binding_id='fixture-binding')
    ctx = SimpleNamespace(user=SimpleNamespace(tenant='https://odoo.invalid', database='fixture', odoo_uid=1), service=SimpleNamespace(client=None), secret='fixture')

    @contextmanager
    def authenticated(*args):
        yield ctx

    monkeypatch.setattr(jobs, 'authenticated_context', authenticated)
    monkeypatch.setattr(jobs, 'QuotationService', QuotationFixture)
    try:
        with sf() as db:
            db.add(jobs.QuotePreparationJob(id='qprep_fixture', user_id=1, tenant=ctx.user.tenant, database=ctx.user.database,
                scope_json=scope.model_dump_json(), payload_json=json.dumps(payload), created_at=time.time()))
            db.commit()
        worker = jobs.QuotePreparationWorker(settings, sf)
        worker.tick()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=sf)))
        result = jobs.load_result(request, ctx, 1, scope, 'qprep_fixture')
        assert result['status'] == 'completed'
        assert result['result']['payload']['lines'][0]['customer_lead'] == 10
        bad_scope = scope.model_copy(update={'telegram_binding_id': 'another-binding'})
        with pytest.raises(HTTPException) as error:
            jobs.load_result(request, ctx, 1, bad_scope, 'qprep_fixture')
        assert error.value.status_code == 403
    finally:
        engine.dispose()


@pytest.mark.parametrize('fails', [False, True])
def test_execution_outbox_persists_result_and_never_replays(payload, monkeypatch, fails):
    from app.models import ActionOperation, User
    from app.quotations import execution
    engine = create_sqlite_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    ctx = SimpleNamespace(user=SimpleNamespace(tenant='https://odoo.invalid', database='fixture', odoo_uid=1), service=SimpleNamespace(client=None), secret='fixture')
    calls = []
    class Fixture(QuotationFixture):
        def create(self, op_id, data, on_created=None):
            calls.append(op_id)
            if fails:
                raise QuotationValidationError('Invalid field: tax_id')
            if on_created:
                on_created(99, 1)
            return {'id': 99, 'name': 'S0099', 'state': 'draft', 'verification': 'matched'}
    @contextmanager
    def auth(*args):
        yield ctx
    monkeypatch.setattr(execution, 'authenticated_context', auth)
    monkeypatch.setattr(execution, 'QuotationService', Fixture)
    scope = TelegramScope(telegram_user_id=7, chat_id=7, telegram_binding_id='fixture-binding')
    with sf() as db:
        db.add(User(id=1, tenant='https://odoo.invalid', database='fixture', odoo_uid=1, login='fixture', name='Fixture'))
        db.flush()
        db.add(ActionOperation(user_id=1, operation_id='op_fixture_worker', action='sales.quotation.create', payload_hash='fixture', status='in_progress'))
        db.add(execution.QuoteExecutionJob(user_id=1, operation_id='op_fixture_worker', tenant=ctx.user.tenant, database='fixture', scope_json=scope.model_dump_json(), payload_json=json.dumps(payload)))
        db.commit()
    worker = execution.QuoteExecutionWorker(SimpleNamespace(TELEGRAM_BOT_TOKEN=''), sf)
    worker.tick()
    worker.tick()
    assert calls == ['op_fixture_worker']
    with sf() as db:
        op = db.query(ActionOperation).one()
        assert op.status == ('failed' if fails else 'completed')
        if not fails:
            assert json.loads(op.result_json)['result']['name'] == 'S0099'
    engine.dispose()
