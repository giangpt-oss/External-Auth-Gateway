"""Durable read-only quotation checks, outside the chat turn deadline."""
import json
import secrets
import threading
import time
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Column, Float, Integer, String, Text

from app.models import Base
from app.quotations.service import QuotationService
from app.reporting.context import authenticated_context
from app.reporting.schemas import TelegramScope


class QuotePreparationJob(Base):
    __tablename__ = 'quotation_preparation_jobs'
    id = Column(String(70), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    tenant = Column(String(500), nullable=False)
    database = Column(String(250), nullable=False)
    scope_json = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default='queued')
    token = Column(String(50), nullable=True)
    lease_until = Column(Float, nullable=False, default=0)
    result_json = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False)
    notified = Column(String(24), nullable=False, default='pending')


def load_result(request, ctx, user_id, scope, job_id):
    with request.app.state.session_factory() as db:
        job = db.query(QuotePreparationJob).filter_by(id=job_id, user_id=user_id).first()
        if not job or time.time() - job.created_at > 1800:
            raise HTTPException(404, 'Lượt kiểm tra không tồn tại hoặc đã quá 30 phút.')
        if job.tenant != ctx.user.tenant or job.database != ctx.user.database or json.loads(job.scope_json) != scope.model_dump():
            raise HTTPException(403, 'Lượt kiểm tra thuộc tài khoản/liên kết khác.')
        result = json.loads(job.result_json) if job.result_json else None
        status = job.status
    if result and result.get('metadata'):
        service = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
        service._check_create()
        company_id, _ = service._companies(result['payload'].get('company_id'))
        for model, meta in result['metadata']['models'].items():
            if not set(meta['fields']).issubset(service._schema(model)):
                raise HTTPException(403, 'Quyền đọc trường đã thay đổi; hãy sửa nháp và kiểm tra lại.')
        payload = result['payload']
        if payload.get('partner_id'):
            service._record('res.partner', payload['partner_id'], company_id)
        for line in payload.get('lines', []):
            if line.get('product_id'):
                service._record('product.product', line['product_id'], company_id)
    return {'status': status, 'job_id': job_id, 'result': result}


class QuotePreparationWorker:
    def __init__(self, settings, session_factory):
        self.settings, self.sf = settings, session_factory
        self.stop_event = threading.Event()
        self.thread = None
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=session_factory)))

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='QuotePreparationWorker')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        import logging
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logging.getLogger(__name__).exception('Quotation preparation worker failed')
            self.stop_event.wait(1)

    def tick(self):
        now = time.time()
        with self.sf() as db:
            db.query(QuotePreparationJob).filter(QuotePreparationJob.status == 'running', QuotePreparationJob.lease_until < now).update({'status': 'queued', 'token': None}, synchronize_session=False)
            job = db.query(QuotePreparationJob).filter(QuotePreparationJob.status == 'queued', QuotePreparationJob.created_at > now - 1800).order_by(QuotePreparationJob.created_at).first()
            if not job:
                return
            job_id, user_id, tenant, database = job.id, job.user_id, job.tenant, job.database
            scope = TelegramScope.model_validate_json(job.scope_json)
            payload = json.loads(job.payload_json)
            token = secrets.token_hex(16)
            changed = db.query(QuotePreparationJob).filter_by(id=job_id, status='queued').update({'status': 'running', 'token': token, 'lease_until': now + 300})
            db.commit()
        if not changed:
            return
        try:
            with authenticated_context(self.request, user_id, scope) as ctx:
                if ctx.user.tenant != tenant or ctx.user.database != database:
                    raise ValueError('Phạm vi Odoo đã thay đổi.')
                result = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).prepare(payload)
                result.pop('values', None)
            status = 'completed'
        except Exception as exc:
            result = {'status': 'error', 'message': str(exc)}
            status = 'failed'
        with self.sf() as db:
            changed = db.query(QuotePreparationJob).filter_by(id=job_id, token=token, status='running').update({'status': status, 'result_json': json.dumps(result, ensure_ascii=False), 'lease_until': 0})
            db.commit()
        if changed and self.settings.TELEGRAM_BOT_TOKEN:
            self.notify(job_id, user_id, scope)

    def notify(self, job_id, user_id, scope):
        # At most one notification attempt. An ambiguous send is never replayed.
        with authenticated_context(self.request, user_id, scope):
            with self.sf() as db:
                changed = db.query(QuotePreparationJob).filter_by(id=job_id, notified='pending').update({'notified': 'sending'})
                db.commit()
            if not changed:
                return
            from app.telegram.client import TelegramClient
            try:
                TelegramClient(self.settings.TELEGRAM_BOT_TOKEN).send_message(scope.chat_id,
                    'Đã kiểm tra xong nháp báo giá. Bạn muốn xem bản báo giá và thông tin cần bổ sung không? Chưa tạo báo giá trên Odoo.')
                state = 'delivered'
            except Exception:
                state = 'unknown'
            with self.sf() as db:
                db.query(QuotePreparationJob).filter_by(id=job_id, notified='sending').update({'notified': state})
                db.commit()
