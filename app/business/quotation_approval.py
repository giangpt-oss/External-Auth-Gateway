"""Confirmed native approval adapter and a durable commercial-data review fence."""
import hashlib
import json
import time

from app.business.models import QuotationApprovalReview
from app.business.service import BusinessError, Conflict, Unsupported
from app.quotations.approval import QuotationApproval, approval_control

FLOW_ACTIONS = {'business.sales.request_approval', 'business.sales.grant_approval',
                'business.sales.reject_approval', 'business.sales.send', 'business.sales.confirm'}
HEADER_FIELDS = ('partner_id', 'partner_invoice_id', 'partner_shipping_id', 'pricelist_id',
                 'payment_term_id', 'validity_date', 'note', 'client_order_ref', 'origin',
                 'currency_id', 'fiscal_position_id', 'user_id', 'team_id', 'order_line',
                 'amount_untaxed', 'amount_tax', 'amount_total')
LABELS = {'partner_id': 'Khách hàng', 'partner_invoice_id': 'Địa chỉ xuất hóa đơn',
          'partner_shipping_id': 'Địa chỉ giao hàng', 'pricelist_id': 'Bảng giá',
          'payment_term_id': 'Điều khoản thanh toán', 'validity_date': 'Ngày hết hạn',
          'note': 'Điều khoản và điều kiện', 'client_order_ref': 'Tham chiếu khách hàng',
          'origin': 'Chứng từ gốc', 'currency_id': 'Tiền tệ', 'fiscal_position_id': 'Vị thế tài chính',
          'user_id': 'Nhân viên bán hàng', 'team_id': 'Đội bán hàng',
          'amount_untaxed': 'Tiền trước thuế', 'amount_tax': 'Tiền thuế', 'amount_total': 'Tổng tiền'}


def native(service):
    ctx = service.ctx
    return QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret, service.company_id)


def commercial_snapshot(service, row):
    meta = service.schema('sale.order')
    extra = [name for name in meta if name.startswith('x_') and not approval_control(name)]
    service.record('sale.order', row['id'], list(HEADER_FIELDS) + extra)
    service.track_children('sale.order', row)
    line_meta = service.schema('sale.order.line')
    line_extra = [name for name in line_meta if name.startswith('x_') and not approval_control(name)]
    for item in list(service.snapshot.values()):
        if item['model'] == 'sale.order.line' and line_extra:
            service.record('sale.order.line', item['id'], line_extra)
    records = []
    for item in service.snapshot.values():
        if item['model'] not in ('sale.order', 'sale.order.line'):
            continue
        keys = set(HEADER_FIELDS) | set(extra) | {'id', 'company_id'} if item['model'] == 'sale.order' else {
            'id', 'company_id', 'product_id', 'name', 'product_uom_qty', 'price_unit', 'tax_id',
            'tax_ids', 'discount', 'customer_lead', 'product_uom', 'product_uom_id'} | set(line_extra)
        values = {k: v for k, v in item['values'].items() if k in keys}
        if item['model'] == 'sale.order.line' and 'name' in item['values']:
            values['name'] = item['values']['name']
        records.append({'model': item['model'], 'id': item['id'], 'values': values})
    records.sort(key=lambda item: (item['model'], item['id']))
    return hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()


def review_key(service, row):
    user = service.ctx.user
    return hashlib.sha256(json.dumps([user.tenant, user.database, service.company_id, row['id']]).encode()).hexdigest()


def require_review(service, row):
    digest = commercial_snapshot(service, row)
    sf = service.ctx.session_factory
    if not sf:
        raise Unsupported('Thiếu nơi lưu phiên bản báo giá đã xin duyệt.')
    with sf() as db:
        review = db.get(QuotationApprovalReview, review_key(service, row))
        if not review or not review.verified or review.digest != digest:
            raise Conflict('Báo giá chưa được xin duyệt qua agent hoặc nội dung đã đổi sau khi xin duyệt. Cần cấp trên thu hồi duyệt cũ trên Odoo và xin duyệt lại; không tự gửi.')


def track_rules(service, state):
    for rule in state['rules']:
        service.record('studio.approval.rule', rule['id'], ['method', 'action_id', 'active', 'domain',
            'approver_ids', 'approval_group_id', 'exclusive_user', 'notification_order', 'entry_ids'])
    for entry in state['entries']:
        service.record('studio.approval.entry', entry['id'], ['rule_id', 'res_id', 'approved', 'user_id'])
    for request in state['requests']:
        service.record('studio.approval.request', request['id'], ['rule_id', 'res_id'])


def prepare(service, definition, payload, row):
    opts = payload.get('options', {})
    if set(opts) - {'rule_id'} or (definition.name.endswith('request_approval') and opts):
        raise BusinessError('Bước duyệt chỉ nhận lựa chọn bước duyệt; không nhận model/method hoặc thay đổi báo giá.')
    if not row:
        return {'missing': ['target_id']}
    state = native(service).inspect(row['id'])
    if not state['rules']:
        raise Unsupported(state['message'])
    commercial_snapshot(service, row)
    track_rules(service, state)
    if definition.name.endswith('request_approval'):
        if state['status'] != 'not_requested':
            raise BusinessError('Báo giá đã có yêu cầu hoặc kết quả duyệt; không tự yêu cầu lại.')
        if any(rule.get('can_validate') for rule in state['rules']):
            raise BusinessError('Tài khoản này có thể tự duyệt. Hãy chọn thao tác duyệt riêng, không tự duyệt khi xin cấp trên duyệt.')
    else:
        require_review(service, row)
        if not state['requests']:
            raise BusinessError('Không có yêu cầu duyệt đang chờ trên Odoo.')
        candidates = [rule for rule in state['rules'] if rule.get('can_validate') is True
                      and not any(entry.get('rule_id', [None])[0] == rule['id'] for entry in state['entries'])]
        if not candidates:
            raise BusinessError('Tài khoản hiện tại không có bước duyệt đang chờ được phép xử lý.')
        if 'rule_id' not in opts and len(candidates) == 1:
            payload.setdefault('options', {})['rule_id'] = candidates[0]['id']
            opts = payload['options']
        if type(opts.get('rule_id')) is not int or opts['rule_id'] not in [r['id'] for r in candidates]:
            return {'missing': ['options.rule_id'], 'preview': {'approval_rules': candidates}}
    ids = sorted({uid for rule in state['rules'] for uid in rule.get('approver_ids', [])})
    people = service.ctx.call('res.users', 'search_read', [[('id', 'in', ids)]], {'fields': ['name']}) if ids else []
    names = {person['id']: person['name'] for person in people}
    for rule in state['rules']:
        rule['approver_names'] = [names[uid] for uid in rule.get('approver_ids', []) if uid in names]
    source = service.snapshot[('sale.order', row['id'])]['values']
    meta = service.schema('sale.order')
    fields = []
    for name in HEADER_FIELDS:
        if name not in meta or name == 'order_line':
            continue
        value = source.get(name)
        if isinstance(value, tuple | list) and meta[name].get('type') == 'many2one':
            value = value[1] if len(value) > 1 else None
        fields.append({'label': LABELS.get(name) or meta[name].get('string') or name, 'value': None if value is False else value})
    lines = [item['values'] for item in service.snapshot.values() if item['model'] == 'sale.order.line']
    return {'preview': {'approval_state': state['status'], 'approval_rules': state['rules'],
            'company_name': source.get('company_id', [None, ''])[1],
            'quotation_fields': fields, 'quotation_lines': lines,
            'notice': 'Chỉ xử lý duyệt nội bộ bằng tài khoản hiện tại. Không gửi email và không chuyển thành đơn bán.'}}


def execute(service, definition, payload, row, operation_id, checkpoint):
    adapter = native(service)
    if definition.name.endswith('request_approval'):
        sf = service.ctx.session_factory
        if not sf:
            raise Unsupported('Thiếu nơi lưu phiên bản xin duyệt.')
        key, digest = review_key(service, row), commercial_snapshot(service, row)
        with sf() as db:
            existing = db.get(QuotationApprovalReview, key)
            if existing:
                db.delete(existing)
                db.flush()
            db.add(QuotationApprovalReview(key=key, digest=digest, operation_id=operation_id,
                requester_id=service.ctx.user.id, created_at=time.time(), verified=0))
            db.commit()
        checkpoint('approval_dispatch', {'model': 'sale.order', 'id': row['id']})
        result = adapter.request(row['id'])
        verified = result['status'] == 'pending'
        if verified:
            with sf() as db:
                db.query(QuotationApprovalReview).filter_by(key=key, operation_id=operation_id).update({'verified': 1})
                db.commit()
        message = 'Đã gửi yêu cầu duyệt nội bộ; đang chờ cấp trên duyệt.' if verified else 'Odoo chưa xác minh được yêu cầu duyệt. Không tự gửi lại.'
    else:
        require_review(service, row)
        approved = definition.name.endswith('grant_approval')
        checkpoint('approval_dispatch', {'model': 'sale.order', 'id': row['id']})
        result = adapter.decide(row['id'], payload['options']['rule_id'], approved)
        rule_id = payload['options']['rule_id']
        verified = any(e.get('rule_id', [None])[0] == rule_id and e.get('approved') is approved for e in result['entries'])
        message = 'Đã ghi nhận quyết định duyệt nội bộ trên Odoo.' if verified else 'Chưa xác minh được quyết định duyệt; không tự thực hiện lại.'
    checkpoint('approval_readback', {'model': 'sale.order', 'id': row['id'], 'approval_status': result['status']})
    return {'status': 'completed' if verified else 'check_required', 'action': definition.name,
            'record': service.readback('sale.order', row['id']), 'approval_status': result['status'], 'message': message}
