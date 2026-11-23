"""Bind each quotation PDF button to one Odoo record and one Odoo write version."""
import secrets
import time

from sqlalchemy import BigInteger, Column, Float, Integer, String, Text

from app.models import Base


class QuotePdfMessage(Base):
    __tablename__ = 'quote_pdf_messages'
    token = Column(String(32), primary_key=True)
    user_id = Column(Integer, nullable=False)
    tenant = Column(String(500), nullable=False)
    database = Column(String(250), nullable=False)
    order_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)
    version = Column(String(64), nullable=False)
    telegram_user_id = Column(BigInteger, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    binding_id = Column(String(64), nullable=False)
    message_id = Column(Integer, nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)


class QuoteApprovalDelivery(Base):
    __tablename__ = 'quote_approval_deliveries'
    token = Column(String(32), primary_key=True)
    pdf_token = Column(String(32), nullable=False)
    rule_id = Column(Integer, nullable=False)
    approver_user_id = Column(Integer, nullable=False)
    order_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)
    version = Column(String(64), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    telegram_user_id = Column(BigInteger, nullable=False)
    binding_id = Column(String(64), nullable=False)
    message_id = Column(Integer, nullable=True)
    status = Column(String(24), nullable=False, default='pending')
    feedback_text = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)


def create_pdf_message(sf, user, scope, result):
    version = result.get('write_date')
    company = result.get('company_id')
    if not version or not isinstance(company, (list, tuple)) or not company:
        raise ValueError('Odoo did not return the quotation write version or company')
    token = secrets.token_hex(12)
    with sf() as db:
        db.add(QuotePdfMessage(token=token, user_id=user.id, tenant=user.tenant,
            database=user.database, order_id=result['id'], company_id=company[0],
            version=str(version), telegram_user_id=scope.telegram_user_id,
            chat_id=scope.chat_id, binding_id=scope.telegram_binding_id))
        db.commit()
    buttons = {'inline_keyboard': [[
        {'text': 'Chỉnh sửa', 'callback_data': 'qe:' + token},
        {'text': 'Xác nhận gửi duyệt', 'callback_data': 'qs:' + token},
    ]]}
    return token, buttons


def mark_pdf_delivered(sf, token, message_id):
    if type(message_id) is not int:
        raise ValueError('Telegram did not confirm a PDF message ID')
    with sf() as db:
        row = db.get(QuotePdfMessage, token)
        if not row:
            raise ValueError('Quotation PDF button token was not persisted')
        row.message_id = message_id
        db.commit()


def send_pdf_with_buttons(client, sf, user, scope, result, filename, content, caption):
    token, buttons = create_pdf_message(sf, user, scope, result)
    sent = client.send_document(scope.chat_id, filename, content, caption=caption,
        content_type='application/pdf', reply_markup=buttons)
    mark_pdf_delivered(sf, token, sent.get('message_id') if isinstance(sent, dict) else None)
    return sent


def find_pdf_message(sf, token, actor_id, chat_id, message_id):
    with sf() as db:
        row = db.get(QuotePdfMessage, token)
        if (not row or row.telegram_user_id != actor_id or row.chat_id != chat_id
                or row.message_id != message_id):
            return None
        return {'user_id': row.user_id, 'tenant': row.tenant, 'database': row.database,
                'order_id': row.order_id, 'company_id': row.company_id,
                'version': row.version, 'binding_id': row.binding_id}
