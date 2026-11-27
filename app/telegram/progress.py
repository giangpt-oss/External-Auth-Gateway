"""Live in-place Telegram progress checklist & typing keepalive while AI Agent / Odoo is working."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

_logger = logging.getLogger(__name__)


def infer_progress_stages(text: str) -> tuple[str, list[str]]:
    """Choose a concise Vietnamese title + 3-step checklist matching the user's request."""
    clean = (text or "").strip()
    lower = clean.casefold()

    if lower.startswith("/confirm"):
        return "⚙️ Đang thực thi thao tác đã xác nhận...", [
            "Xác thực yêu cầu & kiểm tra phiên bản",
            "Gửi lệnh thực thi lên Odoo",
            "Đọc lại & xác minh kết quả",
        ]
    if lower.startswith(("/purchase_approve", "/purchase_grant", "/purchase_reject", "/quote_approve", "/quote_grant")):
        return "⚙️ Đang xử lý phê duyệt...", [
            "Kiểm tra quyền & trạng thái chứng từ",
            "Cập nhật phê duyệt trên Odoo",
            "Gửi thông báo kết quả",
        ]
    if re.search(r"báo cáo|bao cao|thống kê|thong ke", lower):
        return "📊 Đang lập báo cáo...", [
            "Phân tích phạm vi & bộ lọc báo cáo",
            "Truy vấn dữ liệu từ Odoo",
            "Tổng hợp số liệu & xuất file",
        ]
    if re.search(r"giao hàng|giao hang|phiếu giao|phieu giao|nhập kho|nhap kho|xuất kho|xuat kho|phiếu kho|phieu kho|wh/(?:out|in|int)", lower):
        return "📦 Đang xử lý nghiệp vụ kho / giao hàng...", [
            "Phân tích yêu cầu phiếu kho",
            "Tra cứu phiếu & tồn kho trên Odoo",
            "Chuẩn bị bản xem trước thao tác",
        ]
    if re.search(r"mua hàng|mua hang|đơn mua|don mua|rfq|nhà cung cấp|nha cung cap|\bp\d{3,}\b", lower):
        return "🛒 Đang xử lý nghiệp vụ mua hàng...", [
            "Phân tích yêu cầu đơn mua / RFQ",
            "Tra cứu nhà cung cấp & sản phẩm trên Odoo",
            "Chuẩn bị bản xem trước & tính thuế/giá",
        ]
    if re.search(r"báo giá|bao gia|\bs\d{3,}\b", lower):
        return "🧾 Đang xử lý báo giá...", [
            "Phân tích thông tin báo giá",
            "Tra cứu khách hàng & sản phẩm trên Odoo",
            "Chuẩn bị bản xem trước / PDF",
        ]
    return "⚙️ Đang xử lý yêu cầu...", [
        "Phân tích yêu cầu của bạn",
        "Tra cứu dữ liệu & thực thi công cụ",
        "Tổng hợp câu trả lời",
    ]


def render_progress_message(title: str, stages: list[str], elapsed_s: float) -> str:
    """Show the intended steps and elapsed time until the actual result is available."""
    secs = max(0, int(elapsed_s))
    lines = [title, f"Đã chờ {secs}s"]
    lines.extend(f"• {label}" for label in stages)

    if secs >= 15:
        lines.append(f"⏱️ Hệ thống AI/Odoo đang phản hồi hơi chậm ({secs}s), vui lòng đợi thêm chút nhé...")
    return "\n".join(lines)


class TurnProgressTracker:
    """Context manager that keeps Telegram 'typing' alive and edits a single live progress message.

    - Fast turns (< initial_delay_s) never send a temporary message (zero flicker).
    - Longer turns send 1 checklist message and edit it every tick_s seconds.
    - On exit, the temporary message is cleanly deleted right before final delivery.
    """

    def __init__(
        self,
        telegram_client: Any,
        chat_id: int | str,
        text_content: str,
        *,
        initial_delay_s: float = 1.8,
        tick_s: float = 4.0,
    ) -> None:
        self._client = telegram_client
        self._chat_id = chat_id
        self._title, self._stages = infer_progress_stages(text_content)
        self._initial_delay_s = initial_delay_s
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._message_id: int | None = None
        self._last_text: str = ""
        self._started_at: float = 0.0

    def __enter__(self) -> TurnProgressTracker:
        self._started_at = time.monotonic()
        try:
            self._client.send_chat_action(self._chat_id, "typing")
        except Exception as exc:
            _logger.debug("Initial typing action failed: %s", exc)
        self._thread = threading.Thread(target=self._run, name="tg-progress", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            if self._stop.wait(self._initial_delay_s):
                return
            while not self._stop.is_set():
                elapsed = time.monotonic() - self._started_at
                text = render_progress_message(self._title, self._stages, elapsed)
                try:
                    self._client.send_chat_action(self._chat_id, "typing")
                except Exception:
                    pass
                try:
                    if self._message_id is None:
                        res = self._client.send_message(self._chat_id, text)
                        if isinstance(res, dict) and res.get("message_id"):
                            self._message_id = int(res["message_id"])
                            self._last_text = text
                    elif text != self._last_text:
                        self._client.edit_message_text(self._chat_id, self._message_id, text)
                        self._last_text = text
                except Exception as exc:
                    _logger.debug("Progress update failed: %s", exc)
                if self._stop.wait(self._tick_s):
                    break
        finally:
            if self._message_id is not None:
                try:
                    self._client.delete_message(self._chat_id, self._message_id)
                except Exception as exc:
                    _logger.debug("Could not delete progress message %s: %s", self._message_id, exc)
                self._message_id = None

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
