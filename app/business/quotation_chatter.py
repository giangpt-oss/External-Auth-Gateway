"""Chatter actions attached to an existing sale.order."""
import re
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.business.service import BusinessError, Unsupported
from app.odoo.client import OdooError

ACTIVITY_XMLIDS = {
    'todo': 'mail_activity_data_todo',
    'email': 'mail_activity_data_email',
    'call': 'mail_activity_data_call',
    'document': 'mail_activity_data_upload_document',
    'meeting': 'mail_activity_data_meeting',
}
ACTIVITY_LABELS = {'todo': 'Việc cần làm', 'email': 'Email', 'call': 'Gọi',
                   'document': 'Tài liệu', 'meeting': 'Cuộc họp'}


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise BusinessError(f'Cần {label} là văn bản không quá 12000 ký tự.')
    return value.strip()


def _time(value, timezone):
    try:
        if not isinstance(value, str) or not re.search(r'[T ]\d{2}:\d{2}', value):
            raise ValueError('missing time')
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        return parsed.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, KeyError) as exc:
        raise BusinessError('Thời gian cuộc họp không hợp lệ.') from exc


def prepare(service, action, payload, row):
    if not row:
        return {'missing': ['target_id']}
    opts = payload.setdefault('options', {})
    allowed = {
        'message': {'body', 'recipient', 'recipient_partner_id'},
        'note': {'body'},
        'activity': {'activity_type', 'summary', 'date_deadline', 'user_id', 'note', 'model_id', 'type_id'},
        'meeting': {'summary', 'start', 'stop', 'user_id', 'note', 'model_id', 'type_id', 'attendee_ids'},
    }
    if action not in allowed or set(opts) - allowed[action]:
        raise BusinessError('Thông tin hoạt động chưa được hỗ trợ.')
    if action in ('message', 'note'):
        if not opts.get('body'):
            return {'missing': ['options.body']}
        _text(opts['body'], 'nội dung')
        preview = {'body': opts['body'], 'notice': 'Ghi chú sẽ được lưu trên báo giá.'}
        if action == 'message':
            service.access('mail.message', 'create')
            if 'outgoing_email_to' not in service.schema('mail.message'):
                raise Unsupported('Odoo chưa hỗ trợ gửi tin tới địa chỉ email trực tiếp.')
            recipient = opts.get('recipient')
            if recipient == 'customer' or opts.get('recipient_partner_id'):
                order = service.record('sale.order', row['id'], ['partner_id'])
                partner_id = order['partner_id'][0]
                if opts.get('recipient_partner_id') and opts['recipient_partner_id'] != partner_id:
                    raise BusinessError('Khách hàng của báo giá đã thay đổi.')
                current_email = service.record('res.partner', partner_id, ['email']).get('email')
                if recipient != 'customer' and recipient != current_email:
                    raise BusinessError('Email khách hàng đã thay đổi, hãy xác nhận lại.')
                recipient = current_email
                opts['recipient'] = recipient
                opts['recipient_partner_id'] = partner_id
            if not recipient:
                return {'missing': ['options.recipient'], 'preview': preview}
            if not isinstance(recipient, str) or not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', recipient) or len(recipient) > 254:
                raise BusinessError('Địa chỉ email người nhận không hợp lệ.')
            preview['recipients'] = [{'email': recipient}]
            preview['notice'] = 'Odoo sẽ ghi tin nhắn trên báo giá và tiếp nhận yêu cầu gửi email.'
        return {'preview': preview}

    kind = 'meeting' if action == 'meeting' else opts.get('activity_type')
    if kind not in ACTIVITY_XMLIDS:
        return {'missing': ['options.activity_type']}
    if action == 'activity' and kind == 'meeting':
        raise BusinessError('Cuộc họp cần thời gian bắt đầu và kết thúc.')
    required = ['summary', 'start', 'stop'] if action == 'meeting' else []
    missing = [f'options.{field}' for field in required if not opts.get(field)]
    if missing:
        return {'missing': missing}
    if opts.get('summary'):
        _text(opts['summary'], 'tóm tắt')
    if opts.get('note'):
        _text(opts['note'], 'ghi chú')
    model, model_id = service.ctx.call('ir.model.data', 'check_object_reference', ['sale', 'model_sale_order'])
    if model != 'ir.model' or not model_id:
        raise Unsupported('Không xác định được loại chứng từ báo giá trên Odoo.')
    service.record('ir.model', model_id, ['model'])
    type_model, type_id = service.ctx.call('ir.model.data', 'check_object_reference', ['mail', ACTIVITY_XMLIDS[kind]])
    if type_model != 'mail.activity.type' or not type_id:
        raise Unsupported('Loại hoạt động chưa được cấu hình trên Odoo.')
    activity_type = service.record('mail.activity.type', type_id, [
        'category', 'res_model', 'name', 'summary', 'default_user_id', 'delay_count', 'delay_unit',
    ])
    if activity_type.get('res_model') not in (False, None, 'sale.order'):
        raise Unsupported('Loại hoạt động không áp dụng cho báo giá.')
    opts['model_id'], opts['type_id'] = model_id, type_id
    default_user = activity_type.get('default_user_id')
    opts['user_id'] = opts.get('user_id') or (default_user[0] if default_user else service.ctx.user.odoo_uid)
    user = service.record('res.users', opts['user_id'], ['name', 'active'])
    if user.get('active') is False:
        raise BusinessError('Người được phân công đã bị lưu trữ.')
    service.access('mail.activity', 'create')
    attendee_names = []
    if action == 'meeting':
        service.access('calendar.event', 'create')
        timezone = getattr(service.ctx.user, 'tz', None) or 'Asia/Bangkok'
        # UTC values are marked explicitly so re-preparation does not shift the time twice.
        start, stop = _time(opts['start'], timezone), _time(opts['stop'], timezone)
        if start >= stop:
            raise BusinessError('Thời gian kết thúc phải sau thời gian bắt đầu.')
        opts['start'], opts['stop'] = start + '+00:00', stop + '+00:00'
        attendees = opts.get('attendee_ids', [])
        if not isinstance(attendees, list) or len(attendees) > 30 or any(type(item) is not int for item in attendees):
            raise BusinessError('Danh sách người tham dự không hợp lệ.')
        for attendee in attendees:
            person = service.record('res.partner', attendee, ['name', 'email', 'active'])
            if person.get('active') is False:
                raise BusinessError('Người tham dự đã bị lưu trữ.')
            attendee_names.append(person.get('display_name') or person.get('name') or str(attendee))
    else:
        try:
            if not opts.get('date_deadline'):
                today = datetime.now(ZoneInfo(getattr(service.ctx.user, 'tz', None) or 'Asia/Bangkok')).date()
                unit = activity_type.get('delay_unit') or 'days'
                if unit not in ('days', 'weeks', 'months'):
                    raise BusinessError('Không xác định được ngày mặc định của loại hoạt động.')
                delay = activity_type.get('delay_count') or 0
                if unit == 'months':
                    year, month = divmod(today.year * 12 + today.month - 1 + delay, 12)
                    deadline = date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))
                else:
                    deadline = today + timedelta(days=delay * (7 if unit == 'weeks' else 1))
                opts['date_deadline'] = deadline.isoformat()
            opts['date_deadline'] = date.fromisoformat(opts['date_deadline']).isoformat()
        except (TypeError, ValueError) as exc:
            raise BusinessError('Ngày đến hạn không hợp lệ.') from exc
        if not opts.get('summary') and activity_type.get('summary'):
            opts['summary'] = activity_type['summary']
    local_start = datetime.fromisoformat(opts['start']).astimezone(ZoneInfo(timezone)).isoformat() if action == 'meeting' else None
    local_stop = datetime.fromisoformat(opts['stop']).astimezone(ZoneInfo(timezone)).isoformat() if action == 'meeting' else None
    return {'preview': {'activity_type': ACTIVITY_LABELS[kind], 'summary': opts.get('summary') or activity_type.get('name'),
                        'date_deadline': opts.get('date_deadline'), 'start': local_start, 'stop': local_stop,
                        'assignee': user.get('name') or user.get('display_name'), 'attendees': attendee_names,
                        'body': opts.get('note'), 'notice': 'Hoạt động sẽ gắn với báo giá trên Odoo.'}}


def execute(service, action, payload, row, checkpoint):
    opts, ctx, order_id = payload['options'], service.ctx, row['id']
    if action in ('message', 'note'):
        kwargs = {'body': opts['body'], 'message_type': 'comment', 'partner_ids': [],
                  'subtype_xmlid': 'mail.mt_note' if action == 'note' else 'mail.mt_comment',
                  'context': {'mail_notify_force_send': False, 'mail_post_autofollow': False}}
        if action == 'message':
            kwargs.update(outgoing_email_to=opts['recipient'], notify_skip_followers=True)
        result_id = ctx.call('sale.order', 'message_post', [[order_id]], kwargs)
        checkpoint('chatter_posted', {'model': 'mail.message', 'id': result_id, 'target_id': order_id})
        if isinstance(result_id, list | tuple) and len(result_id) == 1:
            result_id = result_id[0]
        if type(result_id) is not int or result_id <= 0:
            raise BusinessError('Đã gửi yêu cầu ghi tin, cần kiểm tra kết quả trên Odoo.')
        message = service.record('mail.message', result_id, ['model', 'res_id', 'outgoing_email_to'] if action == 'message' else ['model', 'res_id'])
        if message.get('model') != 'sale.order' or message.get('res_id') != order_id:
            raise BusinessError('Chưa xác minh tin nhắn đã gắn với báo giá.')
        if action == 'message':
            if (message.get('outgoing_email_to') or '').casefold() != opts['recipient'].casefold():
                raise BusinessError('Đã ghi tin nhắn, cần kiểm tra lại địa chỉ email người nhận trên Odoo.')
            try:
                outgoing = ctx.call('mail.mail', 'search_read', [[('mail_message_id', '=', result_id)]],
                                    {'fields': ['state'], 'limit': 10})
            except OdooError:
                outgoing = []
            states = {item.get('state') for item in outgoing}
            delivery = 'sent' if states == {'sent'} else 'queued' if states and states <= {'outgoing', 'sent'} else 'unknown'
            result = 'Đã ghi tin nhắn trên báo giá.'
        else:
            delivery = None
            result = 'Đã tạo ghi chú nội bộ trên báo giá.'
    elif action == 'meeting':
        values = {'name': opts['summary'], 'start': opts['start'][:19], 'stop': opts['stop'][:19],
                  'res_model_id': opts['model_id'], 'res_id': order_id, 'user_id': opts['user_id']}
        if opts.get('note'):
            values['description'] = escape(opts['note']).replace('\n', '<br>')
        if 'attendee_ids' in opts:
            values['partner_ids'] = [(6, 0, opts['attendee_ids'])]
        result_id = ctx.call('calendar.event', 'create', [values])
        checkpoint('meeting_created', {'model': 'calendar.event', 'id': result_id, 'target_id': order_id})
        event = service.record('calendar.event', result_id, ['res_model_id', 'res_id', 'activity_ids'])
        if event.get('res_id') != order_id or not event.get('activity_ids'):
            raise BusinessError('Đã tạo cuộc họp, cần kiểm tra liên kết hoạt động trên báo giá.')
        result = 'Đã tạo cuộc họp và hoạt động trên báo giá.'
    else:
        values = {'res_model_id': opts['model_id'], 'res_id': order_id, 'activity_type_id': opts['type_id'],
                  'user_id': opts['user_id'], 'date_deadline': opts['date_deadline']}
        if opts.get('summary'):
            values['summary'] = opts['summary']
        if opts.get('note'):
            values['note'] = escape(opts['note']).replace('\n', '<br>')
        result_id = ctx.call('mail.activity', 'create', [values])
        checkpoint('activity_created', {'model': 'mail.activity', 'id': result_id, 'target_id': order_id})
        activity = service.record('mail.activity', result_id, ['res_model', 'res_id'])
        if activity.get('res_model') != 'sale.order' or activity.get('res_id') != order_id:
            raise BusinessError('Đã tạo hoạt động, cần kiểm tra liên kết với báo giá.')
        result = 'Đã lên lịch hoạt động trên báo giá.'
    response = {'status': 'completed', 'action': 'business.sales.' + action,
                'record': service.readback('sale.order', order_id), 'message': result}
    if action == 'message':
        response['delivery_status'] = delivery
    return response
