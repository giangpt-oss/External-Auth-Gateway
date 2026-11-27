from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, TelegramConnection, User
from app.quotations import pdf_review, routes
from app.quotations.pdf_review import QuotationPdfToken
from app.quotations.tracking import QuotationRecordLink
from app.quotations.service import QuotationService


def test_pdf_retry_only_uses_linked_draft(monkeypatch):
    native_request = Mock(side_effect=AssertionError('PDF retry must not request approval'))
    monkeypatch.setattr('app.quotations.approval.QuotationApproval.request', native_request)
    sf = sessionmaker(bind=create_engine('sqlite://'))
    Base.metadata.create_all(sf.kw['bind'])
    with sf() as db:
        db.add(User(id=1, tenant='fixture', database='db', odoo_uid=5,
                    login='sale', name='Sale', active=True))
        db.add(TelegramConnection(user_id=1, telegram_user_id=7, private_chat_id=7,
                                  binding_id='fixture', status='connected'))
        db.add(QuotationRecordLink(user_id=1, operation_id='op_fixture', tenant='fixture',
            database='db', order_id=83, company_id=3))
        db.commit()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(ENABLE_QUOTATIONS=True, TELEGRAM_BOT_TOKEN='fixture'),
        session_factory=sf)))
    ctx = SimpleNamespace(user=SimpleNamespace(id=1, tenant='fixture', database='db', odoo_uid=5),
        service=SimpleNamespace(client=Mock()), secret='fixture')
    monkeypatch.setattr(routes, 'authenticated_context', lambda *args: nullcontext(ctx))
    monkeypatch.setattr(QuotationService, '_companies', lambda self, company_id: (3, [3]))
    read = Mock(return_value={'id': 83, 'name': 'S00084', 'state': 'draft',
                              'write_date': '2026-11-16 12:00:00', 'company_id': [3, 'Company']})
    monkeypatch.setattr(pdf_review, '_read_order_for_token', lambda *args: read(*args))
    render = Mock(return_value=('S00084.pdf', b'%PDF-1.7\n%%EOF'))
    monkeypatch.setattr(pdf_review, 'render_quotation_pdf', render)
    send = Mock(return_value={'message_id': 88})
    monkeypatch.setattr(pdf_review, 'TelegramClient', lambda token: SimpleNamespace(send_document=send))
    body = routes.PdfRetryRequest(telegram_user_id=7, chat_id=7, telegram_binding_id='fixture', order_id=83)
    assert routes.pdf_retry(1, body, request)['status'] == 'sent'
    read.assert_called_once_with(ctx, 83, 3)
    send.assert_called_once()
    native_request.assert_not_called()
    with sf() as db:
        assert db.query(QuotationPdfToken).filter_by(role='sale', message_id=88).count() == 1
        assert db.query(QuotationPdfToken).filter_by(role='approver').count() == 0
    with pytest.raises(HTTPException) as error:
        routes.pdf_retry(2, body, request)
    assert error.value.status_code == 404
    assert read.call_count == 1
    unlinked = routes.PdfRetryRequest(telegram_user_id=7, chat_id=7,
                                     telegram_binding_id='fixture', order_id=84)
    assert routes.pdf_retry(1, unlinked, request)['status'] == 'stale'
    send.assert_called_once()
    read.return_value = {**read.return_value, 'state': 'sale'}
    assert routes.pdf_retry(1, body, request)['status'] == 'locked'
    send.assert_called_once()


def test_payment_term_lookup_resolves_only_unique_exact_match(monkeypatch):
    service = QuotationService.__new__(QuotationService)
    service.uid, service.secret = 29, 'fixture'
    monkeypatch.setattr(service, '_companies', lambda company_id: (3, [3]))
    monkeypatch.setattr(service, '_schema', lambda model: {'id': {}, 'display_name': {}, 'name': {}})
    rpc = Mock(return_value=[{'lang': 'vi_VN'}])
    monkeypatch.setattr(service, '_rpc', rpc)
    vi = ['Thanh toán ngay', '15 ngày', '21 ngày', '30 ngày', '45 ngày',
          'Cuối tháng kế tiếp', '10 ngày sau ngày cuối tháng kế tiếp',
          '30% trả ngay, còn lại trả trong 60 ngày',
          'Trong 30 ngày, giảm 2% trong 7 ngày', '90 ngày, vào ngày 10']
    en = ['Immediate Payment', '15 Days', '21 Days', '30 Days', '45 Days',
          'End of Following Month', '10 Days after End of Next Month',
          '30% Now, Balance 60 Days', '2/7 Net 30', '90 days, on the 10th']
    def execute(_uid, _secret, _model, _method, _args, kwargs):
        names = vi if kwargs['context']['lang'] == 'vi_VN' else en
        return [{'id': index, 'name': name, 'display_name': name}
                for index, name in enumerate(names, 1)]
    service.client = Mock(execute=Mock(side_effect=execute))
    for index, name in enumerate(vi, 1):
        result = service.lookup('payment_terms', name, 3)
        assert result['exact_match_unique'] is True
        assert [row['id'] for row in result['items']] == [index]
    for index, name in enumerate(en, 1):
        result = service.lookup('payment_terms', name, 3)
        assert result['exact_match_unique'] is True
        assert [row['id'] for row in result['items']] == [index]
    assert service.lookup('payment_terms', 'Thanh toan ngay', 3)['items'][0]['id'] == 1
    assert service.lookup('payment_terms', 'tra ngay', 3)['exact_match_unique'] is False
    assert service.lookup('payment_terms', 'tra ngay', 3)['items'][0]['id'] == 1
    assert service.lookup('payment_terms', 'khong ton tai', 3)['exact_match_unique'] is False


def test_partial_pricelist_query_reports_unique_visible_match(monkeypatch):
    service = QuotationService.__new__(QuotationService)
    monkeypatch.setattr(service, '_companies', lambda company_id: (3, [3]))
    monkeypatch.setattr(service, '_schema', lambda model: {'id': {}, 'display_name': {}, 'name': {}})
    monkeypatch.setattr(service, '_rpc', Mock(side_effect=[[], [
        {'id': 33, 'display_name': 'KH thương mại (VND)'}]]))
    result = service.lookup('pricelists', 'Thương mại', 3)
    assert result['unique_query_match'] is True
    assert result['items'][0]['id'] == 33
