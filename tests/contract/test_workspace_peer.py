"""Opt-in real HTTP round trip through Agent and Gateway, with disposable PostgreSQL."""

import asyncio
import json
import os
import secrets
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import Settings
from app.db import normalize_db_url
from app.main import create_app
from app.models import (
    ActionOperation,
    GoogleConnection,
    OdooCredential,
    TelegramConnection,
    TelegramUpdate,
    User,
)
from app.security import encrypt_secret
from app.telegram.worker import TelegramAIWorker
from app.workspace.models import WorkspaceJob
from app.workspace.service import WorkspaceService
from app.workspace.worker import WorkspaceWorker


@pytest.fixture
def live_gateway(fernet_key, monkeypatch):
    db_url = os.getenv("WORKSPACE_PEER_POSTGRES_URL")
    agent_url = os.getenv("WORKSPACE_PEER_AGENT_URL")
    if not db_url or not agent_url:
        pytest.skip("Requires disposable PostgreSQL and the Agent workspace peer")
    engine = create_engine(normalize_db_url(db_url))
    with engine.begin() as db:
        assert db.dialect.name == "postgresql"
        db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    settings = Settings(
        _env_file=None, APP_ENV="test", DATABASE_URL=db_url, ENCRYPTION_KEY=fernet_key,
        ENABLE_AI_AGENT_FORWARDING=True, AI_AGENT_BASE_URL=agent_url,
        ODOO_URL="https://odoo.example.invalid", ODOO_DB="fixture",
        TELEGRAM_BOT_TOKEN="123456:fixture", TELEGRAM_WEBHOOK_SECRET="fixture-webhook",
        AGENT_TO_GATEWAY_SECRET="workspace-peer-agent-to-gateway-secret",
        GATEWAY_TO_AGENT_SECRET="workspace-peer-gateway-to-agent-secret",
        GOOGLE_WORKSPACE_MCP_ENABLED=False,
    )
    app = create_app(settings)
    chat_id = secrets.randbelow(10**12) + 1
    with app.state.session_factory() as db:
        user = User(tenant=settings.ODOO_URL, database="fixture", odoo_uid=chat_id % (2**31 - 1),
                    login=str(chat_id), name="Test")
        db.add(user)
        db.flush()
        uid = user.id
        conn = GoogleConnection(
            user_id=uid, google_sub=f"peer-{chat_id}", email="peer@example.invalid",
            scopes="https://www.googleapis.com/auth/drive",
            encrypted_access_token=encrypt_secret("fixture-google-token", fernet_key),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add_all([
            conn,
            OdooCredential(user_id=uid, credential_kind="api_key",
                           encrypted_secret=encrypt_secret("fixture-odoo-token", fernet_key)),
            TelegramConnection(user_id=uid, telegram_user_id=chat_id, private_chat_id=chat_id,
                               binding_id="peer-binding", status="connected"),
        ])
        db.commit()
        account_id = conn.id

    from app.odoo.service import OdooService

    monkeypatch.setattr(OdooService, "validate_user_account", Mock(return_value={"active": True}))
    monkeypatch.setattr(OdooService, "get_employee_profile", Mock(return_value={}))
    monkeypatch.setattr(OdooService, "compute_permissions", Mock(
        return_value=["workspace.drive.read", "workspace.drive.write"],
    ))
    calls = []

    def google_response(self, client, spec, url, params, kwargs, deadline):
        assert kwargs["headers"]["Authorization"] == "Bearer fixture-google-token"
        assert url == "https://www.googleapis.com/drive/v3/files"
        calls.append(spec["httpMethod"])
        if spec["httpMethod"] == "POST":
            assert kwargs["json"]["name"] == "Peer write.txt"
            return {"id": "peer-created", "name": "Peer write.txt"}
        return {"files": [{"id": "peer-file", "name": "Peer policy.txt"}]}

    monkeypatch.setattr(WorkspaceService, "_read_response", google_response)
    sock = socket.socket()
    sock.bind(("0.0.0.0", int(os.getenv("WORKSPACE_PEER_GATEWAY_PORT", "8765"))))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield app, settings, uid, account_id, chat_id, calls
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_telegram_workspace_round_trip(live_gateway, monkeypatch):
    app, settings, user_id, account_id, chat_id, calls = live_gateway
    send = Mock()
    monkeypatch.setattr("app.telegram.client.TelegramClient.send_message", send)
    monkeypatch.setattr("app.telegram.client.TelegramClient.send_chat_action", Mock())
    telegram = TelegramAIWorker(settings=settings, session_factory=app.state.session_factory)
    payload = {"update_id": chat_id, "message": {
        "message_id": 1, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False}, "text": "List my Workspace files",
    }}
    with TestClient(app) as client:
        def turn(update_id, message):
            payload["update_id"] = update_id
            payload["message"]["text"] = message
            response = client.post("/webhooks/telegram", json=payload, headers={
                "X-Telegram-Bot-Api-Secret-Token": settings.TELEGRAM_WEBHOOK_SECRET,
            })
            assert response.status_code == 200, response.text
            telegram.process_pending_once()

        for _ in range(2):
            turn(chat_id, "List my Workspace files")
        with app.state.session_factory() as db:
            job = db.query(WorkspaceJob).filter_by(request_id=f"tg-{chat_id}").one()
            assert (job.user_id, job.google_account_id, job.status) == (user_id, account_id, "queued")
            job_id = job.job_id

        async def process():
            async with httpx.AsyncClient() as http_client:
                worker = WorkspaceWorker(settings=settings, session_factory=app.state.session_factory,
                                         worker_id="peer-test", http_client=http_client)
                assert await worker.process_one_cycle()
                assert not await worker.deliver_pending_callback(job_id)

        asyncio.run(process())
        telegram.process_pending_once()
        telegram.process_pending_once()
        assert calls == ["GET"]
        turn(chat_id + 1, "Prepare a new file")
        assert calls == ["GET"]
        with app.state.session_factory() as db:
            preview = db.query(TelegramUpdate).filter_by(update_id=chat_id + 1).one()
            confirmation_id = json.loads(preview.ai_response)["confirmation_buttons"]["0"]
        turn(chat_id + 2, f"Confirm {confirmation_id}")
        turn(chat_id + 3, f"Confirm {confirmation_id}")
    assert calls == ["GET", "POST"]
    with app.state.session_factory() as db:
        job = db.query(WorkspaceJob).filter_by(job_id=job_id).one()
        assert job.status == "completed"
        assert job.callback_delivered_at is not None
        operation = db.query(ActionOperation).filter_by(user_id=user_id, action="workspace.drive.write").one()
        assert (operation.status, operation.google_account_id) == ("completed", account_id)
    results = [entry for entry in send.call_args_list if "Peer policy.txt" in str(entry)]
    assert len(results) == 1
    assert results[0].args[0] == chat_id
