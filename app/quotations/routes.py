"""Authenticated read/prepare APIs. Creation uses the existing action ledger."""
import json
import secrets
import time
from html import escape
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from app.internal.security import verify_agent_secret
from app.odoo.client import OdooAccessError, OdooError, OdooNetworkError
from app.quotations.approval import read_approval_status
from app.quotations.service import ORDER_FIELDS, QuotationService, QuotationValidationError
from app.reporting.context import authenticated_context
from app.reporting.schemas import TelegramScope

router = APIRouter(prefix="/internal/v1/users/{user_id}/quotations",
                   dependencies=[Depends(verify_agent_secret)])


def _can_receive_approval(settings, person, order_id, company_id, rule_id):
    """Read Studio eligibility using the recipient's own Odoo identity."""
    from app.odoo.client import OdooClient
    from app.quotations.approval import QuotationApproval
    from app.security import decrypt_secret

    if not person.credential:
        return False
    secret = decrypt_secret(person.credential.encrypted_secret, settings.ENCRYPTION_KEY)
    state = QuotationApproval(OdooClient(person.tenant, person.database), person.odoo_uid,
                              secret, company_id).inspect(order_id)
    return (state['status'] == 'pending' and bool(state['requests'])
            and any(rule['id'] == rule_id and rule.get('can_validate') is True
                    for rule in state['rules'])
            and not any(entry.get('rule_id', [None])[0] == rule_id
                        for entry in state['entries']))


def _record_native_review(ctx, sf, result, token):
    """Keep the existing agent send gate aligned with the PDF approval request."""
    from app.business.models import QuotationApprovalReview
    from app.business.quotation_approval import commercial_snapshot, review_key
    from app.business.service import BusinessService

    service = BusinessService(ctx)
    service.company_id = result['company_id'][0]
    digest = commercial_snapshot(service, result)
    key = review_key(service, result)
    with sf() as db:
        row = db.get(QuotationApprovalReview, key)
        if row is None:
            row = QuotationApprovalReview(key=key, digest=digest,
                operation_id='pdf:' + token, requester_id=ctx.user.id,
                created_at=time.time(), verified=1)
            db.add(row)
        else:
            row.digest = digest
            row.operation_id = 'pdf:' + token
            row.requester_id = ctx.user.id
            row.created_at = time.time()
            row.verified = 1
        db.commit()


class PrepareRequest(TelegramScope):
    payload: dict[str, Any] = Field(default_factory=dict)


class PdfRetryRequest(TelegramScope):
    order_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=100)


class PdfInteractionRequest(TelegramScope):
    token: str = Field(pattern=r'^[0-9a-f]{24}$')


class PdfEditRequest(PdfInteractionRequest):
    line_id: int | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, gt=0)
    payment_term_id: int | None = Field(default=None, gt=0)
    optional_fields: dict[str, Any] | None = None


class ApprovalReplyRequest(TelegramScope):
    token: str = Field(pattern=r'^[0-9a-f]{24}$')
    feedback: str | None = Field(default=None, min_length=1, max_length=4000)


def _approval_delivery(request, ctx, token):
    from app.quotations.interaction import QuoteApprovalDelivery, QuotePdfMessage
    with request.app.state.session_factory() as db:
        row = db.get(QuoteApprovalDelivery, token)
        if (not row or row.message_id is None or row.approver_user_id != ctx.user.id
                or row.chat_id != ctx.user.telegram_connection.private_chat_id
                or row.telegram_user_id != ctx.user.telegram_connection.telegram_user_id
                or row.binding_id != ctx.user.telegram_connection.binding_id):
            raise HTTPException(403, 'Yêu cầu duyệt không thuộc tài khoản Telegram này.')
        if row.status != 'pending':
            raise HTTPException(409, 'Bước duyệt này đã được xử lý.')
        target = {'pdf_token': row.pdf_token, 'rule_id': row.rule_id,
                  'order_id': row.order_id, 'company_id': row.company_id,
                  'version': row.version}
    current = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)._readback(
        target['order_id'], target['company_id'])
    if str(current.get('write_date')) != target['version'] or current.get('state') != 'draft':
        raise HTTPException(409, 'Bản PDF đang duyệt đã cũ. Hãy yêu cầu sale gửi lại bản mới.')
    return target, current


@router.post('/approval-submit')
def submit_quotation_approval(user_id: int, body: PdfInteractionRequest, request: Request):
    from app.models import User
    from app.quotations.approval import QuotationApproval
    from app.quotations.interaction import QuoteApprovalDelivery, QuotePdfMessage
    from app.quotations.pdf import render_quotation_pdf
    from app.telegram.client import TelegramClient
    enabled(request)
    if not request.app.state.settings.ENABLE_QUOTATION_APPROVAL_FLOW:
        raise HTTPException(409, 'Cần bật ENABLE_QUOTATION_APPROVAL_FLOW trên Gateway trước khi gửi duyệt.')
    with authenticated_context(request, user_id, body) as ctx:
        result = _pdf_target(request, ctx, body.token)
        adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret,
                                    result['company_id'][0])
        try:
            state = adapter.inspect(result['id'])
        except OdooAccessError as exc:
            raise HTTPException(403, 'Tài khoản không có quyền đọc quy tắc duyệt báo giá này.') from exc
        except OdooError as exc:
            raise HTTPException(503, 'Chưa đọc được quy tắc duyệt từ Odoo; chưa gửi yêu cầu.') from exc
        if not state['rules']:
            raise HTTPException(409, 'Odoo chưa cấu hình quy tắc duyệt cho báo giá này.')
        if state['status'] in ('rejected', 'approved'):
            raise HTTPException(409, 'Odoo đã ghi quyết định duyệt cho báo giá này. Không tạo yêu cầu hay gửi PDF mới trên quyết định cũ; cần người có quyền thu hồi quyết định trong Odoo rồi xuất PDF mới để gửi lại.')
        approved_rules = {entry['rule_id'][0] for entry in state['entries'] if entry.get('approved') is True}
        remaining = [rule for rule in state['rules'] if rule['id'] not in approved_rules]
        order = min(int(rule.get('notification_order') or 1) for rule in remaining)
        stage = [rule for rule in remaining if int(rule.get('notification_order') or 1) == order]
        approver_ids = sorted({uid for rule in stage for uid in rule.get('approver_ids') or []})
        if not approver_ids:
            raise HTTPException(409, 'Bước duyệt Odoo chưa có người duyệt được chỉ định trực tiếp; cần cấu hình Approvers.')
        with request.app.state.session_factory() as db:
            people = db.query(User).filter(User.tenant == ctx.user.tenant,
                User.database == ctx.user.database, User.odoo_uid.in_(approver_ids), User.active.is_(True)).all()
            linked = {person.odoo_uid: person for person in people if person.telegram_connection
                and person.telegram_connection.status == 'connected'
                and person.telegram_connection.private_chat_id}
            missing_ids = sorted(set(approver_ids) - set(linked))
            existing = db.query(QuoteApprovalDelivery).join(QuotePdfMessage,
                QuoteApprovalDelivery.pdf_token == QuotePdfMessage.token).filter(
                QuotePdfMessage.tenant == ctx.user.tenant,
                QuotePdfMessage.database == ctx.user.database,
                QuoteApprovalDelivery.order_id == result['id'],
                QuoteApprovalDelivery.company_id == result['company_id'][0],
                QuoteApprovalDelivery.version == str(result['write_date'])).all()
            recipients = [(uid, linked[uid].id, linked[uid].telegram_connection.private_chat_id,
                linked[uid].telegram_connection.telegram_user_id,
                linked[uid].telegram_connection.binding_id) for uid in approver_ids if uid in linked]
            delivered_keys = {(row.approver_user_id, row.rule_id) for row in existing
                              if row.message_id is not None or row.status != 'failed'}
            retry_rows = {(row.approver_user_id, row.rule_id): row for row in existing
                          if row.status == 'failed' and row.message_id is None}
        missing_names = []
        if missing_ids:
            try:
                names = adapter.call('res.users', 'search_read',
                    [[('id', 'in', missing_ids)]], fields=['id', 'name'])
                missing_names = [person['name'] for person in names]
            except OdooError:
                missing_names = [f'Odoo ID {uid}' for uid in missing_ids]
        if state['status'] in ('not_requested', 'pending'):
            try:
                state = adapter.request(result['id'])
            except OdooAccessError as exc:
                raise HTTPException(409, str(exc)) from exc
            except OdooError as exc:
                raise HTTPException(503, 'Odoo chưa xác nhận yêu cầu duyệt; cần kiểm tra trạng thái trước khi thử lại.') from exc
        if state['status'] != 'pending':
            raise HTTPException(409, 'Odoo chưa xác nhận bước duyệt đang chờ; chưa gửi PDF cho người duyệt.')
        latest = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)._readback(
            result['id'], result['company_id'][0])
        def commercial(value):
            return {key: item for key, item in value.items()
                    if key not in ('write_date', 'url')}
        if latest.get('state') != 'draft' or commercial(latest) != commercial(result):
            raise HTTPException(409, 'Nội dung báo giá đã đổi trong lúc xin duyệt. Cần xem PDF mới trước khi gửi Telegram.')
        result = latest
        with request.app.state.session_factory() as db:
            source = db.get(QuotePdfMessage, body.token)
            if source:
                source.version = str(result['write_date'])
                db.commit()
        try:
            _record_native_review(ctx, request.app.state.session_factory, result, body.token)
        except (OdooError, ValueError) as exc:
            raise HTTPException(503, 'Odoo đã ghi yêu cầu nhưng chưa lưu được phiên bản kiểm soát; kiểm tra trước khi thử gửi PDF.') from exc
        filename, content = render_quotation_pdf(ctx, result)
        sent = 0
        delivery_failed = False
        cannot_validate = []
        for uid, gateway_user_id, chat_id, telegram_user_id, binding_id in recipients:
            for rule in stage:
                if uid not in (rule.get('approver_ids') or []):
                    continue
                key = (gateway_user_id, rule['id'])
                if key in delivered_keys:
                    continue
                try:
                    with request.app.state.session_factory() as db:
                        recipient = db.get(User, gateway_user_id)
                        eligible = _can_receive_approval(request.app.state.settings, recipient,
                            result['id'], result['company_id'][0], rule['id'])
                except (OdooError, ValueError):
                    eligible = False
                if not eligible:
                    cannot_validate.append(str(uid))
                    continue
                retry = retry_rows.get(key)
                token = retry.token if retry else secrets.token_hex(12)
                with request.app.state.session_factory() as db:
                    if retry:
                        row = db.get(QuoteApprovalDelivery, token)
                        row.status = 'sending'
                    else:
                        db.add(QuoteApprovalDelivery(token=token, pdf_token=body.token,
                            rule_id=rule['id'], approver_user_id=gateway_user_id,
                            order_id=result['id'], company_id=result['company_id'][0],
                            version=str(result['write_date']), chat_id=chat_id,
                            telegram_user_id=telegram_user_id, binding_id=binding_id,
                            status='sending'))
                    db.commit()
                markup = {'inline_keyboard': [[
                    {'text': 'Phê duyệt', 'callback_data': 'qg:' + token},
                    {'text': 'Yêu cầu chỉnh sửa', 'callback_data': 'qr:' + token}]]}
                try:
                    delivered = TelegramClient(request.app.state.settings.TELEGRAM_BOT_TOKEN).send_document(
                        chat_id, filename, content,
                        caption=f"Yêu cầu phê duyệt báo giá {result['name']}\nNgười gửi: {ctx.user.name}\nKhách hàng: {result.get('partner_id', ['', ''])[1]}\nTổng tiền: {result.get('amount_total', '')}",
                        content_type='application/pdf', reply_markup=markup)
                    with request.app.state.session_factory() as db:
                        row = db.get(QuoteApprovalDelivery, token)
                        row.message_id = delivered['message_id']
                        row.status = 'pending'
                        db.commit()
                    sent += 1
                except Exception as exc:
                    from app.telegram.client import BotBlockedError, RateLimitError
                    known_failure = isinstance(exc, (BotBlockedError, RateLimitError))
                    with request.app.state.session_factory() as db:
                        db.get(QuoteApprovalDelivery, token).status = 'failed' if known_failure else 'unknown'
                        db.commit()
                    delivery_failed = True
                    break
        suffix = (f" Chưa gửi Telegram cho: {', '.join(missing_names)}."
                  if missing_names else '')
        if cannot_validate:
            suffix += (' Chưa xác minh được quyền duyệt trên Odoo cho tài khoản Odoo ID: '
                       + ', '.join(cannot_validate) + '.')
        if sent == 0 and not existing and not recipients:
            return {'status': 'partial', 'message': 'Odoo đã ghi yêu cầu duyệt. Chưa có người duyệt liên kết Telegram.' + suffix}
        if sent == 0 and existing and not retry_rows:
            return {'status': 'partial', 'message': 'Odoo đã có yêu cầu duyệt; các bản PDF đã gửi hoặc cần kiểm tra giao nhận. Không tạo yêu cầu trùng.' + suffix}
        if delivery_failed:
            suffix += ' Có lượt gửi Telegram lỗi hoặc chưa xác minh; kiểm tra trước khi thử lại.'
        return {'status': 'sent' if not (missing_names or cannot_validate or delivery_failed) else 'partial',
                'message': f'Odoo đã ghi yêu cầu duyệt; Telegram đã gửi thêm {sent} bản PDF cho bước {order}.' + suffix}


@router.post('/approval-open')
def open_approval_reply(user_id: int, body: ApprovalReplyRequest, request: Request):
    enabled(request)
    with authenticated_context(request, user_id, body) as ctx:
        _, result = _approval_delivery(request, ctx, body.token)
        return {'status': 'OK', 'name': result['name']}


@router.post('/approval-decision')
def decide_quotation_approval(user_id: int, body: ApprovalReplyRequest, request: Request):
    from app.quotations.approval import QuotationApproval
    from app.quotations.interaction import QuoteApprovalDelivery, QuotePdfMessage
    from app.telegram.client import TelegramClient
    enabled(request)
    if not request.app.state.settings.ENABLE_QUOTATION_APPROVAL_FLOW:
        raise HTTPException(409, 'Luồng duyệt chưa được bật trên Gateway.')
    with authenticated_context(request, user_id, body) as ctx:
        target, result = _approval_delivery(request, ctx, body.token)
        with request.app.state.session_factory() as db:
            source = db.get(QuotePdfMessage, target['pdf_token'])
            if not source:
                raise HTTPException(409, 'Không tìm thấy người tạo báo giá.')
            creator_chat, creator_user_id, creator_binding = source.chat_id, source.telegram_user_id, source.binding_id
        adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret,
                                    target['company_id'])
        if body.feedback is not None and not body.feedback.strip():
            raise HTTPException(400, 'Nội dung góp ý không được để trống.')
        with request.app.state.session_factory() as db:
            claimed = db.query(QuoteApprovalDelivery).filter_by(
                pdf_token=target['pdf_token'], status='pending').update(
                    {'status': 'processing'})
            db.commit()
            if claimed < 1:
                raise HTTPException(409, 'Bước duyệt này đang được xử lý hoặc đã có quyết định.')
        if body.feedback is None:
            try:
                state = adapter.decide(target['order_id'], target['rule_id'], True)
            except OdooAccessError as exc:
                with request.app.state.session_factory() as db:
                    db.query(QuoteApprovalDelivery).filter_by(
                        pdf_token=target['pdf_token'], status='processing').update(
                            {'status': 'pending'})
                    db.commit()
                raise HTTPException(403, 'Odoo chưa cho phép tài khoản này duyệt bước được chọn.') from exc
            except OdooError as exc:
                raise HTTPException(503, 'Odoo chưa xác nhận phê duyệt; cần kiểm tra trạng thái trước khi thử lại.') from exc
            entry = next((item for item in state.get('entries', [])
                if item.get('rule_id', [None])[0] == target['rule_id']
                and item.get('approved') is True), None)
            if (not entry or not isinstance(entry.get('user_id'), (list, tuple))
                    or not entry['user_id'] or entry['user_id'][0] != ctx.user.odoo_uid):
                raise HTTPException(503, 'Odoo chưa xác minh quyết định dưới đúng danh tính người duyệt.')
            outcome = 'approved' if state['status'] == 'approved' else 'pending'
            with request.app.state.session_factory() as db:
                db.query(QuoteApprovalDelivery).filter_by(pdf_token=target['pdf_token'],
                    rule_id=target['rule_id']).update({'status': 'approved'})
                db.query(QuoteApprovalDelivery).filter_by(pdf_token=target['pdf_token'],
                    status='processing').update({'status': 'pending'})
                db.commit()
            message = (f"Báo giá {result['name']} đã được phê duyệt đủ các bước trên Odoo."
                if outcome == 'approved' else f"Báo giá {result['name']} đã duyệt bước này; còn chờ các bước khác trên Odoo.")
            if outcome == 'pending':
                try:
                    latest = QuotationService(ctx.service.client, ctx.user.odoo_uid,
                        ctx.secret)._readback(target['order_id'], target['company_id'])
                    stable = lambda value: {key: item for key, item in value.items()
                        if key not in ('write_date', 'url')}
                    if latest.get('state') != 'draft' or stable(latest) != stable(result):
                        raise HTTPException(409, 'Nội dung báo giá đã đổi; cần PDF mới cho bước tiếp theo.')
                    with request.app.state.session_factory() as db:
                        db.get(QuotePdfMessage, target['pdf_token']).version = str(latest['write_date'])
                        db.commit()
                    next_stage = submit_quotation_approval(source.user_id,
                        PdfInteractionRequest(telegram_user_id=creator_user_id,
                            chat_id=creator_chat, telegram_binding_id=creator_binding,
                            token=target['pdf_token']), request)
                    message += ' ' + next_stage['message']
                except Exception:
                    message += ' Chưa giao được PDF cho bước kế tiếp; sale cần kiểm tra và thử gửi lại từ PDF.'
        else:
            try:
                state = adapter.inspect(target['order_id'])
                rule = next((item for item in state['rules']
                    if item['id'] == target['rule_id']), None)
                if (state['status'] != 'pending' or not rule
                        or rule.get('can_validate') is not True
                        or any(item.get('rule_id', [None])[0] == target['rule_id']
                               for item in state['entries'])):
                    raise OdooAccessError('Bước duyệt không còn chờ tài khoản này.')
                note = (f"Yêu cầu chỉnh sửa báo giá {escape(result['name'])} từ "
                        f"{escape(ctx.user.name)}:<br>{escape(body.feedback).replace(chr(10), '<br>')}")
                note_id = adapter.call('sale.order', 'message_post',
                    [[target['order_id']]], body=note, message_type='comment',
                    subtype_xmlid='mail.mt_note', partner_ids=[],
                    context={'mail_notify_force_send': False, 'mail_post_autofollow': False})
                if type(note_id) is not int or note_id <= 0:
                    raise OdooError('Odoo chưa trả về ID ghi chú.')
                recorded = adapter.call('mail.message', 'search_read',
                    [[('id', '=', note_id), ('model', '=', 'sale.order'),
                      ('res_id', '=', target['order_id'])]], fields=['id'], limit=1)
                if len(recorded) != 1:
                    raise OdooError('Odoo chưa xác minh ghi chú trên báo giá.')
                current = QuotationService(ctx.service.client, ctx.user.odoo_uid,
                    ctx.secret)._readback(target['order_id'], target['company_id'])
                if current.get('state') != 'draft' or not current.get('write_date'):
                    raise OdooError('Báo giá không còn là nháp hoặc thiếu phiên bản Odoo.')
            except OdooAccessError as exc:
                with request.app.state.session_factory() as db:
                    db.query(QuoteApprovalDelivery).filter_by(
                        pdf_token=target['pdf_token'], status='processing').update(
                            {'status': 'pending'})
                    db.commit()
                raise HTTPException(403, str(exc)) from exc
            except OdooError as exc:
                raise HTTPException(503, 'Odoo chưa xác minh góp ý; kiểm tra ghi chú trước khi thử lại.') from exc
            with request.app.state.session_factory() as db:
                db.query(QuoteApprovalDelivery).filter_by(pdf_token=target['pdf_token'],
                    status='processing').update({'status': 'feedback',
                        'feedback_text': body.feedback})
                db.commit()
            message = (f"Người duyệt {ctx.user.name} yêu cầu chỉnh sửa báo giá "
                f"{result['name']}:\n{body.feedback}")
        with request.app.state.session_factory() as db:
            from app.models import TelegramConnection
            linked = db.query(TelegramConnection).filter_by(user_id=source.user_id,
                telegram_user_id=creator_user_id, private_chat_id=creator_chat,
                binding_id=creator_binding, status='connected').first()
        if not linked:
            return {'status': 'recorded_not_delivered', 'message': 'Đã ghi nhận quyết định nhưng người tạo chưa còn liên kết Telegram để nhận thông báo.'}
        try:
            client = TelegramClient(request.app.state.settings.TELEGRAM_BOT_TOKEN)
            if body.feedback is None:
                client.send_message(creator_chat, message)
            else:
                from app.quotations.interaction import QuotePdfMessage
                token = secrets.token_hex(12)
                with request.app.state.session_factory() as db:
                    db.add(QuotePdfMessage(token=token, user_id=source.user_id,
                        tenant=source.tenant, database=source.database,
                        order_id=target['order_id'], company_id=target['company_id'],
                        version=str(current['write_date']), telegram_user_id=creator_user_id,
                        chat_id=creator_chat, binding_id=creator_binding))
                    db.commit()
                delivered = client.send_message(creator_chat, message,
                    preserve_text=True, reply_markup={'inline_keyboard': [[
                        {'text': 'Chỉnh sửa', 'callback_data': 'qe:' + token}]]})
                with request.app.state.session_factory() as db:
                    db.get(QuotePdfMessage, token).message_id = delivered['message_id']
                    db.commit()
        except Exception:
            return {'status': 'recorded_not_delivered', 'message': 'Đã ghi nhận quyết định nhưng Telegram chưa xác nhận đã giao thông báo cho người tạo.'}
        return {'status': 'recorded', 'message': 'Đã ghi nhận và gửi thông báo cho người tạo.'}


def _pdf_target(request, ctx, token):
    from app.quotations.interaction import QuotePdfMessage
    with request.app.state.session_factory() as db:
        row = db.get(QuotePdfMessage, token)
        if (not row or row.message_id is None or row.user_id != ctx.user.id
                or row.tenant != ctx.user.tenant or row.database != ctx.user.database
                or row.telegram_user_id != ctx.user.telegram_connection.telegram_user_id
                or row.chat_id != ctx.user.telegram_connection.private_chat_id
                or row.binding_id != ctx.user.telegram_connection.binding_id):
            raise HTTPException(403, 'Nút PDF không thuộc liên kết Telegram hiện tại.')
        order_id, company_id, version = row.order_id, row.company_id, row.version
    with request.app.state.session_factory() as db:
        from app.quotations.tracking import QuotationRecordLink
        link = db.query(QuotationRecordLink).filter_by(user_id=ctx.user.id, order_id=order_id,
            company_id=company_id, tenant=ctx.user.tenant, database=ctx.user.database).first()
        if not link:
            raise HTTPException(403, 'Báo giá không thuộc tài khoản đã tạo PDF này.')
    result = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)._readback(order_id, company_id)
    if result.get('state') != 'draft':
        raise HTTPException(409, 'Báo giá không còn là nháp.')
    if str(result.get('write_date')) != version:
        raise HTTPException(409, 'PDF này đã cũ. Hãy gửi lại PDF trước khi thao tác.')
    return result


@router.post('/pdf-open')
def open_pdf_interaction(user_id: int, body: PdfInteractionRequest, request: Request):
    enabled(request)
    with authenticated_context(request, user_id, body) as ctx:
        result = _pdf_target(request, ctx, body.token)
        return {'status': 'OK', 'order_id': result['id'], 'name': result['name'],
                'version': str(result['write_date']), 'company_id': result['company_id'][0],
                'lines': result.get('lines') or []}


@router.post('/pdf-edit')
def edit_pdf_quotation(user_id: int, body: PdfEditRequest, request: Request):
    from app.quotations.interaction import send_pdf_with_buttons
    from app.quotations.pdf import render_quotation_pdf
    from app.telegram.client import TelegramClient, TelegramError
    enabled(request)
    with authenticated_context(request, user_id, body) as ctx:
        before = _pdf_target(request, ctx, body.token)
        term_edit = body.payment_term_id is not None
        optional_edit = body.optional_fields is not None
        line_edit = body.line_id is not None and body.quantity is not None
        if sum((term_edit, optional_edit, line_edit)) != 1 or (body.line_id is None) != (body.quantity is None):
            raise HTTPException(400, 'Chỉ sửa một trường mỗi lần: điều khoản, mục tùy chọn hoặc số lượng một dòng.')
        if optional_edit and len(body.optional_fields) != 1:
            raise HTTPException(400, 'Mỗi lần chỉ sửa một mục tùy chọn.')
        if line_edit and body.line_id not in {line['id'] for line in before.get('lines') or []}:
            raise HTTPException(400, 'Dòng hàng không thuộc báo giá được bấm.')
        company_id = before['company_id'][0]
        ctx.selected_companies([company_id])
        try:
            if term_edit:
                service = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
                service._record('account.payment.term', body.payment_term_id, company_id)
                written = ctx.call('sale.order', 'write', [[before['id']], {'payment_term_id': body.payment_term_id}])
            elif optional_edit:
                service = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
                values = service.optional_values(body.optional_fields, company_id, before)
                written = ctx.call('sale.order', 'write', [[before['id']], values])
            else:
                written = ctx.call('sale.order.line', 'write', [[body.line_id], {'product_uom_qty': body.quantity}])
            if written is not True:
                raise OdooError('Odoo did not confirm the line update')
            after = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)._readback(before['id'], company_id)
        except OdooAccessError as exc:
            raise HTTPException(403, 'Tài khoản không có quyền sửa dòng báo giá này.') from exc
        except QuotationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OdooError as exc:
            raise HTTPException(503, 'Odoo chưa xác nhận việc sửa báo giá; hãy kiểm tra bản ghi trước khi thử lại.') from exc
        revised = next((line for line in after.get('lines') or [] if line.get('id') == body.line_id), None)
        lines_unchanged = all(any(line.get('id') == old.get('id') and
            float(line.get('product_uom_qty') or 0) == float(old.get('product_uom_qty') or 0)
            for line in after.get('lines') or []) for old in before.get('lines') or [])
        invalid = (after.get('id') != before['id'] or after.get('validity_date') != before.get('validity_date'))
        if term_edit:
            actual_term = after.get('payment_term_id')
            invalid = invalid or (actual_term[0] if isinstance(actual_term, (list, tuple)) else actual_term) != body.payment_term_id or not lines_unchanged
        elif optional_edit:
            field, expected = next(iter(body.optional_fields.items()))
            actual = after.get(field)
            kind = service.optional_spec(field, company_id)['type']
            if kind == 'many2one':
                actual = actual[0] if isinstance(actual, (list, tuple)) and actual else actual
            elif kind == 'many2many':
                actual, expected = sorted(actual or []), sorted(expected)
            other_fields_unchanged = all(after.get(name) == before.get(name)
                for name in ORDER_FIELDS if name != field)
            invalid = invalid or actual != expected or not lines_unchanged or not other_fields_unchanged
        else:
            invalid = invalid or not revised or abs(float(revised.get('product_uom_qty') or 0) - body.quantity) > 0.000001 or after.get('payment_term_id') != before.get('payment_term_id')
        if invalid:
            return {'status': 'saved_pdf_failed', 'order_id': before['id'], 'name': before['name'],
                    'message': 'Odoo trả về dữ liệu khác yêu cầu sau khi sửa. Hãy kiểm tra báo giá trên Odoo trước khi gửi PDF.'}
        try:
            filename, content = render_quotation_pdf(ctx, after)
            send_pdf_with_buttons(TelegramClient(request.app.state.settings.TELEGRAM_BOT_TOKEN),
                request.app.state.session_factory, ctx.user, body, after, filename, content,
                f"Báo giá {after['name']} sau chỉnh sửa (nháp).")
        except (OdooError, TelegramError, ValueError) as exc:
            return {'status': 'saved_pdf_failed', 'order_id': after['id'], 'name': after['name'],
                    'message': 'Đã lưu trên Odoo nhưng chưa gửi được PDF. Hãy gửi lại PDF theo mã báo giá.'}
        return {'status': 'sent', 'order_id': after['id'], 'name': after['name'],
                'version': str(after.get('write_date'))}


@router.post('/pdf-retry')
def retry_quotation_pdf(user_id: int, body: PdfRetryRequest, request: Request):
    """Send a fresh draft preview for a quotation already linked to this sale."""
    from app.quotations.pdf import render_quotation_pdf
    from app.quotations.interaction import send_pdf_with_buttons
    from app.quotations.tracking import QuotationRecordLink
    from app.telegram.client import TelegramClient, TelegramError

    enabled(request)
    with authenticated_context(request, user_id, body) as ctx:
        order_id = body.order_id
        if body.name:
            found = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).status_by_name(body.name)
            if found.get('status') != 'OK':
                raise HTTPException(404, 'Không tìm thấy đúng một báo giá có mã này trong phạm vi tài khoản.')
            if order_id and order_id != found['result']['id']:
                raise HTTPException(409, 'Mã và ID báo giá không khớp.')
            order_id = found['result']['id']
        if not order_id:
            raise HTTPException(400, 'Cần mã hoặc ID báo giá để gửi lại PDF.')
        with request.app.state.session_factory() as db:
            link = db.query(QuotationRecordLink).filter_by(user_id=user_id, order_id=order_id,
                tenant=ctx.user.tenant, database=ctx.user.database).first()
            if not link or not link.company_id:
                raise HTTPException(404, 'Không tìm thấy báo giá đã tạo từ tài khoản Telegram này.')
            company_id = link.company_id
        try:
            result = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)._readback(order_id, company_id)
            if result.get('state') != 'draft':
                raise HTTPException(409, 'Báo giá không còn ở trạng thái nháp.')
            filename, content = render_quotation_pdf(ctx, result)
        except OdooAccessError as exc:
            raise HTTPException(403, 'Tài khoản không có quyền xem hoặc xuất PDF báo giá này.') from exc
        except OdooNetworkError as exc:
            raise HTTPException(503, 'Chưa đọc được báo giá từ Odoo; có thể thử lại trên cùng bản ghi.') from exc
        except QuotationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OdooError as exc:
            raise HTTPException(503, 'Odoo chưa xuất được PDF nháp; báo giá đã lưu, có thể thử lại.') from exc
        try:
            send_pdf_with_buttons(TelegramClient(request.app.state.settings.TELEGRAM_BOT_TOKEN),
                request.app.state.session_factory, ctx.user, body, result, filename, content,
                f"Bản xem trước báo giá {result['name']} (nháp).")
        except ValueError as exc:
            raise HTTPException(503, 'Chưa ghi nhận được nút PDF hoặc phiên bản Odoo; báo giá vẫn là nháp, hãy kiểm tra rồi thử lại.') from exc
        except TelegramError as exc:
            raise HTTPException(503, 'Telegram chưa xác nhận đã nhận PDF; hãy kiểm tra cuộc trò chuyện trước khi thử lại.') from exc
        return {'status': 'sent', 'order_id': order_id, 'name': result['name']}


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


@router.get('/optional-metadata')
def optional_metadata(user_id: int, request: Request, field: str, company_id: int | None = None,
                      binding: TelegramScope = Depends(query_scope)):
    enabled(request)
    with authenticated_context(request, user_id, binding) as ctx:
        try:
            return QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).optional_spec(field, company_id)
        except OdooAccessError as exc:
            raise HTTPException(403, str(exc)) from exc
        except OdooNetworkError as exc:
            raise HTTPException(503, 'Không thể kết nối Odoo.') from exc
        except QuotationValidationError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get('/optional-candidates')
def optional_candidates(user_id: int, request: Request, field: str, query: str = '',
                        company_id: int | None = None, binding: TelegramScope = Depends(query_scope)):
    enabled(request)
    with authenticated_context(request, user_id, binding) as ctx:
        try:
            return QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret).optional_candidates(field, query, company_id)
        except OdooAccessError as exc:
            raise HTTPException(403, str(exc)) from exc
        except OdooNetworkError as exc:
            raise HTTPException(503, 'Không thể kết nối Odoo.') from exc
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
                result['approval'] = read_approval_status(ctx, result)
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
