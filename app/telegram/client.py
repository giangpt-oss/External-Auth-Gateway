import logging
from typing import Any

import httpx

_logger = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


class TelegramDeliveryUnknownError(TelegramError):
    pass


class BotBlockedError(TelegramError):
    pass


class RateLimitError(TelegramError):
    def __init__(self, message: str, retry_after: int = 5):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramClient:
    API_BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str):
        self.bot_token = bot_token.strip() if bot_token else ""

    def _get_url(self, method: str) -> str:
        return f"{self.API_BASE}/bot{self.bot_token}/{method}"

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        if not self.bot_token:
            _logger.warning("Telegram Bot Token is not configured. Skipping send_message.")
            return {}

        url = self._get_url("sendMessage")
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }

        try:
            resp = httpx.post(url, json=payload, timeout=15.0)
            data = resp.json()

            if resp.status_code == 403:
                raise BotBlockedError(
                    f"Bot was blocked by user {chat_id}: {data.get('description')}"
                )
            if resp.status_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                raise RateLimitError(
                    f"Telegram rate limited: {data.get('description')}", retry_after=retry_after
                )

            if not data.get("ok"):
                raise TelegramError(
                    f"Telegram API Error: {data.get('description', 'Unknown error')}"
                )

            return data.get("result", {})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise TelegramDeliveryUnknownError(f"Network error connecting to Telegram: {e}") from e

    def send_chat_action(
        self,
        chat_id: int | str,
        action: str = "typing",
    ) -> bool:
        if not self.bot_token:
            return False
        url = self._get_url("sendChatAction")
        payload = {"chat_id": chat_id, "action": action}
        try:
            resp = httpx.post(url, json=payload, timeout=5.0)
            data = resp.json()
            return bool(data.get("ok"))
        except Exception as e:
            _logger.debug("Failed to sendChatAction %s to chat %s: %s", action, chat_id, e)
            return False

