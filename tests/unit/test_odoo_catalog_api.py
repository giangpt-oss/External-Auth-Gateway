from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import OdooCredential, User
from app.odoo.client import OdooAccessError, OdooNetworkError
from app.security import encrypt_secret


@pytest.fixture
def catalog_api_client(tmp_path):
    db_file = tmp_path / "test_catalog_api.db"
    key = Fernet.generate_key().decode()
    settings = Settings(
        DATABASE_URL=f"sqlite:///{db_file}",
        DEBUG=True,
        APP_ENV="development",
        GATEWAY_PUBLIC_URL="http://localhost:8080",
        ENCRYPTION_KEY=key,
        AGENT_TO_GATEWAY_SECRET="agent_secret_catalog_api_min_32_chars_12345",
        GATEWAY_TO_AGENT_SECRET="gateway_secret_catalog_api_min_32_chars_12345",
        ODOO_URL="http://mock-odoo:8069",
        ODOO_DB="test_db",
    )
    app = create_app(settings=settings)

    from app.db import init_db
    init_db(settings.DATABASE_URL)

    with app.state.session_factory() as db:
        user = User(
            id=1,
            tenant="default",
            database="odoo_db",
            odoo_uid=10,
            login="user@example.com",
            name="Test User",
            active=True,
        )
        db.add(user)
        enc_sec = encrypt_secret("my_odoo_password", key)
        cred = OdooCredential(
            user_id=1,
            credential_kind="password",
            encrypted_secret=enc_sec,
        )
        db.add(cred)
        db.commit()

    return TestClient(app), "agent_secret_catalog_api_min_32_chars_12345"


def test_get_odoo_catalog(catalog_api_client):
    client, secret = catalog_api_client
    resp = client.get(
        "/internal/v1/odoo/catalog",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "OK"
    catalog = data["catalog"]
    models = {item["model"] for item in catalog}
    assert "crm.lead" in models
    assert "res.partner" in models
    assert "product.product" in models
    assert "sale.order" in models
    assert "purchase.order" in models
    assert "stock.picking" in models
    assert "stock.quant" in models
    assert "account.move" in models
    assert "hr.employee" in models


def test_query_odoo_records_success(catalog_api_client):
    client, secret = catalog_api_client
    with patch("app.odoo.service.OdooService.validate_user_account", return_value={"uid": 10, "name": "Test User", "active": True}), \
         patch("app.odoo.client.OdooClient.execute") as mock_exec:
        mock_exec.return_value = [
            {"id": 101, "name": "Công ty ABC", "email": "abc@example.com"}
        ]

        resp = client.post(
            "/internal/v1/users/1/odoo/query",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "res.partner",
                "fields": ["name", "email"],
                "domain": [["name", "ilike", "ABC"]],
                "limit": 5,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "OK"
        assert data["model"] == "res.partner"
        assert data["count"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Công ty ABC"

        # Check call arguments
        mock_exec.assert_called_once()
        kwargs = mock_exec.call_args[1]
        assert kwargs["model"] == "res.partner"
        assert kwargs["method"] == "search_read"
        assert kwargs["args"] == [[("name", "ilike", "ABC")]]
        assert "name" in kwargs["kwargs"]["fields"]
        assert "email" in kwargs["kwargs"]["fields"]
        assert kwargs["kwargs"]["limit"] == 5


def test_query_odoo_disallowed_model_rejected(catalog_api_client):
    client, secret = catalog_api_client
    with patch("app.odoo.service.OdooService.validate_user_account", return_value={"uid": 10, "name": "Test User", "active": True}):
        resp = client.post(
            "/internal/v1/users/1/odoo/query",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "ir.config_parameter",
                "fields": ["key", "value"],
            },
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["detail"]["code"] == "INVALID_QUERY"
        assert "không nằm trong danh mục" in data["detail"]["message"]


def test_query_odoo_disallowed_field_rejected(catalog_api_client):
    client, secret = catalog_api_client
    with patch("app.odoo.service.OdooService.validate_user_account", return_value={"uid": 10, "name": "Test User", "active": True}):
        resp = client.post(
            "/internal/v1/users/1/odoo/query",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "res.partner",
                "fields": ["password_hash"],
            },
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["detail"]["code"] == "INVALID_QUERY"
        assert "password_hash" in data["detail"]["message"]


def test_query_odoo_disallowed_domain_operator(catalog_api_client):
    client, secret = catalog_api_client
    with patch("app.odoo.service.OdooService.validate_user_account", return_value={"uid": 10, "name": "Test User", "active": True}):
        resp = client.post(
            "/internal/v1/users/1/odoo/query",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "res.partner",
                "domain": [["name", "child_of", "abc"]],
            },
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["detail"]["code"] == "INVALID_QUERY"
        assert "Toán tử 'child_of' không được hỗ trợ" in data["detail"]["message"]


def test_query_odoo_permission_denied_from_odoo(catalog_api_client):
    client, secret = catalog_api_client
    with patch("app.odoo.service.OdooService.validate_user_account", return_value={"uid": 10, "name": "Test User", "active": True}), \
         patch("app.odoo.client.OdooClient.execute", side_effect=OdooAccessError("Access denied by Odoo ACL")):
        resp = client.post(
            "/internal/v1/users/1/odoo/query",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "hr.employee",
                "fields": ["name", "work_email"],
            },
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["detail"]["code"] == "PERMISSION_DENIED"


def test_query_odoo_network_error(catalog_api_client):
    client, secret = catalog_api_client
    with patch("app.odoo.service.OdooService.validate_user_account", return_value={"uid": 10, "name": "Test User", "active": True}), \
         patch("app.odoo.client.OdooClient.execute", side_effect=OdooNetworkError("Connection refused")):
        resp = client.post(
            "/internal/v1/users/1/odoo/query",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "sale.order",
            },
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data["detail"]["code"] == "ODOO_UNAVAILABLE"
