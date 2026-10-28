import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from app.db import init_db
from app.models import GoogleConnection, TelegramConnection, User

_logger = logging.getLogger(__name__)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockTelegramHandler(BaseHTTPRequestHandler):
    received_messages = []

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}

        if "text" in payload:
            MockTelegramHandler.received_messages.append(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = json.dumps(
            {"ok": True, "result": {"message_id": len(MockTelegramHandler.received_messages)}}
        )
        self.wfile.write(response.encode("utf-8"))

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def mock_tg_server():
    port = find_free_port()
    MockTelegramHandler.received_messages = []
    server = HTTPServer(("127.0.0.1", port), MockTelegramHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, MockTelegramHandler
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def full_lifecycle_env(tmp_path_factory, mock_tg_server):
    mock_tg_port, tg_handler = mock_tg_server
    tmp_path = tmp_path_factory.mktemp("full_lifecycle")
    db_path = tmp_path / "gw_lifecycle.db"
    db_url = f"sqlite:///{db_path}"

    agent_port = find_free_port()
    gw_port = find_free_port()

    gw_to_agent_secret = "gw_to_agent_secret_32_chars_long_11111"
    agent_to_gw_secret = "agent_to_gw_secret_32_chars_long_22222"
    webhook_secret = "tg_webhook_secret_full_lifecycle"
    from cryptography.fernet import Fernet
    fernet_key = Fernet.generate_key().decode()

    # 1. Initialize SQLite Database schema & seed initial user and connections
    engine, session_factory = init_db(db_url)
    with session_factory() as db:
        user1 = User(
            id=1,
            tenant="https://erp.lifecycle.com",
            database="prod_db",
            odoo_uid=202,
            login="tran.van.b@lifecycle.com",
            name="Trần Văn B",
            active=True,
        )
        tele_conn = TelegramConnection(
            id=1,
            user_id=1,
            telegram_user_id=999,
            private_chat_id=999,
            username="tranvanb",
            binding_id="tg_bind_life_123",
            status="connected",
        )
        google_conn = GoogleConnection(
            id=1,
            user_id=1,
            google_sub="google_sub_life_999",
            email="tran.van.b@lifecycle.com",
            encrypted_access_token="dummy_access",
            encrypted_refresh_token="dummy_refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            status="connected",
            scopes="https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/gmail.send",
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

    gw_dir = Path(__file__).resolve().parents[2]
    agent_dir_cand = gw_dir.parents[1] / "Module AI - Agent"
    if not agent_dir_cand.exists():
        agent_dir_cand = Path(r"D:\ODOO\Module AI - Agent")
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

    agent_log_path = tmp_path / "agent.log"
    gw_log_path = tmp_path / "gw.log"
    agent_log = open(agent_log_path, "w", encoding="utf-8")
    gw_log = open(gw_log_path, "w", encoding="utf-8")

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
        stdout=agent_log,
        stderr=subprocess.STDOUT,
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
        stdout=gw_log,
        stderr=subprocess.STDOUT,
    )

    # 4. Wait for both servers to be ready
    def wait_ready(url, timeout=25):
        start = time.time()
        while time.time() - start < timeout:
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

    if not agent_ready or not gw_ready:
        agent_proc.terminate()
        gw_proc.terminate()
        agent_log_content = agent_log_path.read_text(encoding="utf-8", errors="replace") if agent_log_path.exists() else ""
        gw_log_content = gw_log_path.read_text(encoding="utf-8", errors="replace") if gw_log_path.exists() else ""
        pytest.fail(f"Servers failed to boot in time: agent={agent_ready}, gw={gw_ready}\nAGENT LOG:\n{agent_log_content}\nGW LOG:\n{gw_log_content}")

    yield {
        "gw_port": gw_port,
        "agent_port": agent_port,
        "mock_tg_port": mock_tg_port,
        "tg_handler": tg_handler,
        "webhook_secret": webhook_secret,
        "session_factory": session_factory,
        "tmp_path": tmp_path,
    }

    # Cleanup processes
    agent_proc.terminate()
    gw_proc.terminate()
    agent_proc.wait(timeout=5)
    gw_proc.wait(timeout=5)


def send_tg_webhook_message(env, update_id: int, user_id: int, chat_id: int, text: str):
    headers = {"X-Telegram-Bot-Api-Secret-Token": env["webhook_secret"]}
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": int(time.time()),
            "from": {"id": user_id, "first_name": "Test", "username": "testuser"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }
    resp = httpx.post(
        f"http://127.0.0.1:{env['gw_port']}/webhooks/telegram",
        json=payload,
        headers=headers,
        timeout=5.0,
    )
    assert resp.status_code == 200


def wait_for_mock_tg_messages(env, min_count: int, timeout: float = 12.0):
    start = time.time()
    while time.time() - start < timeout:
        if len(env["tg_handler"].received_messages) >= min_count:
            return env["tg_handler"].received_messages
        time.sleep(0.3)
    return env["tg_handler"].received_messages


def test_full_lifecycle_cross_app_task_create_preview_confirm(full_lifecycle_env):
    env = full_lifecycle_env
    env["tg_handler"].received_messages.clear()

    base_uid = int(time.time() * 1000)

    # Bước 1: Người dùng gửi lệnh tạo task
    send_tg_webhook_message(
        env,
        update_id=base_uid,
        user_id=999,
        chat_id=999,
        text="/task create Chuẩn bị slide báo cáo | 2026-10-31 | high",
    )

    # Đợi bot gửi phản hồi Preview vào Mock Telegram
    msgs = wait_for_mock_tg_messages(env, min_count=1)
    assert len(msgs) >= 1
    preview_body = msgs[-1].get("text", "")
    assert "Bản xem trước tạo công việc cá nhân" in preview_body
    assert "/confirm c_" in preview_body

    # Trích xuất confirmation ID
    import re
    m = re.search(r"/confirm\s+(c_[a-f0-9]+)", preview_body)
    assert m is not None
    conf_id = m.group(1)

    # Bước 2: Người dùng gửi /confirm <conf_id>
    send_tg_webhook_message(
        env,
        update_id=base_uid + 1,
        user_id=999,
        chat_id=999,
        text=f"/confirm {conf_id}",
    )

    # Đợi bot gửi xác nhận hoàn tất
    msgs = wait_for_mock_tg_messages(env, min_count=2)
    assert len(msgs) >= 2
    success_body = msgs[-1].get("text", "")
    assert ("Thực hiện thành công" in success_body) or ("Đã tạo thành công" in success_body)
    assert "Chuẩn bị slide báo cáo" in success_body


def test_full_lifecycle_relink_invalidates_pending_confirmation(full_lifecycle_env):
    env = full_lifecycle_env
    env["tg_handler"].received_messages.clear()

    base_uid = int(time.time() * 1000) + 100

    # 1. Tạo preview
    send_tg_webhook_message(
        env,
        update_id=base_uid,
        user_id=999,
        chat_id=999,
        text="/task create Kế hoạch tài chính quý 4",
    )
    msgs = wait_for_mock_tg_messages(env, min_count=1)
    preview_body = msgs[-1].get("text", "")
    import re
    m = re.search(r"/confirm\s+(c_[a-f0-9]+)", preview_body)
    assert m is not None
    conf_id = m.group(1)

    # 2. Giả lập người dùng ghép nối lại Telegram (đổi binding_id mới trên Gateway)
    with env["session_factory"]() as db:
        tele = db.query(TelegramConnection).filter_by(user_id=1).first()
        tele.binding_id = "tg_bind_fresh_after_relink"
        db.commit()

    # 3. Người dùng thử confirm mã cũ -> Phải bị từ chối do binding_id không còn khớp!
    send_tg_webhook_message(
        env,
        update_id=base_uid + 1,
        user_id=999,
        chat_id=999,
        text=f"/confirm {conf_id}",
    )
    msgs = wait_for_mock_tg_messages(env, min_count=2)
    rejection_body = msgs[-1].get("text", "")
    assert "Liên kết Telegram của bạn đã được thay đổi kể từ lúc tạo yêu cầu" in rejection_body
