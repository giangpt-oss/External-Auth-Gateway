from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from app.odoo.client import (
    OdooAccessError,
    OdooAuthError,
    OdooClient,
    OdooNetworkError,
)


@respx.mock
def test_authenticate_password_uses_odoo_web_login():
    client = OdooClient(url="https://test.odoo.com", db="test_db")
    route = respx.post(
        "https://test.odoo.com/web/session/authenticate",
        json={
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"db": "test_db", "login": "user@example.com", "password": "password"},
        },
    ).respond(json={"result": {"uid": 42}})

    assert client.authenticate_password("user@example.com", "password") == 42
    assert route.called


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"data": {"name": "odoo.exceptions.AccessDenied"}}},
        {"result": {"uid": None}},
    ],
)
@respx.mock
def test_authenticate_password_rejects_unsuccessful_web_login(body):
    client = OdooClient(url="https://test.odoo.com", db="test_db")
    respx.post("https://test.odoo.com/web/session/authenticate").respond(json=body)

    with patch.object(client, "authenticate") as rpc_auth:
        with pytest.raises(OdooAuthError):
            client.authenticate_password("user@example.com", "rejected-secret")
        rpc_auth.assert_not_called()


@respx.mock
def test_authenticate_password_handles_network_error():
    client = OdooClient(url="https://test.odoo.com", db="test_db")
    respx.post("https://test.odoo.com/web/session/authenticate").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    with pytest.raises(OdooNetworkError):
        client.authenticate_password("user@example.com", "password")


def test_odoo_client_authenticate_success():
    client = OdooClient(url="https://test.odoo.com", db="test_db", timeout=20)
    with patch("xmlrpc.client.ServerProxy") as mock_proxy:
        mock_common = MagicMock()
        mock_common.authenticate.return_value = 42
        mock_proxy.return_value = mock_common

        uid = client.authenticate(login="user@example.com", secret="valid_key")
        assert uid == 42
        mock_common.authenticate.assert_called_once_with(
            "test_db", "user@example.com", "valid_key", {}
        )


def test_odoo_client_authenticate_failure():
    client = OdooClient(url="https://test.odoo.com", db="test_db")
    with patch("xmlrpc.client.ServerProxy") as mock_proxy:
        mock_common = MagicMock()
        mock_common.authenticate.return_value = False
        mock_proxy.return_value = mock_common

        with pytest.raises(OdooAuthError):
            client.authenticate(login="user@example.com", secret="wrong_key")


def test_odoo_client_execute_success():
    client = OdooClient(url="https://test.odoo.com", db="test_db")
    with patch("xmlrpc.client.ServerProxy") as mock_proxy:
        mock_models = MagicMock()
        mock_models.execute_kw.return_value = [{"id": 1, "name": "Lead 1"}]
        mock_proxy.return_value = mock_models

        result = client.execute(
            uid=42,
            secret="valid_key",
            model="crm.lead",
            method="search_read",
            args=[[("active", "=", True)]],
            kwargs={"fields": ["id", "name"], "limit": 5},
        )
        assert result == [{"id": 1, "name": "Lead 1"}]
        mock_models.execute_kw.assert_called_once_with(
            "test_db",
            42,
            "valid_key",
            "crm.lead",
            "search_read",
            [[("active", "=", True)]],
            {"fields": ["id", "name"], "limit": 5},
        )


def test_odoo_client_access_error():
    client = OdooClient(url="https://test.odoo.com", db="test_db")
    with patch("xmlrpc.client.ServerProxy") as mock_proxy:
        mock_models = MagicMock()
        import xmlrpc.client

        mock_models.execute_kw.side_effect = xmlrpc.client.Fault(
            "odoo.exceptions.AccessError", "Access Denied"
        )
        mock_proxy.return_value = mock_models

        with pytest.raises(OdooAccessError):
            client.execute(
                uid=42,
                secret="valid_key",
                model="crm.lead",
                method="search_read",
                args=[],
            )


def test_odoo_client_network_error():
    client = OdooClient(url="https://test.odoo.com", db="test_db")
    with patch("xmlrpc.client.ServerProxy") as mock_proxy:
        mock_common = MagicMock()
        mock_common.authenticate.side_effect = TimeoutError("timed out")
        mock_proxy.return_value = mock_common

        with pytest.raises(OdooNetworkError):
            client.authenticate(login="user@example.com", secret="any_key")
