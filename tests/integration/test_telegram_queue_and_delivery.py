import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import TelegramConnection, TelegramUpdate, User


def _setup(temp_db_path, fernet_key, enable_ai=True):
    settings = Settings(
        APP_ENV="development",
        PUBLIC_BASE_URL="http://localhost:8080",
        ODOO_URL="https://mycompany.odoo.com",
        ODOO_DB="test_db",
        DATABASE_URL=f"sqlite:///{temp_db_path}",
        ENCRYPTION_KEY=fernet_key,
        TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        TELEGRAM_BOT_USERNAME="TestBot",
        TELEGRAM_WEBHOOK_SECRET="expected_webhook_secret_xyz",
        AGENT_TO_GATEWAY_SECRET="agent_secret_min_32_chars_1234567890",
        GATEWAY_TO_AGENT_SECRET="gateway_secret_min_32_chars_1234567890",
        ENABLE_AI_AGENT_FORWARDING=enable_ai,
        AI_AGENT_BASE_URL="http://localhost:8000",
    )
    app = create_app(settings)
    client = TestClient(app)
    return app, client, settings


def _create_user_and_conn(app, user_id=1, tele_id=12345, chat_id=12345):
    with app.state.session_factory() as db:
        user = User(
            id=user_id,
            tenant="https://mycompany.odoo.com",
            database="test_db",
            odoo_uid=user_id,
            login=f"user{user_id}@example.com",
            name=f"User {user_id}",
            tz="Asia/Bangkok",
            active=True,
        )
        db.add(user)
        conn = TelegramConnection(
            user_id=user_id,
            telegram_user_id=tele_id,
            private_chat_id=chat_id,
            status="connected",
        )
        db.add(conn)
        db.commit()


def test_webhook_acks_immediately_without_calling_ai(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)
    _create_user_and_conn(app, 1, 99999, 99999)

    payload = {
        "update_id": 9001,
        "message": {
            "message_id": 1,
            "chat": {"id": 99999, "type": "private"},
            "from": {"id": 99999, "is_bot": False},
            "text": "Hôm nay tôi có gì?",
        },
    }

    with patch("httpx.post") as mock_http:
        resp = client.post(
            "/webhooks/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "expected_webhook_secret_xyz"},
        )
        assert resp.status_code == 200
        # Webhook MUST ACK without blocking or calling AI directly
        mock_http.assert_not_called()

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9001).first()
        assert up is not None
        assert up.status == "pending"
        assert up.delivery_status == "none"


def test_worker_processes_pending_and_delivers(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)
    _create_user_and_conn(app, 1, 88888, 88888)

    with app.state.session_factory() as db:
        up = TelegramUpdate(
            update_id=9002,
            payload=json.dumps(
                {
                    "update_id": 9002,
                    "message": {
                        "chat": {"id": 88888, "type": "private"},
                        "from": {"id": 88888, "is_bot": False},
                        "text": "Hôm nay tôi có gì?",
                    },
                }
            ),
            status="pending",
            delivery_status="none",
        )
        db.add(up)
        db.commit()

    ai_data = {
        "request_id": "tg-9002",
        "messages": ["Điểm tin sáng nay: 1 cuộc họp lúc 10h."],
    }

    class MockResp:
        status_code = 200
        text = json.dumps(ai_data)

        def json(self):
            return ai_data

    from app.telegram.worker import TelegramAIWorker

    worker = TelegramAIWorker(
        settings=settings,
        session_factory=app.state.session_factory,
    )

    with patch("httpx.post", return_value=MockResp()) as mock_http:
        with (
            patch("app.telegram.client.TelegramClient.send_message") as mock_tele,
            patch("app.telegram.client.TelegramClient.send_chat_action"),
        ):
            # Process one item
            processed_count = worker.process_pending_once()
            assert processed_count == 1
            mock_http.assert_called_once()
            mock_tele.assert_called_once()

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9002).first()
        assert up.status == "processed"
        assert up.delivery_status == "delivered"
        assert up.delivery_index == 1


def test_worker_retry_on_5xx_and_timeout(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)
    _create_user_and_conn(app, 1, 77777, 77777)

    with app.state.session_factory() as db:
        up = TelegramUpdate(
            update_id=9003,
            payload=json.dumps(
                {
                    "update_id": 9003,
                    "message": {
                        "chat": {"id": 77777, "type": "private"},
                        "from": {"id": 77777, "is_bot": False},
                        "text": "Hôm nay có gì?",
                    },
                }
            ),
            status="pending",
            delivery_status="none",
        )
        db.add(up)
        db.commit()

    import httpx

    from app.telegram.worker import TelegramAIWorker

    worker = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)

    # Attempt 1: 500 error -> retry_wait with 5s delay
    class Mock500Resp:
        status_code = 500
        text = "Internal Server Error"

        def json(self):
            return {}

    with patch("httpx.post", return_value=Mock500Resp()):
        worker.process_pending_once()

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9003).first()
        assert up.status == "retry_wait"
        assert up.attempts == 1
        assert up.retry_at is not None

    # Force retry_at to past and run Attempt 2: Timeout -> retry_wait with 30s delay
    with app.state.session_factory() as db:
        db.query(TelegramUpdate).filter_by(update_id=9003).update(
            {"retry_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        db.commit()

    with patch("httpx.post", side_effect=httpx.ConnectTimeout("Connection timed out")):
        worker.process_pending_once()

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9003).first()
        assert up.status == "retry_wait"
        assert up.attempts == 2

    # Force retry_at to past and run Attempt 3: 502 error -> max retries -> failed
    with app.state.session_factory() as db:
        db.query(TelegramUpdate).filter_by(update_id=9003).update(
            {"retry_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        db.commit()

    with patch("httpx.post", return_value=Mock500Resp()):
        with patch("app.telegram.client.TelegramClient.send_message") as mock_tele:
            worker.process_pending_once()
            mock_tele.assert_called_once()
            assert "không thể xử lý yêu cầu sau nhiều lần thử lại" in mock_tele.call_args[0][1]

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9003).first()
        assert up.status == "failed"
        assert up.attempts == 3


def test_worker_http_4xx_does_not_retry(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)
    _create_user_and_conn(app, 1, 66666, 66666)

    with app.state.session_factory() as db:
        up = TelegramUpdate(
            update_id=9004,
            payload=json.dumps(
                {
                    "update_id": 9004,
                    "message": {
                        "chat": {"id": 66666, "type": "private"},
                        "from": {"id": 66666, "is_bot": False},
                        "text": "Test 400",
                    },
                }
            ),
            status="pending",
            delivery_status="none",
        )
        db.add(up)
        db.commit()

    class Mock400Resp:
        status_code = 400
        text = "Bad Request"

        def json(self):
            return {"detail": "Invalid"}

    from app.telegram.worker import TelegramAIWorker

    worker = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)

    with patch("httpx.post", return_value=Mock400Resp()):
        with patch("app.telegram.client.TelegramClient.send_message") as mock_tele:
            worker.process_pending_once()
            mock_tele.assert_called_once()

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9004).first()
        assert up.status == "failed"
        assert up.attempts == 1


def test_delivery_timeout_marks_delivery_unknown_and_stops(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)
    _create_user_and_conn(app, 1, 55555, 55555)

    # Pre-seed ai_received with 2 chunks
    chunk1 = "A" * 3000
    chunk2 = "B" * 3000
    messages = [chunk1, chunk2]

    with app.state.session_factory() as db:
        up = TelegramUpdate(
            update_id=9005,
            user_id=1,
            payload=json.dumps(
                {
                    "update_id": 9005,
                    "message": {
                        "chat": {"id": 55555, "type": "private"},
                        "from": {"id": 55555, "is_bot": False},
                        "text": "Long briefing",
                    },
                }
            ),
            status="ai_received",
            ai_response=json.dumps(messages),
            delivery_status="none",
        )
        db.add(up)
        db.commit()

    from app.telegram.worker import TelegramAIWorker

    worker = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)

    # First chunk succeeds, second chunk times out
    import httpx

    call_count = 0

    def mock_send_side_effect(chat_id, text, parse_mode=""):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"ok": True}
        raise httpx.ReadTimeout("Telegram API timed out during delivery")

    with patch(
        "app.telegram.client.TelegramClient.send_message", side_effect=mock_send_side_effect
    ):
        worker.process_pending_once()

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9005).first()
        # Delivery progress was saved for chunk 1, but chunk 2 timed out -> delivery_unknown
        assert up.delivery_status == "delivery_unknown"
        assert up.delivery_index == 1
        assert up.status == "failed"

    # If worker runs again, it MUST NOT resend or retry
    with patch("app.telegram.client.TelegramClient.send_message") as mock_send2:
        worker.process_pending_once()
        mock_send2.assert_not_called()


def test_restart_recovers_sending_to_delivery_unknown(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)

    with app.state.session_factory() as db:
        up = TelegramUpdate(
            update_id=9006,
            payload=json.dumps(
                {
                    "update_id": 9006,
                    "message": {"chat": {"id": 11111}},
                }
            ),
            status="ai_received",
            delivery_status="sending",
        )
        db.add(up)
        db.commit()

    from app.telegram.worker import TelegramAIWorker

    worker = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)
    worker.recover_on_startup()

    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=9006).first()
        assert up.delivery_status == "delivery_unknown"
        assert up.status == "failed"
