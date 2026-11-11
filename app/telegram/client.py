import logging
from typing import Any

import httpx

from app.telegram.text import plain_text

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


def normalize_agent_response(payload: Any) -> dict[str, Any]:
    """Validate the delivery contract without accepting paths or remote file URLs."""
    if isinstance(payload, list):
        payload = {"messages": payload}
    if not isinstance(payload, dict):
        raise ValueError("AI response must be an object or a legacy message list")
    messages = payload.get("messages", [])
    attachments = payload.get("attachments", [])
    if not isinstance(messages, list) or not all(isinstance(m, str) for m in messages):
        raise ValueError("AI response messages must be a list of strings")
    if not isinstance(attachments, list) or len(attachments) > 10:
        raise ValueError("AI response attachments must be a list of at most 10 artifacts")
    normalized = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("Invalid attachment descriptor")
        artifact_id = attachment.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id or len(artifact_id) > 128:
            raise ValueError("Attachment requires an internal artifact_id")
        if set(attachment) - {"artifact_id", "filename", "content_type"}:
            raise ValueError("Attachment accepts only an internal artifact reference")
        normalized.append({"artifact_id": artifact_id})
    return {"messages": [plain_text(message) for message in messages], "attachments": normalized}


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
        parse_mode: str = "",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        if not self.bot_token:
            _logger.warning("Telegram Bot Token is not configured. Skipping send_message.")
            return {}

        # Retain parse_mode in the signature for old callers; all transport text is plain.
        url = self._get_url("sendMessage")
        payload = {
            "chat_id": chat_id,
            "text": plain_text(text),
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

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str = "",
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Upload trusted artifact bytes; never ask Telegram to fetch a URL or path."""
        if not self.bot_token:
            raise TelegramError("Telegram Bot Token is not configured")
        if not isinstance(content, bytes) or not content or len(content) > 49 * 1024 * 1024:
            raise TelegramError("Document must contain between 1 byte and 49 MiB")
        safe_name = filename.replace("\\", "/").split("/")[-1]
        safe_name = "".join(ch for ch in safe_name if ch.isprintable())[:200] or "report.xlsx"
        try:
            response = httpx.post(
                self._get_url("sendDocument"),
                data={"chat_id": str(chat_id), "caption": plain_text(caption)[:1024]},
                files={"document": (safe_name, content, content_type)},
                timeout=30.0,
            )
            try:
                data = response.json()
            except ValueError as exc:
                raise TelegramDeliveryUnknownError("Telegram returned an unreadable upload result") from exc
            if not isinstance(data, dict):
                raise TelegramDeliveryUnknownError("Telegram returned an invalid upload result")
            if response.status_code == 403:
                raise BotBlockedError("Bot was blocked during document delivery")
            if response.status_code == 429:
                raise RateLimitError(
                    "Telegram rate limited document delivery",
                    retry_after=data.get("parameters", {}).get("retry_after", 5),
                )
            if not data.get("ok"):
                raise TelegramError(f"Telegram document delivery rejected: {data.get('description', 'Unknown error')}")
            result = data.get("result")
            if not isinstance(result, dict) or not result.get("message_id"):
                raise TelegramDeliveryUnknownError("Telegram upload lacks a confirmed message identifier")
            return result
        except httpx.TransportError as exc:
            raise TelegramDeliveryUnknownError("Network error during document delivery") from exc

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

