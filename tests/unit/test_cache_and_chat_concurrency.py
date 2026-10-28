import json
import time
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.internal.cache import IdentityVerificationCache
from app.models import Base, TelegramConnection, TelegramUpdate, User
from app.telegram.client import TelegramClient
from app.telegram.worker import TelegramAIWorker


@pytest.fixture
def thread_safe_db():
    # SQLite memory with StaticPool allows all threads in the process to share the exact same in-memory DB
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session


def test_identity_cache_hit_and_ttl():
    """Kiểm tra cache 60s hit, miss và hết hạn TTL."""
    cache = IdentityVerificationCache(ttl_seconds=1.0)
    key = "user:1:test"
    data = {"permissions": ["tasks.read"], "job_title": "Developer"}

    # 1. Miss ban đầu
    assert cache.get(key) is None

    # 2. Set và Hit
    cache.set(key, data)
    assert cache.get(key) == data

    # 3. Hết hạn sau 1.1s
    time.sleep(1.1)
    assert cache.get(key) is None


def test_identity_cache_invalidation():
    """Kiểm tra vô hiệu hóa cache khi người dùng thay đổi thông tin."""
    cache = IdentityVerificationCache(ttl_seconds=60.0)
    cache.set("42:tenant:db:uid:hash1", {"title": "A"})
    cache.set("42:tenant:db:uid:hash2", {"title": "B"})
    cache.set("99:tenant:db:uid:hash3", {"title": "C"})

    assert cache.size == 3
    # Vô hiệu hóa user 42
    removed = cache.invalidate_user(42)
    assert removed == 2
    assert cache.get("42:tenant:db:uid:hash1") is None
    assert cache.get("99:tenant:db:uid:hash3") is not None


def test_worker_chat_concurrency_and_fifo(thread_safe_db):
    """Kiểm tra worker hỗ trợ 2 chat khác nhau song song, nhưng 1 chat luôn xử lý đúng thứ tự FIFO."""
    Session = thread_safe_db
    settings = Settings()
    mock_client = MagicMock(spec=TelegramClient)
    worker = TelegramAIWorker(
        settings=settings, session_factory=Session, telegram_client=mock_client
    )

    with Session() as db:
        user1 = User(id=1, tenant="t1", database="db1", odoo_uid=101, login="u1@test.com", name="User 1", active=True)
        user2 = User(id=2, tenant="t2", database="db2", odoo_uid=102, login="u2@test.com", name="User 2", active=True)
        conn1 = TelegramConnection(id=1, user_id=1, telegram_user_id=1001, private_chat_id=1001, status="connected")
        conn2 = TelegramConnection(id=2, user_id=2, telegram_user_id=1002, private_chat_id=1002, status="connected")
        db.add_all([user1, user2, conn1, conn2])

        # Chat 1 có 2 tin nhắn: msg 1 và msg 2 (cùng chat_id=1001)
        up1 = TelegramUpdate(
            id=1,
            update_id=101,
            payload=json.dumps({"message": {"chat": {"id": 1001}, "from": {"id": 1001}, "text": "Tin 1 chat 1"}}),
            status="pending",
        )
        up2 = TelegramUpdate(
            id=2,
            update_id=102,
            payload=json.dumps({"message": {"chat": {"id": 1001}, "from": {"id": 1001}, "text": "Tin 2 chat 1"}}),
            status="pending",
        )
        # Chat 2 có 1 tin nhắn: msg 3 (chat_id=1002)
        up3 = TelegramUpdate(
            id=3,
            update_id=103,
            payload=json.dumps({"message": {"chat": {"id": 1002}, "from": {"id": 1002}, "text": "Tin 1 chat 2"}}),
            status="pending",
        )
        db.add_all([up1, up2, up3])
        db.commit()

    # Trong lượt 1: worker phải chọn up1 (chat 1) và up3 (chat 2), còn up2 (chat 1) phải bị hoãn do chat 1 đang bận
    processed_calls: list[int] = []

    def mock_process(update_db_id: int):
        processed_calls.append(update_db_id)
        # Giả lập thành công: đánh dấu processed
        with Session() as s:
            rec = s.query(TelegramUpdate).filter_by(id=update_db_id).first()
            if rec:
                rec.status = "processed"
                s.commit()
        return True

    with patch.object(worker, "_process_single_update", side_effect=mock_process):
        count_batch1 = worker.process_pending_once()
        assert count_batch1 == 2
        # Cả up1 và up3 đã được xử lý (2 chat độc lập)
        assert 1 in processed_calls
        assert 3 in processed_calls
        assert 2 not in processed_calls  # up2 của chat 1 chưa được chạy

        # Lượt 2: chat 1 đã rảnh, up2 giờ mới được xử lý
        count_batch2 = worker.process_pending_once()
        assert count_batch2 == 1
        assert 2 in processed_calls
        # Thứ tự đối với chat 1: 1 chạy trước 2 (FIFO tuyệt đối)
        idx1 = processed_calls.index(1)
        idx2 = processed_calls.index(2)
        assert idx1 < idx2

    worker.stop()
