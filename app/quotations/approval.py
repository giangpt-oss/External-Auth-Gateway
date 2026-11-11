"""Native Studio approval of sending a quotation, never sale-order confirmation.

Contracts verified against this tenant's web_studio approval service (Odoo 19).
Discovery is read-only. check_approval and set_approval are write operations and
must only be called by the confirmed business-action adapter.
"""
from app.odoo.client import OdooAccessError, OdooError

# User chose to keep native Odoo rules unchanged. Reuse the existing internal
# decision protecting confirmation as an additional Gateway-only send gate.
APPROVAL_METHOD = 'action_confirm'


APPROVAL_LABELS = {'not_requested': 'Chưa yêu cầu duyệt', 'pending': 'Đang chờ cấp trên duyệt',
                   'approved': 'Đã duyệt nội bộ', 'rejected': 'Bị từ chối duyệt',
                   'review_required': 'Cần kiểm tra và xin duyệt lại nội dung',
                   'configuration_required': 'Chưa xác minh được quy tắc duyệt phù hợp',
                   'unknown': 'Chưa đọc được trạng thái duyệt'}


def read_approval_status(ctx, result):
    """Read Studio's actual decision separately from eligibility to send via Agent."""
    company_id = result['company_id'][0]
    adapter = QuotationApproval(ctx.service.client, ctx.user.odoo_uid, ctx.secret, company_id)
    try:
        state = adapter.inspect(result['id'])
    except OdooError:
        return {'status': 'unknown'}
    approval = {'status': state['status'], 'scope': 'agent_send_gate', 'message': state['message']}
    result['state'] = state['record'].get('state') or result['state']
    ids = sorted({uid for rule in state['rules'] for uid in rule.get('approver_ids', [])})
    if ids:
        try:
            people = adapter.call('res.users', 'search_read', [[('id', 'in', ids)]], fields=['name'])
            approval['approvers'] = [p['name'] for p in people]
        except OdooError:
            pass
    if state['status'] in ('approved', 'pending'):
        from app.business.quotation_approval import require_review
        from app.business.service import BusinessError, BusinessService

        business = BusinessService(ctx)
        business.company_id = company_id
        try:
            require_review(business, result)
        except (BusinessError, OdooError):
            approval['agent_review_required'] = True
    return approval


def approval_control(name):
    lowered = name.lower()
    return name.startswith('x_') and any(word in lowered for word in ('approv', 'duyet', 'grant'))


class QuotationApproval:
    def __init__(self, client, uid, secret, company_id):
        self.client, self.uid, self.secret = client, uid, secret
        self.company_id = company_id

    def call(self, model, method, args, **kwargs):
        kwargs['context'] = {'allowed_company_ids': [self.company_id], 'force_company': self.company_id}
        return self.client.execute(self.uid, self.secret, model, method, args, kwargs)

    def inspect(self, record_id, method=APPROVAL_METHOD):
        # Prove source read access and company membership before querying Studio,
        # whose own public getters must not be treated as record authorization.
        rows = self.call('sale.order', 'search_read', [[('id', '=', record_id), ('company_id', '=', self.company_id)]],
                         fields=['id', 'name', 'state'], limit=1)
        if len(rows) != 1:
            raise OdooAccessError('Không có quyền xem báo giá trong công ty đã chọn.')
        raw = self.client.studio_approval_spec(self.uid, self.secret, record_id, method, self.company_id)
        matches = [value for key, value in raw.get('sale.order', []) if key == [record_id, method, False]]
        if len(matches) != 1:
            raise OdooError('Không xác minh được dữ liệu phê duyệt Studio cho báo giá.')
        data = matches[0]
        rules = []
        for rule_id in data.get('rules', []):
            rule = raw.get('all_rules', {}).get(str(rule_id)) or raw.get('all_rules', {}).get(rule_id)
            if not rule or rule.get('method') != method or rule.get('action_id'):
                raise OdooError('Quy tắc phê duyệt không khớp hành động báo giá.')
            rules.append(rule)
        entries = data.get('entries', [])
        approved = {item['rule_id'][0] for item in entries if item.get('approved') is True}
        rejected = {item['rule_id'][0] for item in entries if item.get('approved') is False}
        requests = self.call('studio.approval.request', 'search_read',
            [[('res_id', '=', record_id), ('rule_id', 'in', [r['id'] for r in rules])]],
            fields=['id', 'rule_id', 'res_id']) if rules else []
        if not rules:
            state = 'configuration_required'
        elif all(rule['id'] in approved for rule in rules):
            state = 'approved'
        elif any(rule['id'] in rejected for rule in rules):
            state = 'rejected'
        elif requests:
            state = 'pending'
        else:
            state = 'not_requested'
        return {'status': state, 'record': rows[0], 'method': method, 'rules': rules,
                'entries': entries, 'requests': requests,
                'scope': 'agent_send_gate',
                'message': 'Chưa có quy tắc duyệt nội bộ đã xác minh trên Odoo.' if not rules else ''}

    def require_approved(self, record_id, method=APPROVAL_METHOD):
        result = self.inspect(record_id, method)
        if result['status'] != 'approved':
            raise OdooAccessError(result['message'] or 'Báo giá chưa được duyệt đủ các bước; chưa được gửi hoặc chuyển thành đơn bán.')
        return result

    def request(self, record_id):
        state = self.inspect(record_id)
        if not state['rules']:
            raise OdooAccessError(state['message'])
        if state['status'] in ('approved', 'pending', 'rejected'):
            raise OdooAccessError('Báo giá đã có kết quả/yêu cầu duyệt; không tự tạo lại yêu cầu.')
        if any(rule.get('can_validate') for rule in state['rules']):
            raise OdooAccessError('Tài khoản có thể duyệt một bước. Hãy chọn thao tác duyệt riêng, tránh tự duyệt khi chỉ yêu cầu cấp trên duyệt.')
        self.call('studio.approval.rule', 'check_approval', ['sale.order', record_id, APPROVAL_METHOD, False])
        return self.inspect(record_id)

    def decide(self, record_id, rule_id, approved):
        state = self.inspect(record_id)
        rule = next((r for r in state['rules'] if r['id'] == rule_id), None)
        if not rule or rule.get('can_validate') is not True:
            raise OdooAccessError('Tài khoản hiện tại không được duyệt bước này.')
        if any(e.get('rule_id', [None])[0] == rule_id for e in state['entries']):
            raise OdooAccessError('Bước duyệt đã có quyết định. Không ghi đè hoặc tự thu hồi quyết định.')
        if not state['requests']:
            raise OdooAccessError('Báo giá chưa được gửi yêu cầu duyệt.')
        self.call('studio.approval.rule', 'set_approval', [[rule_id]], res_id=record_id, approved=approved)
        return self.inspect(record_id)
