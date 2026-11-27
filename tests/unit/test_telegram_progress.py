"""Unit tests for Telegram live progress checklist & typing keepalive."""
import time
from unittest.mock import MagicMock

from app.telegram.progress import (
    TurnProgressTracker,
    infer_progress_stages,
    render_progress_message,
)


def test_infer_progress_stages_contextual():
    t1, s1 = infer_progress_stages("Tạo đơn mua hàng cho NCC ABC")
    assert "mua hàng" in t1.lower()
    assert len(s1) == 3

    t2, _ = infer_progress_stages("Hoàn tất giao hàng WH/OUT/00018")
    assert "giao hàng" in t2.lower()

    t3, _ = infer_progress_stages("Lập báo cáo bán hàng tháng 11")
    assert "báo cáo" in t3.lower()

    t4, _ = infer_progress_stages("/confirm c_12345678")
    assert "xác nhận" in t4.lower()


def test_progress_does_not_claim_completion_based_on_elapsed_time():
    title, stages = infer_progress_stages("Tạo báo giá cho Anh Diệp")
    for seconds in (2.1, 6.0, 16.0):
        message = render_progress_message(title, stages, seconds)
        assert "✅" not in message
        assert all(stage in message for stage in stages)
        assert f"{int(seconds)}s" in message
    assert "phản hồi hơi chậm (16s)" in message


def test_fast_turn_never_sends_temp_message():
    client = MagicMock()
    with TurnProgressTracker(client, 123, "hello", initial_delay_s=0.5, tick_s=0.2):
        time.sleep(0.05)
    client.send_chat_action.assert_called_once_with(123, "typing")
    client.send_message.assert_not_called()
    client.delete_message.assert_not_called()


def test_slow_turn_sends_edits_and_deletes_progress_message():
    client = MagicMock()
    client.send_message.return_value = {"message_id": 999}
    with TurnProgressTracker(client, 123, "Tạo đơn mua hàng", initial_delay_s=0.05, tick_s=0.08):
        time.sleep(0.22)
    assert client.send_message.call_count == 1
    assert client.send_chat_action.call_count >= 2
    client.delete_message.assert_called_once_with(123, 999)
