import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.business import purchase_approval
from app.business.models import BusinessPreparation
from app.business.service import Conflict
from app.config import Settings
from app.db import init_db
from app.main import create_app
from app.odoo.client import OdooAuthError
from app.reporting.schemas import TelegramScope
from app.telegram.client import TelegramClient, TelegramDeliveryUnknownError
from app.telegram.worker import TelegramAIWorker


def test_login_failures_accumulate_across_http_requests(temp_db_path, fernet_key):
    app = create_app(Settings(APP_ENV="test", DATABASE_URL=f"sqlite:///{temp_db_path}",
                              ENCRYPTION_KEY=fernet_key))
    with TestClient(app) as client, patch(
        "app.odoo.client.OdooClient.authenticate_password", side_effect=OdooAuthError("denied"),
    ) as authenticate:
        statuses = [client.post("/auth/login", data={"login": "test@example.com", "secret": "wrong"}).status_code
                    for _ in range(6)]
    assert statuses == [401] * 5 + [429]
    assert authenticate.call_count == 5


@pytest.mark.parametrize("origin", ["https://other.example", "http://["])
def test_cross_origin_browser_write_is_rejected(temp_db_path, fernet_key, origin):
    app = create_app(Settings(APP_ENV="test", DATABASE_URL=f"sqlite:///{temp_db_path}",
                              ENCRYPTION_KEY=fernet_key))
    with TestClient(app) as client:
        response = client.post("/auth/login", data={"login": "user", "secret": "password"},
                               headers={"Origin": origin})
    assert response.status_code == 403


@pytest.mark.parametrize("profile", [{"active": False, "share": False}, {"active": True, "share": True}])
def test_disallowed_odoo_account_returns_forbidden(temp_db_path, fernet_key, profile):
    app = create_app(Settings(APP_ENV="test", DATABASE_URL=f"sqlite:///{temp_db_path}",
                              ENCRYPTION_KEY=fernet_key))
    with TestClient(app) as client, patch(
        "app.odoo.client.OdooClient.authenticate_password", return_value=77,
    ), patch("app.odoo.client.OdooClient.execute", return_value=[{"id": 77, **profile}]):
        response = client.post("/auth/login", data={"login": "test@example.com", "secret": "password"})
    assert response.status_code == 403


@pytest.mark.parametrize("payload", [[], {"ok": True}, {"ok": True, "result": {}}])
def test_unconfirmed_telegram_send_is_unknown(payload):
    response = httpx.Response(200, json=payload)
    with patch("app.telegram.client.httpx.post", return_value=response):
        with pytest.raises(TelegramDeliveryUnknownError):
            TelegramClient("test-token").send_message(123, "hello")


def test_option_buttons_are_delivered_as_messages():
    telegram = Mock()
    worker = TelegramAIWorker(Settings(APP_ENV="test"), Mock(), telegram)
    worker._verify_recipient = Mock(return_value=(True, None))
    worker._save_delivery_chunks = Mock(return_value=True)
    worker._set_delivery_status = Mock(return_value=True)
    worker._update_chunk_progress = Mock(return_value=True)
    worker._mark_delivered = Mock()
    worker._mark_delivery_failed = Mock()
    result = worker._deliver_messages(
        1, 2, 123, 123,
        json.dumps({"messages": ["Choose"], "option_buttons": {"0": ["A", "B"]}}),
        None, 0, "claim", 1,
    )
    assert result is True
    telegram.send_message.assert_called_once_with(123, "Choose", option_buttons=["A", "B"])
    telegram.send_document.assert_not_called()
    worker._mark_delivery_failed.assert_not_called()
    worker._executor.shutdown(wait=True)


@pytest.mark.parametrize("changed", [False, True])
def test_purchase_review_uses_snapshot_and_durable_operation(temp_db_path, monkeypatch, changed):
    engine, sf = init_db(f"sqlite:///{temp_db_path}")
    scope = TelegramScope(telegram_user_id=1, chat_id=2, telegram_binding_id="binding")
    row = SimpleNamespace(token="a" * 24, user_id=7, company_id=1,
                          order_id=9, tenant="tenant", database="database",
                          telegram_binding_id="binding", chat_id=2)
    ctx = Mock()
    ctx.user.tenant, ctx.user.database = "tenant", "database"
    ctx.selected_companies.return_value = [1]
    settings = Settings(APP_ENV="test", ENABLE_BUSINESS_ACTIONS=True, ENABLE_BUSINESS_PURCHASES=True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=settings, session_factory=sf,
    )))
    with sf() as db:
        db.add(BusinessPreparation(
            id="pr_" + row.token, user_id=7, tenant="tenant", database="database",
            binding_id="binding", chat_id="2", action="purchase.review",
            data_json="[]", created_at=0, expires_at=9999999999,
        ))
        db.commit()
    service = Mock()
    service.prepare.return_value = {"status": "ready", "payload": {"target_id": 9},
                                    "snapshot": [], "_values": {}}
    if changed:
        service.verify_snapshot.side_effect = Conflict("changed")
    monkeypatch.setattr(purchase_approval, "BusinessService", Mock(return_value=service))
    execute = Mock(return_value={"status": "completed", "result": {"status": "completed"}})
    monkeypatch.setattr("app.business.routes.execute_business_action", execute)
    try:
        if changed:
            with pytest.raises(Conflict):
                purchase_approval._execute_review(request, ctx, row, scope, "business.purchases.confirm")
            execute.assert_not_called()
        else:
            for _ in range(2):
                purchase_approval._execute_review(request, ctx, row, scope, "business.purchases.confirm")
            operations = [call.args[1].operation_id for call in execute.call_args_list]
            assert operations == ["purchase_" + row.token] * 2
            assert service.prepare.call_count == 1
    finally:
        engine.dispose()


def test_purchase_review_honors_disabled_feature(monkeypatch):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=Settings(APP_ENV="test", ENABLE_BUSINESS_ACTIONS=False),
    )))
    execute = Mock()
    monkeypatch.setattr("app.business.routes.execute_business_action", execute)
    with pytest.raises(HTTPException) as error:
        purchase_approval._execute_review(request, Mock(), Mock(), Mock(), "business.purchases.confirm")
    assert error.value.status_code == 409
    execute.assert_not_called()
