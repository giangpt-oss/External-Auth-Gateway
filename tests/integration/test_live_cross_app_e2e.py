import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.db import init_db
from app.models import GoogleConnection, TelegramConnection, TelegramUpdate, User

_logger = logging.getLogger(__name__)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockTelegramHandler(BaseHTTPRequestHandler):
    received_messages = []
    on_send_hook: Callable[[dict], None] | None = None

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}

        if "text" in payload:
            MockTelegramHandler.received_messages.append(payload)

            # Trigger hook if set (e.g. revoke user permissions after chunk 1)
            if MockTelegramHandler.on_send_hook:
                try:
                    MockTelegramHandler.on_send_hook(payload)
                except Exception as e:
                    _logger.error("Error in on_send_hook: %s", e)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = json.dumps(
            {"ok": True, "result": {"message_id": len(MockTelegramHandler.received_messages)}}
        )
        self.wfile.write(response.encode("utf-8"))

    def log_message(self, format, *args):
        # Silence standard HTTP access logging in tests
        pass


@pytest.fixture(scope="module")
def mock_tg_server():
    port = find_free_port()
    MockTelegramHandler.received_messages = []
    MockTelegramHandler.on_send_hook = None
    server = HTTPServer(("127.0.0.1", port), MockTelegramHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, MockTelegramHandler
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def live_cross_app_env(tmp_path_factory, mock_tg_server):
    mock_tg_port, tg_handler = mock_tg_server
    tmp_path = tmp_path_factory.mktemp("cross_app")
    db_path = tmp_path / "gateway_live.db"
    db_url = f"sqlite:///{db_path}"

    agent_port = find_free_port()
    gw_port = find_free_port()

    gw_to_agent_secret = "gw_to_agent_secret_32_chars_long_11111"
    agent_to_gw_secret = "agent_to_gw_secret_32_chars_long_22222"
    webhook_secret = "telegram_webhook_secret_token_live"
    from cryptography.fernet import Fernet
    fernet_key = Fernet.generate_key().decode()

    # 1. Initialize SQLite Database schema & seed initial user and connections
    engine, session_factory = init_db(db_url)
    with session_factory() as db:
        user1 = User(
            id=1,
            tenant="https://erp.mycompany.com",
            database="prod_db",
            odoo_uid=101,
            login="cuong.le@company.com",
            name="Lê Văn Cường",
            active=True,
        )
        tele_conn = TelegramConnection(
            id=1,
            user_id=1,
            telegram_user_id=777,
            private_chat_id=777,
            username="cuongle",
            binding_id="tg_bind_e2e_123",
            status="connected",
        )
        google_conn = GoogleConnection(
            id=1,
            user_id=1,
            google_sub="google_sub_123456",
            email="cuong.le@company.com",
            encrypted_access_token="dummy_enc_access",
            encrypted_refresh_token="dummy_enc_refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="connected",
            scopes="https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.readonly",
        )
        from app.models import OdooCredential
        from app.security import encrypt_secret
        odoo_cred = OdooCredential(
            id=1,
            user_id=1,
            credential_kind="password",
            encrypted_secret=encrypt_secret("test_pass", fernet_key),
        )
        db.add_all([user1, tele_conn, google_conn, odoo_cred])
        db.commit()

    import sys
    from pathlib import Path

    gw_dir = Path(__file__).resolve().parents[2]
    agent_dir_cand = gw_dir.parents[1] / "Module AI - Agent"
    if not agent_dir_cand.exists():
        agent_dir_cand = Path(r"D:\ODOO\Module AI - Agent")
    if not agent_dir_cand.exists():
        agent_dir_cand = Path(r"D:\Module AI - Agent")
    agent_dir = agent_dir_cand.resolve()

    agent_py = agent_dir / ".venv" / "Scripts" / "python.exe"
    agent_python = str(agent_py) if agent_py.exists() else sys.executable

    gw_py = gw_dir / ".venv" / "Scripts" / "python.exe"
    gw_python = str(gw_py) if gw_py.exists() else sys.executable

    # 2. Spawn Subprocess 1: AI Agent
    agent_env = os.environ.copy()
    agent_env.update(
        {
            "INTEGRATION_MODE": "gateway",
            "GATEWAY_BASE_URL": f"http://127.0.0.1:{gw_port}",
            "GATEWAY_TO_AGENT_SECRET": gw_to_agent_secret,
            "AGENT_TO_GATEWAY_SECRET": agent_to_gw_secret,
            "AGENT_DATA_DIR": str(tmp_path / "agent_data"),
            "PYTHONPATH": str(agent_dir),
        }
    )

    agent_proc = subprocess.Popen(
        [
            agent_python,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(agent_port),
        ],
        cwd=str(agent_dir),
        env=agent_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 3. Spawn Subprocess 2: Gateway Test Server
    gw_server_script = str(gw_dir / "tests" / "integration" / "run_gateway_test_server.py")
    gw_proc = subprocess.Popen(
        [
            gw_python,
            gw_server_script,
            "--port",
            str(gw_port),
            "--db-url",
            db_url,
            "--agent-url",
            f"http://127.0.0.1:{agent_port}",
            "--gw-to-agent-secret",
            gw_to_agent_secret,
            "--agent-to-gw-secret",
            agent_to_gw_secret,
            "--tg-mock-url",
            f"http://127.0.0.1:{mock_tg_port}",
            "--webhook-secret",
            webhook_secret,
            "--encryption-key",
            fernet_key,
        ],
        cwd=str(gw_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 4. Wait for both servers to be ready
    def wait_ready(url, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = httpx.get(url, timeout=1.0)
                if r.status_code in (200, 404):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = httpx.get(url, timeout=1.0)
                if r.status_code in (200, 404):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    agent_ready = wait_ready(f"http://127.0.0.1:{agent_port}/")
    gw_ready = wait_ready(f"http://127.0.0.1:{gw_port}/healthz")

    try:
        assert agent_ready, f"AI Agent on port {agent_port} failed to start in time"
        assert gw_ready, f"Gateway on port {gw_port} failed to start in time"

        yield {
            "agent_port": agent_port,
            "gw_port": gw_port,
            "mock_tg_port": mock_tg_port,
            "webhook_secret": webhook_secret,
            "session_factory": session_factory,
            "tg_handler": tg_handler,
        }
    finally:
        # Guarantee termination of both subprocesses in finally block
        for proc in (agent_proc, gw_proc):
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def test_live_cross_app_connection_status_ok(live_cross_app_env):
    """Test full live HTTP roundtrip across both applications:
    Telegram Webhook -> Gateway -> Worker -> HTTP to AI Agent ->
    AI Agent HTTP to Gateway (resolve_identity & connections) ->
    AI Agent formats status response -> Gateway delivers to Mock Telegram.
    """
    env = live_cross_app_env
    gw_port = env["gw_port"]
    webhook_secret = env["webhook_secret"]
    tg_handler = env["tg_handler"]
    session_factory = env["session_factory"]

    tg_handler.received_messages.clear()
    tg_handler.on_send_hook = None

    webhook_payload = {
        "update_id": 9001,
        "message": {
            "message_id": 101,
            "date": int(time.time()),
            "chat": {"id": 777, "type": "private"},
            "from": {"id": 777, "is_bot": False, "first_name": "Cuong"},
            "text": "Trạng thái kết nối",
        },
    }

    # Post real HTTP webhook to Gateway
    resp = httpx.post(
        f"http://127.0.0.1:{gw_port}/webhooks/telegram",
        json=webhook_payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret},
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json().get("status") in ("ok", "accepted")

    # Wait for the worker to process, call AI Agent, and deliver to Telegram mock
    deadline = time.time() + 10
    delivered = False
    while time.time() < deadline:
        if len(tg_handler.received_messages) >= 1:
            delivered = True
            break
        time.sleep(0.3)

    assert delivered, "Message was not delivered to Telegram mock within timeout"

    msg_payload = tg_handler.received_messages[0]
    assert msg_payload["chat_id"] == 777
    text = msg_payload["text"]

    # Assert real rendered contract from AI Agent formatter
    assert "Đã kết nối" in text
    assert "cuong.le@company.com" in text
    assert "✅ Lịch Google" in text
    assert "✅ Gmail (đọc)" in text
    assert "Hệ thống Odoo ERP" in text

    # Verify DB record is completed
    with session_factory() as db:
        rec = db.query(TelegramUpdate).filter_by(update_id=9001).first()
        assert rec is not None
        assert rec.status == "processed"
        assert rec.delivery_status == "delivered"


def test_live_cross_app_connection_status_reconnect_required(live_cross_app_env):
    """Test live roundtrip when Google connection requires reconnection:
    Gateway reflects RECONNECT_REQUIRED -> AI Agent formats 'Cần kết nối lại' ->
    Delivered via Telegram.
    """
    env = live_cross_app_env
    gw_port = env["gw_port"]
    webhook_secret = env["webhook_secret"]
    tg_handler = env["tg_handler"]
    session_factory = env["session_factory"]

    # Update Google status in gateway DB
    with session_factory() as db:
        db.query(GoogleConnection).filter_by(user_id=1).update({"status": "reconnect_required"})
        db.commit()

    tg_handler.received_messages.clear()
    tg_handler.on_send_hook = None

    webhook_payload = {
        "update_id": 9002,
        "message": {
            "message_id": 102,
            "date": int(time.time()),
            "chat": {"id": 777, "type": "private"},
            "from": {"id": 777, "is_bot": False, "first_name": "Cuong"},
            "text": "Kiểm tra trạng thái kết nối",
        },
    }

    resp = httpx.post(
        f"http://127.0.0.1:{gw_port}/webhooks/telegram",
        json=webhook_payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret},
        timeout=5.0,
    )
    assert resp.status_code == 200

    deadline = time.time() + 10
    delivered = False
    while time.time() < deadline:
        if len(tg_handler.received_messages) >= 1:
            delivered = True
            break
        time.sleep(0.3)

    assert delivered, "Message was not delivered to Telegram mock within timeout"
    msg_payload = tg_handler.received_messages[0]
    text = msg_payload["text"]

    assert "Cần kết nối lại" in text


def test_live_cross_app_multi_chunk_revocation_stops_subsequent_chunks(live_cross_app_env):
    """Test live scenario where a multi-chunk response is in delivery,
    and recipient's account is deactivated immediately after chunk 1 is delivered:
    Chunk 2 must NOT be sent, delivery_index preserved at 1, status marked failed.
    """
    env = live_cross_app_env
    tg_handler = env["tg_handler"]
    session_factory = env["session_factory"]

    # Re-enable user 1 and Google status first
    with session_factory() as db:
        db.query(User).filter_by(id=1).update({"active": True})
        db.query(GoogleConnection).filter_by(user_id=1).update({"status": "connected"})
        db.commit()

    tg_handler.received_messages.clear()

    chunk1 = "Live Multi-Chunk Test - Part 1"
    chunk2 = "Live Multi-Chunk Test - Part 2: Confidential"
    messages = [chunk1, chunk2]

    # Hook: as soon as chunk 1 is sent to Telegram, revoke user 1!
    def revoke_after_chunk1(payload):
        if chunk1 in payload.get("text", ""):
            with session_factory() as db:
                db.query(User).filter_by(id=1).update({"active": False})
                db.commit()

    tg_handler.on_send_hook = revoke_after_chunk1

    # Insert a multi-chunk update directly into the queue
    with session_factory() as db:
        up = TelegramUpdate(
            update_id=9003,
            user_id=1,
            payload=json.dumps(
                {"message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "Report"}}
            ),
            status="ai_received",
            ai_response=json.dumps(messages),
            delivery_status="none",
        )
        db.add(up)
        db.commit()

    # Wait for the live worker thread to process chunk 1, attempt chunk 2, and halt
    deadline = time.time() + 10
    halted = False
    while time.time() < deadline:
        with session_factory() as db:
            rec = db.query(TelegramUpdate).filter_by(update_id=9003).first()
            if rec and rec.status == "failed" and rec.delivery_status == "failed":
                halted = True
                break
        time.sleep(0.3)

    assert halted, "Update was not marked failed within timeout"

    # Crucial live assertions:
    # Filter messages belonging to this test
    test_chunks = [
        m for m in tg_handler.received_messages if "Live Multi-Chunk" in m.get("text", "")
    ]
    # 1. Telegram mock received ONLY ONE message (chunk 1), NOT chunk 2!
    assert len(test_chunks) == 1
    assert test_chunks[0]["text"] == chunk1

    # 2. Database state confirms chunk 1 progress was preserved and claim cleared
    with session_factory() as db:
        rec = db.query(TelegramUpdate).filter_by(update_id=9003).first()
        assert rec.delivery_index == 1
        assert rec.status == "failed"
        assert rec.delivery_status == "failed"
        assert rec.claim_token is None
        assert rec.lease_expires_at is None
