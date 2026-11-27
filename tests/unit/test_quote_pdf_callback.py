"""PDF button callbacks are mapped through persisted message identity."""
import json
import time
from unittest.mock import ANY, Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, TelegramConnection, TelegramUpdate, User
from app.quotations.pdf_review import QuotationPdfToken
from app.telegram.service import TelegramService


def test_callback_uses_pdf_token_and_duplicate_update_is_idempotent():
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    token = 'a' * 24
    with sf() as db:
        db.add(User(id=1, tenant='tenant', database='db', odoo_uid=5,
                    login='sale', name='Sale'))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7, private_chat_id=7,
                                  binding_id='binding', status='connected'))
        db.add(QuotationPdfToken(token=token, role='sale', user_id=1, tenant='tenant',
            database='db', chat_id=7, telegram_binding_id='binding', order_id=97,
            order_name='S00097', company_id=3, order_version='v1',
            message_id=44, active=1, created_at=time.time()))
        db.commit()
    client = Mock()
    service = TelegramService(sf, client, 'bot', 'secret', enable_ai_forwarding=True)
    callback = {'update_id': 901, 'callback_query': {'id': 'click-1',
        'data': 'qedit:' + token, 'from': {'id': 7},
        'message': {'message_id': 44, 'chat': {'id': 7, 'type': 'private'}}}}
    assert service.ingest_update(callback)['status'] == 'ok'
    assert service.ingest_update(callback).get('duplicate') is True
    with sf() as db:
        updates = db.query(TelegramUpdate).all()
        assert len(updates) == 1
        assert json.loads(updates[0].payload)['message']['text'] == '/quote_edit ' + token
    wrong = {**callback, 'update_id': 902, 'callback_query': {
        **callback['callback_query'], 'message': {'message_id': 45,
            'chat': {'id': 7, 'type': 'private'}}}}
    assert service.ingest_update(wrong)['status'] == 'ignored'
    confirm = {**callback, 'update_id': 903, 'callback_query': {
        **callback['callback_query'], 'id': 'click-3', 'data': 'qappr:' + token}}
    assert service.ingest_update(confirm)['status'] == 'ok'
    old_button = {**confirm, 'update_id': 904, 'callback_query': {
        **confirm['callback_query'], 'id': 'click-4', 'data': 'qa:' + token}}
    assert service.ingest_update(old_button)['status'] == 'ignored'
    typed = {'update_id': 905, 'message': {'chat': {'id': 7, 'type': 'private'},
        'from': {'id': 7}, 'text': '/quote_approve ' + token}}
    assert service.ingest_update(typed)['status'] == 'ignored'
    client.answer_callback_query.assert_any_call('click-4', ANY)
    with sf() as db:
        updates = db.query(TelegramUpdate).order_by(TelegramUpdate.update_id).all()
        assert len(updates) == 2
        assert json.loads(updates[1].payload)['message']['text'] == '/quote_approve ' + token


def test_one_approver_can_select_two_different_quotations_without_mixup():
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    with sf() as db:
        db.add(User(id=2, tenant='tenant', database='db', odoo_uid=12,
                    login='approver', name='Approver'))
        db.add(TelegramConnection(user_id=2, telegram_user_id=8, private_chat_id=8,
                                  binding_id='approver-binding', status='connected'))
        for token, order_id, message_id in [('b' * 24, 97, 51), ('c' * 24, 98, 52)]:
            db.add(QuotationPdfToken(token=token, role='approver', user_id=2,
                tenant='tenant', database='db', chat_id=8,
                telegram_binding_id='approver-binding', order_id=order_id,
                order_name=f'S000{order_id}', company_id=3, order_version='v1',
                message_id=message_id, active=1, created_at=time.time()))
        db.commit()
    service = TelegramService(sf, Mock(), 'bot', 'secret', enable_ai_forwarding=True)
    for update_id, action, token, message_id in [
        (910, 'qgrant', 'b' * 24, 51), (911, 'qfeed', 'c' * 24, 52)]:
        callback = {'update_id': update_id, 'callback_query': {'id': str(update_id),
            'data': action + ':' + token, 'from': {'id': 8},
            'message': {'message_id': message_id, 'chat': {'id': 8, 'type': 'private'}}}}
        assert service.ingest_update(callback)['status'] == 'ok'
    with sf() as db:
        texts = [json.loads(row.payload)['message']['text'] for row in
                 db.query(TelegramUpdate).order_by(TelegramUpdate.update_id).all()]
    assert texts == ['/quote_grant ' + 'b' * 24, '/quote_feedback ' + 'c' * 24]


def test_same_telegram_account_uses_separate_sale_and_approver_messages():
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    sale_token, approval_token = 'a' * 24, 'b' * 24
    with sf() as db:
        db.add(User(id=1, tenant='tenant', database='db', odoo_uid=29,
                    login='both', name='Both roles'))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7, private_chat_id=7,
                                  binding_id='binding', status='connected'))
        db.add(QuotationPdfToken(token=sale_token, role='sale', user_id=1,
            tenant='tenant', database='db', chat_id=7, telegram_binding_id='binding',
            order_id=97, order_name='S00097', company_id=3, order_version='v1',
            message_id=44, active=1, created_at=time.time()))
        db.add(QuotationPdfToken(token=approval_token, role='approver', user_id=1,
            tenant='tenant', database='db', chat_id=7, telegram_binding_id='binding',
            order_id=97, order_name='S00097', company_id=3, order_version='v1',
            message_id=55, active=1, created_at=time.time()))
        db.commit()
    service = TelegramService(sf, Mock(), 'bot', 'secret', enable_ai_forwarding=True)
    for update_id, data, message_id in [(901, 'qappr:' + sale_token, 44),
                                        (902, 'qgrant:' + approval_token, 55)]:
        callback = {'update_id': update_id, 'callback_query': {'id': str(update_id),
            'data': data, 'from': {'id': 7}, 'message': {'message_id': message_id,
            'chat': {'id': 7, 'type': 'private'}}}}
        assert service.ingest_update(callback)['status'] == 'ok'
    with sf() as db:
        texts = [json.loads(row.payload)['message']['text'] for row in
                 db.query(TelegramUpdate).order_by(TelegramUpdate.update_id).all()]
    assert texts == ['/quote_approve ' + sale_token, '/quote_grant ' + approval_token]
