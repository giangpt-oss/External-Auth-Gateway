import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.db import init_db
from app.jobs.scheduler import BackgroundScheduler
from app.jobs.service import JobService
from app.models import LinkToken, Outbox, Session, TelegramUpdate, User


@pytest.fixture
def scheduler_components(temp_db_path):
    engine, session_factory = init_db(f"sqlite:///{temp_db_path}")

    mock_google = MagicMock()
    mock_reminder = MagicMock()

    job_service = JobService(
        session_factory=session_factory,
        google_oauth=mock_google,
        reminder_service=mock_reminder,
    )

    yield job_service, session_factory
    engine.dispose()


def test_scheduler_lifecycle(scheduler_components):
    job_service, _ = scheduler_components
    scheduler = BackgroundScheduler(
        job_service=job_service,
        refresh_interval=1,
        reminder_interval=1,
    )

    scheduler.start()
    assert scheduler._thread is not None
    assert scheduler._thread.is_alive()

    # Let it run brief slice
    time.sleep(0.1)

    scheduler.stop(timeout=2.0)
    assert not scheduler._thread.is_alive()


def test_cleanup_old_data_job(scheduler_components):
    job_service, session_factory = scheduler_components
    now = datetime.now(UTC)
    old_date = now - timedelta(days=35)

    with session_factory() as db:
        user = User(
            id=1,
            tenant="https://odoo.test",
            database="testdb",
            odoo_uid=1,
            login="user1@example.com",
            name="User 1",
        )
        db.add(user)
        db.flush()
        # 1. Expired session
        db.add(
            Session(
                token_hash="exp_session",
                user_id=1,
                csrf_secret="csrf",
                expires_at=now - timedelta(hours=1),
            )
        )
        # 2. Expired link token
        db.add(
            LinkToken(
                token_hash="exp_link",
                purpose="telegram_link",
                user_id=1,
                expires_at=now - timedelta(minutes=1),
            )
        )
        # 3. Old processed update
        db.add(
            TelegramUpdate(
                update_id=123456,
                payload="{}",
                status="processed",
                created_at=old_date,
            )
        )
        # 4. Old sent outbox item
        db.add(
            Outbox(
                user_id=1,
                dedup_key="old_sent",
                payload="msg",
                status="sent",
                created_at=old_date,
            )
        )
        # 5. New pending outbox item (must NOT be deleted)
        db.add(
            Outbox(
                user_id=1,
                dedup_key="new_pending",
                payload="msg2",
                status="pending",
                created_at=now,
            )
        )
        db.commit()

    job_service.cleanup_old_data_job()

    with session_factory() as db:
        assert db.query(Session).filter_by(token_hash="exp_session").first() is None
        assert db.query(LinkToken).filter_by(token_hash="exp_link").first() is None
        assert db.query(TelegramUpdate).filter_by(update_id=123456).first() is None
        assert db.query(Outbox).filter_by(dedup_key="old_sent").first() is None
        assert db.query(Outbox).filter_by(dedup_key="new_pending").first() is not None
