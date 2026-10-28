import json
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
        AGENT_TO_GATEWAY_SECRET="agent_secret_min_32_characters_1234567890",
        GATEWAY_TO_AGENT_SECRET="gateway_secret_min_32_characters_1234567890",
        ENABLE_AI_AGENT_FORWARDING=enable_ai,
        AI_AGENT_BASE_URL="http://localhost:8000",
    )
    app = create_app(settings)
    client = TestClient(app)
    return app, client, settings


def test_telegram_unlinked_user_guides_to_portal(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)
    payload = {
        "update_id": 1001,
        "message": {
            "message_id": 1,
            "chat": {"id": 88888, "type": "private"},
            "from": {"id": 88888, "is_bot": False},
            "text": "Hôm nay tôi có gì?",
        },
    }

    with patch("app.telegram.client.TelegramClient.send_message") as mock_send:
        resp = client.post(
            "/webhooks/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "expected_webhook_secret_xyz"},
        )
        assert resp.status_code == 200
        from app.telegram.worker import TelegramAIWorker

        worker = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)
        worker.process_pending_once()
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        assert "chưa được liên kết" in sent_text
        assert "Cổng Portal" in sent_text


def test_telegram_ai_forwarding_success(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)

    with app.state.session_factory() as db:
        user = User(
            tenant="https://mycompany.odoo.com",
            database="test_db",
            odoo_uid=2,
            login="user@company.com",
            name="Nguyễn Văn A",
            tz="Asia/Bangkok",
            active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id

        conn = TelegramConnection(
            user_id=user_id,
            telegram_user_id=77777,
            private_chat_id=77777,
            status="connected",
        )
        db.add(conn)
        db.commit()

    payload = {
        "update_id": 2002,
        "message": {
            "message_id": 2,
            "chat": {"id": 77777, "type": "private"},
            "from": {"id": 77777, "is_bot": False},
            "text": "Hôm nay tôi có gì?",
        },
    }

    ai_response_data = {
        "request_id": "tg-2002",
        "messages": [
            "👋 Chào bạn **Nguyễn Văn A**! Lịch trình hôm nay:",
            "📅 **LỊCH LÀM VIỆC HÔM NAY:**\n• 09:00 - 10:00: Họp chiến lược",
        ],
    }

    class MockResponse:
        status_code = 200
        text = json.dumps(ai_response_data)

        def json(self):
            return ai_response_data

    with patch("httpx.post", return_value=MockResponse()) as mock_http:
        with (
            patch("app.telegram.client.TelegramClient.send_message") as mock_send,
            patch("app.telegram.client.TelegramClient.send_chat_action"),
        ):
            resp = client.post(
                "/webhooks/telegram",
                json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": "expected_webhook_secret_xyz"},
            )
            assert resp.status_code == 200

            from app.telegram.worker import TelegramAIWorker

            worker = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)
            worker.process_pending_once()

            # Verify AI Agent called with proper headers and payload
            mock_http.assert_called_once()
            call_url = mock_http.call_args[0][0]
            assert "/internal/v1/telegram/turn" in call_url
            call_headers = mock_http.call_args[1]["headers"]
            assert call_headers["Authorization"] == f"Bearer {settings.GATEWAY_TO_AGENT_SECRET}"
            req_body = mock_http.call_args[1]["json"]
            assert req_body["request_id"] == "tg-2002"
            assert req_body["gateway_user_id"] == user_id

            # Verify messages sent to Telegram
            assert mock_send.call_count == 2

    # Verify update status in DB
    with app.state.session_factory() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=2002).first()
        assert up is not None
        assert up.status == "processed"
        assert up.delivery_status == "delivered"
        assert "Nguyễn Văn A" in json.loads(up.ai_response)[0]


def test_telegram_ai_forwarding_uses_cached_response(temp_db_path, fernet_key):
    app, client, settings = _setup(temp_db_path, fernet_key, enable_ai=True)

    with app.state.session_factory() as db:
        user = User(
            tenant="https://mycompany.odoo.com",
            database="test_db",
            odoo_uid=3,
            login="user3@company.com",
            name="Trần Thị B",
            active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        conn = TelegramConnection(
            user_id=user.id,
            telegram_user_id=55555,
            private_chat_id=55555,
            status="connected",
        )
        db.add(conn)

        # Pre-seed a TelegramUpdate with status 'ai_received' (e.g. restart after AI answered)
        cached_messages = ["Điểm tin đã được lưu trong cache"]
        up = TelegramUpdate(
            update_id=3003,
            user_id=user.id,
            payload=json.dumps({"message": {"chat": {"id": 55555}, "from": {"id": 55555}}}),
            status="ai_received",
            ai_response=json.dumps(cached_messages),
            delivery_status="none",
        )
        db.add(up)
        db.commit()

    with patch("httpx.post") as mock_http:
        with patch("app.telegram.client.TelegramClient.send_message") as mock_send:
            # Simulate processing the update

            # Direct call to service

            from app.telegram.worker import TelegramAIWorker

            worker = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)
            worker.process_pending_once()

            # AI was NOT called again
            mock_http.assert_not_called()
            # Cached message was delivered
            mock_send.assert_called_once()
            assert "Điểm tin đã được lưu trong cache" in mock_send.call_args[0][1]
