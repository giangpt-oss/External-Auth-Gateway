"""Authenticated read/prepare APIs. Creation uses the existing action ledger."""
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from app.internal.security import verify_agent_secret
from app.odoo.client import OdooAccessError, OdooError, OdooNetworkError
from app.quotations.approval import QuotationApproval
from app.quotations.service import QuotationService, QuotationValidationError
from app.reporting.context import authenticated_context
from app.reporting.schemas import TelegramScope

router = APIRouter(prefix="/internal/v1/users/{user_id}/quotations",
                   dependencies=[Depends(verify_agent_secret)])


class PrepareRequest(TelegramScope):
    payload: dict[str, Any] = Field(default_factory=dict)


def enabled(request):
    if not request.app.state.settings.ENABLE_QUOTATIONS:
        raise HTTPException(409, "Chức năng báo giá chưa được bật trên Gateway.")


def query_scope(telegram_user_id: int = Query(...), chat_id: int = Query(...),
                telegram_binding_id: str = Query(...)) -> TelegramScope:
    return TelegramScope(telegram_user_id=telegram_user_id, chat_id=chat_id,
                         telegram_binding_id=telegram_binding_id)


@router.get("/metadata")
def metadata(user_id: int, request: Request, company_id: int | None = None,
             binding: TelegramScope = Depends(query_scope)):
    enabled(request)
    with authenticated_context(request, user_id, binding) as ctx:
        try:
            return QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).metadata(company_id)
        except OdooAccessError as exc:
            raise HTTPException(403, str(exc)) from exc
        except OdooNetworkError as exc:
            raise HTTPException(503, "Không thể kết nối Odoo.") from exc
        except QuotationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get('/status')
def quotation_status(user_id: int, request: Request, name: str = Query(..., min_length=1, max_length=100),
                     company_id: int | None = None, binding: TelegramScope = Depends(query_scope)):
    enabled(request)
    with authenticated_context(request, user_id, binding) as ctx:
        try:
            response = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).status_by_name(name, company_id)
            result = response.get('result')
            if result and getattr(request.app.state.settings, 'ENABLE_QUOTATION_APPROVAL_FLOW', False):
                try:
                    company = result['company_id'][0]
                    adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret, company)
                    state = adapter.inspect(result['id'])
                    if state['status'] in ('approved', 'pending'):
                        from app.business.quotation_approval import require_review
                        from app.business.service import BusinessService, Conflict
                        business = BusinessService(ctx)
                        business.company_id = company
                        try:
                            require_review(business, result)
                        except Conflict:
                            state['status'] = 'review_required'
                            state['message'] = 'Nội dung chưa có phiên bản xin duyệt hợp lệ trong agent; chưa được gửi.'
                    ids = sorted({uid for rule in state['rules'] for uid in rule.get('approver_ids', [])})
                    people = adapter.call('res.users', 'search_read', [[('id', 'in', ids)]], fields=['name']) if ids else []
                    result['approval'] = {'status': state['status'], 'approvers': [p['name'] for p in people],
                                          'scope': 'agent_send_gate', 'message': state['message']}
                except OdooError:
                    result['approval'] = {'status': 'unknown', 'message': 'Chưa đọc được trạng thái duyệt nội bộ; không coi là đã duyệt.'}
            return response
        except OdooAccessError as exc:
            raise HTTPException(403, 'Tài khoản không có quyền xem báo giá này.') from exc
        except OdooNetworkError as exc:
            raise HTTPException(503, 'Chưa đọc được trạng thái hiện tại từ Odoo.') from exc
        except QuotationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get("/lookup")
def lookup(user_id: int, request: Request, kind: str, query: str = "", company_id: int | None = None,
           binding: TelegramScope = Depends(query_scope)):
    enabled(request)
    with authenticated_context(request, user_id, binding) as ctx:
        try:
            return QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).lookup(kind, query, company_id)
        except OdooAccessError as exc:
            raise HTTPException(403, str(exc)) from exc
        except OdooNetworkError as exc:
            raise HTTPException(503, "Không thể kết nối Odoo.") from exc
        except QuotationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.post("/prepare")
def prepare(user_id: int, body: PrepareRequest, request: Request):
    enabled(request)
    with authenticated_context(request, user_id, body) as ctx:
        try:
            result = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).prepare(body.payload)
            result.pop("values", None)
            return result
        except OdooAccessError as exc:
            raise HTTPException(403, str(exc)) from exc
        except OdooNetworkError as exc:
            raise HTTPException(503, "Không thể kết nối Odoo.") from exc
        except QuotationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.post('/prepare-jobs')
def start_prepare_job(user_id: int, body: PrepareRequest, request: Request):
    from app.quotations.jobs import QuotePreparationJob
    enabled(request)
    if len(json.dumps(body.payload)) > 150000:
        raise HTTPException(413, 'Nháp quá lớn.')
    with authenticated_context(request, user_id, body) as ctx:
        job_id = 'qprep_' + secrets.token_hex(16)
        scope = TelegramScope(telegram_user_id=body.telegram_user_id, chat_id=body.chat_id, telegram_binding_id=body.telegram_binding_id)
        with request.app.state.session_factory() as db:
            db.add(QuotePreparationJob(id=job_id, user_id=user_id, tenant=ctx.user.tenant, database=ctx.user.database,
                scope_json=scope.model_dump_json(), payload_json=json.dumps(body.payload, ensure_ascii=False), created_at=time.time()))
            db.commit()
        return {'status': 'queued', 'job_id': job_id}


@router.get('/prepare-jobs/{job_id}')
def get_prepare_job(user_id: int, job_id: str, request: Request, binding: TelegramScope = Depends(query_scope)):
    from app.quotations.jobs import load_result
    enabled(request)
    with authenticated_context(request, user_id, binding) as ctx:
        return load_result(request, ctx, user_id, binding, job_id)
