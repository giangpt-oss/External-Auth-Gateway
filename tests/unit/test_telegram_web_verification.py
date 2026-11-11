import re
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import TelegramConnection, TelegramUpdate
from app.odoo.service import OdooService
from app.telegram.service import create_telegram_service


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



@pytest.mark.parametrize("flow", ["oidc", "browser"])
@pytest.mark.parametrize("confirmed_id", [42, 99])
def test_web_verification_binds_only_matching_telegram_id(
    temp_db_path, fernet_key, monkeypatch, confirmed_id, flow,
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
    monkeypatch.setattr("app.odoo.client.OdooClient.authenticate", lambda *args, **kwargs: 12)

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
        assert initial_status.json() == {"verified": False, "state": "not_verified", "revision": ""}
        dashboard = client.get("/").text
        assert dashboard.count('id="btn-verify-telegram"') == 1
        fallback = client.get("/connections/telegram/verification-options")
        assert fallback.status_code == 200
        assert 'action="/connections/telegram/verify-browser"' in fallback.text
        assert 'action="/connections/telegram/verify"' in fallback.text
        assert '<details id="telegram-verification-options"' in dashboard
        options = dashboard.split('<details id="telegram-verification-options"', 1)[1].split("</details>", 1)[0]
        assert '<summary id="btn-verify-telegram"' in options
        assert 'action="/connections/telegram/verify-browser"' in options
        assert 'action="/connections/telegram/verify"' in options
        assert "https://t.me/ExampleOfficeBot" not in client.get("/?status=telegram_verified").text
        assert client.post("/connections/telegram/link").status_code == 409
        assert client.post("/connections/telegram/verify", data={"csrf_token": "invalid"}).status_code == 403
        if flow == "browser":
            response = client.post("/connections/telegram/verify-browser", data={"csrf_token": csrf})
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            code = re.search(r'value="/verify ([A-Za-z0-9_-]{32})"', response.text)[1]
            bot = MagicMock()
            service = create_telegram_service(settings, app.state.session_factory, telegram_client=bot)
            update = {
                "update_id": 123,
                "message": {
                    "chat": {"type": "private", "id": confirmed_id},
                    "from": {"id": confirmed_id, "username": "employee"},
                    "text": f"/verify {code}",
                },
            }
            service.ingest_update(update)
            status_result = client.get(status_path).json()
            assert status_result["verified"] == (confirmed_id == 42)
            assert bool(status_result["revision"]) == (confirmed_id == 42)
            reply = bot.send_message.call_args.args[1]
            assert ("Đã xác minh Telegram" in reply) == (confirmed_id == 42)
            assert ('href="https://t.me/ExampleOfficeBot"' in client.get("/").text) == (confirmed_id == 42)
            with app.state.session_factory() as db:
                assert code not in db.query(TelegramUpdate).one().payload
            if confirmed_id == 42:
                service.ingest_update({**update, "update_id": 124})
                assert "đã được dùng" in bot.send_message.call_args.args[1]
            exchange.assert_not_called()
            return

        start = client.post("/connections/telegram/verify", data={"csrf_token": csrf})
        assert start.status_code == 303
        redirect = urlsplit(start.headers["location"])
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
