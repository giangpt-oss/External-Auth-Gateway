import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Base, TelegramUpdate
from app.telegram.service import TelegramService
from app.telegram.worker import TelegramAIWorker


@pytest.fixture
def test_setup():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    mock_client = MagicMock()
    service = TelegramService(
        session_factory=Session,
        telegram_client=mock_client,
        bot_username="test_bot",
        webhook_secret="test_secret",
        enable_ai_forwarding=True,
    )
    settings = Settings()
    worker = TelegramAIWorker(
        settings=settings, session_factory=Session, telegram_client=mock_client
    )
    return Session, service, worker, mock_client


def test_gateway_commands_classified_before_commit(test_setup):
    """Test that gateway commands (/start, /help, /crm@bot, /status) are classified as gateway_command
    and transition to processed on success, never pending in AI queue.
    """
    Session, service, worker, mock_client = test_setup

    update = {
        "update_id": 1001,
        "message": {
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345, "is_bot": False, "username": "testuser"},
            "text": "/crm@test_bot Lead search",
        },
    }

    res = service.ingest_update(update)
    assert res["status"] == "ok"

    with Session() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=1001).first()
        assert up is not None
        # On success, it is processed, but it was NEVER in AI worker queue
        assert up.status == "processed"

    # AI worker should find 0 pending updates
    assert worker.process_pending_once() == 0


def test_gateway_command_failure_sets_gateway_command_failed(test_setup):
    """Test that if a gateway command fails with an exception, status transitions to gateway_command_failed,
    never retried by AI worker.
    """
    Session, service, worker, mock_client = test_setup

    # Mock handler to raise exception
    service._handle_status = MagicMock(side_effect=RuntimeError("Database down"))

    update = {
        "update_id": 1002,
        "message": {
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345, "is_bot": False},
            "text": "/status",
        },
    }

    res = service.ingest_update(update)
    assert res["status"] == "ok"

    with Session() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=1002).first()
        assert up.status == "gateway_command_failed"

    # AI worker never touches gateway_command_failed
    assert worker.process_pending_once() == 0


def test_startup_recovery_quarantines_legacy_commands(test_setup):
    """Test that legacy pending updates containing gateway commands are quarantined
    during recover_on_startup so AI worker does not call AI on them.
    """
    Session, service, worker, mock_client = test_setup

    with Session() as db:
        up = TelegramUpdate(
            update_id=1003,
            payload=json.dumps(
                {
                    "update_id": 1003,
                    "message": {
                        "chat": {"id": 12345, "type": "private"},
                        "from": {"id": 12345, "is_bot": False},
                        "text": "/start token_abc_123",
                    },
                }
            ),
            status="pending",
        )
        db.add(up)
        db.commit()

    worker.recover_on_startup()

    with Session() as db:
        up = db.query(TelegramUpdate).filter_by(update_id=1003).first()
        assert up.status == "gateway_command"

    # AI worker should not process this
    assert worker.process_pending_once() == 0


def test_action_status_with_id_is_forwarded_to_agent(test_setup):
    Session, service, _, _ = test_setup
    service._handle_status = MagicMock()
    service.ingest_update({"update_id": 1004, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/status c_12345678"}})
    service._handle_status.assert_not_called()
    with Session() as db:
        assert db.query(TelegramUpdate).filter_by(update_id=1004).one().status == "pending"


def test_recovery_does_not_quarantine_action_status(test_setup):
    Session, _, worker, _ = test_setup
    with Session() as db:
        db.add(TelegramUpdate(update_id=1005, status="pending", payload=json.dumps({"message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/status c_12345678"}})))
        db.commit()
    worker.recover_on_startup()
    with Session() as db:
        assert db.query(TelegramUpdate).filter_by(update_id=1005).one().status == "pending"
