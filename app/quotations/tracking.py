"""Local quotation identity. Business fields are never used as new tracking markers."""
import time

from sqlalchemy import Column, Float, Integer, String

from app.models import Base
from app.odoo.client import OdooAccessError


class QuotationRecordLink(Base):
    __tablename__ = 'quotation_record_links'
    user_id = Column(Integer, primary_key=True)
    operation_id = Column(String(64), primary_key=True)
    tenant = Column(String(500), nullable=False)
    database = Column(String(250), nullable=False)
    order_id = Column(Integer, nullable=True)
    company_id = Column(Integer, nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)


def begin_tracking(sf, user_id, operation_id, tenant, database):
    with sf() as db:
        row = db.get(QuotationRecordLink, (user_id, operation_id))
        if row:
            if (row.tenant, row.database) != (tenant, database):
                raise OdooAccessError('Liên kết báo giá thuộc tài khoản Odoo khác.')
            return
        db.add(QuotationRecordLink(user_id=user_id, operation_id=operation_id, tenant=tenant, database=database))
        db.commit()


def record_created(sf, user_id, operation_id, order_id, company_id):
    with sf() as db:
        row = db.get(QuotationRecordLink, (user_id, operation_id))
        if not row or (row.order_id and row.order_id != order_id):
            raise ValueError('Không thể thay thế liên kết báo giá đã ghi nhận.')
        row.order_id, row.company_id = order_id, company_id
        db.commit()


def reconcile_tracked(sf, user_id, operation_id, tenant, database, service):
    with sf() as db:
        row = db.get(QuotationRecordLink, (user_id, operation_id))
        if row:
            if (row.tenant, row.database) != (tenant, database):
                raise OdooAccessError('Liên kết báo giá thuộc tài khoản Odoo khác.')
            if not row.order_id:
                return {'status': 'unknown_result', 'message': 'Chưa nhận được số báo giá từ Odoo. Cần kiểm tra thủ công trước khi tạo lại.'}
            company_id, _ = service._companies(row.company_id)
            result = service._readback(row.order_id, company_id)
            result['verification'] = 'check_required'
            return {'status': 'completed', 'result': result}
    # Compatibility for old operations only. This reads the old marker; never writes it.
    return service.reconcile(operation_id)
