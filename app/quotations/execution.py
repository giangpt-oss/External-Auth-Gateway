"""Confirmed quotation outbox. A claimed write is never automatically replayed."""
import json
import logging
import time

from sqlalchemy import Column, Float, Integer, String, Text

from app.models import ActionOperation, Base
from app.odoo.client import OdooNetworkError
from app.quotations.jobs import QuotePreparationWorker
from app.quotations.service import QuotationService
from app.quotations.tracking import begin_tracking, record_created
from app.reporting.context import authenticated_context
from app.reporting.schemas import TelegramScope


class QuoteExecutionJob(Base):
    __tablename__ = 'quotation_execution_jobs'
    operation_id = Column(String(64), primary_key=True)
    user_id = Column(Integer, primary_key=True)
    tenant = Column(String(500), nullable=False)
    database = Column(String(250), nullable=False)
    scope_json = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default='queued')
    started_at = Column(Float, nullable=True)
    notified = Column(String(24), nullable=False, default='pending')


def error_summary(error):
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return (lines[-1] if lines else 'Không xác định được lỗi.')[:700]


def result_text(result):
    name = result.get('name') or str(result.get('id', 'chưa có mã'))
    state = result.get('state') or 'chưa xác định'
    labels = {'draft': 'Báo giá nháp', 'sent': 'Báo giá đã gửi', 'sale': 'Đơn bán đã xác nhận', 'cancel': 'Đã hủy', 'done': 'Đã khóa/hoàn tất'}
    message = f"Báo giá {name}\nTrạng thái: {labels.get(state, state)}.\nĐã tạo trên Odoo."
    if result.get('amount_total') is not None:
        currency = result.get('currency_id') or []
        label = currency[1] if isinstance(currency, list) and len(currency) > 1 else ''
        message += f"\nTổng tiền: {result['amount_total']} {label}."
    if result.get('verification') == 'check_required':
        message += '\nCần kiểm tra chênh lệch với bản xem trước trước khi sử dụng.'
    if result.get('url'):
        message += '\n' + result['url']
    return message


class QuoteExecutionWorker(QuotePreparationWorker):
    def notify_pending(self):
        """Recover terminal results persisted before a crash, without replaying writes."""
        if not self.settings.TELEGRAM_BOT_TOKEN:
            return
        with self.sf() as db:
            job = db.query(QuoteExecutionJob).filter(QuoteExecutionJob.notified == 'pending',
                QuoteExecutionJob.status.in_(['completed', 'failed', 'unknown_result'])).first()
            if not job:
                return
            op_id, user_id = job.operation_id, job.user_id
            tenant, database = job.tenant, job.database
            scope = TelegramScope.model_validate_json(job.scope_json)
            operation = db.query(ActionOperation).filter_by(operation_id=op_id, user_id=user_id).first()
            if not operation:
                return
            if operation.status == 'completed':
                message = result_text(json.loads(operation.result_json)['result'])
            elif operation.status == 'unknown_result':
                message = 'Trạng thái: Chưa rõ kết quả tạo báo giá. Không tự tạo lại; bạn có thể hỏi tiến độ để đối soát.'
            else:
                message = 'Trạng thái: Lỗi tạo báo giá.\nLý do: ' + error_summary(operation.error_message or 'Chưa xác định được lỗi.') + '\nNháp được giữ; hãy sửa thông tin rồi yêu cầu xem trước lại.'
        claimed = False
        try:
            with authenticated_context(self.request, user_id, scope) as ctx:
                if ctx.user.tenant != tenant or ctx.user.database != database:
                    raise ValueError('Tài khoản Odoo đã thay đổi.')
                with self.sf() as db:
                    claimed = bool(db.query(QuoteExecutionJob).filter_by(operation_id=op_id, user_id=user_id, notified='pending').update({'notified': 'sending'}))
                    db.commit()
                if not claimed:
                    return
                from app.telegram.client import TelegramClient
                TelegramClient(self.settings.TELEGRAM_BOT_TOKEN).send_message(scope.chat_id, message + '\nBạn có thể hỏi trạng thái bằng số báo giá trên Odoo.')
            delivery = 'delivered'
        except Exception as exc:
            # A network failure after dispatch is ambiguous; never blindly resend.
            delivery = 'unknown' if claimed else 'blocked'
            logging.getLogger(__name__).warning('Quote notification %s: %s', delivery, type(exc).__name__)
        with self.sf() as db:
            db.query(QuoteExecutionJob).filter_by(operation_id=op_id, user_id=user_id,
                notified='sending' if claimed else 'pending').update({'notified': delivery})
            db.commit()

    def tick(self):
        self.notify_pending()
        with self.sf() as db:
            # A crash after claim is ambiguous even when no response was persisted.
            stale = db.query(QuoteExecutionJob).filter(QuoteExecutionJob.status == 'executing', QuoteExecutionJob.started_at < time.time() - 300).all()
            for job in stale:
                job.status = 'unknown_result'
                db.query(ActionOperation).filter_by(user_id=job.user_id, operation_id=job.operation_id, status='in_progress').update({'status': 'unknown_result', 'error_message': 'Worker gián đoạn; cần đối soát, không tự tạo lại.'})
            job = db.query(QuoteExecutionJob).filter_by(status='queued').first()
            if not job:
                db.commit()
                return
            op_id, user_id, tenant, database = job.operation_id, job.user_id, job.tenant, job.database
            scope = TelegramScope.model_validate_json(job.scope_json)
            payload = json.loads(job.payload_json)
            claimed = db.query(QuoteExecutionJob).filter_by(operation_id=op_id, user_id=user_id, status='queued').update({'status': 'executing', 'started_at': time.time()})
            db.commit()
        if not claimed:
            return
        try:
            with authenticated_context(self.request, user_id, scope) as ctx:
                if ctx.user.tenant != tenant or ctx.user.database != database:
                    raise ValueError('Tài khoản Odoo đã thay đổi.')
                begin_tracking(self.sf, user_id, op_id, tenant, database)
                result = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).create(op_id, payload,
                    on_created=lambda order_id, company_id: record_created(self.sf, user_id, op_id, order_id, company_id))
                result['url'] = tenant.rstrip('/') + f"/web#id={result['id']}&model=sale.order&view_type=form"
            status = 'completed'
            final = {'status': status, 'result': result}
            error = None
        except Exception as exc:
            status = 'unknown_result' if isinstance(exc, OdooNetworkError) else 'failed'
            error = error_summary(exc)
            final = None
        with self.sf() as db:
            db.query(QuoteExecutionJob).filter_by(operation_id=op_id, user_id=user_id).update({'status': status})
            db.query(ActionOperation).filter(ActionOperation.user_id == user_id, ActionOperation.operation_id == op_id,
                ActionOperation.status.in_(['in_progress', 'unknown_result'])).update({'status': status,
                    'result_json': json.dumps(final, ensure_ascii=False) if final else None, 'error_message': error})
            db.commit()
        self.notify_pending()
