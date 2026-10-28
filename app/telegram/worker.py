import concurrent.futures
import json
import logging
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import TelegramConnection, TelegramUpdate, User
from app.telegram.client import TelegramClient, TelegramDeliveryUnknownError

_logger = logging.getLogger(__name__)


class TelegramAIWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        telegram_client: TelegramClient | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        if telegram_client is None:
            self.telegram_client = TelegramClient(bot_token=settings.TELEGRAM_BOT_TOKEN)
        else:
            self.telegram_client = telegram_client

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="TgChatWorker"
        )
        self._active_chat_ids: set[int] = set()
        self._chat_lock = threading.Lock()

    def recover_on_startup(self) -> None:
        """On gateway startup, recover any unfinished updates:
        - Updates with delivery_status='sending' whose lease has expired or is None
          must transition to 'delivery_unknown' and 'failed' to prevent blind double delivery.
        - Updates containing gateway commands (/start, /help, /status, /crm) left in AI states
          are quarantined to 'gateway_command' so the AI worker never touches them.
        """
        now = datetime.now(UTC)
        with self.session_factory() as db:
            # 1. Quarantining gateway commands
            pending_cmds = (
                db.query(TelegramUpdate)
                .filter(TelegramUpdate.status.in_(["pending", "processing_ai", "retry_wait"]))
                .all()
            )
            for up in pending_cmds:
                try:
                    payload = json.loads(up.payload or "{}")
                    msg = payload.get("message") or payload.get("edited_message") or {}
                    txt = (msg.get("text") or "").strip()
                    if txt.startswith("/"):
                        tokens = txt.split()
                        base_cmd = tokens[0].lower().split("@")[0]
                        is_gw = False
                        if base_cmd in {"/start", "/help", "/status"}:
                            is_gw = True
                        elif base_cmd == "/crm":
                            sub_cmd = tokens[1].lower() if len(tokens) > 1 else ""
                            is_gw = sub_cmd not in {"search", "create", "update"}

                        if is_gw:
                            _logger.warning(
                                "Recovery: quarantining gateway command update %s from AI queue.",
                                up.update_id,
                            )
                            up.status = "gateway_command"
                            up.lease_expires_at = None
                            up.claim_token = None
                            up.updated_at = now
                except Exception:
                    pass

            # 2. Mark expired in-flight sends as delivery_unknown via atomic sweep
            db.commit()
        self._sweep_expired_sending()

    def _extract_chat_id(self, payload_str: str | None, default_id: int) -> int:
        if not payload_str:
            return default_id
        try:
            data = json.loads(payload_str)
            msg = data.get("message") or data.get("edited_message") or {}
            chat = msg.get("chat", {})
            return chat.get("id") or default_id
        except Exception:
            return default_id

    def process_pending_once(self) -> int:
        """Process one batch of pending, retryable, or deliverable updates.
        Supports up to 4 concurrent chats with strict FIFO per chat.
        Returns the number of updates processed.
        """
        # Sweep expired sending records first
        self._sweep_expired_sending()

        now = datetime.now(UTC)

        with self.session_factory() as db:
            # Find eligible candidate IDs and payloads
            candidates = (
                db.query(TelegramUpdate.id, TelegramUpdate.payload)
                .filter(
                    (TelegramUpdate.status == "pending")
                    | ((TelegramUpdate.status == "retry_wait") & (TelegramUpdate.retry_at <= now))
                    | (
                        (TelegramUpdate.status == "processing_ai")
                        & (TelegramUpdate.lease_expires_at <= now)
                    )
                    | (
                        (TelegramUpdate.status == "ai_received")
                        & (TelegramUpdate.delivery_status.in_(["none", "ready"]))
                        & (
                            (TelegramUpdate.lease_expires_at.is_(None))
                            | (TelegramUpdate.lease_expires_at <= now)
                        )
                    )
                )
                .order_by(TelegramUpdate.id.asc())
                .limit(20)
                .all()
            )

        if not candidates:
            return 0

        # Select up to 4 candidates with distinct chat_ids not currently active
        selected_tasks: list[tuple[int, int]] = []
        with self._chat_lock:
            for up_id, payload_str in candidates:
                if len(selected_tasks) >= 4:
                    break
                chat_id = self._extract_chat_id(payload_str, default_id=up_id)
                if chat_id not in self._active_chat_ids and not any(c == chat_id for _, c in selected_tasks):
                    selected_tasks.append((up_id, chat_id))
                    self._active_chat_ids.add(chat_id)

        if not selected_tasks:
            return 0

        if len(selected_tasks) == 1:
            u_id, c_id = selected_tasks[0]
            try:
                return 1 if self._process_single_update(u_id) else 0
            finally:
                with self._chat_lock:
                    self._active_chat_ids.discard(c_id)

        # Execute concurrent tasks across separate chats
        def _run_with_lock(u_id: int, c_id: int) -> bool:
            try:
                return self._process_single_update(u_id)
            finally:
                with self._chat_lock:
                    self._active_chat_ids.discard(c_id)

        futures = [
            self._executor.submit(_run_with_lock, u_id, c_id)
            for u_id, c_id in selected_tasks
        ]
        done, _ = concurrent.futures.wait(futures, timeout=30.0)

        processed_count = 0
        for f in done:
            try:
                if f.result():
                    processed_count += 1
            except Exception as e:
                _logger.error("Exception in chat worker task: %s", e)

        return processed_count

    def _claim_update_for_ai(self, update_db_id: int, claim_token: str) -> bool:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=90)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET status = 'processing_ai',
                    lease_expires_at = :lease_until,
                    claim_token = :claim_token,
                    attempts = attempts + 1,
                    updated_at = :now
                WHERE id = :id AND (
                    status = 'pending'
                    OR (status = 'retry_wait' AND (retry_at IS NULL OR retry_at <= :now))
                    OR (status = 'processing_ai' AND (lease_expires_at IS NULL OR lease_expires_at <= :now))
                )
            """)
            res = db.execute(
                stmt,
                {
                    "id": update_db_id,
                    "lease_until": lease_until,
                    "claim_token": claim_token,
                    "now": now,
                },
            )
            db.commit()
            return res.rowcount == 1

    def _claim_update_for_delivery(self, update_db_id: int, claim_token: str) -> bool:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=90)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET lease_expires_at = :lease_until,
                    claim_token = :claim_token,
                    delivery_status = 'ready',
                    updated_at = :now
                WHERE id = :id AND status = 'ai_received' AND delivery_status IN ('none', 'ready')
                  AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
            """)
            res = db.execute(
                stmt,
                {
                    "id": update_db_id,
                    "lease_until": lease_until,
                    "claim_token": claim_token,
                    "now": now,
                },
            )
            db.commit()
            return res.rowcount == 1

    def _save_ai_response(self, update_db_id: int, claim_token: str, ai_response_str: str) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET ai_response = :ai_response,
                    status = 'ai_received',
                    delivery_status = 'ready',
                    updated_at = :now
                WHERE id = :id AND claim_token = :claim_token
            """)
            res = db.execute(
                stmt,
                {
                    "id": update_db_id,
                    "ai_response": ai_response_str,
                    "claim_token": claim_token,
                    "now": now,
                },
            )
            db.commit()
            return res.rowcount == 1

    def _schedule_retry(
        self,
        update_db_id: int,
        claim_token: str,
        attempts: int,
        retry_at: datetime,
        error_desc: str,
    ) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET status = 'retry_wait',
                    retry_at = :retry_at,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    last_error = :last_error,
                    updated_at = :now
                WHERE id = :id AND claim_token = :claim_token
            """)
            res = db.execute(
                stmt,
                {
                    "id": update_db_id,
                    "retry_at": retry_at,
                    "last_error": error_desc,
                    "claim_token": claim_token,
                    "now": now,
                },
            )
            db.commit()
            return res.rowcount == 1

    def _mark_failed(
        self, update_db_id: int, error_desc: str, claim_token: str | None = None
    ) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            if claim_token:
                stmt = text("""
                    UPDATE telegram_updates
                    SET status = 'failed',
                        last_error = :last_error,
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        updated_at = :now
                    WHERE id = :id AND claim_token = :claim_token
                """)
                res = db.execute(
                    stmt,
                    {
                        "id": update_db_id,
                        "last_error": error_desc,
                        "claim_token": claim_token,
                        "now": now,
                    },
                )
            else:
                stmt = text("""
                    UPDATE telegram_updates
                    SET status = 'failed',
                        last_error = :last_error,
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        updated_at = :now
                    WHERE id = :id
                """)
                res = db.execute(
                    stmt,
                    {"id": update_db_id, "last_error": error_desc, "now": now},
                )
            db.commit()
            return res.rowcount == 1

    def _sweep_expired_sending(self) -> int:
        """Sweeps records with delivery_status='sending' whose lease has expired or is NULL.
        Transitions them atomically to delivery_status='delivery_unknown' and status='failed'.
        """
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET delivery_status = 'delivery_unknown',
                    status = 'failed',
                    last_error = 'Message sending lease expired or process restarted during transmission',
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    updated_at = :now
                WHERE delivery_status = 'sending'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
            """)
            res = db.execute(stmt, {"now": now})
            db.commit()
            if res.rowcount > 0:
                _logger.warning(
                    "Swept %s expired 'sending' records to 'delivery_unknown'", res.rowcount
                )
            return res.rowcount

    def _verify_recipient(
        self, bound_user_id: int, telegram_user_id: int, chat_id: int
    ) -> tuple[bool, str | None]:
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
            if not conn or conn.user_id != bound_user_id:
                return (
                    False,
                    "Telegram connection disconnected or recipient rebound before delivery",
                )

            user = db.query(User).filter_by(id=bound_user_id, active=True).first()
            if not user:
                return False, f"User {bound_user_id} deactivated before delivery"

        return True, None

    def _mark_delivery_failed(self, update_db_id: int, claim_token: str, error_desc: str) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET status = 'failed',
                    delivery_status = 'failed',
                    last_error = :last_error,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    updated_at = :now
                WHERE id = :id AND claim_token = :claim_token
            """)
            res = db.execute(
                stmt,
                {
                    "id": update_db_id,
                    "last_error": error_desc,
                    "claim_token": claim_token,
                    "now": now,
                },
            )
            db.commit()
            return res.rowcount == 1

    def _mark_delivered(self, update_db_id: int, claim_token: str) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET status = 'processed',
                    delivery_status = 'delivered',
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    updated_at = :now
                WHERE id = :id AND claim_token = :claim_token
            """)
            res = db.execute(
                stmt,
                {"id": update_db_id, "claim_token": claim_token, "now": now},
            )
            db.commit()
            return res.rowcount == 1

    def _set_user_id(self, update_db_id: int, claim_token: str, user_id: int) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET user_id = :user_id,
                    updated_at = :now
                WHERE id = :id AND claim_token = :claim_token
            """)
            res = db.execute(
                stmt,
                {"id": update_db_id, "user_id": user_id, "claim_token": claim_token, "now": now},
            )
            db.commit()
            return res.rowcount == 1

    def _update_chunk_progress(
        self, update_db_id: int, claim_token: str, delivery_index: int, lease_until: datetime
    ) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET delivery_index = :delivery_index,
                    delivery_status = 'ready',
                    lease_expires_at = :lease_until,
                    updated_at = :now
                WHERE id = :id AND claim_token = :claim_token
            """)
            res = db.execute(
                stmt,
                {
                    "id": update_db_id,
                    "delivery_index": delivery_index,
                    "lease_until": lease_until,
                    "claim_token": claim_token,
                    "now": now,
                },
            )
            db.commit()
            return res.rowcount == 1

    def _set_delivery_status(
        self,
        update_db_id: int,
        claim_token: str,
        delivery_status: str,
        status: str | None = None,
        error_desc: str | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        params = {
            "id": update_db_id,
            "claim_token": claim_token,
            "delivery_status": delivery_status,
            "now": now,
        }
        set_clauses = ["delivery_status = :delivery_status", "updated_at = :now"]
        if status is not None:
            set_clauses.append("status = :status")
            params["status"] = status
        if error_desc is not None:
            set_clauses.append("last_error = :last_error")
            params["last_error"] = error_desc

        query_str = f"UPDATE telegram_updates SET {', '.join(set_clauses)} WHERE id = :id AND claim_token = :claim_token"
        with self.session_factory() as db:
            stmt = text(query_str)
            res = db.execute(stmt, params)
            db.commit()
            return res.rowcount == 1

    def _save_delivery_chunks(self, update_db_id: int, claim_token: str, chunks_json: str) -> bool:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            stmt = text("""
                UPDATE telegram_updates
                SET delivery_chunks = :chunks,
                    delivery_index = 0,
                    delivery_status = 'ready',
                    updated_at = :now
                WHERE id = :id AND claim_token = :claim_token
            """)
            res = db.execute(
                stmt,
                {
                    "id": update_db_id,
                    "chunks": chunks_json,
                    "claim_token": claim_token,
                    "now": now,
                },
            )
            db.commit()
            return res.rowcount == 1

    def _process_single_update(self, update_db_id: int) -> bool:
        claim_token = secrets.token_hex(16)

        # Inspect current state first to decide whether we are claiming for AI or Delivery
        with self.session_factory() as db:
            up = db.query(TelegramUpdate).filter_by(id=update_db_id).first()
            if not up:
                return False
            initial_status = up.status

        if initial_status == "ai_received":
            # Claim for delivery
            if not self._claim_update_for_delivery(update_db_id, claim_token):
                return False
        elif initial_status in {"pending", "retry_wait", "processing_ai"}:
            # Claim for calling AI
            if not self._claim_update_for_ai(update_db_id, claim_token):
                return False
        else:
            return False

        # Read the claimed record
        with self.session_factory() as db:
            up = db.query(TelegramUpdate).filter_by(id=update_db_id).first()
            if not up:
                return False
            update_id = up.update_id
            payload_str = up.payload
            current_status = up.status
            current_attempts = up.attempts
            ai_response_str = up.ai_response
            delivery_chunks_str = up.delivery_chunks
            delivery_index = up.delivery_index
            bound_user_id = up.user_id

        try:
            payload = json.loads(payload_str or "{}")
        except Exception as e:
            _logger.error("Malformed payload for update %s: %s", update_id, e)
            self._mark_failed(update_db_id, f"Malformed payload JSON: {e}", claim_token=claim_token)
            return False

        message = payload.get("message") or payload.get("edited_message") or {}
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        chat_id = chat.get("id")
        telegram_user_id = from_user.get("id")
        text_content = (message.get("text") or "").strip()

        if not chat_id or not telegram_user_id:
            self._mark_failed(
                update_db_id, "Missing chat_id or telegram_user_id", claim_token=claim_token
            )
            return False

        # If already ai_received, proceed directly to delivery
        if current_status == "ai_received":
            return self._deliver_messages(
                update_db_id=update_db_id,
                update_id=update_id,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                ai_response_str=ai_response_str,
                delivery_chunks_str=delivery_chunks_str,
                delivery_index=delivery_index,
                claim_token=claim_token,
                bound_user_id=bound_user_id,
            )

        # Verify identity before calling AI
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
                self._mark_failed(
                    update_db_id,
                    "Telegram account is not connected to any active user",
                    claim_token=claim_token,
                )
                self._notify_error(
                    chat_id,
                    "⚠️ Tài khoản Telegram của bạn chưa được liên kết. Vui lòng đăng nhập Cổng Portal để kết nối.",
                )
                return True

            user = db.query(User).filter_by(id=conn.user_id, active=True).first()
            if not user:
                self._mark_failed(
                    update_db_id,
                    f"User {conn.user_id} is inactive or not found",
                    claim_token=claim_token,
                )
                self._notify_error(
                    chat_id,
                    "⚠️ Tài khoản của bạn hiện đang bị vô hiệu hóa hoặc không khả dụng.",
                )
                return True

            current_binding_id = conn.binding_id
            # If not yet bound, bind user_id now
            if not bound_user_id:
                bound_user_id = user.id
                self._set_user_id(update_db_id, claim_token, bound_user_id)
            elif bound_user_id != user.id:
                self._mark_failed(
                    update_db_id,
                    f"Binding conflict: update user_id {bound_user_id} != connection user_id {user.id}",
                    claim_token=claim_token,
                )
                return False

        # Call AI Agent (Timeout 25s)
        ai_url = f"{self.settings.AI_AGENT_BASE_URL.rstrip('/')}/internal/v1/telegram/turn"
        request_id = f"tg-{update_id}"
        req_payload = {
            "request_id": request_id,
            "gateway_user_id": bound_user_id,
            "telegram_user_id": telegram_user_id,
            "chat_id": chat_id,
            "text": text_content,
            "telegram_binding_id": current_binding_id,
            "received_at": datetime.now(UTC).isoformat(),
        }

        # Send typing action to Telegram
        try:
            self.telegram_client.send_chat_action(chat_id, "typing")
        except Exception as e:
            _logger.debug("Failed to send typing chat action: %s", e)

        t_ai_start = time.time()
        try:
            headers = {
                "Authorization": f"Bearer {self.settings.GATEWAY_TO_AGENT_SECRET}",
                "Content-Type": "application/json",
            }
            resp = httpx.post(ai_url, json=req_payload, headers=headers, timeout=25.0)
            t_ai = time.time() - t_ai_start
            _logger.info("TIMING_AI: request_id=%s duration=%.3fs status=%s", request_id, t_ai, resp.status_code)

            if resp.status_code == 200:
                resp_data = resp.json()
                if not isinstance(resp_data, dict):
                    self._mark_failed(
                        update_db_id,
                        f"Invalid response type from AI Agent: {type(resp_data)}",
                        claim_token=claim_token,
                    )
                    return True

                ret_req_id = resp_data.get("request_id")
                if ret_req_id != request_id:
                    self._mark_failed(
                        update_db_id,
                        f"Request ID mismatch: expected {request_id}, got {ret_req_id}",
                        claim_token=claim_token,
                    )
                    return True

                messages = resp_data.get("messages")
                if not isinstance(messages, list) or not all(isinstance(m, str) for m in messages):
                    self._mark_failed(
                        update_db_id,
                        "AI Agent response 'messages' must be a list of strings",
                        claim_token=claim_token,
                    )
                    return True

                ai_response_str = json.dumps(messages, ensure_ascii=False)
                # Save response
                if not self._save_ai_response(update_db_id, claim_token, ai_response_str):
                    _logger.warning(
                        "Failed to save AI response for update %s: claim lost", update_id
                    )
                    return False

                # Proceed to deliver
                return self._deliver_messages(
                    update_db_id=update_db_id,
                    update_id=update_id,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    ai_response_str=ai_response_str,
                    delivery_chunks_str=None,
                    delivery_index=0,
                    claim_token=claim_token,
                    bound_user_id=bound_user_id,
                )
            elif 500 <= resp.status_code < 600:
                return self._handle_retry(
                    update_db_id,
                    chat_id,
                    current_attempts,
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                    claim_token,
                )
            else:
                self._mark_failed(
                    update_db_id,
                    f"HTTP {resp.status_code} client error from AI Agent: {resp.text[:200]}",
                    claim_token=claim_token,
                )
                self._notify_error(
                    chat_id,
                    "⚠️ Hệ thống AI không thể xử lý yêu cầu lúc này. Vui lòng thử lại sau.",
                )
                return True
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.NetworkError,
        ) as e:
            return self._handle_retry(
                update_db_id, chat_id, current_attempts, f"Network/Timeout error: {e}", claim_token
            )
        except Exception as e:
            _logger.error(
                "Unexpected error forwarding to AI for update %s: %s", update_id, e, exc_info=True
            )
            self._mark_failed(update_db_id, f"Unexpected error: {e}", claim_token=claim_token)
            return True

    def _handle_retry(
        self,
        update_db_id: int,
        chat_id: int,
        attempts: int,
        error_desc: str,
        claim_token: str,
    ) -> bool:
        if attempts < 3:
            delay_seconds = 5 if attempts == 1 else 30
            retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
            self._schedule_retry(
                update_db_id,
                claim_token=claim_token,
                attempts=attempts,
                retry_at=retry_at,
                error_desc=error_desc,
            )
            _logger.warning(
                "Update %s attempt %s failed (%s). Retrying in %ss at %s",
                update_db_id,
                attempts,
                error_desc,
                delay_seconds,
                retry_at.isoformat(),
            )
            return True
        else:
            self._mark_failed(
                update_db_id,
                f"Max retries (3) exhausted. Last error: {error_desc}",
                claim_token=claim_token,
            )
            self._notify_error(
                chat_id,
                "⚠️ AI Agent hiện không thể xử lý yêu cầu sau nhiều lần thử lại. Vui lòng thử lại sau.",
            )
            return True

    def _deliver_messages(
        self,
        update_db_id: int,
        update_id: int,
        chat_id: int,
        telegram_user_id: int,
        ai_response_str: str | None,
        delivery_chunks_str: str | None,
        delivery_index: int,
        claim_token: str,
        bound_user_id: int | None,
    ) -> bool:
        if not ai_response_str:
            self._mark_failed(update_db_id, "No AI response to deliver", claim_token=claim_token)
            return False

        # Task 3: If bound_user_id is None, do not deliver!
        if not bound_user_id:
            self._mark_failed(
                update_db_id,
                "Missing verified owner (user_id is None); withholding delivery for manual audit",
                claim_token=claim_token,
            )
            return False

        # Prepare chunks if not already prepared
        if not delivery_chunks_str:
            try:
                messages = json.loads(ai_response_str)
            except Exception:
                messages = [ai_response_str]

            all_chunks = []
            for msg in messages:
                all_chunks.extend(self._split_text(msg, max_length=4000))

            delivery_chunks_str = json.dumps(all_chunks, ensure_ascii=False)
            delivery_index = 0
            if not self._save_delivery_chunks(update_db_id, claim_token, delivery_chunks_str):
                _logger.warning(
                    "Failed to save delivery chunks for update %s: claim lost", update_id
                )
                return False
        else:
            try:
                all_chunks = json.loads(delivery_chunks_str)
            except Exception:
                all_chunks = [delivery_chunks_str]

        # Deliver chunk by chunk
        total_chunks = len(all_chunks)
        for idx in range(delivery_index, total_chunks):
            # Re-verify recipient before each chunk
            is_valid, err_msg = self._verify_recipient(bound_user_id, telegram_user_id, chat_id)
            if not is_valid:
                _logger.warning(
                    "Recipient verification failed before sending chunk %s for update %s: %s",
                    idx,
                    update_id,
                    err_msg,
                )
                self._mark_delivery_failed(
                    update_db_id,
                    claim_token,
                    err_msg or "Recipient verification failed",
                )
                return True

            chunk = all_chunks[idx]

            # Mark sending immediately before each Telegram API call
            if not self._set_delivery_status(update_db_id, claim_token, "sending"):
                _logger.warning("Claim lost before sending chunk %s for update %s", idx, update_id)
                return False

            try:
                # Plain text strictly per contract
                self.telegram_client.send_message(chat_id, chunk, parse_mode="")
            except (TelegramDeliveryUnknownError, httpx.TimeoutException, httpx.NetworkError) as e:
                _logger.error(
                    "Delivery unknown on chunk %s/%s for update %s (req_id tg-%s): %s",
                    idx + 1,
                    total_chunks,
                    update_id,
                    update_id,
                    e,
                )
                self._set_delivery_status(
                    update_db_id,
                    claim_token,
                    delivery_status="delivery_unknown",
                    status="failed",
                    error_desc=f"Delivery unknown on chunk {idx + 1}: {e}",
                )
                # Stop immediately; DO NOT retry automatically
                return True
            except Exception as e:
                _logger.error("Telegram API error delivering chunk %s: %s", idx + 1, e)
                self._mark_failed(
                    update_db_id,
                    f"Telegram API error: {e}",
                    claim_token=claim_token,
                )
                return True

            # Chunk delivered successfully: persist progress, renew lease, set ready
            lease_until = datetime.now(UTC) + timedelta(seconds=90)
            if not self._update_chunk_progress(update_db_id, claim_token, idx + 1, lease_until):
                _logger.warning(
                    "Claim lost while saving progress for chunk %s on update %s", idx, update_id
                )
                return False

        # All chunks confirmed delivered
        self._mark_delivered(update_db_id, claim_token)
        return True

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

    def _notify_error(self, chat_id: int, message: str) -> None:
        try:
            self.telegram_client.send_message(chat_id, message)
        except Exception:
            pass

    def start(self) -> None:
        self.recover_on_startup()
        self._stop_event.clear()

        def _worker_loop():
            _logger.info("TelegramAIWorker started.")
            while not self._stop_event.is_set():
                processed = 0
                try:
                    processed = self.process_pending_once()
                except Exception as e:
                    _logger.error("Error in TelegramAIWorker loop: %s", e)
                if processed > 0:
                    self._stop_event.wait(0.02)
                else:
                    self._stop_event.wait(0.2)
            _logger.info("TelegramAIWorker stopped.")

        self._thread = threading.Thread(target=_worker_loop, daemon=True, name="TelegramAIWorker")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._executor.shutdown(wait=False, cancel_futures=True)
