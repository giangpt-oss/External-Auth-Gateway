import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import ActionOperation, Base
from app.quotations import execution


@pytest.mark.parametrize('delivery_case', ['sent', 'ambiguous', 'scope_changed'])
def test_restart_delivers_saved_result_without_recreating_quote(monkeypatch, delivery_case):
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    scope = {'telegram_user_id': 7, 'chat_id': 7, 'telegram_binding_id': 'fixture'}
    with sf() as db:
        db.add(execution.QuoteExecutionJob(operation_id='op_fixture', user_id=1, tenant='https://odoo.invalid', database='fixture',
            scope_json=json.dumps(scope), payload_json='{}', status='completed', notified='pending'))
        db.add(ActionOperation(operation_id='op_fixture', user_id=1, action='sales.quotation.create', payload_hash='fixture', status='completed',
            result_json=json.dumps({'status': 'completed', 'result': {'id': 83, 'name': 'S00084', 'state': 'draft'}})))
        db.commit()
    auth = Mock(side_effect=lambda *args: nullcontext(SimpleNamespace(user=SimpleNamespace(tenant='https://other.invalid' if delivery_case == 'scope_changed' else 'https://odoo.invalid', database='fixture'))))
    monkeypatch.setattr(execution, 'authenticated_context', auth)
    create = Mock(side_effect=AssertionError('Must never replay a saved write'))
    monkeypatch.setattr(execution.QuotationService, 'create', create)
    send = Mock(side_effect=RuntimeError('ambiguous network send') if delivery_case == 'ambiguous' else None)
    monkeypatch.setattr('app.telegram.client.TelegramClient', lambda token: SimpleNamespace(send_message=send))
    worker = execution.QuoteExecutionWorker(SimpleNamespace(TELEGRAM_BOT_TOKEN='fixture'), sf)
    worker.tick()
    worker.tick()
    assert send.call_count == (0 if delivery_case == 'scope_changed' else 1)
    if send.call_count:
        assert 'S00084' in send.call_args.args[1]
        assert send.call_args.args[0] == 7
    create.assert_not_called()
    with sf() as db:
        row = db.query(execution.QuoteExecutionJob).one()
        assert row.status == 'completed'
        assert row.notified == {'sent': 'delivered', 'ambiguous': 'unknown', 'scope_changed': 'blocked'}[delivery_case]
