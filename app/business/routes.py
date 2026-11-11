"""Verified preparations and durable execution through the existing action API."""
import hashlib
import json
import re
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.business.catalog import ACTIONS
from app.business.models import (
    BusinessContinuation,
    BusinessEvidence,
    BusinessPreparation,
    BusinessTargetLock,
)
from app.business.schemas import LookupRequest, PrepareRequest
from app.business.service import BusinessError, BusinessService, Conflict, Unsupported, enabled
from app.internal.security import verify_agent_secret
from app.models import ActionOperation
from app.odoo.client import OdooAccessError, OdooError, OdooNetworkError
from app.quotations.routes import query_scope
from app.reporting.context import authenticated_context
from app.reporting.schemas import TelegramScope

router = APIRouter(prefix='/internal/v1/users/{user_id}/business', dependencies=[Depends(verify_agent_secret)])


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def error_response(exc):
    code = 'PERMISSION_DENIED' if isinstance(exc, OdooAccessError) else 'VERSION_CONFLICT' if isinstance(exc, Conflict) else 'UNSUPPORTED' if isinstance(exc, Unsupported) else 'SERVICE_ERROR'
    return {'status': code, 'message': str(exc)}


def gate(settings, action):
    definition = ACTIONS.get(action)
    if not definition or not enabled(settings, definition):
        raise HTTPException(409, 'Thao tác chưa hỗ trợ hoặc nhóm thao tác chưa được bật để nghiệm thu.')
    return definition


@router.get('/capabilities')
def capabilities(user_id: int, request: Request, binding: TelegramScope = Depends(query_scope)):
    with authenticated_context(request, user_id, binding) as ctx:
        return BusinessService(ctx).capabilities(request.app.state.settings)


@router.post('/lookup')
def lookup(user_id: int, body: LookupRequest, request: Request):
    if not request.app.state.settings.ENABLE_BUSINESS_ACTIONS and not (
        getattr(request.app.state.settings, 'ENABLE_QUOTATION_APPROVAL_FLOW', False) and body.kind in ('sales', 'contacts', 'attachments')
    ):
        raise HTTPException(409, 'Chức năng thao tác nghiệp vụ chưa được bật.')
    with authenticated_context(request, user_id, body) as ctx:
        try:
            return BusinessService(ctx).lookup(body.kind, body.query, body.company_id)
        except (BusinessError, OdooError) as exc:
            return error_response(exc)


@router.post('/prepare')
def prepare(user_id: int, body: PrepareRequest, request: Request):
    gate(request.app.state.settings, body.action)
    if len(dumps(body.payload)) > 150000:
        raise HTTPException(413, 'Dữ liệu nháp quá lớn.')
    with authenticated_context(request, user_id, body) as ctx:
        continuation_id = None
        if set(body.payload) - {'company_id', 'target_id', 'target_kind', 'values', 'lines', 'options'}:
            raise HTTPException(400, 'Payload chứa trường không được phép.')
        if body.action == 'business.stock.backorder':
            continuation_id = (body.payload.get('options') or {}).get('continuation_token')
            with request.app.state.session_factory() as db:
                continuation = db.get(BusinessContinuation, continuation_id) if isinstance(continuation_id, str) else None
                if not continuation or continuation.user_id != user_id or continuation.expires_at <= time.time() or continuation.preparation_id:
                    raise HTTPException(409, 'Token bước tiếp theo không tồn tại, đã dùng hoặc hết hạn.')
                principal(continuation, ctx, body)
                source = json.loads(continuation.data_json)
                body.payload['_continuation'] = source
                body.payload['target_id'] = source['picking_id']
                body.payload['company_id'] = source['company_id']
        try:
            prepared = BusinessService(ctx).prepare(body.action, body.payload)
        except (BusinessError, OdooError) as exc:
            return error_response(exc)
        prepared.pop('_values', None)
        if prepared['status'] != 'ready':
            return prepared
        token = 'bp_' + secrets.token_urlsafe(24)
        now = time.time()
        with request.app.state.session_factory() as db:
            if continuation_id:
                claimed = db.query(BusinessContinuation).filter_by(id=continuation_id, user_id=user_id, preparation_id=None).update({'preparation_id': token}, synchronize_session=False)
                if claimed != 1:
                    raise HTTPException(409, 'Bước tiếp theo vừa được chuẩn bị ở tác vụ khác.')
            db.add(BusinessPreparation(id=token, user_id=user_id, tenant=ctx.user.tenant,
                database=ctx.user.database, binding_id=body.telegram_binding_id, chat_id=str(body.chat_id),
                action=body.action, data_json=dumps(prepared), created_at=now, expires_at=now + 300))
            db.commit()
        return dict(prepared, prepared_id=token, expires_at=now + 300)


def principal(preparation, ctx, binding):
    if preparation.tenant != ctx.user.tenant or preparation.database != ctx.user.database or preparation.binding_id != binding.telegram_binding_id or preparation.chat_id != str(binding.chat_id):
        raise HTTPException(403, 'Bản xem trước thuộc tài khoản/công ty/liên kết khác.')


def execute_business_action(user_id, req, request):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', req.operation_id):
        raise HTTPException(400, 'operation_id không hợp lệ.')
    if set(req.arguments) != {'prepared_id'} or not isinstance(req.arguments['prepared_id'], str):
        raise HTTPException(400, 'Chỉ chấp nhận prepared_id đã lưu tại Gateway.')
    definition = gate(request.app.state.settings, req.action)
    scope = TelegramScope(telegram_user_id=req.telegram_user_id, chat_id=req.chat_id, telegram_binding_id=req.telegram_binding_id)
    sf = request.app.state.session_factory
    op_key = f'{user_id}:{req.operation_id}'
    payload_hash = hashlib.sha256(dumps({'action': req.action, 'arguments': req.arguments}).encode()).hexdigest()
    with authenticated_context(request, user_id, scope) as ctx:
        try:
            with sf() as db:
                db.execute(text('BEGIN IMMEDIATE'))
                old = db.query(ActionOperation).filter_by(user_id=user_id, operation_id=req.operation_id).first()
                if old:
                    if old.telegram_binding_id != req.telegram_binding_id or old.payload_hash != payload_hash:
                        raise HTTPException(409, 'operation_id đã thuộc payload hoặc liên kết khác.')
                    # Replayed requests must recheck source access before returning historical data.
                    evidence = db.get(BusinessEvidence, op_key)
                    old_prep = db.get(BusinessPreparation, evidence.preparation_id) if evidence else None
                    if not old_prep:
                        raise HTTPException(409, 'Thiếu bản xem trước của thao tác đã lưu.')
                    principal(old_prep, ctx, scope)
                    revalidate_sources(ctx, json.loads(old_prep.data_json), old.action, old.status)
                    revalidate_result(ctx, old, json.loads(old_prep.data_json))
                    return cached_status(old)
                prep = db.query(BusinessPreparation).filter_by(id=req.arguments['prepared_id'], user_id=user_id, action=req.action).first()
                if not prep:
                    raise HTTPException(404, 'Không tìm thấy bản xem trước.')
                principal(prep, ctx, scope)
                if prep.operation_id:
                    raise HTTPException(409, 'Bản xem trước đã dùng cho thao tác khác; không được xác nhận lại bằng mã mới.')
                if prep.expires_at <= time.time():
                    raise HTTPException(409, 'Bản xem trước hết hạn sau 5 phút; hãy chuẩn bị lại.')
                prepared = json.loads(prep.data_json)
                prep.operation_id = req.operation_id
                target_id = prepared['payload'].get('target_id')
                if target_id:
                    target_model = definition.model
                    if target_model == 'mail.thread':
                        from app.business.catalog import LOOKUPS
                        target_model = LOOKUPS[prepared['payload']['target_kind']]
                    key = hashlib.sha256(dumps([ctx.user.tenant, ctx.user.database, target_model, target_id]).encode()).hexdigest()
                    db.add(BusinessTargetLock(key=key, operation_key=op_key, created_at=time.time()))
                db.add(ActionOperation(operation_id=req.operation_id, user_id=user_id, action=req.action,
                    payload_hash=payload_hash, telegram_binding_id=req.telegram_binding_id, status='in_progress'))
                db.add(BusinessEvidence(key=op_key, preparation_id=prep.id, steps_json='[]', updated_at=time.time()))
                db.commit()
        except IntegrityError:
            raise HTTPException(409, 'Chứng từ đang có thao tác khác hoặc đang chờ đối soát.') from None
        dispatched = False

        def checkpoint(stage, data):
            nonlocal dispatched
            dispatched = True
            with sf() as db:
                evidence = db.get(BusinessEvidence, op_key)
                steps = json.loads(evidence.steps_json)
                steps.append({'stage': stage, 'data': data, 'at': time.time()})
                evidence.steps_json = dumps(steps)
                evidence.updated_at = time.time()
                db.commit()

        try:
            result = BusinessService(ctx).execute(req.action, prepared['payload'], prepared['snapshot'], req.operation_id, checkpoint)
            continuation = result.pop('continuation', None)
            if continuation:
                token = 'bc_' + secrets.token_urlsafe(24)
                continuation['company_id'] = prepared['payload']['company_id']
                with sf() as db:
                    db.add(BusinessContinuation(id=token, user_id=user_id, binding_id=req.telegram_binding_id,
                        chat_id=str(req.chat_id), tenant=ctx.user.tenant, database=ctx.user.database,
                        parent_operation_id=req.operation_id, data_json=dumps(continuation), expires_at=time.time() + 300))
                    db.commit()
                result['continuation_token'] = token
            response = {'status': 'completed', 'result': result}
            finish(sf, user_id, req.operation_id, op_key, 'completed', response, release=True)
            return response
        except OdooNetworkError:
            response = {'status': 'UNKNOWN_RESULT', 'message': 'Mất kết nối; chưa xác định kết quả. Không tự gửi lại. Dùng /status để xem dấu vết và kiểm tra Odoo.'}
            finish(sf, user_id, req.operation_id, op_key, 'unknown_result', response, release=False)
            return response
        except (BusinessError, OdooError, HTTPException) as exc:
            if dispatched:
                response = {'status': 'UNKNOWN_RESULT', 'message': 'Một bước đã được thực hiện nhưng toàn bộ thao tác chưa xác minh xong. Không tự lặp hoặc hoàn tác.'}
            else:
                response = error_response(exc)
            finish(sf, user_id, req.operation_id, op_key, 'unknown_result' if dispatched else 'failed', response, release=not dispatched)
            return response
        except Exception:
            # A response/persistence failure after provider acceptance must never become retryable.
            response = {'status': 'UNKNOWN_RESULT', 'message': 'Không xác minh được kết quả thao tác. Không tự thực hiện lại; cần kiểm tra sổ thao tác và Odoo.'}
            finish(sf, user_id, req.operation_id, op_key, 'unknown_result', response, release=False)
            return response


def finish(sf, user_id, operation_id, op_key, status, response, release):
    with sf() as db:
        db.query(ActionOperation).filter_by(user_id=user_id, operation_id=operation_id, status='in_progress').update(
            {'status': status, 'result_json': dumps(response)}, synchronize_session=False)
        if release:
            db.query(BusinessTargetLock).filter_by(operation_key=op_key).delete(synchronize_session=False)
        db.commit()


def cached_status(operation):
    if operation.result_json:
        return json.loads(operation.result_json)
    if operation.status == 'in_progress':
        return {'status': 'IN_PROGRESS', 'message': 'Thao tác đã được nhận; không thực thi lại.'}
    return {'status': 'UNKNOWN_RESULT', 'message': 'Chưa có kết quả xác minh; cần đối soát.'}


def revalidate_sources(ctx, prepared, action, status):
    service = BusinessService(ctx)
    service.company_id = ctx.selected_companies([prepared['payload']['company_id']])[0]
    definition = ACTIONS[action]
    if definition.model != 'mail.thread':
        service.access(definition.model, 'read')
    removed = set()
    if status == 'completed' and definition.kind == 'update' and definition.module in ('sales', 'purchases', 'stock'):
        child = 'sale.order.line' if definition.module == 'sales' else 'purchase.order.line' if definition.module == 'purchases' else 'stock.move'
        removed = {(child, line['id']) for line in prepared['payload'].get('lines', []) if line.get('id') and line.get('remove')}
    for source in prepared['snapshot']:
        if (source['model'], source['id']) in removed:
            continue
        if status == 'completed' and definition.kind in ('delete', 'activity_done') and source['model'] == definition.model and source['id'] == prepared['payload'].get('target_id'):
            continue  # The action itself removed this record; do not expose its old field values.
        service.record(source['model'], source['id'], list(source['values']), track=False)


def revalidate_result(ctx, operation, prepared):
    result = (json.loads(operation.result_json or '{}').get('result') or {})
    record = result.get('record')
    if not isinstance(record, dict) or not record.get('id'):
        return
    definition = ACTIONS[operation.action]
    model = definition.model
    if model == 'mail.thread':
        from app.business.catalog import LOOKUPS
        model = LOOKUPS[prepared['payload']['target_kind']]
    service = BusinessService(ctx)
    service.company_id = ctx.selected_companies([prepared['payload']['company_id']])[0]
    fields = set(record) - {'url', 'lines'}
    if not fields.issubset(set(service.schema(model)) | {'id'}):
        raise HTTPException(403, 'Quyền đọc trường kết quả đã thay đổi.')
    service.record(model, record['id'], list(fields), track=False)


def business_action_status(user_id, operation_id, binding, request):
    with authenticated_context(request, user_id, binding) as ctx:
        sf = request.app.state.session_factory
        with sf() as db:
            op = db.query(ActionOperation).filter_by(user_id=user_id, operation_id=operation_id).first()
            if not op or not op.action.startswith('business.'):
                return None
            if op.telegram_binding_id != binding.telegram_binding_id:
                raise HTTPException(403, 'Thao tác thuộc liên kết cũ.')
            evidence = db.get(BusinessEvidence, f'{user_id}:{operation_id}')
            prep = db.get(BusinessPreparation, evidence.preparation_id) if evidence else None
            if not prep:
                raise HTTPException(409, 'Thiếu dữ liệu đối soát; cần kiểm tra thủ công.')
            principal(prep, ctx, binding)
            definition = ACTIONS[op.action]
            service = BusinessService(ctx)
            service.company_id = ctx.selected_companies([json.loads(prep.data_json)['payload']['company_id']])[0]
            if definition.model != 'mail.thread':
                service.access(definition.model, 'read')
            # Do not return historical sensitive values after access was revoked.
            prepared = json.loads(prep.data_json)
            revalidate_sources(ctx, prepared, op.action, op.status)
            revalidate_result(ctx, op, prepared)
            steps = json.loads(evidence.steps_json)
            result = cached_status(op)
            if op.status == 'in_progress' and time.time() - evidence.updated_at > 60:
                op.status = 'unknown_result'
                db.commit()
                result = {'status': 'UNKNOWN_RESULT', 'message': 'Thao tác quá thời gian theo dõi. Không tự gọi lại; cần kiểm tra Odoo.'}
            return {'operation_id': operation_id, 'action': op.action, 'status': op.status,
                    'result': result.get('result'), 'message': result.get('message'),
                    'steps': [{'stage': step['stage'], 'at': step['at'], 'reference': {key: value for key, value in step['data'].items() if key in ('model', 'id', 'picking_id')}} for step in steps],
                    'automatic_retry': False}
