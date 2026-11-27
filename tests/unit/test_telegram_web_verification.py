from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import TelegramConnection
from app.odoo.service import OdooService


def test_portal_uses_private_bot_pairing_when_web_oauth_is_unconfigured(
    temp_db_path, fernet_key, monkeypatch,
):
    from app.telegram.service import create_telegram_service
    from app.models import LinkToken

    settings = Settings(
        APP_ENV="test", DATABASE_URL=f"sqlite:///{temp_db_path}",
        ENCRYPTION_KEY=fernet_key, TELEGRAM_BOT_TOKEN="fixture-token",
        TELEGRAM_BOT_USERNAME="ExampleOfficeBot",
        ODOO_EMPLOYEE_TELEGRAM_ID_FIELD="",
    )
    app = create_app(settings)
    monkeypatch.setattr("app.odoo.client.OdooClient.authenticate_password",
                        lambda *args, **kwargs: 12)
    monkeypatch.setattr("app.odoo.client.OdooClient.execute",
                        lambda *args, **kwargs: [{
                            "id": 12, "active": True, "share": False,
                            "name": "Approver",
                        }])
    with TestClient(app, follow_redirects=False) as client:
        login = client.post("/auth/login", data={"login": "approver@example.test", "secret": "key"})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert client.post("/connections/telegram/verify", data={"csrf_token": "bad"}).status_code == 403
        start = client.post("/connections/telegram/verify", data={"csrf_token": csrf},
                            headers={"Accept": "application/json"})
        assert start.status_code == 200
        link = start.json()["link_url"]
        assert link.startswith("https://t.me/ExampleOfficeBot?start=")
        token = parse_qs(urlsplit(link).query)["start"][0]
        telegram = MagicMock()
        service = create_telegram_service(settings, app.state.session_factory,
                                          telegram_client=telegram)
        service.process_update_dict({"update_id": 101, "message": {
            "from": {"id": 1234567890, "is_bot": False},
            "chat": {"id": 1234567890, "type": "private"},
            "text": f"/start {token}",
        }})
        status = client.get("/connections/telegram/verification-status").json()
        assert status["linked"] is True and status["revision"]
        with app.state.session_factory() as db:
            connection = db.query(TelegramConnection).one()
            assert connection.telegram_user_id == 1234567890
            assert connection.private_chat_id == 1234567890
            assert db.query(LinkToken).filter_by(purpose="telegram_link").one().used_at
        service.process_update_dict({"update_id": 102, "message": {
            "from": {"id": 9999999999, "is_bot": False},
            "chat": {"id": 9999999999, "type": "private"},
            "text": f"/start {token}",
        }})
        with app.state.session_factory() as db:
            assert db.query(TelegramConnection).one().telegram_user_id == 1234567890


@pytest.mark.parametrize(("raw", "expected"), [
    (" 9876543210 ", 9876543210), (42, 42), (False, None),
    ("@someone", None), (True, None), ("-1", None),
])
def test_expected_id_is_read_from_own_employee_record(raw, expected):
    client = MagicMock()
    client.execute.return_value = [{"x_test_telegram_id": raw}]
    assert OdooService(client).get_employee_telegram_id(12, "key", "x_test_telegram_id") == expected
    client.execute.assert_called_once_with(
        uid=12, secret="key", model="hr.employee", method="search_read",
        args=[[("user_id", "=", 12)]],
        kwargs={"fields": ["x_test_telegram_id"], "limit": 2},
    )


def test_ambiguous_employee_records_are_rejected():
    client = MagicMock()
    client.execute.return_value = [{"x_telegram": "42"}, {"x_telegram": "42"}]
    assert OdooService(client).get_employee_telegram_id(12, "key", "x_telegram") is None



@pytest.mark.parametrize("confirmed_id", [42, 99])
def test_web_verification_binds_only_matching_telegram_id(
    temp_db_path, fernet_key, monkeypatch, confirmed_id,
):
    from app.telegram import web_verification

    settings = Settings(
        APP_ENV="test",
        PUBLIC_BASE_URL="http://localhost:8080",
        DATABASE_URL=f"sqlite:///{temp_db_path}",
        ENCRYPTION_KEY=fernet_key,
        ODOO_EMPLOYEE_TELEGRAM_ID_FIELD="x_telegram",
        TELEGRAM_BOT_USERNAME="ExampleOfficeBot",
        TELEGRAM_WEB_BASE_URL="https://portal.example.test",
        TELEGRAM_WEB_CLIENT_ID="123",
        TELEGRAM_WEB_CLIENT_SECRET="test-only",
    )
    app = create_app(settings)
    monkeypatch.setattr("app.odoo.client.OdooClient.authenticate_password", lambda *args, **kwargs: 12)

    expected_telegram_id = "42"

    def execute(self, **kwargs):
        if kwargs["model"] == "hr.employee":
            assert kwargs["args"] == [[("user_id", "=", 12)]]
            return [{"x_telegram": expected_telegram_id}]
        return [{"id": 12, "active": True, "share": False, "name": "Employee"}]

    monkeypatch.setattr("app.odoo.client.OdooClient.execute", execute)
    exchange = MagicMock(return_value={"id": confirmed_id, "preferred_username": "employee"})
    monkeypatch.setattr(web_verification, "_exchange_code", exchange)

    with TestClient(app, follow_redirects=False) as client:
        status_path = "/connections/telegram/verification-status"
        assert client.get(status_path).status_code == 401
        login = client.post("/auth/login", data={"login": "test@example.test", "secret": "key"})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        initial_status = client.get(status_path)
        assert initial_status.headers["Cache-Control"] == "no-store"
        assert initial_status.json() == {
            "verified": False, "state": "not_verified", "linked": False, "revision": "",
        }
        dashboard = client.get("/").text
        assert "Liên kết Telegram" in dashboard
        fallback = client.get("/connections/telegram/verification-options")
        assert fallback.status_code == 200
        assert "Chưa liên kết" in dashboard and "Chưa liên kết" in fallback.text
        assert 'action="/connections/telegram/verify"' in fallback.text
        assert "https://t.me/ExampleOfficeBot" not in client.get("/?status=telegram_verified").text
        assert client.post("/connections/telegram/link").status_code == 409
        assert client.post("/connections/telegram/verify", data={"csrf_token": "invalid"}).status_code == 403
        assert client.post("/connections/telegram/verify-browser", data={"csrf_token": csrf}).status_code == 404

        start = client.post(
            "/connections/telegram/verify", data={"csrf_token": csrf},
            headers={"Accept": "application/json"},
        )
        assert start.status_code == 200
        assert start.headers["Cache-Control"] == "no-store"
        payload = start.json()
        assert payload["qr_code_data_uri"].startswith("data:image/png;base64,")
        assert payload["expires_at"]
        redirect = urlsplit(payload["link_url"])
        params = parse_qs(redirect.query)
        assert redirect.netloc == "oauth.telegram.org"
        assert params["scope"] == ["openid profile"]
        assert params["redirect_uri"] == ["https://portal.example.test/oauth/telegram/callback"]
        assert params["code_challenge_method"] == ["S256"]

        callback = {"code": "test-code", "state": params["state"][0]}
        assert client.get("/oauth/telegram/callback", params={**callback, "state": "wrong"}).status_code == 303
        exchange.assert_not_called()
        finish = client.get("/oauth/telegram/callback", params=callback)
        assert ("status=telegram_verified" in finish.headers["location"]) == (confirmed_id == 42)
        status_result = client.get(status_path).json()
        assert status_result["verified"] == (confirmed_id == 42)
        assert bool(status_result["revision"]) == (confirmed_id == 42)
        dashboard = client.get("/")
        assert ('href="https://t.me/ExampleOfficeBot"' in dashboard.text) == (confirmed_id == 42)
        assert "nonce" not in params
        assert exchange.call_args.args[3] is None
        replay = client.get("/oauth/telegram/callback", params=callback)
        assert "status=telegram_verified" not in replay.headers["location"]
        exchange.assert_called_once()
        with app.state.session_factory() as db:
            conn = db.query(TelegramConnection).first()
            assert (conn is not None) == (confirmed_id == 42)
            if conn:
                assert conn.telegram_user_id == 42 and conn.binding_id
        if confirmed_id == 42:
            expected_telegram_id = "99"
            assert client.get(status_path).json()["state"] == "mismatch"
            changed_dashboard = client.get("/").text
            assert 'href="https://t.me/ExampleOfficeBot"' not in changed_dashboard
            assert "Telegram ID trên Odoo đã thay đổi" in changed_dashboard
            assert "Cần xác minh" in changed_dashboard
            assert "Cần xác minh" in client.get("/connections/telegram/verification-options").text

        from datetime import UTC, datetime, timedelta

        from app.models import LinkToken
        from app.security import hash_token

        new_attempt = client.post("/connections/telegram/verify", data={"csrf_token": csrf})
        new_state = parse_qs(urlsplit(new_attempt.headers["location"]).query)["state"][0]
        with app.state.session_factory() as db:
            token = db.query(LinkToken).filter_by(token_hash=hash_token(new_state)).one()
            token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        expired = client.get("/oauth/telegram/callback", params={"state": new_state, "code": "test-code"})
        assert "status=telegram_verified" not in expired.headers["location"]
        exchange.assert_called_once()



def test_telegram_id_token_validates_signature_audience_and_nonce():
    import base64
    from datetime import UTC, datetime

    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import jwt
    from google.auth.crypt import RSASigner
    from google.auth.exceptions import GoogleAuthError

    from app.telegram.web_verification import ISSUER, _decode_id_token

    def encoded(number):
        data = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_numbers()
    keys = [{"kid": "test", "kty": "RSA", "e": encoded(public.e), "n": encoded(public.n)}]
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": ISSUER, "aud": "123", "sub": "opaque", "id": 42,
        "nonce": "expected", "iat": now, "exp": now + 60,
    }
    signer = RSASigner(key, "test")
    token = jwt.encode(signer, claims).decode("ascii")
    assert _decode_id_token(token, keys, "123", "expected")["id"] == 42
    claims_without_nonce = {name: value for name, value in claims.items() if name != "nonce"}
    oidc_token = jwt.encode(signer, claims_without_nonce).decode("ascii")
    assert _decode_id_token(oidc_token, keys, "123", None)["id"] == 42
    signed_text_id = jwt.encode(signer, {**claims_without_nonce, "id": "42"}).decode("ascii")
    assert _decode_id_token(signed_text_id, keys, "123", None)["id"] == 42
    missing_id = jwt.encode(
        signer, {name: value for name, value in claims_without_nonce.items() if name != "id"},
    ).decode("ascii")
    with pytest.raises(ValueError, match="missing_or_invalid_telegram_id"):
        _decode_id_token(missing_id, keys, "123", None)
    with pytest.raises(ValueError):
        _decode_id_token(token, keys, "123", "wrong")
    with pytest.raises(ValueError, match="wrong_audience"):
        _decode_id_token(token, keys, "other-client", "expected")
    slightly_early = jwt.encode(signer, {**claims, "iat": now + 3}).decode("ascii")
    assert _decode_id_token(slightly_early, keys, "123", "expected")["id"] == 42
    too_early = jwt.encode(signer, {**claims, "iat": now + 60}).decode("ascii")
    with pytest.raises(ValueError, match="token_time_invalid"):
        _decode_id_token(too_early, keys, "123", "expected")
    expired = jwt.encode(signer, {**claims, "iat": now - 60, "exp": now - 30}).decode("ascii")
    with pytest.raises(ValueError, match="token_time_invalid"):
        _decode_id_token(expired, keys, "123", "expected")

    bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tampered = jwt.encode(RSASigner(bad_key, "test"), claims).decode("ascii")
    with pytest.raises(GoogleAuthError):
        _decode_id_token(tampered, keys, "123", "expected")


def test_web_verification_cross_domain_callback(temp_db_path, fernet_key, monkeypatch):
    from app.telegram import web_verification

    settings = Settings(
        APP_ENV="test",
        PUBLIC_BASE_URL="http://localhost:8080",
        DATABASE_URL=f"sqlite:///{temp_db_path}",
        ENCRYPTION_KEY=fernet_key,
        ODOO_EMPLOYEE_TELEGRAM_ID_FIELD="x_telegram",
        TELEGRAM_BOT_USERNAME="ExampleOfficeBot",
        TELEGRAM_WEB_BASE_URL="https://portal.example.test",
        TELEGRAM_WEB_CLIENT_ID="123",
        TELEGRAM_WEB_CLIENT_SECRET="test-only",
    )
    app = create_app(settings)
    monkeypatch.setattr("app.odoo.client.OdooClient.authenticate_password", lambda *args, **kwargs: 12)

    def execute(self, **kwargs):
        if kwargs["model"] == "hr.employee":
            return [{"x_telegram": "42"}]
        return [{"id": 12, "active": True, "share": False, "name": "Employee"}]

    monkeypatch.setattr("app.odoo.client.OdooClient.execute", execute)
    exchange = MagicMock(return_value={"id": 42, "preferred_username": "employee"})
    monkeypatch.setattr(web_verification, "_exchange_code", exchange)

    with TestClient(app, follow_redirects=False) as client:
        # 1. Login on localhost
        login = client.post("/auth/login", data={"login": "test@example.test", "secret": "key"})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]

        # 2. Start verification
        start = client.post("/connections/telegram/verify", data={"csrf_token": csrf})
        assert start.status_code == 303
        redirect = urlsplit(start.headers["location"])
        params = parse_qs(redirect.query)
        state = params["state"][0]

        # 3. Simulate cross-domain callback on ngrok where session_id cookie is NOT sent
        client.cookies.clear()
        callback = {"code": "test-code", "state": state}
        finish = client.get("/oauth/telegram/callback", params=callback)
        assert finish.status_code == 303
        assert "status=telegram_verified" in finish.headers["location"]
        assert "session_id" in finish.cookies

        # 4. Verify status is now verified with the newly established session
        status_result = client.get("/connections/telegram/verification-status").json()
        assert status_result["verified"] is True

