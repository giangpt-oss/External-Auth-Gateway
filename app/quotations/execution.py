"""Confirmed quotation outbox. A claimed write is never automatically replayed."""
import json
import logging
import time

from sqlalchemy import Column, Float, Integer, String, Text

from app.models import ActionOperation, Base
from app.odoo.client import OdooNetworkError
from app.quotations.approval import APPROVAL_LABELS, read_approval_status
from app.quotations.jobs import QuotePreparationWorker
from app.quotations.pdf import render_quotation_pdf
from app.quotations.service import QuotationMissingFields, QuotationService
from app.quotations.tracking import QuotationRecordLink, begin_tracking, record_created
from app.reporting.context import authenticated_context, bound_telegram_user
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


def quotation_failure_message(error, details):
    summary = error_summary(error or 'Chưa xác định được lỗi.')
    legacy_prefix = 'Còn thiếu thông tin:'
    missing = details.get('missing_fields') or []
    if not missing and summary.startswith(legacy_prefix):
        missing = [path.strip() for path in summary[len(legacy_prefix):].split(',') if path.strip()]
    if not missing:
        return f'Trạng thái: Lỗi tạo báo giá.\nLý do: {summary}\nNháp được giữ.'

    defaults = {'partner_id': 'Khách hàng', 'partner_invoice_id': 'Địa chỉ xuất hóa đơn',
                'partner_shipping_id': 'Địa chỉ giao hàng', 'lines': 'Dòng sản phẩm',
                'product_id': 'Sản phẩm', 'quantity': 'Số lượng', 'price_unit': 'Đơn giá',
                'tax_ids': 'Thuế', 'uom_id': 'Đơn vị tính'}
    labels = details.get('field_labels') or {}
    fields = []
    for path in missing:
        parts = path.split('.')
        field = parts[-1]
        label = labels.get(path)
        if not label or label.casefold() == field.casefold():
            label = defaults.get(field, 'Trường bắt buộc trên báo giá')
        prefix = f'Dòng {int(parts[1]) + 1}: ' if len(parts) > 2 and parts[0] == 'lines' and parts[1].isdigit() else ''
        fields.append(prefix + label)
    instruction = ('Bạn gửi giá trị cho các trường trên, tôi sẽ tiếp tục từ nháp đã lưu.'
                   if details.get('missing_fields') else 'Nháp được giữ.')
    return 'Odoo cần bổ sung thông tin để tạo báo giá:\n• ' + '\n• '.join(dict.fromkeys(fields)) + '\n' + instruction


def result_text(result):
    name = result.get('name') or str(result.get('id', 'chưa có mã'))
    state = result.get('state') or 'chưa xác định'
    labels = {'draft': 'Báo giá nháp', 'sent': 'Đã gửi tới khách hàng (trạng thái Odoo)', 'sale': 'Đơn bán đã xác nhận', 'cancel': 'Đã hủy', 'done': 'Đã khóa/hoàn tất'}
    message = f"Báo giá {name}\nTrạng thái: {labels.get(state, state)}.\nĐã tạo trên Odoo."
    if result.get('amount_total') is not None:
        currency = result.get('currency_id') or []
        label = currency[1] if isinstance(currency, list) and len(currency) > 1 else ''
        message += f"\nTổng tiền: {result['amount_total']} {label}."
    if result.get('verification') == 'check_required':
        message += '\nCần kiểm tra chênh lệch với bản xem trước trước khi sử dụng.'
    if result.get('approval'):
        message += '\nDuyệt nội bộ: ' + APPROVAL_LABELS.get(result['approval'].get('status'), APPROVAL_LABELS['unknown']) + '.'
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
            result = None
            if operation.status == 'completed':
                result = json.loads(operation.result_json)['result']
                message = result_text(result)
            elif operation.status == 'unknown_result':
                message = 'Trạng thái: Chưa rõ kết quả tạo báo giá. Không tự tạo lại; bạn có thể hỏi tiến độ để đối soát.'
            else:
                details = (json.loads(operation.result_json).get('result') or {}) if operation.result_json else {}
                message = quotation_failure_message(operation.error_message, details)
        claimed = False
        send_started = False
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
                client = TelegramClient(self.settings.TELEGRAM_BOT_TOKEN)
                document = None
                if result is not None:
                    if getattr(self.settings, 'ENABLE_QUOTATION_APPROVAL_FLOW', False):
                        try:
                            result['approval'] = read_approval_status(ctx, result)
                        except Exception as exc:
                            result['approval'] = {'status': 'unknown'}
                            logging.getLogger(__name__).warning('Quote approval status unavailable: %s', type(exc).__name__)
                        message = result_text(result)
                    try:
                        document = render_quotation_pdf(ctx, result)
                    except Exception as exc:
                        logging.getLogger(__name__).warning('Quote PDF unavailable: %s', type(exc).__name__)
                        message += '\nChưa lấy được file PDF. Bạn có thể mở báo giá trên Odoo và chọn In để tải.'
                with self.sf() as db:
                    recipient = bound_telegram_user(db, user_id, scope)
                    if recipient.tenant != tenant or recipient.database != database:
                        raise ValueError('Tài khoản Odoo đã thay đổi.')
                send_started = True
                if document:
                    filename, content = document
                    client.send_document(scope.chat_id, filename, content, caption=message, content_type='application/pdf')
                else:
                    client.send_message(scope.chat_id, message)
            delivery = 'delivered'
        except Exception as exc:
            # A network failure after dispatch is ambiguous; never blindly resend.
            delivery = 'unknown' if send_started else 'blocked'
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
            with self.sf() as db:
                link = db.get(QuotationRecordLink, (user_id, op_id))
                created = bool(link and link.order_id)
            status = 'unknown_result' if created or isinstance(exc, OdooNetworkError) else 'failed'
            error = error_summary(exc)
            final = {'status': status, 'result': exc.details} if status == 'failed' and isinstance(exc, QuotationMissingFields) else None
        with self.sf() as db:
            db.query(QuoteExecutionJob).filter_by(operation_id=op_id, user_id=user_id).update({'status': status})
            db.query(ActionOperation).filter(ActionOperation.user_id == user_id, ActionOperation.operation_id == op_id,
                ActionOperation.status.in_(['in_progress', 'unknown_result'])).update({'status': status,
                    'result_json': json.dumps(final, ensure_ascii=False) if final else None, 'error_message': error})
            db.commit()
        self.notify_pending()
