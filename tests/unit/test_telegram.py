from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.db import init_db
from app.models import LinkToken, TelegramConnection, User
from app.telegram.client import TelegramClient
from app.telegram.service import TelegramService


@pytest.mark.parametrize('choice', ['confirm', 'cancel'])
def test_confirmation_button_is_queued_as_a_scoped_command(telegram_components, choice):
    import json

    from app.models import TelegramUpdate

    service, sf, _, client = telegram_components
    service.enable_ai_forwarding = True
    update = {'update_id': 801, 'callback_query': {
        'id': 'click', 'from': {'id': 7}, 'data': f'{choice}:c_0123abcd',
        'message': {'message_id': 41, 'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 99, 'is_bot': True}},
    }}
    assert service.ingest_update(update)['status'] == 'ok'
    assert service.ingest_update(update)['duplicate']
    with sf() as db:
        stored = json.loads(db.query(TelegramUpdate).one().payload)
        assert stored['message']['text'] == f'/{choice} c_0123abcd'
        assert stored['message']['from']['id'] == 7
    client.answer_callback_query.assert_called_with('click')
    client.clear_inline_keyboard.assert_called_once_with(7, 41)


def test_confirmation_callback_in_group_is_ignored(telegram_components):
    service, _, _, client = telegram_components
    assert service.ingest_update({'update_id': 802, 'callback_query': {
        'id': 'click', 'from': {'id': 7}, 'data': 'confirm:c_0123abcd',
        'message': {'message_id': 41, 'chat': {'id': -1, 'type': 'group'}},
    }})['status'] == 'ignored'
    client.answer_callback_query.assert_called_once_with('click')
    client.clear_inline_keyboard.assert_not_called()


def test_preview_buttons_follow_the_last_chunk():
    from app.telegram.client import agent_delivery_chunks, normalize_agent_response

    delivery = normalize_agent_response({'messages': ['first last', 'next'],
                                         'confirmation_buttons': {'0': 'c_0123abcd'}})
    assert agent_delivery_chunks(delivery, str.split) == [
        'first', {'type': 'confirmation', 'text': 'last', 'confirmation_id': 'c_0123abcd'}, 'next',
    ]


def test_telegram_send_message_includes_inline_buttons(monkeypatch):
    post = MagicMock()
    post.return_value.status_code = 200
    post.return_value.json.return_value = {'ok': True, 'result': {'message_id': 41}}
    monkeypatch.setattr('app.telegram.client.httpx.post', post)
    TelegramClient('test-token').send_message(7, 'Xem trước báo giá', confirmation_id='c_0123abcd')
    assert post.call_args.kwargs['json']['reply_markup']['inline_keyboard'] == [[
        {'text': 'Xác nhận', 'callback_data': 'confirm:c_0123abcd'},
        {'text': 'Từ chối', 'callback_data': 'cancel:c_0123abcd'},
    ]]


@pytest.fixture
def telegram_components(temp_db_path):
    engine, session_factory = init_db(f"sqlite:///{temp_db_path}")

    # Create test user
    with session_factory() as db:
        user = User(
            tenant="mytenant",
            database="mydb",
            odoo_uid=42,
            login="user42@company.com",
            name="Employee Forty Two",
        )
        db.add(user)
        db.commit()
        user_id = user.id

    mock_client = MagicMock(spec=TelegramClient)
    service = TelegramService(
        session_factory=session_factory,
        telegram_client=mock_client,
        bot_username="MyOdooBot",
        webhook_secret="super_secret_webhook_token_123",
    )

    yield service, session_factory, user_id, mock_client
    engine.dispose()


def test_telegram_issue_link_and_qr(telegram_components):
    service, session_factory, user_id, _ = telegram_components

    link_data = service.issue_link(user_id=user_id)
    assert link_data.url.startswith("https://t.me/MyOdooBot?start=")


    assert link_data.qr_code_data_uri.startswith("data:image/png;base64,")
    assert link_data.expires_at > datetime.now(UTC)

    # Verify link token saved in DB
    with session_factory() as db:
        tokens = db.query(LinkToken).filter_by(user_id=user_id, purpose="telegram_link").all()
        assert len(tokens) == 1
        assert tokens[0].used_at is None

    # Issue second link -> first link must be superseded/invalidated
    _ = service.issue_link(user_id=user_id)
    with session_factory() as db:
        active_tokens = (
            db.query(LinkToken).filter_by(user_id=user_id, purpose="telegram_link").all()
        )
        # Old one deleted or invalidated, only 1 active link
        assert len(active_tokens) == 1


def test_help_uses_conversation_instead_of_command_templates(telegram_components):
    service, _, _, client = telegram_components
    service._handle_help(123)
    text = client.send_message.call_args.args[1]
    assert '/crm' not in text and '/status' not in text and '/help' not in text
    assert 'mô tả' in text and 'xem trước' in text


def test_telegram_pairing_success(telegram_components):
    service, session_factory, user_id, mock_client = telegram_components
    link_data = service.issue_link(user_id=user_id)
    raw_token = link_data.url.split("start=")[1]

    # Simulate /start <token> update from Telegram private chat
    update_data = {
        "update_id": 1001,
        "message": {
            "message_id": 50,
            "from": {
                "id": 987654321,
                "is_bot": False,
                "first_name": "FortyTwo",
                "username": "user42_tele",
            },
            "chat": {
                "id": 987654321,
                "type": "private",
            },
            "text": f"/start {raw_token}",
        },
    }

    service.process_update_dict(update_data)

    # Verify connection created
    with session_factory() as db:
        conn = db.query(TelegramConnection).filter_by(user_id=user_id).first()
        assert conn is not None
        assert conn.telegram_user_id == 987654321
        assert conn.private_chat_id == 987654321
        assert conn.username == "user42_tele"
        assert conn.status == "connected"

    # Verify confirmation message was sent
    mock_client.send_message.assert_called()


def test_telegram_pairing_expired_or_invalid_token(telegram_components):
    service, session_factory, user_id, mock_client = telegram_components

    update_data = {
        "update_id": 1002,
        "message": {
            "message_id": 51,
            "from": {"id": 1111, "is_bot": False},
            "chat": {"id": 1111, "type": "private"},
            "text": "/start invalid_or_expired_token",
        },
    }

    service.process_update_dict(update_data)

    with session_factory() as db:
        conn = db.query(TelegramConnection).filter_by(telegram_user_id=1111).first()
        assert conn is None

    # Should send a generic helpful message without revealing details
    call_args = mock_client.send_message.call_args
    msg_sent = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("text", "")
    assert "hết hạn" in msg_sent or "không hợp lệ" in msg_sent


def test_telegram_rejects_already_linked_to_different_user(telegram_components):
    service, session_factory, user_id, mock_client = telegram_components

    # Pre-link telegram_user_id 987654321 to another user
    with session_factory() as db:
        other_user = User(
            tenant="mytenant",
            database="mydb",
            odoo_uid=99,
            login="other@company.com",
            name="Other Staff",
        )
        db.add(other_user)
        db.flush()
        db.add(
            TelegramConnection(
                user_id=other_user.id,
                telegram_user_id=987654321,
                private_chat_id=987654321,
                username="other_tele",
                status="connected",
            )
        )
        db.commit()

    link_data = service.issue_link(user_id=user_id)
    raw_token = link_data.url.split("start=")[1]

    update_data = {
        "update_id": 1003,
        "message": {
            "message_id": 52,
            "from": {"id": 987654321, "is_bot": False},
            "chat": {"id": 987654321, "type": "private"},
            "text": f"/start {raw_token}",
        },
    }

    service.process_update_dict(update_data)

    # Ensure user_id did NOT steal the connection
    with session_factory() as db:
        conn = db.query(TelegramConnection).filter_by(user_id=user_id).first()
        assert conn is None
