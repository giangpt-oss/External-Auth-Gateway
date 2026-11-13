"""Version-aware adapters for workflows that are not generic CRUD.

Preparation only reads. Every write below runs after a Gateway preparation claim.
Unexpected wizards are returned to the user without selecting their defaults.
"""
from dataclasses import replace
from html import escape

from app.business.catalog import LOOKUPS, TARGET_MODELS
from app.business.service import BusinessError, Unsupported, identity, number, positive_id


def options(payload, allowed):
    value = payload.get('options', {})
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise BusinessError('options chứa trường chưa hỗ trợ.')
    return value


def activity_target(service, payload, row):
    incoming = payload.get('values', {})
    source = row or incoming
    if row:
        source = service.record('mail.activity', row['id'], ['res_model', 'res_model_id', 'res_id'])
        model = source.get('res_model')
    else:
        model_id = incoming.get('res_model_id')
        if not model_id or not incoming.get('res_id'):
            raise BusinessError('Hoạt động cần values.res_model_id và values.res_id.')
        model = service.record('ir.model', model_id, ['model']).get('model')
    if model not in TARGET_MODELS:
        raise Unsupported('Chỉ hỗ trợ hoạt động gắn với bản ghi trong năm phân hệ.')
    target = service.record(model, positive_id(source.get('res_id')))
    service.access(model, 'write')
    service.rules(model, [target['id']], 'write')
    return target


def stock_lines(service, payload, row):
    lines = payload.get('lines')
    if not isinstance(lines, list) or not lines or len(lines) > 100:
        raise BusinessError('Cần 1–100 dòng hàng kho.')
    commands, preview = [], []
    for line in lines:
        if not isinstance(line, dict) or set(line) - {'id', 'product_id', 'quantity', 'uom_id', 'name', 'remove'}:
            raise BusinessError('Dòng kho chưa hợp lệ.')
        move_id = line.get('id')
        if move_id:
            move = service.record('stock.move', move_id, ['picking_id', 'state'])
            if not row or identity(move.get('picking_id')) != row['id'] or move.get('state') != 'draft':
                raise BusinessError('Chỉ sửa dòng nháp thuộc phiếu này.')
            service.access('stock.move', 'unlink' if line.get('remove') else 'write')
            service.rules('stock.move', [move_id], 'unlink' if line.get('remove') else 'write')
        else:
            service.access('stock.move', 'create')
        if line.get('remove'):
            if not move_id:
                raise BusinessError('Cần ID dòng cần xóa.')
            commands.append((2, move_id, 0))
            preview.append(line)
            continue
        product = service.record('product.product', line.get('product_id'), ['uom_id', 'active'])
        if product.get('active') is False:
            raise BusinessError('Sản phẩm đã được lưu trữ.')
        uom = line.get('uom_id') or identity(product.get('uom_id'))
        service.record('uom.uom', uom)
        values = {'name': line.get('name') or product.get('display_name'), 'product_id': product['id'],
                  'product_uom_qty': number(line.get('quantity')), 'product_uom': uom}
        locations = (payload.get('values') or {})
        for field in ('location_id', 'location_dest_id'):
            selected = locations.get(field) or identity((row or {}).get(field))
            if not selected:
                raise BusinessError(f'Cần values.{field}.')
            service.record('stock.location', selected)
            values[field] = selected
        commands.append((1, move_id, values) if move_id else (0, 0, values))
        preview.append(dict(line, product=product.get('display_name')))
    return commands, preview


def prepare(service, definition, payload, row):
    kind, model = definition.kind, definition.model
    if kind == 'quotation_chatter':
        from app.business.quotation_chatter import prepare as prepare_chatter
        return prepare_chatter(service, definition.name.rsplit('.', 1)[-1], payload, row)
    if kind == 'quotation_approval':
        from app.business.quotation_approval import prepare as prepare_approval
        return prepare_approval(service, definition, payload, row)
    if kind == 'backorder':
        opts = options(payload, {'continuation_token', 'backorder'})
        source = payload.get('_continuation')
        if not source or source.get('kind') != 'backorder' or not row or source.get('picking_id') != row['id']:
            raise BusinessError('Cần token bước tiếp theo do Gateway cấp cho đúng phiếu kho.')
        choice = opts.get('backorder') or source.get('choice')
        if choice not in ('create', 'cancel'):
            return {'missing': ['options.backorder (create/cancel)']}
        service.access('stock.backorder.confirmation', 'create')
        wizard_id = source.get('wizard_id')
        if wizard_id:
            wizard = service.record('stock.backorder.confirmation', wizard_id, ['pick_ids', 'create_uid'])
            if wizard.get('pick_ids') != [row['id']] or identity(wizard.get('create_uid')) != service.ctx.user.odoo_uid:
                raise BusinessError('Wizard không thuộc người dùng hoặc phiếu kho hiện tại.')
        return {'preview': {'backorder': choice, 'notice': 'Tiếp tục bước đã lưu; không nhập lại số lượng và không gọi lại bước trước.'}}
    if kind in ('stock_create', 'stock_update'):
        values, missing = service.values(model, payload.get('values', {}), definition.fields, kind == 'stock_create')
        if row:
            row.update(service.record(model, row['id'], ['location_id', 'location_dest_id', 'picking_type_id']))
        if kind == 'stock_create':
            for field in ('picking_type_id', 'location_id', 'location_dest_id'):
                if not values.get(field):
                    missing.append(f'values.{field}')
            values['company_id'] = service.company_id
        if payload.get('lines'):
            commands, preview = stock_lines(service, payload, row)
            values['move_ids'] = commands
        else:
            preview = []
            if kind == 'stock_create':
                missing.append('lines')
        return {'values': values, 'missing': missing, 'preview': {'changes': values, 'lines': preview}}
    if kind == 'convert':
        opts = options(payload, {'partner_action', 'partner_id', 'user_id', 'team_id'})
        service.access('crm.lead2opportunity.partner', 'create')
        required = {'lead_id', 'name', 'action', 'partner_id', 'user_id', 'team_id'}
        if not required.issubset(service.schema('crm.lead2opportunity.partner')):
            raise Unsupported('Wizard chuyển đổi CRM phiên bản này chưa hỗ trợ.')
        missing = []
        if opts.get('partner_action') not in ('exist', 'create'):
            missing.append('options.partner_action (exist/create)')
        if opts.get('partner_action') == 'exist' and not opts.get('partner_id'):
            missing.append('options.partner_id')
        for field, related in (('partner_id', 'res.partner'), ('user_id', 'res.users'), ('team_id', 'crm.team')):
            if opts.get(field):
                service.record(related, opts[field])
        if opts.get('partner_action') == 'create':
            service.access('res.partner', 'create')
        if row:
            service.button(definition, row, action_model='crm.lead2opportunity.partner')
            lead = service.record(model, row['id'], ['type', 'probability', 'partner_name', 'contact_name', 'partner_id'])
            if lead.get('type') != 'lead' or lead.get('probability') == 100:
                raise BusinessError('Chỉ chuyển lead chưa đóng thành cơ hội.')
        return {'missing': missing, 'preview': {'conversion': opts, 'notice': 'Chỉ chuyển đổi; không tự gộp hay xóa lead trùng.'}}
    if kind == 'lost':
        incoming = payload.get('values', {})
        if set(incoming) - {'lost_reason_id'}:
            raise BusinessError('Đánh dấu thua chỉ nhận values.lost_reason_id.')
        reason_id = incoming.get('lost_reason_id')
        if not reason_id:
            return {'missing': ['values.lost_reason_id']}
        reason = service.record('crm.lost.reason', reason_id)
        service.access('crm.lead.lost', 'create')
        if not {'lead_ids', 'lost_reason_id'}.issubset(service.schema('crm.lead.lost')):
            raise Unsupported('Wizard lý do thua chưa hỗ trợ phiên bản này.')
        if row:
            service.button(definition, row, action_model='crm.lead.lost')
        return {'preview': {'lost_reason': reason.get('display_name')}}
    if kind == 'email':
        if row:
            service.button(replace(definition, method='action_quotation_send' if definition.module == 'sales' else 'action_rfq_send'), row)
        opts = options(payload, {'recipient_ids', 'subject', 'body', 'attachment_ids'})
        missing = []
        for field in ('recipient_ids', 'subject', 'body'):
            if not opts.get(field):
                missing.append(f'options.{field}')
        recipients = opts.get('recipient_ids', [])
        attachments = opts.get('attachment_ids', [])
        if not isinstance(recipients, list) or not isinstance(attachments, list) or len(recipients) > 20 or len(attachments) > 10:
            raise BusinessError('Tối đa 20 người nhận và 10 file đính kèm.')
        addresses = []
        for recipient in recipients:
            partner = service.record('res.partner', recipient, ['email', 'active'])
            if not partner.get('email') or partner.get('active') is False:
                raise BusinessError('Người nhận không có email hợp lệ hoặc đã lưu trữ.')
            addresses.append({'id': recipient, 'name': partner.get('display_name'), 'email': partner['email']})
        files = []
        for attachment in attachments:
            item = service.record('ir.attachment', attachment, ['res_model', 'res_id', 'name', 'mimetype', 'checksum'])
            if not row or item.get('res_model') != model or item.get('res_id') != row['id']:
                raise BusinessError('Chỉ gửi file đã gắn với đúng chứng từ được xác nhận.')
            files.append({'id': attachment, 'name': item.get('name'), 'mimetype': item.get('mimetype')})
        for field in ('subject', 'body'):
            if field in opts and (not isinstance(opts[field], str) or len(opts[field]) > 12000):
                raise BusinessError('Tiêu đề/nội dung phải là văn bản tối đa 12000 ký tự.')
        service.access('mail.compose.message', 'create')
        compose = service.schema('mail.compose.message')
        if not {'model', 'res_ids', 'partner_ids', 'subject', 'body', 'composition_mode', 'attachment_ids'}.issubset(compose):
            raise Unsupported('Phiên bản trình soạn email chưa được hỗ trợ.')
        if definition.module == 'sales' and not {'notify_skip_followers', 'notify_author', 'force_send', 'template_id'}.issubset(compose):
            raise Unsupported('Chưa xác minh được cách giới hạn email tới đúng người nhận đã xem trước.')
        return {'missing': missing, 'preview': {'recipients': addresses, 'subject': opts.get('subject'), 'body': opts.get('body'), 'attachments': files,
                'notice': 'Gửi nội dung và file đã chọn. Không tự sinh/gắn PDF hoặc thêm người nhận; kết quả gửi được theo dõi riêng.'}}
    if kind == 'validate':
        opts = options(payload, {'backorder'})
        missing = []
        if opts.get('backorder') not in ('create', 'cancel'):
            missing.append('options.backorder (create/cancel)')
        if row:
            service.button(replace(definition, method='button_validate'), row)
            picking = service.record(model, row['id'], ['move_line_ids', 'picking_type_id'])
            service.record('stock.picking.type', identity(picking['picking_type_id']), ['create_backorder'])
            lines = payload.get('lines')
            if not isinstance(lines, list) or not lines or len(lines) > 200:
                missing.append('lines (id dòng thực hiện, quantity, lot_id nếu cần)')
            else:
                seen = set()
                for item in lines:
                    if not isinstance(item, dict) or set(item) - {'id', 'move_id', 'quantity', 'lot_id'}:
                        raise BusinessError('Dòng thực hiện nhận id hoặc move_id mới, quantity, lot_id.')
                    if item.get('id'):
                        line = service.record('stock.move.line', item['id'], ['picking_id', 'product_id', 'lot_id', 'product_uom_id', 'location_id', 'location_dest_id', 'quantity', 'qty_done', 'picked'])
                        if identity(line.get('picking_id')) != row['id'] or line['id'] in seen:
                            raise BusinessError('Dòng thực hiện bị trùng hoặc thuộc phiếu khác.')
                        seen.add(line['id'])
                        service.rules('stock.move.line', [line['id']], 'write')
                    else:
                        service.access('stock.move.line', 'create')
                        move = service.record('stock.move', item.get('move_id'), ['picking_id', 'product_id', 'product_uom', 'location_id', 'location_dest_id', 'state'])
                        if identity(move['picking_id']) != row['id'] or move.get('state') in ('done', 'cancel'):
                            raise BusinessError('Dòng hàng mới không thuộc phiếu đang thực hiện.')
                        line = {'product_id': move['product_id']}
                    amount = number(item.get('quantity'))
                    product = service.record('product.product', identity(line['product_id']), ['tracking'])
                    lot_id = item.get('lot_id') or identity(line.get('lot_id'))
                    if product.get('tracking') in ('lot', 'serial') and amount:
                        if not lot_id:
                            raise BusinessError('Cần chọn lô/serial thật cho từng dòng thực hiện.')
                        lot = service.record('stock.lot', lot_id, ['product_id'])
                        if identity(lot['product_id']) != identity(line['product_id']):
                            raise BusinessError('Lô/serial không thuộc sản phẩm.')
                        if product.get('tracking') == 'serial' and amount != 1:
                            raise BusinessError('Mỗi dòng serial phải có số lượng 1 hoặc 0.')
                if seen != set(picking.get('move_line_ids', [])):
                    raise BusinessError('Cần chốt mọi dòng thực hiện, kể cả dòng số lượng 0; không tự giữ số lượng mặc định.')
                service.access('stock.move.line', 'write')
        return {'missing': missing, 'preview': {'execution_lines': payload.get('lines'), 'backorder': opts.get('backorder')}}
    if kind == 'return':
        opts = options(payload, {'location_id'})
        service.access('stock.return.picking', 'create')
        service.access('stock.return.picking.line', 'create')
        return_schema = service.schema('stock.return.picking')
        if not {'picking_id', 'product_return_moves'}.issubset(return_schema):
            raise Unsupported('Wizard trả hàng chưa hỗ trợ phiên bản này.')
        missing = []
        if not opts.get('location_id'):
            missing.append('options.location_id')
        else:
            service.record('stock.location', opts['location_id'])
        if row and 'location_id' not in return_schema:
            picking = service.record(model, row['id'], ['picking_type_id', 'location_id', 'location_dest_id'])
            picking_type = service.record('stock.picking.type', identity(picking['picking_type_id']), ['return_picking_type_id'])
            return_type_id = identity(picking_type.get('return_picking_type_id'))
            destination = identity(picking['location_id'])
            if return_type_id:
                return_type = service.record('stock.picking.type', return_type_id, ['code', 'default_location_dest_id'])
                if return_type.get('code') == 'incoming':
                    destination = identity(return_type['default_location_dest_id'])
            if opts.get('location_id') and opts['location_id'] != destination:
                raise Unsupported(f'Phiên bản này trả về vị trí ID {destination} theo cấu hình; không hỗ trợ đổi đích qua wizard.')
        lines = payload.get('lines')
        if not isinstance(lines, list) or not lines:
            missing.append('lines (move_id, quantity)')
        else:
            seen = set()
            for item in lines:
                if not isinstance(item, dict) or set(item) != {'move_id', 'quantity'}:
                    raise BusinessError('Dòng trả hàng cần move_id và quantity.')
                move = service.record('stock.move', item['move_id'], ['picking_id', 'product_id', 'quantity', 'product_uom_qty', 'state', 'returned_move_ids'])
                if not row or identity(move['picking_id']) != row['id'] or move.get('state') != 'done' or move['id'] in seen:
                    raise BusinessError('Dòng trả hàng không hợp lệ.')
                seen.add(move['id'])
                qty = number(item['quantity'])
                done = move.get('quantity', move.get('product_uom_qty', 0))
                returned = 0
                for return_id in move.get('returned_move_ids', []):
                    other = service.record('stock.move', return_id, ['quantity', 'product_uom_qty', 'state'])
                    if other.get('state') != 'cancel':
                        returned += other.get('quantity', other.get('product_uom_qty', 0))
                if qty <= 0 or qty > done - returned:
                    raise BusinessError('Số lượng trả vượt phần còn có thể trả.')
        return {'missing': missing, 'preview': {'return_lines': lines, 'destination': opts.get('location_id'), 'notice': 'Tạo phiếu trả hàng theo quy trình Odoo; chưa tự hoàn tất phiếu trả.'}}
    if kind == 'inventory':
        opts = options(payload, {'counted_quantity'})
        if 'inventory_quantity' not in service.schema('stock.quant'):
            raise Unsupported('Workflow kiểm kê của phiên bản này chưa hỗ trợ.')
        if 'counted_quantity' not in opts:
            return {'missing': ['options.counted_quantity']}
        number(opts['counted_quantity'])
        if row:
            quant = service.record('stock.quant', row['id'], ['quantity', 'reserved_quantity', 'inventory_quantity', 'inventory_quantity_set', 'product_id', 'location_id', 'lot_id', 'in_date'])
            location = service.record('stock.location', identity(quant['location_id']), ['usage'])
            if location.get('usage') not in ('internal', 'transit'):
                raise BusinessError('Chỉ kiểm kê tồn tại vị trí nội bộ/transit.')
            service.record('product.product', identity(quant['product_id']), ['tracking'])
            if quant.get('inventory_quantity_set'):
                raise BusinessError('Đã có số kiểm kê chưa áp dụng; cần xử lý trên Odoo trước.')
            return {'preview': {'product': quant['product_id'], 'location': quant['location_id'], 'lot': quant.get('lot_id'),
                    'before_quantity': quant['quantity'], 'counted_quantity': opts['counted_quantity'], 'difference': opts['counted_quantity'] - quant['quantity']}}
        return {}
    if kind == 'activity_done':
        opts = options(payload, {'feedback'})
        if 'feedback' in opts and (not isinstance(opts['feedback'], str) or len(opts['feedback']) > 12000):
            raise BusinessError('Nội dung hoàn tất không hợp lệ.')
        activity_target(service, payload, row) if row else None
        return {'preview': {'feedback': opts.get('feedback', '')}}
    if kind == 'note':
        opts = options(payload, {'body'})
        if not opts.get('body'):
            return {'missing': ['options.body']}
        if not isinstance(opts['body'], str) or len(opts['body']) > 12000:
            raise BusinessError('Nội dung ghi chú không hợp lệ.')
        return {'preview': {'body': opts['body'], 'notice': 'Ghi chú nội bộ; không gửi email cho khách hàng.'}}
    if kind == 'export':
        if any(name.startswith('x_') and any(token in name.lower() for token in ('approv', 'duyet', 'grant')) for name in service.schema(model)):
            raise Unsupported('Có phê duyệt tùy chỉnh trên chứng từ; chưa xác minh quyền xuất PDF qua API.')
        service.access('ir.actions.report', 'read')
        reports = service.ctx.call('ir.actions.report', 'search_read', [[('model', '=', model), ('report_name', '=', 'sale.report_saleorder'), ('report_type', '=', 'qweb-pdf')]], {'fields': ['id', 'name', 'report_name'], 'limit': 2})
        if len(reports) != 1:
            raise Unsupported('Chưa xác định được mẫu PDF bán hàng chuẩn.')
        service.record('ir.actions.report', reports[0]['id'], ['report_name', 'model', 'report_type'])
        return {'preview': {'document': reports[0]['name'], 'notice': 'Trả liên kết tải PDF trên Odoo; cần đăng nhập tài khoản Odoo khi mở.'}}
    raise Unsupported('Adapter nghiệp vụ chưa được hỗ trợ.')


def wizard_result(service, definition, row, result):
    if isinstance(result, dict) and result.get('type') == 'ir.actions.act_window' and result.get('res_model', '').startswith(('stock.', 'crm.')):
        return {'status': 'awaiting_input', 'action': definition.name, 'message': 'Odoo yêu cầu wizard bổ sung. Cần lựa chọn mới; không tự thực hiện mặc định.',
                'wizard': {'model': result.get('res_model'), 'id': result.get('res_id')}, 'record': service.readback(definition.model, row['id'])}
    return None


def execute(service, definition, payload, row, operation_id, checkpoint):
    if definition.kind == 'quotation_chatter':
        from app.business.quotation_chatter import execute as execute_chatter
        return execute_chatter(service, definition.name.rsplit('.', 1)[-1], payload, row, checkpoint)
    if definition.kind == 'quotation_approval':
        from app.business.quotation_approval import execute as execute_approval
        return execute_approval(service, definition, payload, row, operation_id, checkpoint)
    ctx, kind, model = service.ctx, definition.kind, definition.model
    opts = payload.get('options', {})
    result_id = row['id'] if row else None
    if kind == 'backorder':
        source = payload['_continuation']
        choice = opts.get('backorder') or source['choice']
        wizard_id = source.get('wizard_id')
        context = {'button_validate_picking_ids': [result_id], 'active_model': 'stock.picking', 'active_id': result_id, 'active_ids': [result_id]}
        if not wizard_id:
            wizard_id = ctx.call('stock.backorder.confirmation', 'create', [{'pick_ids': [(6, 0, [result_id])],
                'backorder_confirmation_line_ids': [(0, 0, {'picking_id': result_id, 'to_backorder': choice == 'create'})]}], {'context': context})
            checkpoint('backorder_wizard_created', {'model': 'stock.backorder.confirmation', 'id': wizard_id, 'picking_id': result_id})
        response = ctx.call('stock.backorder.confirmation', 'process' if choice == 'create' else 'process_cancel_backorder', [[wizard_id]], {'context': context})
        checkpoint('backorder_processed', {'model': model, 'id': result_id, 'response': response})
        pending = wizard_result(service, definition, row, response)
        if pending:
            return pending
    elif kind in ('stock_create', 'stock_update'):
        prepared = prepare(service, definition, payload, row)
        values = prepared['values']
        if kind == 'stock_create':
            result_id = ctx.call(model, 'create', [values])
        else:
            ctx.call(model, 'write', [[result_id], values])
        checkpoint('stock_saved', {'model': model, 'id': result_id})
    elif kind == 'lost':
        wizard = ctx.call('crm.lead.lost', 'create', [{'lead_ids': [(6, 0, [result_id])], 'lost_reason_id': payload['values']['lost_reason_id']}])
        checkpoint('lost_wizard_created', {'model': 'crm.lead.lost', 'id': wizard, 'lead_id': result_id})
        ctx.call('crm.lead.lost', 'action_lost_reason_apply', [[wizard]])
        checkpoint('lead_marked_lost', {'model': model, 'id': result_id})
    elif kind == 'convert':
        values = {'lead_id': result_id, 'name': 'convert', 'action': opts['partner_action']}
        values.update({f: opts[f] for f in ('partner_id', 'user_id', 'team_id') if f in opts})
        context = {'active_model': 'crm.lead', 'active_id': result_id, 'active_ids': [result_id]}
        wizard = ctx.call('crm.lead2opportunity.partner', 'create', [values], {'context': context})
        checkpoint('wizard_created', {'model': 'crm.lead2opportunity.partner', 'id': wizard})
        ctx.call('crm.lead2opportunity.partner', 'action_apply', [[wizard]], {'context': context})
        checkpoint('converted', {'model': model, 'id': result_id})
    elif kind == 'email':
        values = {'model': model, 'res_ids': repr([result_id]), 'composition_mode': 'mass_mail',
                  'partner_ids': [(6, 0, opts['recipient_ids'])], 'subject': opts['subject'],
                  'body': escape(opts['body']).replace('\n', '<br>'), 'attachment_ids': [(6, 0, opts.get('attachment_ids', []))]}
        rpc_kwargs = {}
        if definition.module == 'sales':
            # Use the actual document action, then a closed context and explicit
            # values: never inherit a template's recipients or attachments.
            action = ctx.call(model, 'action_quotation_send', [[result_id]])
            checkpoint('email_action_opened', {'model': model, 'id': result_id})
            if not isinstance(action, dict) or action.get('res_model') != 'mail.compose.message':
                raise Unsupported('Odoo yêu cầu wizard gửi khác; cần xử lý trên Odoo, chưa gửi email.')
            values.update(composition_mode='comment', template_id=False,
                          notify_skip_followers=True, notify_author=False, force_send=False)
            rpc_kwargs = {'context': {'mark_so_as_sent': True, 'force_email': True,
                                     'mail_post_autofollow': False, 'mail_notify_force_send': False}}
        wizard = ctx.call('mail.compose.message', 'create', [values], rpc_kwargs)
        checkpoint('email_prepared', {'model': 'mail.compose.message', 'id': wizard, 'target_id': result_id})
        if definition.module == 'sales':
            checked = ctx.call('mail.compose.message', 'read', [[wizard]], {'fields': list(values), **rpc_kwargs})
            if not checked or any(
                sorted(checked[0].get(field) or []) != sorted(opts.get(option, []))
                for field, option in (('partner_ids', 'recipient_ids'), ('attachment_ids', 'attachment_ids'))
            ) or any(checked[0].get(field) != values[field] for field in
                     ('model', 'res_ids', 'subject', 'body', 'composition_mode', 'notify_skip_followers', 'notify_author', 'force_send', 'template_id')):
                raise Unsupported('Người nhận, file hoặc cấu hình gửi khác bản xem trước. Chưa gửi email.')
            from app.business.quotation_approval import native, require_review
            native(service).require_approved(result_id)
            require_review(service, row)
        ctx.call('mail.compose.message', 'action_send_mail', [[wizard]], rpc_kwargs)
        checkpoint('email_submitted', {'model': model, 'id': result_id, 'wizard_id': wizard})
        record = service.readback(model, result_id)
        return {'status': 'check_required' if definition.module == 'sales' and record.get('state') != 'sent' else 'completed',
                'action': definition.name, 'record': record, 'delivery_status': 'submitted',
                'message': 'Odoo đã nhận yêu cầu gửi. Chưa xác minh email đã đến người nhận; không tự gửi lại.'}
    elif kind == 'validate':
        line_schema = service.schema('stock.move.line')
        qty_field = 'quantity' if 'quantity' in line_schema else 'qty_done' if 'qty_done' in line_schema else None
        if not qty_field:
            raise Unsupported('Chưa xác định trường số lượng thực hiện.')
        for item in payload['lines']:
            values = {qty_field: item['quantity']}
            if 'picked' in line_schema:
                values['picked'] = True
            if item.get('lot_id'):
                values['lot_id'] = item['lot_id']
            if item.get('id'):
                line_id = item['id']
                ctx.call('stock.move.line', 'write', [[line_id], values])
            else:
                move = service.record('stock.move', item['move_id'], ['product_id', 'product_uom', 'location_id', 'location_dest_id'])
                values.update({'picking_id': result_id, 'move_id': item['move_id'], 'product_id': identity(move['product_id']),
                    'product_uom_id': identity(move['product_uom']), 'location_id': identity(move['location_id']),
                    'location_dest_id': identity(move['location_dest_id']), 'company_id': service.company_id})
                line_id = ctx.call('stock.move.line', 'create', [values])
            checkpoint('execution_line_saved', {'model': 'stock.move.line', 'id': line_id, 'picking_id': result_id})
        response = ctx.call(model, 'button_validate', [[result_id]])
        checkpoint('validation_returned', {'model': model, 'id': result_id, 'response': response})
        pending = wizard_result(service, definition, row, response)
        if pending:
            # A backorder choice was explicitly previewed. Never select an unknown wizard.
            if response.get('res_model') != 'stock.backorder.confirmation':
                return pending
            return dict(pending, continuation={'kind': 'backorder', 'picking_id': result_id,
                        'wizard_id': response.get('res_id'), 'choice': opts['backorder']},
                        message='Số lượng thực hiện đã lưu. Odoo yêu cầu xác nhận bước xử lý phần còn lại; cần bản xác nhận mới.')
    elif kind == 'return':
        values = {'picking_id': result_id, 'product_return_moves': [(0, 0, {
            'move_id': item['move_id'], 'product_id': identity(service.record('stock.move', item['move_id'], ['product_id'])['product_id']), 'quantity': item['quantity']}) for item in payload['lines']]}
        if 'location_id' in service.schema('stock.return.picking'):
            values['location_id'] = opts['location_id']
        context = {'active_model': model, 'active_id': result_id, 'active_ids': [result_id]}
        wizard = ctx.call('stock.return.picking', 'create', [values], {'context': context})
        checkpoint('return_wizard_created', {'model': 'stock.return.picking', 'id': wizard, 'picking_id': result_id})
        response = ctx.call('stock.return.picking', 'action_create_returns', [[wizard]], {'context': context})
        if not isinstance(response, dict) or response.get('res_model') != 'stock.picking' or not response.get('res_id'):
            raise BusinessError('Chưa xác minh được phiếu trả hàng đã tạo; không tự tạo lại.')
        result_id = response['res_id']
        checkpoint('return_created', {'model': 'stock.picking', 'id': result_id})
    elif kind == 'inventory':
        context = {'inventory_mode': True}
        ctx.call(model, 'write', [[result_id], {'inventory_quantity': opts['counted_quantity']}], {'context': context})
        checkpoint('inventory_count_saved', {'model': model, 'id': result_id})
        response = ctx.call(model, 'action_apply_inventory', [[result_id]], {'context': context})
        checkpoint('inventory_apply_returned', {'model': model, 'id': result_id, 'response': response})
        pending = wizard_result(service, definition, row, response)
        if pending:
            return pending
    elif kind == 'activity_done':
        ctx.call(model, 'action_feedback', [[result_id]], {'feedback': opts.get('feedback', '')})
        checkpoint('activity_completed', {'model': model, 'id': result_id})
        remaining = ctx.call(model, 'search_count', [[('id', '=', result_id)]])
        return {'status': 'completed', 'action': definition.name, 'activity_id': result_id,
                'message': 'Đã hoàn tất hoạt động.' if not remaining else 'Odoo đã xử lý hoạt động; cần kiểm tra trạng thái còn lại.'}
    elif kind == 'note':
        model = LOOKUPS[payload['target_kind']]
        result_id = payload['target_id']
        message_id = ctx.call(model, 'message_post', [[result_id]], {'body': escape(opts['body']), 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_note', 'partner_ids': [], 'context': {'mail_notify_force_send': False}})
        checkpoint('note_posted', {'model': 'mail.message', 'id': message_id})
        message = service.record('mail.message', message_id, ['model', 'res_id', 'body'])
        return {'status': 'completed', 'action': definition.name, 'record': service.readback(model, result_id), 'message_id': message['id'], 'message': 'Đã thêm ghi chú nội bộ.'}
    elif kind == 'export':
        return {'status': 'completed', 'action': definition.name, 'record': service.readback(model, result_id),
                'download_url': f'{ctx.service.client.url}/report/pdf/sale.report_saleorder/{result_id}',
                'message': 'Mở liên kết bằng phiên Odoo đã đăng nhập để tải PDF chính thức.'}
    else:
        raise Unsupported('Adapter chưa có bước thực thi.')
    record = service.readback(model, result_id)
    if kind == 'convert' and record.get('type') != 'opportunity':
        return {'status': 'check_required', 'action': definition.name, 'record': record, 'message': 'Chưa xác minh lead đã chuyển thành cơ hội; kiểm tra Odoo, không tự gọi lại.'}
    if kind == 'lost' and (record.get('active') or record.get('probability') != 0):
        return {'status': 'check_required', 'action': definition.name, 'record': record, 'message': 'Trạng thái thua chưa được xác minh; kiểm tra Odoo, không tự gọi lại.'}
    if kind in ('validate', 'backorder') and record.get('state') != 'done':
        return {'status': 'check_required', 'action': definition.name, 'record': record, 'message': 'Đã có bước được xử lý nhưng phiếu chưa hoàn tất; kiểm tra Odoo, không tự gọi lại.'}
    if kind == 'inventory' and record.get('quantity') != opts['counted_quantity']:
        return {'status': 'check_required', 'action': definition.name, 'record': record, 'message': 'Số tồn đọc lại khác số kiểm kê được xác nhận; cần kiểm tra, không tự áp dụng lại.'}
    return {'status': 'completed', 'action': definition.name, 'record': record, 'message': 'Odoo đã xử lý; dữ liệu được đọc lại.'}
