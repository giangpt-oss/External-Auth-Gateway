import base64
import html
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import qrcode
from sqlalchemy.orm import sessionmaker

from app.models import LinkToken, OdooCredential, TelegramConnection, User
from app.odoo.client import OdooAccessError, OdooClient, OdooError
from app.security import decrypt_secret, ensure_utc, generate_secure_token, hash_token
from app.telegram.client import TelegramClient, normalize_agent_response

_logger = logging.getLogger(__name__)


@dataclass
class TelegramLink:
    url: str
    qr_code_data_uri: str
    expires_at: datetime


class TelegramService:
    def __init__(
        self,
        session_factory: sessionmaker,
        telegram_client: TelegramClient,
        bot_username: str,
        webhook_secret: str,
        odoo_client: OdooClient | None = None,
        encryption_key: str = "",
        odoo_base_url: str = "",
        enable_ai_forwarding: bool = False,
        ai_agent_base_url: str = "http://localhost:8000",
        gateway_to_agent_secret: str = "",
    ):
        self.enable_ai_forwarding = enable_ai_forwarding
        self.ai_agent_base_url = ai_agent_base_url.rstrip("/")
        self.gateway_to_agent_secret = gateway_to_agent_secret
        self.session_factory = session_factory
        self.telegram_client = telegram_client
        self.bot_username = bot_username.lstrip("@").strip()
        self.webhook_secret = webhook_secret
        self.odoo_client = odoo_client
        self.encryption_key = encryption_key
        self.odoo_base_url = odoo_base_url.rstrip("/")

        # Rate limiter: user_id -> list of query timestamps (max 10 / min)
        self._crm_rate_limits: dict[int, list[datetime]] = {}

    def issue_link(self, user_id: int) -> TelegramLink:
        raw_token = generate_secure_token(24)
        token_hash = hash_token(raw_token)
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=10)

        # Invalidate / delete old unused telegram_link tokens for this user
        with self.session_factory() as db:
            db.query(LinkToken).filter(
                LinkToken.user_id == user_id,
                LinkToken.purpose == "telegram_link",
                LinkToken.used_at.is_(None),
            ).delete()

            lt = LinkToken(
                token_hash=token_hash,
                purpose="telegram_link",
                user_id=user_id,
                expires_at=expires_at,
            )
            db.add(lt)
            db.commit()

        link_url = f"https://t.me/{self.bot_username}?start={raw_token}"

        # Generate QR code locally (strictly in-process, no external API)
        qr_img = qrcode.make(link_url)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        qr_data_uri = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"

        return TelegramLink(
            url=link_url,
            qr_code_data_uri=qr_data_uri,
            expires_at=expires_at,
        )

    def _set_update_status(self, update_id: int, status: str) -> None:
        with self.session_factory() as db:
            from app.models import TelegramUpdate

            db.query(TelegramUpdate).filter_by(update_id=update_id).update(
                {
                    "status": status,
                    "updated_at": datetime.now(UTC),
                }
            )
            db.commit()

    def ingest_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Ingest update idempotently into telegram_updates queue.
        Gateway commands (/start, /help, /status, /crm) are processed immediately.
        Conversational AI turns are stored with status='pending' (or rejected if forwarding disabled).
        """
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"status": "ignored", "reason": "No message"}

        update_id = update.get("update_id")
        if not update_id or not isinstance(update_id, int):
            return {"status": "ignored", "reason": "Invalid or missing update_id"}

        chat = message.get("chat", {})
        from_user = message.get("from", {})
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()

        if (
            not chat_id
            or not text
            or chat.get("type") != "private"
            or from_user.get("is_bot", False)
        ):
            return {"status": "ignored", "reason": "Non-private or bot message"}

        # Classify command BEFORE committing to queue
        telegram_user_id = from_user.get("id")
        username = from_user.get("username")
        cmd_tokens = text.split() if text else []
        cmd_part = cmd_tokens[0].lower() if cmd_tokens else ""
        base_cmd = cmd_part.split("@")[0] if cmd_part.startswith("/") else ""

        is_gw_cmd = False
        if base_cmd in {"/start", "/help", "/verify"} or (base_cmd == "/status" and len(cmd_tokens) == 1):
            is_gw_cmd = True
        elif base_cmd == "/crm":
            sub_cmd = cmd_tokens[1].lower() if len(cmd_tokens) > 1 else ""
            if sub_cmd in {"search", "create", "update"}:
                is_gw_cmd = False
            else:
                is_gw_cmd = True

        if is_gw_cmd:
            initial_status = "gateway_command"
        elif not self.enable_ai_forwarding:
            initial_status = "processed"
        else:
            initial_status = "pending"

        # Atomic insert into telegram_updates using unique constraint on update_id
        import json

        from sqlalchemy.exc import IntegrityError

        from app.models import TelegramUpdate

        stored_update = update
        if base_cmd == "/verify":
            message_key = "message" if update.get("message") else "edited_message"
            stored_update = {**update, message_key: {**message, "text": "/verify"}}
        with self.session_factory() as db:
            try:
                db_update = TelegramUpdate(
                    update_id=update_id,
                    payload=json.dumps(stored_update, ensure_ascii=False),
                    status=initial_status,
                    delivery_status="none",
                    attempts=0,
                )
                db.add(db_update)
                db.commit()
            except IntegrityError:
                # Duplicate update_id: ignore idempotently
                return {"status": "ok", "duplicate": True}

        if is_gw_cmd:
            try:
                if base_cmd == "/start":
                    parts = text.split(maxsplit=1)
                    if len(parts) > 1:
                        token_arg = parts[1].strip()
                        self._handle_pairing(chat_id, telegram_user_id, username, token_arg)
                    else:
                        self._handle_start_help(chat_id, telegram_user_id)
                elif base_cmd == "/verify":
                    self._handle_browser_verification(chat_id, telegram_user_id, username, cmd_tokens)
                elif base_cmd == "/help":
                    self._handle_help(chat_id)
                elif base_cmd == "/status":
                    self._handle_status(chat_id, telegram_user_id)
                elif base_cmd == "/crm":
                    parts = text.split(maxsplit=1)
                    search_query = parts[1].strip() if len(parts) > 1 else None
                    self._handle_crm(chat_id, telegram_user_id, search_query)
                self._set_update_status(update_id, "processed")
            except Exception as e:
                _logger.error(
                    "Error executing gateway command %s for update %s: %s", base_cmd, update_id, e
                )
                self._set_update_status(update_id, "gateway_command_failed")
        else:
            # Conversational / AI Turn
            if not self.enable_ai_forwarding:
                self.telegram_client.send_message(
                    chat_id,
                    "Tính năng AI Agent hiện đang tạm dừng kết nối. Vui lòng thử lại sau hoặc kiểm tra kết nối tại cổng tài khoản.",
                )

        return {"status": "ok"}

    def process_update_dict(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat = message.get("chat", {})
        from_user = message.get("from", {})
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()

        if not chat_id or not text:
            return

        # We only accept commands from private chats with non-bots
        if chat.get("type") != "private" or from_user.get("is_bot", False):
            return

        telegram_user_id = from_user.get("id")
        username = from_user.get("username")

        # Normalize command (e.g. /crm@MyBot -> /crm)
        cmd_part = text.split()[0].lower()
        base_cmd = cmd_part.split("@")[0]

        if base_cmd == "/start":
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                token_arg = parts[1].strip()
                self._handle_pairing(chat_id, telegram_user_id, username, token_arg)
            else:
                self._handle_start_help(chat_id, telegram_user_id)
        elif base_cmd == "/verify":
            self._handle_browser_verification(chat_id, telegram_user_id, username, text.split())
        elif base_cmd == "/help":
            self._handle_help(chat_id)
        elif base_cmd == "/status" and len(text.split()) > 1:
            self._handle_ai_turn(update, chat_id, telegram_user_id, username, text)
        elif base_cmd == "/status":
            self._handle_status(chat_id, telegram_user_id)
        elif base_cmd == "/crm":
            parts = text.split()
            sub_cmd = parts[1].lower() if len(parts) > 1 else ""
            if sub_cmd in {"search", "create", "update"}:
                self._handle_ai_turn(update, chat_id, telegram_user_id, username, text)
            else:
                parts_legacy = text.split(maxsplit=1)
                search_query = parts_legacy[1].strip() if len(parts_legacy) > 1 else None
                self._handle_crm(chat_id, telegram_user_id, search_query)
        else:
            self._handle_ai_turn(update, chat_id, telegram_user_id, username, text)

    def _handle_browser_verification(
        self, chat_id: int, telegram_user_id: int, username: str | None, tokens: list[str],
    ) -> None:
        from app.telegram.web_verification import confirm_browser_code

        settings = getattr(self, "settings", None)
        if not settings or len(tokens) != 2:
            message = "Hãy sao chép lệnh /verify từ portal Odoo."
        else:
            message = confirm_browser_code(
                self.session_factory, settings, chat_id, telegram_user_id, username, tokens[1],
            )
        self.telegram_client.send_message(chat_id, message)

    def _handle_pairing(
        self,
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        token_arg: str,
    ) -> None:
        if getattr(getattr(self, "settings", None), "ODOO_EMPLOYEE_TELEGRAM_ID_FIELD", ""):
            self.telegram_client.send_message(
                chat_id, "Vui lòng xác minh tài khoản Telegram trên cổng Auth Gateway.",
            )
            return
        token_hash = hash_token(token_arg)
        now = datetime.now(UTC)

        with self.session_factory() as db:
            # 1. Check if token is valid and unused
            lt = (
                db.query(LinkToken)
                .filter(
                    LinkToken.token_hash == token_hash,
                    LinkToken.purpose == "telegram_link",
                    LinkToken.used_at.is_(None),
                )
                .first()
            )

            if not lt or ensure_utc(lt.expires_at) <= now:
                self.telegram_client.send_message(
                    chat_id,
                    "⚠️ <b>Mã liên kết không hợp lệ hoặc đã hết hạn!</b>\n\n"
                    "Vui lòng đăng nhập vào Cổng Auth Gateway trên trình duyệt và bấm <b>'Liên kết Telegram'</b> để lấy mã mới.",
                )
                return

            # 2. Check if this telegram_user_id is already linked to another Odoo user
            existing_conn = (
                db.query(TelegramConnection).filter_by(telegram_user_id=telegram_user_id).first()
            )
            if existing_conn and existing_conn.user_id != lt.user_id:
                self.telegram_client.send_message(
                    chat_id,
                    "⚠️ <b>Tài khoản Telegram này đã được liên kết với một nhân viên khác!</b>\n"
                    "Vui lòng ngắt liên kết cũ trước khi ghép nối tài khoản mới.",
                )
                return

            # 3. Mark token as used
            lt.used_at = now
            target_user = db.query(User).filter_by(id=lt.user_id).first()
            if not target_user:
                return
            target_user_name = target_user.name

            # 4. Upsert TelegramConnection with fresh binding_id
            import secrets
            fresh_binding_id = f"tg_bind_{secrets.token_hex(16)}"
            if existing_conn:
                existing_conn.private_chat_id = chat_id
                existing_conn.username = username
                existing_conn.binding_id = fresh_binding_id
                existing_conn.status = "connected"
            else:
                user_conn = db.query(TelegramConnection).filter_by(user_id=target_user.id).first()
                if user_conn:
                    user_conn.telegram_user_id = telegram_user_id
                    user_conn.private_chat_id = chat_id
                    user_conn.username = username
                    user_conn.binding_id = fresh_binding_id
                    user_conn.status = "connected"
                else:
                    db.add(
                        TelegramConnection(
                            user_id=target_user.id,
                            telegram_user_id=telegram_user_id,
                            private_chat_id=chat_id,
                            username=username,
                            binding_id=fresh_binding_id,
                            status="connected",
                        )
                    )
            db.commit()

        safe_name = html.escape(target_user_name)
        welcome_text = (
            f"🎉 <b>Liên kết thành công!</b>\n\n"
            f"Xin chào <b>{safe_name}</b>, tài khoản Telegram của bạn đã được kết nối an toàn với Odoo.\n\n"
            f"Bạn có thể trao đổi bằng lời bình thường để tra cứu dữ liệu, lập báo cáo hoặc thực hiện công việc. "
            f"Tôi sẽ hỏi thông tin còn thiếu và xin xác nhận trước khi thay đổi dữ liệu. Bạn muốn bắt đầu việc gì?"
        )
        try:
            self.telegram_client.send_message(chat_id, welcome_text)
        except Exception as e:
            _logger.warning(f"Could not send welcome message: {e}")

    def _handle_start_help(self, chat_id: int, telegram_user_id: int) -> None:
        with self.session_factory() as db:
            conn = (
                db.query(TelegramConnection)
                .filter_by(telegram_user_id=telegram_user_id, status="connected")
                .first()
            )
            if conn:
                user = db.query(User).filter_by(id=conn.user_id).first()
                user_name = user.name if user else "bạn"
                msg = (
                    f"👋 Xin chào <b>{html.escape(user_name)}</b>! Bạn đã liên kết với hệ thống Odoo.\n\n"
                    f"Bạn muốn tra cứu thông tin hay thực hiện công việc gì? Hãy mô tả yêu cầu của bạn."
                )
            else:
                msg = (
                    "👋 Xin chào! Để liên kết tài khoản Odoo của bạn với bot này, "
                    "vui lòng đăng nhập vào Cổng Portal Auth Gateway trên trình duyệt và bấm <b>'Liên kết Telegram'</b>."
                )
        try:
            self.telegram_client.send_message(chat_id, msg)
        except Exception as e:
            _logger.warning(f"Could not send start help message: {e}")

    def _handle_help(self, chat_id: int) -> None:
        help_text = (
            "Tôi có thể hỗ trợ tra cứu dữ liệu Odoo, lập báo cáo, báo giá và công việc theo quyền tài khoản. "
            "Bạn chỉ cần mô tả điều muốn làm; tôi sẽ hỏi thông tin còn thiếu và cho xem trước trước khi thay đổi dữ liệu."
        )
        self.telegram_client.send_message(chat_id, help_text)

    def _handle_status(self, chat_id: int, telegram_user_id: int) -> None:
        with self.session_factory() as db:
            conn = (
                db.query(TelegramConnection)
                .filter_by(telegram_user_id=telegram_user_id, status="connected")
                .first()
            )
            if not conn:
                self.telegram_client.send_message(
                    chat_id,
                    "⚠️ Bạn chưa liên kết tài khoản Odoo. Vui lòng truy cập Portal để kết nối.",
                )
                return

            user = db.query(User).filter_by(id=conn.user_id).first()
            google_conn = user.google_connection if user else None
            g_status = (
                "Đã kết nối (" + google_conn.email + ")"
                if (google_conn and google_conn.status == "connected")
                else "Chưa kết nối"
            )

            status_text = (
                f"👤 <b>Hồ sơ liên kết:</b>\n"
                f"• Nhân viên: <b>{html.escape(user.name)}</b>\n"
                f"• Email Odoo: <code>{html.escape(user.login)}</code>\n"
                f"• Google Workspace: {html.escape(g_status)}\n"
                f"• Telegram: Đã liên kết (@{html.escape(conn.username or 'N/A')})"
            )
        self.telegram_client.send_message(chat_id, status_text)

    def _handle_crm(self, chat_id: int, telegram_user_id: int, search_query: str | None) -> None:
        # 1. Resolve user and check connection
        with self.session_factory() as db:
            conn = (
                db.query(TelegramConnection)
                .filter_by(telegram_user_id=telegram_user_id, status="connected")
                .first()
            )
            if not conn:
                self.telegram_client.send_message(
                    chat_id,
                    "⚠️ Bạn chưa liên kết tài khoản Odoo. Vui lòng đăng nhập Cổng Portal để liên kết trước khi tra cứu CRM.",
                )
                return

            user = db.query(User).filter_by(id=conn.user_id).first()
            if not user or not user.active:
                self.telegram_client.send_message(
                    chat_id, "⚠️ Tài khoản người dùng đã bị vô hiệu hóa."
                )
                return

            user_id = user.id
            odoo_uid = user.odoo_uid

            cred = db.query(OdooCredential).filter_by(user_id=user_id).first()
            if not cred or not cred.encrypted_secret:
                self.telegram_client.send_message(
                    chat_id,
                    "⚠️ Chưa có thông tin xác thực Odoo của bạn. Vui lòng đăng nhập lại vào Portal.",
                )
                return

            secret = decrypt_secret(cred.encrypted_secret, self.encryption_key)

        # 2. Rate limiting check (max 10 queries per minute per user)
        now = datetime.now(UTC)
        window_start = now - timedelta(minutes=1)
        history = self._crm_rate_limits.get(user_id, [])
        valid_history = [t for t in history if t > window_start]
        self._crm_rate_limits[user_id] = valid_history

        if len(valid_history) >= 10:
            self.telegram_client.send_message(
                chat_id,
                "⚠️ <b>Bạn thao tác quá nhanh!</b> Giới hạn tra cứu là 10 lần/phút. Vui lòng chờ giây lát.",
            )
            return

        self._crm_rate_limits[user_id].append(now)

        # 3. Query Odoo via OdooClient using employee's credentials
        if not self.odoo_client:
            self.telegram_client.send_message(chat_id, "⚠️ Dịch vụ Odoo chưa sẵn sàng.")
            return

        domain: list[Any] = [
            ("user_id", "=", odoo_uid),
            ("active", "=", True),
        ]
        if search_query:
            clean_search = search_query.strip()[:100]
            domain.append(("name", "ilike", clean_search))

        fields = ["id", "name", "stage_id"]
        order = "write_date desc, id desc"
        limit = 5

        try:
            records = self.odoo_client.execute(
                odoo_uid,
                secret,
                "crm.lead",
                "search_read",
                [domain],
                {"fields": fields, "order": order, "limit": limit},
            )
        except OdooAccessError:
            self.telegram_client.send_message(
                chat_id,
                "⚠️ <b>Không có quyền truy cập CRM:</b> Tài khoản Odoo của bạn không được cấp quyền đọc cơ hội bán hàng.",
            )
            return
        except OdooError as e:
            _logger.error(f"Error querying CRM for user {user_id}: {e}")
            self.telegram_client.send_message(
                chat_id,
                "⚠️ Có lỗi xảy ra khi kết nối đến máy chủ Odoo. Vui lòng thử lại sau.",
            )
            return

        # 4. Format response
        if not records:
            if search_query:
                self.telegram_client.send_message(
                    chat_id,
                    f"🔍 Không tìm thấy cơ hội bán hàng nào với từ khóa: <b>{html.escape(search_query)}</b>.",
                )
            else:
                self.telegram_client.send_message(
                    chat_id,
                    "📭 Hiện tại bạn chưa có cơ hội bán hàng (Lead) nào được giao phụ trách.",
                )
            return

        base_url = self.odoo_base_url or "https://odoo.com"
        lines = [f"📊 <b>Cơ hội bán hàng của bạn</b> ({len(records)} kết quả mới nhất):\n"]

        for idx, lead in enumerate(records, start=1):
            lead_id = lead.get("id")
            lead_name = html.escape(lead.get("name") or "Chưa đặt tên")
            stage_val = lead.get("stage_id")
            stage_name = (
                html.escape(stage_val[1])
                if isinstance(stage_val, list | tuple) and len(stage_val) > 1
                else "N/A"
            )

            lead_url = f"{base_url}/odoo/crm/{lead_id}"
            lines.append(f'{idx}. <a href="{lead_url}"><b>{lead_name}</b></a>')
            lines.append(f"   • Giai đoạn: <code>{stage_name}</code>")

        lines.append("\nBạn muốn xem cơ hội nào? Có thể cho biết tên khách hàng hoặc nội dung cần tìm.")
        response_text = "\n".join(lines)
        self.telegram_client.send_message(chat_id, response_text)

    def disconnect(self, user_id: int) -> None:
        with self.session_factory() as db:
            conn = db.query(TelegramConnection).filter_by(user_id=user_id).first()
            if conn:
                conn.status = "disconnected"
                db.commit()

    # Alias for flexibility
    process_update_payload = process_update_dict

    def _split_text(self, text: str, max_length: int = 4000) -> list[str]:
        if len(text) <= max_length:
            return [text]
        chunks: list[str] = []
        lines = text.split("\n")
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > max_length:
                if current:
                    chunks.append(current.strip())
                    current = ""
                if len(line) > max_length:
                    for i in range(0, len(line), max_length):
                        chunks.append(line[i : i + max_length])
                else:
                    current = line
            else:
                current = (current + "\n" + line) if current else line
        if current:
            chunks.append(current.strip())
        return chunks

    def _safe_send_message(self, chat_id: int, text: str) -> None:
        from app.telegram.text import plain_text
        for chunk in self._split_text(plain_text(text)):
            # Plain text has no parse-error fallback. Never replay an uncertain send.
            self.telegram_client.send_message(chat_id, chunk, parse_mode="")

    def _handle_ai_turn(
        self,
        update: dict[str, Any],
        chat_id: int,
        telegram_user_id: int,
        username: str | None,
        text: str,
    ) -> None:
        update_id = update.get("update_id")

        # 1. Resolve identity
        with self.session_factory() as db:
            conn = (
                db.query(TelegramConnection)
                .filter_by(
                    telegram_user_id=telegram_user_id,
                    private_chat_id=chat_id,
                    status="connected",
                )
                .first()
            )
            if not conn:
                self.telegram_client.send_message(
                    chat_id,
                    "👋 Xin chào! Tài khoản Telegram của bạn chưa được liên kết với Odoo.\n"
                    "Vui lòng đăng nhập vào Cổng Portal trên trình duyệt và bấm <b>'Liên kết Telegram'</b> để bắt đầu sử dụng.",
                )
                return

            user = db.query(User).filter_by(id=conn.user_id).first()
            if not user or not user.active:
                self.telegram_client.send_message(
                    chat_id,
                    "⚠️ Tài khoản người dùng của bạn hiện đang bị vô hiệu hóa. Vui lòng liên hệ quản trị viên.",
                )
                return

            user_id = user.id
            binding_id = conn.binding_id

        # 2. Check AI forwarding toggle
        if not self.enable_ai_forwarding:
            self.telegram_client.send_message(
                chat_id,
                "Tính năng AI Agent hiện đang tạm dừng kết nối. Vui lòng thử lại sau hoặc kiểm tra kết nối tại cổng tài khoản.",
            )
            return

        # 3. Check TelegramUpdate record for idempotency and resume
        delivery = None
        with self.session_factory() as db:
            import json

            from app.models import TelegramUpdate

            up_record = None
            if update_id:
                up_record = db.query(TelegramUpdate).filter_by(update_id=update_id).first()
                if up_record is None:
                    from sqlalchemy.exc import IntegrityError

                    up_record = TelegramUpdate(
                        update_id=update_id, user_id=user_id,
                        payload=json.dumps(update, ensure_ascii=False), status="pending",
                    )
                    db.add(up_record)
                    try:
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        return
                if up_record:
                    if up_record.status == "processed":
                        return
                    if up_record.delivery_status in {"delivery_unknown", "sending"}:
                        _logger.warning(
                            "Update %s has unknown delivery status. Avoiding duplicate resend.",
                            update_id,
                        )
                        return
                    if up_record.status == "ai_received" and up_record.ai_response:
                        try:
                            cached = json.loads(up_record.ai_response)
                            delivery = normalize_agent_response(cached)
                            if isinstance(cached, dict) and cached.get("telegram_binding_id") != binding_id:
                                return
                        except Exception:
                            pass

        # 4. If AI messages not yet fetched, call AI Agent
        if delivery is None:
            if update_id:
                with self.session_factory() as db:
                    from app.models import TelegramUpdate

                    # Conditional lock / update
                    claimed = db.query(TelegramUpdate).filter_by(update_id=update_id).filter(
                        TelegramUpdate.status.in_(["pending", "retry_wait", "failed"]),
                        TelegramUpdate.delivery_status.notin_(["sending", "delivery_unknown"]),
                    ).update(
                        {"status": "processing_ai"}
                    )
                    db.commit()
                    if not claimed:
                        return

            ai_url = f"{self.ai_agent_base_url}/internal/v1/telegram/turn"
            headers = {
                "Authorization": f"Bearer {self.gateway_to_agent_secret}",
                "Content-Type": "application/json",
            }
            req_payload = {
                "request_id": f"tg-{update_id or telegram_user_id}",
                "gateway_user_id": user_id,
                "telegram_user_id": telegram_user_id,
                "chat_id": chat_id,
                "telegram_binding_id": binding_id,
                "text": text,
                "received_at": datetime.now(UTC).isoformat(),
            }

            import httpx

            try:
                resp = httpx.post(ai_url, json=req_payload, headers=headers, timeout=35.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("request_id") != req_payload["request_id"]:
                        raise ValueError("AI response request identifier mismatch")
                    delivery = normalize_agent_response(data)
                else:
                    _logger.error("AI Agent returned %s: %s", resp.status_code, resp.text)
                    self.telegram_client.send_message(
                        chat_id,
                        "⚠️ AI Agent hiện không thể xử lý yêu cầu. Vui lòng thử lại sau.",
                    )
                    if update_id:
                        with self.session_factory() as db:
                            from app.models import TelegramUpdate

                            db.query(TelegramUpdate).filter_by(update_id=update_id).update(
                                {"status": "failed"}
                            )
                            db.commit()
                    return
            except Exception as e:
                _logger.error("Error connecting to AI Agent: %s", e)
                self.telegram_client.send_message(
                    chat_id,
                    "⚠️ Không thể kết nối đến AI Agent (Lỗi kết nối hoặc hết thời gian chờ).",
                )
                if update_id:
                    with self.session_factory() as db:
                        from app.models import TelegramUpdate

                        db.query(TelegramUpdate).filter_by(update_id=update_id).update(
                            {"status": "failed"}
                        )
                        db.commit()
                return

            if update_id and delivery is not None:
                with self.session_factory() as db:
                    import json

                    from app.models import TelegramUpdate

                    db.query(TelegramUpdate).filter_by(update_id=update_id).update(
                        {
                            "ai_response": json.dumps(
                                {**delivery, "telegram_binding_id": binding_id}, ensure_ascii=False
                            ),
                            "status": "ai_received",
                            "user_id": user_id,
                        }
                    )
                    db.commit()

        # 5. Deliver messages and internal artifacts; unknown sends are never replayed.
        if delivery is None:
            return
        chunks = [
            chunk for message in delivery["messages"] for chunk in self._split_text(message)
        ]
        chunks.extend({"type": "document", **item} for item in delivery["attachments"])
        if update_id:
            with self.session_factory() as db:
                from app.models import TelegramUpdate

                claimed = db.query(TelegramUpdate).filter_by(
                    update_id=update_id, status="ai_received"
                ).filter(TelegramUpdate.delivery_status.notin_(["sending", "delivery_unknown"])).update(
                    {
                        "delivery_status": "sending",
                        "delivery_chunks": json.dumps(chunks, ensure_ascii=False),
                        "delivery_index": 0,
                    }
                )
                db.commit()
                if not claimed:
                    return

        try:
            for index, chunk in enumerate(chunks):
                self._verify_ai_recipient(user_id, telegram_user_id, chat_id, binding_id)
                if isinstance(chunk, str):
                    self.telegram_client.send_message(chat_id, chunk, parse_mode="")
                else:
                    self._send_ai_attachment(
                        user_id, telegram_user_id, chat_id, binding_id, chunk["artifact_id"]
                    )
                if update_id:
                    with self.session_factory() as db:
                        db.query(TelegramUpdate).filter_by(update_id=update_id).update(
                            {"delivery_index": index + 1}
                        )
                        db.commit()
            if update_id:
                with self.session_factory() as db:
                    from app.models import TelegramUpdate

                    db.query(TelegramUpdate).filter_by(update_id=update_id).update(
                        {
                            "delivery_status": "delivered",
                            "status": "processed",
                        }
                    )
                    db.commit()

        except Exception as e:
            _logger.error("Failed to deliver message to Telegram chat %s: %s", chat_id, e)
            if update_id:
                with self.session_factory() as db:
                    from app.models import TelegramUpdate

                    db.query(TelegramUpdate).filter_by(update_id=update_id).update(
                        {
                            "delivery_status": "delivery_unknown",
                            "status": "failed",
                        }
                    )
                    db.commit()

    def _verify_ai_recipient(
        self, user_id: int, telegram_user_id: int, chat_id: int, binding_id: str
    ) -> None:
        with self.session_factory() as db:
            conn = db.query(TelegramConnection).filter_by(
                user_id=user_id, telegram_user_id=telegram_user_id, private_chat_id=chat_id,
                binding_id=binding_id, status="connected",
            ).first()
            user = db.query(User).filter_by(id=user_id, active=True).first()
            if not conn or not user:
                raise ValueError("Telegram recipient or binding changed before delivery")

    def _send_ai_attachment(
        self, user_id: int, telegram_user_id: int, chat_id: int,
        binding_id: str, artifact_id: str,
    ) -> None:
        from app.reporting.artifacts import load_artifact, revalidate_for_delivery

        if not getattr(self, "settings", None):
            raise ValueError("Document delivery requires configured gateway settings")
        revalidate_for_delivery(
            self.settings, self.session_factory, user_id, telegram_user_id, chat_id,
            binding_id, artifact_id=artifact_id,
        )
        artifact = load_artifact(self.session_factory, user_id, artifact_id, binding_id)
        self._verify_ai_recipient(user_id, telegram_user_id, chat_id, binding_id)
        self.telegram_client.send_document(
            chat_id, artifact["filename"], artifact["content"],
            content_type=artifact["content_type"],
        )


def create_telegram_service(
    settings: Any,
    session_factory: sessionmaker,
    telegram_client: TelegramClient | None = None,
    odoo_client: OdooClient | None = None,
) -> TelegramService:
    """Unified factory for building TelegramService with consistent AI forwarding parameters."""
    if telegram_client is None:
        telegram_client = TelegramClient(bot_token=settings.TELEGRAM_BOT_TOKEN)
    if odoo_client is None:
        odoo_client = OdooClient(url=settings.ODOO_URL, db=settings.ODOO_DB)

    service = TelegramService(
        session_factory=session_factory,
        telegram_client=telegram_client,
        bot_username=settings.TELEGRAM_BOT_USERNAME,
        webhook_secret=settings.TELEGRAM_WEBHOOK_SECRET,
        odoo_client=odoo_client,
        encryption_key=settings.ENCRYPTION_KEY or "dev_dummy_key_000000000000000000000=",
        odoo_base_url=settings.ODOO_URL,
        enable_ai_forwarding=settings.ENABLE_AI_AGENT_FORWARDING,
        ai_agent_base_url=settings.AI_AGENT_BASE_URL,
        gateway_to_agent_secret=settings.GATEWAY_TO_AGENT_SECRET,
    )
    service.settings = settings
    return service
