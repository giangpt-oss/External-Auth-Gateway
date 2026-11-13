import asyncio
import enum
import json
import logging
import random
import time
from typing import Any, Optional

import httpx

from app.google.oauth import GoogleOAuthService
from app.workspace.network_slots import outbound_slot

logger = logging.getLogger(__name__)


class GoogleErrorCode(str, enum.Enum):
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RECONNECT_REQUIRED = "RECONNECT_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMIT = "RATE_LIMIT"
    SERVICE_TEMPORARY = "SERVICE_TEMPORARY"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    NETWORK_ERROR = "NETWORK_ERROR"


class GoogleApiError(Exception):
    def __init__(
        self,
        error_code: GoogleErrorCode,
        message: str,
        status_code: Optional[int] = None,
        retryable: bool = False,
        retry_after: Optional[float] = None,
        provider: str = "google_rest",
        raw_error: Optional[Any] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.provider = provider
        self.raw_error = raw_error


class UnifiedGoogleClient:
    """Lớp kết nối Google Workspace đồng nhất, tối ưu đa người dùng.

    Đặc tính:
    - Quản lý vòng đời HTTP client với Connection Pool (HTTPX).
    - Rate limiter per-user: tối đa 2 đọc và 1 ghi đồng thời.
    - Global concurrency limiter: tối đa 8 yêu cầu Google đồng thời trên mỗi worker.
    - Tự động đính kèm bearer token và force refresh khi nhận 401.
    - Thử lại thao tác đọc tối đa 3 lần với exponential backoff + jitter và tuân thủ Retry-After.
    - Tuyệt đối không tự động thử lại thao tác ghi khi kết quả chưa rõ (chuyển sang UNKNOWN_RESULT).
    - Giới hạn dữ liệu phản hồi tối đa 8 MiB.
    """
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MiB

    def __init__(
        self,
        oauth_service: GoogleOAuthService,
        max_global_concurrency: int = 8,
        max_user_reads: int = 2,
        max_user_writes: int = 1,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.oauth_service = oauth_service
        self.global_semaphore = asyncio.Semaphore(max_global_concurrency)
        self.max_user_reads = max_user_reads
        self.max_user_writes = max_user_writes
        self._user_read_semaphores: dict[int, asyncio.Semaphore] = {}
        self._user_write_semaphores: dict[int, asyncio.Semaphore] = {}
        self._http_client = http_client or httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0),
            timeout=120.0,
        )

    def get_user_google_connection(self, user_id: int):
        with self.oauth_service.session_factory() as db:
            from app.models import GoogleConnection
            return db.query(GoogleConnection).filter_by(user_id=user_id).first()

    def _get_user_semaphore(self, user_id: int, is_write: bool) -> asyncio.Semaphore:
        if is_write:
            if user_id not in self._user_write_semaphores:
                self._user_write_semaphores[user_id] = asyncio.Semaphore(self.max_user_writes)
            return self._user_write_semaphores[user_id]
        else:
            if user_id not in self._user_read_semaphores:
                self._user_read_semaphores[user_id] = asyncio.Semaphore(self.max_user_reads)
            return self._user_read_semaphores[user_id]

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def request(
        self,
        user_id: int,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        content: Optional[bytes] = None,
        is_write: bool = False,
        timeout: float = 120.0,
    ) -> Any:
        user_sem = self._get_user_semaphore(user_id, is_write)
        max_attempts = 1 if is_write else 3
        attempt = 0
        refreshed_on_401 = False

        while attempt < max_attempts:
            attempt += 1
            async with self.global_semaphore:
                async with user_sem:
                    # Lấy access token (force_refresh nếu lần trước bị 401)
                    if refreshed_on_401:
                        token = self.oauth_service.get_valid_access_token(user_id, force_refresh=True)
                    else:
                        token = self.oauth_service.get_valid_access_token(user_id)

                    req_headers = dict(headers or {})
                    req_headers["Authorization"] = f"Bearer {token}"

                    req = self._http_client.build_request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_data,
                        content=content,
                        headers=req_headers,
                        timeout=timeout,
                    )

                    try:
                        resp = await self._http_client.send(req)
                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        if is_write:
                            raise GoogleApiError(
                                error_code=GoogleErrorCode.UNKNOWN_RESULT,
                                message=f"Lỗi mạng hoặc timeout khi thực hiện thao tác ghi Google: {exc}",
                                status_code=None,
                                retryable=False,
                                raw_error=exc,
                            ) from exc
                        if attempt < max_attempts:
                            backoff = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                            await asyncio.sleep(backoff)
                            continue
                        raise GoogleApiError(
                            error_code=GoogleErrorCode.NETWORK_ERROR,
                            message=f"Lỗi mạng khi kết nối Google sau {max_attempts} lần thử: {exc}",
                            status_code=None,
                            retryable=True,
                            raw_error=exc,
                        ) from exc

                    # 1. Xử lý 401 Unauthorized -> Force Refresh đúng 1 lần
                    if resp.status_code == 401:
                        if not refreshed_on_401:
                            refreshed_on_401 = True
                            # Thử lại ngay lập tức với token mới
                            continue
                        raise GoogleApiError(
                            error_code=GoogleErrorCode.AUTH_EXPIRED,
                            message="Phiên xác thực Google đã hết hạn hoặc bị thu hồi. Vui lòng kết nối lại trên Portal.",
                            status_code=401,
                            retryable=False,
                            raw_error=resp.text,
                        )

                    # 2. Xử lý 429 Rate Limit hoặc 5xx Server Error
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if is_write:
                            err_code = GoogleErrorCode.UNKNOWN_RESULT if resp.status_code >= 500 else GoogleErrorCode.RATE_LIMIT
                            raise GoogleApiError(
                                error_code=err_code,
                                message=f"Google trả mã lỗi {resp.status_code} cho thao tác ghi; không tự động thử lại để bảo vệ tính nhất quán.",
                                status_code=resp.status_code,
                                retryable=False,
                                raw_error=resp.text,
                            )
                        if attempt < max_attempts:
                            retry_after_str = resp.headers.get("Retry-After")
                            try:
                                retry_after = float(retry_after_str) if retry_after_str else None
                            except ValueError:
                                retry_after = None
                            backoff = retry_after if retry_after is not None else (0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.25))
                            await asyncio.sleep(backoff)
                            continue
                        err_code = GoogleErrorCode.RATE_LIMIT if resp.status_code == 429 else GoogleErrorCode.SERVICE_TEMPORARY
                        raise GoogleApiError(
                            error_code=err_code,
                            message=f"Google tạm thời quá tải hoặc gặp sự cố nội bộ ({resp.status_code}).",
                            status_code=resp.status_code,
                            retryable=True,
                            raw_error=resp.text,
                        )

                    # 3. Xử lý 403 Forbidden / Quota Exceeded
                    if resp.status_code == 403:
                        err_text = resp.text.lower()
                        is_quota = "quota" in err_text or "ratelimitexceeded" in err_text
                        err_code = GoogleErrorCode.QUOTA_EXCEEDED if is_quota else GoogleErrorCode.PERMISSION_DENIED
                        msg = "Tài khoản Google đã vượt hạn mức quota." if is_quota else "Chưa được cấp quyền truy cập tài nguyên Google này."
                        raise GoogleApiError(
                            error_code=err_code,
                            message=msg,
                            status_code=403,
                            retryable=False,
                            raw_error=resp.text,
                        )

                    # 4. Xử lý các mã lỗi client khác (404, 409, 412...)
                    if resp.status_code == 404:
                        raise GoogleApiError(
                            error_code=GoogleErrorCode.NOT_FOUND,
                            message="Tài nguyên Google được yêu cầu không tồn tại.",
                            status_code=404,
                            retryable=False,
                            raw_error=resp.text,
                        )
                    if resp.status_code in (409, 412):
                        raise GoogleApiError(
                            error_code=GoogleErrorCode.CONFLICT,
                            message="Xung đột phiên bản dữ liệu (ETag / Precondition failed) trên Google.",
                            status_code=resp.status_code,
                            retryable=False,
                            raw_error=resp.text,
                        )
                    if resp.status_code >= 400:
                        raise GoogleApiError(
                            error_code=GoogleErrorCode.SERVICE_TEMPORARY,
                            message=f"Google trả mã lỗi HTTP {resp.status_code}.",
                            status_code=resp.status_code,
                            retryable=False,
                            raw_error=resp.text,
                        )

                    # 5. Kiểm tra giới hạn dung lượng phản hồi (tối đa 8 MiB)
                    resp_bytes = resp.content
                    if len(resp_bytes) > self.MAX_RESPONSE_BYTES:
                        raise GoogleApiError(
                            error_code=GoogleErrorCode.RESPONSE_TOO_LARGE,
                            message=f"Dữ liệu Google trả về ({len(resp_bytes)} bytes) vượt quá giới hạn cho phép 8 MiB.",
                            status_code=resp.status_code,
                            retryable=False,
                        )

                    if not resp_bytes:
                        return {}

                    content_type = resp.headers.get("content-type", "")
                    if "json" in content_type:
                        try:
                            return resp.json()
                        except Exception:
                            return json.loads(resp_bytes.decode("utf-8", errors="replace"))

                    return resp_bytes


class GoogleClient:
    CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
    DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
    SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, oauth_service: GoogleOAuthService):
        self.oauth_service = oauth_service
        self.unified_client = UnifiedGoogleClient(oauth_service=oauth_service)

    def _request(
        self,
        user_id: int,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry_on_401: bool = True,
    ) -> httpx.Response:
        token = self.oauth_service.get_valid_access_token(user_id)
        req_headers = headers.copy() if headers else {}
        req_headers["Authorization"] = f"Bearer {token}"

        with outbound_slot(user_id, write=method not in {"GET", "HEAD"}):
            resp = httpx.request(
                method, url, params=params, json=json_data, headers=req_headers, timeout=25.0
            )

        # Retry once on 401 if token expired unexpectedly
        if resp.status_code == 401 and retry_on_401:
            # Force refresh by expiring the token in memory/DB or calling get_valid_access_token
            # In our implementation, we can retry with a refreshed token
            try:
                # Discard current access token and re-fetch
                token = self.oauth_service.get_valid_access_token(user_id)
                req_headers["Authorization"] = f"Bearer {token}"
                with outbound_slot(user_id, write=method not in {"GET", "HEAD"}):
                    resp = httpx.request(
                        method, url, params=params, json=json_data, headers=req_headers, timeout=25.0
                    )
            except Exception:
                pass

        return resp

    # --- Calendar API ---
    def list_calendar_events(
        self,
        user_id: int,
        calendar_id: str = "primary",
        time_min: str | None = None,
        time_max: str | None = None,
        page_token: str | None = None,
        single_events: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "singleEvents": "true" if single_events else "false",
            "maxResults": 250,
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        if page_token:
            params["pageToken"] = page_token

        url = f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events"
        resp = self._request(user_id, "GET", url, params=params)
        resp.raise_for_status()
        return resp.json()

    def create_calendar_event(
        self,
        user_id: int,
        calendar_id: str = "primary",
        event_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events"
        resp = self._request(user_id, "POST", url, json_data=event_data)
        resp.raise_for_status()
        return resp.json()

    def update_calendar_event(
        self,
        user_id: int,
        calendar_id: str,
        event_id: str,
        event_data: dict[str, Any],
        etag: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}"
        headers = {}
        if etag:
            headers["If-Match"] = etag
        resp = self._request(user_id, "PATCH", url, json_data=event_data, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_calendar_event(
        self,
        user_id: int,
        calendar_id: str,
        event_id: str,
    ) -> dict[str, Any]:
        url = f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}"
        resp = self._request(user_id, "GET", url)
        resp.raise_for_status()
        return resp.json()

    def delete_calendar_event(
        self,
        user_id: int,
        calendar_id: str = "primary",
        event_id: str = "",
    ) -> bool:
        url = f"{self.CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}"
        resp = self._request(user_id, "DELETE", url)
        if resp.status_code in (200, 204, 404, 410):
            return True
        resp.raise_for_status()
        return True

    # --- Internal Drive & Sheets Helpers ---
    def upload_drive_file(
        self,
        user_id: int,
        name: str,
        content_bytes: bytes,
        mime_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        # Upload using Drive multipart or upload API
        url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=media"
        headers = {"Content-Type": mime_type}
        token = self.oauth_service.get_valid_access_token(user_id)
        headers["Authorization"] = f"Bearer {token}"
        with outbound_slot(user_id, write=True):
            resp = httpx.post(url, content=content_bytes, headers=headers, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def append_sheet_row(
        self,
        user_id: int,
        spreadsheet_id: str,
        range_name: str,
        values: list[Any],
    ) -> dict[str, Any]:
        url = f"{self.SHEETS_API_BASE}/{spreadsheet_id}/values/{range_name}:append?valueInputOption=USER_ENTERED"
        resp = self._request(user_id, "POST", url, json_data={"values": [values]})
        resp.raise_for_status()
        return resp.json()

    # --- Gmail API ---
    def get_user_google_connection(self, user_id: int):
        from app.models import GoogleConnection

        with self.oauth_service.session_factory() as db:
            return db.query(GoogleConnection).filter_by(user_id=user_id).first()

    def check_has_scope(self, user_id: int, scope_fragment: str) -> bool:
        conn = self.get_user_google_connection(user_id)
        if not conn or not conn.scopes:
            return False
        return any(scope_fragment in s for s in conn.scopes.split())

    def list_unread_emails(self, user_id: int, max_results: int = 5) -> dict[str, Any]:
        """Fetch unread emails for user. Checks scope, status, and handles errors."""
        conn = self.get_user_google_connection(user_id)
        if not conn or conn.status == "disconnected":
            return {"status": "NOT_CONNECTED", "total_unread": 0, "messages": []}
        if conn.status == "reconnect_required":
            return {"status": "RECONNECT_REQUIRED", "total_unread": 0, "messages": []}

        # Check actual granted scope
        if not self.check_has_scope(user_id, "gmail.readonly"):
            return {
                "status": "PERMISSION_DENIED",
                "total_unread": 0,
                "messages": [],
                "instruction": "Vui lòng cấp thêm quyền đọc Gmail qua Cổng Portal.",
            }

        gmail_base = "https://gmail.googleapis.com/gmail/v1/users/me"
        try:
            # 1. Get messages
            resp = self._request(
                user_id,
                "GET",
                f"{gmail_base}/messages",
                params={"q": "in:inbox is:unread", "maxResults": max_results},
            )
            if resp.status_code == 403:
                return {
                    "status": "PERMISSION_DENIED",
                    "total_unread": 0,
                    "messages": [],
                    "instruction": "Vui lòng cấp thêm quyền đọc Gmail qua Cổng Portal.",
                }
            resp.raise_for_status()
            data = resp.json()

            msg_items = data.get("messages", [])

            # Accurate unread count strictly from INBOX label
            accurate_total = None
            try:
                lbl_resp = self._request(user_id, "GET", f"{gmail_base}/labels/INBOX")
                if lbl_resp.status_code == 200:
                    lbl_data = lbl_resp.json()
                    accurate_total = lbl_data.get("messagesUnread")
            except Exception:
                pass

            messages = []
            for item in msg_items[:max_results]:
                msg_id = item["id"]
                try:
                    detail_resp = self._request(
                        user_id,
                        "GET",
                        f"{gmail_base}/messages/{msg_id}",
                        params={
                            "format": "metadata",
                            "metadataHeaders": ["From", "Subject", "Date"],
                        },
                    )
                    if detail_resp.status_code == 200:
                        m_data = detail_resp.json()
                        headers = {
                            h.get("name", "").lower(): h.get("value", "")
                            for h in m_data.get("payload", {}).get("headers", [])
                        }
                        messages.append(
                            {
                                "id": msg_id,
                                "from": headers.get("from", "Không rõ người gửi"),
                                "subject": headers.get("subject", "Không có tiêu đề"),
                                "date": headers.get("date", ""),
                                "link": f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
                            }
                        )
                except Exception:
                    continue

            return {
                "status": "OK",
                "total_unread": accurate_total,
                "messages": messages,
            }
        except Exception as e:
            from app.google.oauth import GoogleAuthRevokedError

            if isinstance(e, GoogleAuthRevokedError):
                return {"status": "RECONNECT_REQUIRED", "total_unread": 0, "messages": []}
            return {"status": "ERROR", "total_unread": 0, "messages": [], "message": str(e)}

    def send_email(
        self,
        user_id: int,
        to: list[str] | str,
        subject: str,
        body_text: str,
    ) -> dict[str, Any]:
        """Gửi email qua Gmail API dưới tài khoản Google đã liên kết của người dùng."""
        import base64
        from email.mime.text import MIMEText

        to_str = ", ".join(to) if isinstance(to, list) else to
        msg = MIMEText(body_text, _charset="utf-8")
        msg["To"] = to_str
        msg["Subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        resp = self._request(user_id, "POST", url, json_data={"raw": raw})
        if resp.status_code == 403:
            return {
                "status": "PERMISSION_DENIED",
                "message": "Chưa được cấp quyền gửi Gmail (cần scope gmail.send). Vui lòng mở Portal để cấp thêm quyền.",
            }
        resp.raise_for_status()
        data = resp.json()
        return {"status": "OK", "id": data.get("id"), "thread_id": data.get("threadId")}
