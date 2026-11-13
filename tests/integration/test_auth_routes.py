from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_auth_routes_login_me_logout(temp_db_path, fernet_key):
    settings = Settings(
        APP_ENV="development",
        PUBLIC_BASE_URL="http://localhost:8080",
        ODOO_URL="https://mycompany.odoo.com",
        ODOO_DB="test_db",
        DATABASE_URL=f"sqlite:///{temp_db_path}",
        ENCRYPTION_KEY=fernet_key,
    )
    app = create_app(settings)
    client = TestClient(app)

    with (
        patch("app.odoo.client.OdooClient.authenticate_password") as mock_auth,
        patch("app.odoo.client.OdooClient.execute") as mock_exec,
    ):
        mock_auth.return_value = 55
        mock_exec.return_value = [
            {
                "id": 55,
                "name": "Tran Thi B",
                "login": "b.tran@company.com",
                "active": True,
                "share": False,
                "tz": "Asia/Bangkok",
                "partner_id": [155, "Tran Thi B"],
            }
        ]

        rejected = client.post(
            "/auth/login",
            data={
                "login": "b.tran@company.com",
                "secret": "my_api_key_123",
                "credential_kind": "api_key",
            },
        )
        assert rejected.status_code == 401
        assert "session_id" not in rejected.cookies
        mock_auth.assert_not_called()

        # 1. Login
        login_res = client.post(
            "/auth/login",
            data={
                "login": "b.tran@company.com",
                "secret": "odoo_password",
            },
        )
        assert login_res.status_code == 200
        mock_auth.assert_called_once_with("b.tran@company.com", "odoo_password")
        data = login_res.json()
        assert data["user"]["odoo_uid"] == 55
        assert data["user"]["name"] == "Tran Thi B"
        assert "session_id" in login_res.cookies

        # 2. GET /api/me
        me_res = client.get("/api/me")
        assert me_res.status_code == 200
        me_data = me_res.json()
        assert me_data["odoo_uid"] == 55
        assert me_data["login"] == "b.tran@company.com"
        # Verify secret is NEVER leaked
        assert "secret" not in me_data
        assert "password" not in me_data
        assert "encrypted_secret" not in me_data

        # 3. Logout
        logout_res = client.post("/auth/logout")
        assert logout_res.status_code == 200

        # 4. Check GET /api/me after logout -> 401
        me_after_logout = client.get("/api/me")
        assert me_after_logout.status_code == 401
