import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.routes import get_current_user
from app.auth.service import AuthenticatedUser
from app.config import Settings
from app.models import TelegramConnection
from app.telegram.client import TelegramClient
from app.telegram.service import TelegramService

_logger = logging.getLogger(__name__)
router = APIRouter(tags=["Telegram Bot"])


def get_telegram_client(request: Request) -> TelegramClient:
    settings: Settings = request.app.state.settings
    return TelegramClient(bot_token=settings.TELEGRAM_BOT_TOKEN)


def get_telegram_service(
    request: Request,
    telegram_client: TelegramClient = Depends(get_telegram_client),
) -> TelegramService:
    settings: Settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    odoo_client = getattr(request.app.state, "odoo_client", None)
    from app.telegram.service import create_telegram_service

    return create_telegram_service(
        settings=settings,
        session_factory=session_factory,
        telegram_client=telegram_client,
        odoo_client=odoo_client,
    )


@router.post("/connections/telegram/link")
def create_telegram_link(
    current_user: AuthenticatedUser = Depends(get_current_user),
    telegram_service: TelegramService = Depends(get_telegram_service),
):
    link = telegram_service.issue_link(user_id=current_user.id)
    return {
        "status": "success",
        "url": link.url,
        "link_url": link.url,
        "qr_code": link.qr_code_data_uri,
        "qr_code_data_uri": link.qr_code_data_uri,
        "expires_at": link.expires_at.isoformat(),
    }


@router.post("/connections/telegram/disconnect")
def disconnect_telegram(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    telegram_service: TelegramService = Depends(get_telegram_service),
):
    telegram_service.disconnect(user_id=current_user.id)
    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
        "Content-Type", ""
    )
    if is_html:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return {"status": "success", "message": "Telegram disconnected successfully"}


@router.post("/connections/telegram/test")
def send_test_message(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    telegram_client: TelegramClient = Depends(get_telegram_client),
):
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        conn = (
            db.query(TelegramConnection)
            .filter_by(user_id=current_user.id, status="connected")
            .first()
        )
        if not conn:
            is_html = "text/html" in request.headers.get(
                "Accept", ""
            ) or "form" in request.headers.get("Content-Type", "")
            if is_html:
                return RedirectResponse(
                    url="/?error=telegram_not_connected", status_code=status.HTTP_303_SEE_OTHER
                )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "code": "NOT_CONNECTED",
                        "message": "Tài khoản Telegram chưa được liên kết.",
                    }
                },
            )
        chat_id = conn.private_chat_id

    test_text = (
        f"🔔 <b>Thông báo thử nghiệm từ Auth Gateway</b>\n\n"
        f"Xin chào <b>{current_user.name}</b>, kết nối Telegram của bạn đang hoạt động bình thường!"
    )
    telegram_client.send_message(chat_id, test_text)
    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
        "Content-Type", ""
    )
    if is_html:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return {"status": "success", "message": "Đã gửi tin nhắn thử nghiệm thành công"}


@router.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None),
    telegram_service: TelegramService = Depends(get_telegram_service),
):
    settings: Settings = request.app.state.settings

    # 1. Verify Secret Token
    if (
        not settings.TELEGRAM_WEBHOOK_SECRET
        or x_telegram_bot_api_secret_token != settings.TELEGRAM_WEBHOOK_SECRET
    ):
        _logger.warning("Rejected telegram webhook: Invalid secret token.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Invalid Telegram Webhook Secret Token"},
        )

    # 2. Parse payload
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_JSON", "message": f"Malformed JSON: {e}"},
        ) from e

    # 3. Ingest into queue and ACK immediately
    return telegram_service.ingest_update(payload)
