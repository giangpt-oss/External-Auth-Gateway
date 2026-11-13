from unittest.mock import MagicMock

import pytest

from app.auth.service import AuthService, InactiveUserError, PortalUserError, RateLimitExceeded
from app.db import init_db
from app.models import OdooCredential, User
from app.odoo.client import OdooAuthError
from app.security import decrypt_secret


@pytest.fixture
def auth_components(temp_db_path, fernet_key):
    engine, session_factory = init_db(f"sqlite:///{temp_db_path}")
    mock_odoo_client = MagicMock()
    service = AuthService(
        session_factory=session_factory,
        odoo_client=mock_odoo_client,
        tenant="mytenant",
        database="mydb",
        encryption_key=fernet_key,
        session_lifetime_hours=8,
    )
    yield service, mock_odoo_client, session_factory
    engine.dispose()


def test_login_success_active_internal_user(auth_components, fernet_key):
    service, mock_odoo, session_factory = auth_components
    mock_odoo.authenticate_password.return_value = 101
    mock_odoo.execute.return_value = [
        {
            "id": 101,
            "name": "Nguyen Van A",
            "login": "a.nguyen@company.com",
            "active": True,
            "share": False,
            "tz": "Asia/Bangkok",
            "partner_id": [201, "Nguyen Van A"],
        }
    ]

    auth_user, session_token, csrf_secret = service.login(
        login="a.nguyen@company.com",
        secret="secret123",
        client_ip="127.0.0.1",
    )

    assert auth_user.odoo_uid == 101
    assert auth_user.name == "Nguyen Van A"
    assert session_token is not None
    assert csrf_secret is not None

    # Verify user & credential stored encrypted
    with session_factory() as db:
        user = db.query(User).filter_by(odoo_uid=101).first()
        assert user is not None
        cred = db.query(OdooCredential).filter_by(user_id=user.id).first()
        assert cred is not None
        assert cred.credential_kind == "password"
        assert decrypt_secret(cred.encrypted_secret, fernet_key) == "secret123"


def test_login_rejects_portal_user(auth_components):
    service, mock_odoo, _ = auth_components
    mock_odoo.authenticate_password.return_value = 102
    mock_odoo.execute.return_value = [
        {
            "id": 102,
            "name": "Portal Customer",
            "login": "customer@gmail.com",
            "active": True,
            "share": True,  # Portal / External user!
            "tz": "Asia/Bangkok",
            "partner_id": [202, "Portal Customer"],
        }
    ]

    with pytest.raises(PortalUserError):
        service.login(
            login="customer@gmail.com",
            secret="portal_pass",
            credential_kind="password",
            client_ip="127.0.0.1",
        )


def test_login_rejects_inactive_user(auth_components):
    service, mock_odoo, _ = auth_components
    mock_odoo.authenticate_password.return_value = 103
    mock_odoo.execute.return_value = [
        {
            "id": 103,
            "name": "Disabled Staff",
            "login": "disabled@company.com",
            "active": False,
            "share": False,
            "tz": "Asia/Bangkok",
            "partner_id": [203, "Disabled Staff"],
        }
    ]

    with pytest.raises(InactiveUserError):
        service.login(
            login="disabled@company.com",
            secret="some_pass",
            credential_kind="password",
            client_ip="127.0.0.1",
        )


def test_login_rate_limiting(auth_components):
    service, mock_odoo, _ = auth_components
    mock_odoo.authenticate_password.side_effect = OdooAuthError("Invalid credentials")

    # 5 failed attempts
    for _ in range(5):
        with pytest.raises(OdooAuthError):
            service.login(
                login="brute@company.com",
                secret="wrong",
                credential_kind="password",
                client_ip="192.168.1.50",
            )

    # 6th attempt must trigger RateLimitExceeded
    with pytest.raises(RateLimitExceeded):
        service.login(
            login="brute@company.com",
            secret="wrong",
            credential_kind="password",
            client_ip="192.168.1.50",
        )
