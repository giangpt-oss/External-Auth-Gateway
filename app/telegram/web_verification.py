"""Verify Telegram ownership against the current Odoo employee record."""

import base64
import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from google.auth import jwt
from google.auth.exceptions import GoogleAuthError, InvalidValue
from sqlalchemy.exc import IntegrityError

from app.auth.routes import get_auth_service
from app.auth.service import AuthService
from app.config import Settings
from app.models import TELEGRAM_WEB_BINDING_PREFIX, LinkToken, Session, TelegramConnection, User
from app.odoo.client import OdooClient, OdooError, OdooNetworkError
from app.odoo.service import OdooService
from app.portal.routes import templates
from app.security import decrypt_secret, encrypt_secret, generate_secure_token, hash_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Telegram Bot"])
ISSUER = "https://oauth.telegram.org"
PURPOSE = "telegram_web_oauth"
BROWSER_CODE_PURPOSE = "telegram_browser_code"
CALLBACK_PATH = "/oauth/telegram/callback"


def _redirect_uri(settings: Settings) -> str:
    base_url = settings.TELEGRAM_WEB_BASE_URL or settings.PUBLIC_BASE_URL
    return base_url.rstrip("/") + CALLBACK_PATH


def _session(auth: AuthService, cookie: str | None):
    result = auth.get_user_by_session(cookie) if cookie else None
    if not result:
        raise HTTPException(401, "Vui lòng đăng nhập lại vào Odoo.")
    return result


def _error(message: str) -> RedirectResponse:
    return RedirectResponse("/?" + urlencode({"error": message}), status_code=303)


@router.post("/connections/telegram/verify")
def start_verification(
    request: Request,
    csrf_token: str = Form(...),
    session_id: str | None = Cookie(None),
    auth: AuthService = Depends(get_auth_service),
):
    user, session = _session(auth, session_id)
    if not secrets.compare_digest(csrf_token.encode(), session.csrf_secret.encode()):
        raise HTTPException(403, "Yêu cầu xác minh không hợp lệ.")
    settings = request.app.state.settings
    if not settings.telegram_web_configured:
        return _error("Chưa cấu hình xác minh Telegram trên web.")

    state = generate_secure_token()
    verifier = generate_secure_token()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    with request.app.state.session_factory() as db:
        db.query(LinkToken).filter_by(
            purpose=PURPOSE, user_id=user.id, session_id=session.id, used_at=None,
        ).delete(synchronize_session=False)
        db.add(LinkToken(
            purpose=PURPOSE, user_id=user.id, session_id=session.id, token_hash=hash_token(state),
            encrypted_pkce_verifier=encrypt_secret(verifier, settings.ENCRYPTION_KEY),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ))
        db.commit()
    query = urlencode({
        "client_id": settings.TELEGRAM_WEB_CLIENT_ID,
        "redirect_uri": _redirect_uri(settings),
        "response_type": "code",
        "scope": "openid profile",
        "state": state,
        "code_challenge": challenge.rstrip(b"=").decode("ascii"),
        "code_challenge_method": "S256",
    })
    return RedirectResponse(f"{ISSUER}/auth?{query}", status_code=303)


class TelegramIdentityRejected(ValueError):
    pass


def _decode_id_token(token: str, keys: list[dict], audience: str, nonce: str | None) -> dict:
    header = jwt.decode_header(token)
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise TelegramIdentityRejected("unsupported_signing_algorithm")
    matches = [
        key for key in keys
        if key.get("kid") == header["kid"] and key.get("kty") == "RSA"
        and key.get("alg", "RS256") == "RS256" and key.get("use", "sig") == "sig"
    ]
    if len(matches) != 1:
        raise TelegramIdentityRejected("unknown_signing_key")
    key = matches[0]

    def decode_integer(value: str) -> int:
        return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")

    public_key = RSAPublicNumbers(decode_integer(key["e"]), decode_integer(key["n"])).public_key()
    pem = public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    try:
        claims = jwt.decode(token, certs={header["kid"]: pem}, audience=audience, clock_skew_in_seconds=5)
    except InvalidValue as exc:
        message = str(exc)
        if message.startswith(("Token used too early", "Token expired")):
            reason = "token_time_invalid"
        elif message.startswith("Token has wrong audience"):
            reason = "wrong_audience"
        else:
            reason = "invalid_token_value"
        raise TelegramIdentityRejected(reason) from exc
    if (
        claims.get("iss") != ISSUER
        or (nonce is not None and claims.get("nonce") != nonce)
        or not isinstance(claims.get("sub"), str)
        or not claims["sub"]
    ):
        raise TelegramIdentityRejected("unexpected_identity_claims")
    telegram_id = claims.get("id")
    if isinstance(telegram_id, str) and re.fullmatch(r"[0-9]{1,16}", telegram_id):
        telegram_id = int(telegram_id)
    if type(telegram_id) is not int or not 0 < telegram_id < 2**52:
        raise TelegramIdentityRejected("missing_or_invalid_telegram_id")
    return {**claims, "id": telegram_id}


class TelegramTokenRejected(ValueError):
    pass


def _exchange_code(settings: Settings, code: str, verifier: str, nonce: str | None) -> dict:
    with httpx.Client(timeout=8.0) as client:
        response = client.post(
            f"{ISSUER}/token",
            auth=(settings.TELEGRAM_WEB_CLIENT_ID, settings.TELEGRAM_WEB_CLIENT_SECRET),
            data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": _redirect_uri(settings), "client_id": settings.TELEGRAM_WEB_CLIENT_ID,
                "code_verifier": verifier,
            },
        )
        token_result = response.json()
        error = token_result.get("error")
        if error:
            safe_errors = {"invalid_client", "invalid_grant", "invalid_request", "unauthorized_client"}
            raise TelegramTokenRejected(error if isinstance(error, str) and error in safe_errors else "other")
        response.raise_for_status()
        jwks = client.get(f"{ISSUER}/.well-known/jwks.json")
        jwks.raise_for_status()
    return _decode_id_token(
        token_result["id_token"], jwks.json()["keys"], settings.TELEGRAM_WEB_CLIENT_ID, nonce,
    )


def _store_binding(db, user_id: int, claims: dict, chat_id: int | None = None) -> None:
    telegram_id = claims["id"]
    existing = db.query(TelegramConnection).filter_by(telegram_user_id=telegram_id).first()
    if existing and existing.user_id != user_id:
        raise ValueError("Telegram account is already linked")
    connection = existing or db.query(TelegramConnection).filter_by(user_id=user_id).first()
    if connection is None:
        connection = TelegramConnection(user_id=user_id)
        db.add(connection)
    if chat_id is not None or connection.telegram_user_id != telegram_id or not connection.private_chat_id:
        connection.private_chat_id = chat_id if chat_id is not None else telegram_id
    connection.telegram_user_id = telegram_id
    username = claims.get("preferred_username")
    connection.username = username[:255] if isinstance(username, str) else None
    connection.binding_id = f"{TELEGRAM_WEB_BINDING_PREFIX}{secrets.token_hex(16)}"
    connection.status = "connected"
    db.commit()


@router.post("/connections/telegram/verify-browser")
def start_browser_verification(
    request: Request,
    csrf_token: str = Form(...),
    session_id: str | None = Cookie(None),
    auth: AuthService = Depends(get_auth_service),
):
    user, session = _session(auth, session_id)
    if not secrets.compare_digest(csrf_token.encode(), session.csrf_secret.encode()):
        raise HTTPException(403, "Yêu cầu xác minh không hợp lệ.")
    settings = request.app.state.settings
    username = settings.TELEGRAM_BOT_USERNAME.strip().lstrip("@")
    if not settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD or not re.fullmatch(r"[A-Za-z0-9_]+", username):
        return _error("Gateway chưa được cấu hình xác minh bằng Telegram Web.")

    code = generate_secure_token(24)
    with request.app.state.session_factory() as db:
        db.query(LinkToken).filter_by(
            purpose=BROWSER_CODE_PURPOSE, user_id=user.id, session_id=session.id, used_at=None,
        ).delete(synchronize_session=False)
        db.add(LinkToken(
            purpose=BROWSER_CODE_PURPOSE, user_id=user.id, session_id=session.id,
            token_hash=hash_token(code), expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ))
        db.commit()
    return templates.TemplateResponse(
        request=request, name="telegram_browser_verification.html",
        context={"user": user, "csrf_token": csrf_token, "bot_username": username, "code": code},
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def confirm_browser_code(session_factory, settings: Settings, chat_id: int,
                         telegram_id: int, username: str | None, code: str) -> str:
    invalid = "Mã xác minh không hợp lệ, đã hết hạn hoặc đã được dùng."
    if (
        not settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD
        or type(telegram_id) is not int
        or telegram_id <= 0
        or chat_id != telegram_id
        or not re.fullmatch(r"[A-Za-z0-9_-]{32}", code)
    ):
        return invalid

    with session_factory() as db:
        now = datetime.now(UTC)
        attempt = db.query(LinkToken).join(Session, LinkToken.session_id == Session.id).filter(
            LinkToken.token_hash == hash_token(code), LinkToken.purpose == BROWSER_CODE_PURPOSE,
            LinkToken.used_at.is_(None), LinkToken.expires_at > now,
            Session.user_id == LinkToken.user_id, Session.expires_at > now,
        ).first()
        if not attempt:
            return invalid
        user = db.get(User, attempt.user_id)
        if not user or not user.active or not user.credential:
            return invalid
        try:
            secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            odoo = OdooService(OdooClient(url=user.tenant, db=user.database))
            odoo.validate_user_account(user.odoo_uid, secret)
            expected_id = odoo.get_employee_telegram_id(
                user.odoo_uid, secret, settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD,
            )
        except OdooNetworkError:
            return "Odoo tạm thời không phản hồi. Vui lòng gửi lại lệnh."
        except (OdooError, InvalidToken, TypeError, ValueError):
            return "Không đọc được Telegram ID từ hồ sơ Odoo."
        if expected_id != telegram_id:
            return "Telegram ID của tài khoản này không trùng với hồ sơ Odoo."

        now = datetime.now(UTC)
        active_sessions = db.query(Session.id).filter(
            Session.user_id == user.id, Session.expires_at > now,
        ).statement
        claimed = db.query(LinkToken).filter(
            LinkToken.id == attempt.id, LinkToken.used_at.is_(None), LinkToken.expires_at > now,
            LinkToken.session_id.in_(active_sessions),
        ).update({"used_at": now}, synchronize_session=False)
        if claimed != 1:
            return invalid
        try:
            _store_binding(db, user.id, {"id": telegram_id, "preferred_username": username}, chat_id)
        except (ValueError, IntegrityError):
            return "Tài khoản Telegram này đã được liên kết với hồ sơ khác."
    return "Đã xác minh Telegram. Quay lại portal Odoo và bấm Kiểm tra kết quả."


@router.get(CALLBACK_PATH)
def finish_verification(
    request: Request,
    state: str = "",
    code: str = "",
    session_id: str | None = Cookie(None),
    auth: AuthService = Depends(get_auth_service),
):
    user, session = _session(auth, session_id)
    settings = request.app.state.settings
    if not settings.telegram_web_configured or not state or len(state) > 128:
        return _error("Yêu cầu xác minh Telegram không hợp lệ.")

    with request.app.state.session_factory() as db:
        attempt = db.query(LinkToken).filter_by(
            purpose=PURPOSE, token_hash=hash_token(state), user_id=user.id, session_id=session.id,
        ).first()
        now = datetime.now(UTC)
        if not attempt or db.query(LinkToken).filter(
            LinkToken.id == attempt.id, LinkToken.used_at.is_(None), LinkToken.expires_at > now,
        ).update({"used_at": now}, synchronize_session=False) != 1:
            return _error("Phiên xác minh Telegram đã hết hạn hoặc đã được sử dụng.")
        nonce, encrypted_verifier = attempt.oauth_nonce, attempt.encrypted_pkce_verifier
        db.commit()

    if not code:
        return _error("Bạn chưa chấp thuận xác minh trên Telegram.")
    try:
        verifier = decrypt_secret(encrypted_verifier, settings.ENCRYPTION_KEY)
        claims = _exchange_code(settings, code, verifier, nonce)
    except TelegramTokenRejected as exc:
        logger.warning("Telegram OAuth token rejected: %s", exc)
        if str(exc) == "invalid_client":
            return _error("Telegram từ chối thông tin Login Widget của Gateway.")
        if str(exc) == "invalid_grant":
            return _error("Telegram từ chối mã đăng nhập. Hãy bắt đầu phiên xác minh mới.")
        return _error("Không xác thực được tài khoản Telegram. Vui lòng thử lại.")
    except TelegramIdentityRejected as exc:
        logger.warning("Telegram OAuth identity rejected: %s", exc)
        if str(exc) == "unsupported_signing_algorithm":
            return _error("Gateway cần thuật toán RS256 trong Login Widget của BotFather.")
        if str(exc) == "token_time_invalid":
            return _error("Token Telegram chưa có hiệu lực hoặc đã hết hạn. Hãy bắt đầu phiên xác minh mới.")
        if str(exc) == "wrong_audience":
            return _error("Token Telegram không được cấp cho bot đang cấu hình trên Gateway.")
        if str(exc) == "missing_or_invalid_telegram_id":
            return _error("Token đã ký từ Telegram không có Telegram ID hợp lệ để đối chiếu với Odoo.")
        return _error("Không xác thực được tài khoản Telegram. Vui lòng thử lại.")
    except httpx.HTTPStatusError as exc:
        logger.warning("Telegram OAuth provider response: HTTP %s", exc.response.status_code)
        return _error("Telegram không trả về thông tin xác minh hợp lệ. Vui lòng thử lại.")
    except (httpx.HTTPError, GoogleAuthError, InvalidToken, AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Telegram OAuth verification failed: %s", type(exc).__name__)
        return _error("Không xác thực được tài khoản Telegram. Vui lòng thử lại.")

    try:
        secret = auth.get_user_secret(user.id)
        if not secret:
            return _error("Vui lòng đăng nhập lại vào Odoo.")
        odoo = OdooService(OdooClient(url=user.tenant, db=user.database))
        odoo.validate_user_account(user.odoo_uid, secret)
        expected_id = odoo.get_employee_telegram_id(
            user.odoo_uid, secret, settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD,
        )
    except OdooNetworkError:
        return _error("Odoo tạm thời không phản hồi. Vui lòng thử lại.")
    except (OdooError, InvalidToken):
        return _error("Không đọc được Telegram ID từ hồ sơ Odoo.")
    if expected_id is None:
        return _error("Hồ sơ nhân viên Odoo chưa có Telegram ID hợp lệ hoặc có nhiều bản ghi.")
    if claims["id"] != expected_id:
        return _error("Tài khoản Telegram bạn xác nhận không trùng với Telegram ID trên Odoo.")

    try:
        with request.app.state.session_factory() as db:
            _store_binding(db, user.id, claims)
    except (ValueError, IntegrityError):
        return _error("Tài khoản Telegram này đã được liên kết với hồ sơ khác.")
    return RedirectResponse("/?status=telegram_verified", status_code=303)
