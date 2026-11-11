"""Deterministic authorization, read-only preparation and narrow Odoo execution."""
import ast
import copy
import math
import re
from datetime import UTC, date, datetime
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from app.business.catalog import ACTIONS, LOOKUPS, TARGET_MODELS
from app.odoo.client import OdooAccessError


class BusinessError(ValueError):
    pass


class Unsupported(BusinessError):
    pass


class Conflict(BusinessError):
    pass


def identity(value):
    return value[0] if isinstance(value, list | tuple) and value else value


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise BusinessError('ID phải là số nguyên dương được tra cứu từ Odoo.')
    return value


def number(value, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise BusinessError('Số lượng/giá trị phải hữu hạn và không âm.')
    return value


def enabled(settings, definition):
    from app.business.quotation_approval import FLOW_ACTIONS
    if definition.name in FLOW_ACTIONS and getattr(settings, 'ENABLE_QUOTATION_APPROVAL_FLOW', False):
        return True
    if not getattr(settings, 'ENABLE_BUSINESS_ACTIONS', False):
        return False
    if not getattr(settings, f'ENABLE_BUSINESS_{definition.module.upper()}', False):
        return False
    if definition.kind == 'delete' and not getattr(settings, 'ENABLE_BUSINESS_DELETE', False):
        return False
    if definition.kind == 'email' and not getattr(settings, 'ENABLE_BUSINESS_EMAIL', False):
        return False
    return True


def condition(expression, values):
    """Evaluate only the small expression language used for button visibility."""
    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in values:
            return identity(values[node.id])
        if isinstance(node, ast.List | ast.Tuple):
            return [evaluate(item) for item in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not evaluate(node.operand)
        if isinstance(node, ast.BoolOp):
            return (all if isinstance(node.op, ast.And) else any)(evaluate(item) for item in node.values)
        if isinstance(node, ast.Compare):
            left = evaluate(node.left)
            for op, right_node in zip(node.ops, node.comparators, strict=False):
                right = evaluate(right_node)
                if isinstance(op, ast.Eq):
                    result = left == right
                elif isinstance(op, ast.NotEq):
                    result = left != right
                elif isinstance(op, ast.In):
                    result = left in right
                elif isinstance(op, ast.NotIn):
                    result = left not in right
                elif isinstance(op, ast.Gt):
                    result = left > right
                elif isinstance(op, ast.Lt):
                    result = left < right
                elif isinstance(op, ast.GtE):
                    result = left >= right
                elif isinstance(op, ast.LtE):
                    result = left <= right
                else:
                    raise Unsupported('Điều kiện nút nghiệp vụ chưa hỗ trợ.')
                if not result:
                    return False
                left = right
            return True
        raise Unsupported('Điều kiện nút nghiệp vụ chưa hỗ trợ; hãy thao tác trên Odoo.')
    return bool(evaluate(ast.parse(expression or 'False', mode='eval').body))


class BusinessService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.snapshot = {}
        self.company_id = ctx.default_company_id
        self._permissions = set()
        self._groups = {}

    def schema(self, model):
        return self.ctx.read_fields(model)

    def access(self, model, operation):
        if (model, operation) in self._permissions:
            return
        if not self.ctx.call(model, 'check_access_rights', [operation, False]):
            raise OdooAccessError(f'Không có quyền {operation} trên {model}.')
        self._permissions.add((model, operation))

    def group(self, external_id):
        if external_id not in self._groups:
            self._groups[external_id] = bool(self.ctx.call('res.users', 'has_group', [external_id]))
        return self._groups[external_id]

    def record(self, model, record_id, fields=None, track=True):
        record_id = positive_id(record_id)
        if model == 'res.company' and record_id != self.company_id:
            raise OdooAccessError('Công ty tham chiếu phải trùng công ty đang thao tác.')
        self.access(model, 'read')
        metadata = self.schema(model)
        base = ['id', 'display_name', 'name', 'company_id', 'write_date', 'active', 'state']
        selected = list(dict.fromkeys(f for f in base + list(fields or []) if f == 'id' or f in metadata))
        domain = [('id', '=', record_id)]
        if 'company_id' in metadata:
            domain += ['|', ('company_id', '=', False), ('company_id', '=', self.company_id)]
        rows = self.ctx.call(model, 'search_read', [domain], {'fields': selected, 'limit': 1, 'context': {'active_test': False}})
        if len(rows) != 1:
            raise OdooAccessError(f'Không tìm thấy hoặc không có quyền đọc {model} #{record_id}.')
        row = rows[0]
        if track:
            key = (model, record_id)
            prior = self.snapshot.get(key)
            if prior and prior['values'].get('write_date') != row.get('write_date'):
                raise Conflict('Bản ghi thay đổi trong lúc chuẩn bị; hãy lấy lại bản xem trước.')
            values = dict(prior['values']) if prior else {}
            values.update(row)
            self.snapshot[key] = {'model': model, 'id': record_id, 'values': values}
        return row

    def rules(self, model, ids, operation):
        """Use the public ORM access check; no assumption from a readable row."""
        if ids:
            if not self.ctx.call(model, 'has_access', [ids, operation]):
                raise OdooAccessError(f'Không có quyền {operation} trên các bản ghi đã chọn.')

    def lookup(self, kind, query='', company_id=None):
        if kind not in LOOKUPS:
            raise Unsupported('Loại tra cứu chưa hỗ trợ.')
        self.company_id = self.ctx.selected_companies([positive_id(company_id)] if company_id else None)[0]
        model = LOOKUPS[kind]
        self.access(model, 'read')
        meta = self.schema(model)
        fields = [f for f in ('id', 'display_name', 'name', 'default_code', 'state', 'company_id', 'product_id', 'location_id', 'lot_id', 'quantity', 'reserved_quantity', 'product_uom_qty', 'partner_id', 'type') if f == 'id' or f in meta]
        domain = []
        if 'company_id' in meta:
            domain += ['|', ('company_id', '=', False), ('company_id', '=', self.company_id)]
        if kind == 'models':
            domain += [('model', 'in', sorted(TARGET_MODELS))]
        if kind == 'attachments':
            domain += [('res_model', 'in', sorted(TARGET_MODELS))]
        if query:
            domain += [('display_name', 'ilike', query)]
        items = self.ctx.call(model, 'search_read', [domain], {'fields': fields, 'limit': 21, 'context': {'active_test': False}})
        return {'status': 'ok', 'kind': kind, 'items': items[:20], 'has_more': len(items) > 20}

    def capabilities(self, settings):
        permissions, result = {}, []
        for definition in ACTIONS.values():
            available = enabled(settings, definition)
            reason = '' if available else 'Nhóm thao tác chưa được bật để nghiệm thu.'
            if available:
                try:
                    model = definition.model
                    if model != 'mail.thread':
                        for op in ('read', self.operation(definition)):
                            key = (model, op)
                            if key not in permissions:
                                permissions[key] = bool(self.ctx.call(model, 'check_access_rights', [op, False]))
                            if not permissions[key]:
                                raise OdooAccessError(f'Thiếu quyền {op} {model}.')
                    if definition.group and not self.group(definition.group):
                        raise OdooAccessError('Thiếu nhóm quyền nghiệp vụ.')
                except Exception as exc:
                    available, reason = False, str(exc)
            result.append({'action': definition.name, 'title': definition.title,
                           'status': 'conditional' if available else 'unavailable', 'reason': reason,
                           'requires_confirmation': True})
        return {'status': 'ok', 'actions': result, 'notice': 'Khả năng phụ thuộc bản ghi, trường, trạng thái và quy trình duyệt; kiểm tra lại khi chuẩn bị/thực thi.'}

    @staticmethod
    def operation(definition):
        return 'create' if definition.kind in ('create', 'stock_create', 'copy', 'note') else 'unlink' if definition.kind == 'delete' else 'read' if definition.kind == 'export' else 'write'

    def values(self, model, incoming, allowed, creating=False):
        if not isinstance(incoming, dict):
            raise BusinessError('values phải là object.')
        meta = self.schema(model)
        allowed = set(allowed)
        output = {}
        for name, value in incoming.items():
            from app.quotations.approval import approval_control
            if approval_control(name):
                raise Unsupported('Trường duyệt chỉ được xử lý bằng thao tác duyệt riêng, không ghi trực tiếp.')
            spec = meta.get(name)
            if name not in allowed and not (name.startswith('x_') and name not in {'x_studio_approval', 'x_studio_approved'}):
                raise Unsupported(f'Trường chưa hỗ trợ: {name}.')
            if not spec or spec.get('readonly') or name in {'state', 'company_id', 'qty_available', 'quantity', 'reserved_quantity'}:
                raise Unsupported(f'Không được ghi trường {name}.')
            if name.startswith('x_') and re.search('approv|duyet|grant', name, re.I):
                raise Unsupported('Trường phê duyệt tùy chỉnh phải qua quy trình Odoo.')
            kind = spec['type']
            if value is False and not spec.get('required'):
                output[name] = False
                continue
            if kind in ('char', 'text', 'html'):
                if not isinstance(value, str) or len(value) > 12000:
                    raise BusinessError(f'{name} phải là văn bản tối đa 12000 ký tự.')
                if kind == 'html':
                    from html import escape
                    value = escape(value).replace('\n', '<br>')
            elif kind == 'boolean':
                if type(value) is not bool:
                    raise BusinessError(f'{name} phải là boolean.')
            elif kind in ('integer', 'float', 'monetary'):
                number(value)
                if kind == 'integer' and type(value) is not int:
                    raise BusinessError(f'{name} phải là số nguyên.')
            elif kind in ('date', 'datetime'):
                try:
                    if not isinstance(value, str):
                        raise ValueError('date must be text')
                    if kind == 'date':
                        value = date.fromisoformat(value).isoformat()
                    else:
                        parsed = datetime.fromisoformat(value)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=ZoneInfo(getattr(self.ctx.user, 'tz', None) or 'Asia/Bangkok'))
                        value = parsed.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError) as exc:
                    raise BusinessError(f'{name} cần ngày/giờ ISO hợp lệ.') from exc
            elif kind == 'selection':
                if value not in {item[0] for item in spec.get('selection', [])}:
                    raise BusinessError(f'{name} không thuộc lựa chọn Odoo.')
            elif kind == 'many2one':
                self.record(spec['relation'], positive_id(value))
            elif model == 'mail.activity' and name == 'res_id' and kind == 'many2one_reference':
                positive_id(value)
            elif model == 'product.product' and name == 'product_template_attribute_value_ids' and kind == 'many2many':
                if not isinstance(value, list) or len(value) > 30 or any(type(item) is not int for item in value) or len(set(value)) != len(value):
                    raise BusinessError('Chọn tối đa 30 giá trị biến thể bằng ID, không dùng command ORM.')
                for item in value:
                    self.record('product.template.attribute.value', positive_id(item), ['product_tmpl_id', 'attribute_id'])
                value = [(6, 0, value)]
            else:
                raise Unsupported(f'Kiểu trường chưa hỗ trợ: {name} ({kind}).')
            output[name] = value
        if creating:
            defaults = self.ctx.call(model, 'default_get', [list(meta)])
            # Freeze editable defaults in the prepared values instead of resolving them again on create.
            for name in allowed - set(output):
                if model == 'product.product' and output.get('product_tmpl_id') and name not in {'product_tmpl_id', 'product_template_attribute_value_ids', 'default_code', 'barcode'}:
                    continue
                if name in meta and not meta[name].get('readonly') and defaults.get(name) not in (None, False, ''):
                    default = defaults[name]
                    if meta[name]['type'] == 'datetime' and isinstance(default, str) and datetime.fromisoformat(default).tzinfo is None:
                        default += '+00:00'  # Odoo default_get returns UTC; user-entered naive times are local.
                    normalized, _ = self.values(model, {name: default}, allowed)
                    output.update(normalized)
            missing = [name for name, spec in meta.items() if spec.get('required') and not spec.get('readonly')
                       and name not in output and not defaults.get(name) and name not in {'company_id', 'order_line', 'move_ids'}]
            if model == 'product.product' and output.get('product_tmpl_id'):
                missing = [name for name in missing if name != 'name']
            if model == 'product.product':
                # Delegated product creation creates the template and inverse variant relation.
                missing = [name for name in missing if name not in {'product_tmpl_id', 'product_variant_ids'}]
            if missing:
                return output, missing
        return output, []

    def button(self, definition, record, action_model=None):
        """Honor visible object-button guards; unknown Studio approvals fail closed."""
        meta = self.schema(definition.model)
        verified_studio = False
        if definition.model == 'sale.order' and definition.method in ('action_confirm', 'action_quotation_send'):
            from app.business.quotation_approval import native, require_review, track_rules
            state = native(self).require_approved(record['id'])
            require_review(self, record)
            track_rules(self, state)
            verified_studio = True
        controls = [name for name in meta if name.startswith('x_') and re.search('approv|duyet|grant', name, re.I)]
        if any(name != 'x_studio_a_phe_duyet' or not verified_studio for name in controls):
            raise Unsupported('Chứng từ có phê duyệt tùy chỉnh chưa xác minh; hãy thực hiện bước này trên Odoo.')
        view = self.ctx.call(definition.model, 'get_view', [], {'view_type': 'form'})
        if any(re.search('approv|studio_approval', key, re.I) and value for key, value in view.items() if key != 'arch'):
            raise Unsupported('Nút có quy tắc phê duyệt tùy chỉnh chưa hỗ trợ.')
        root = ET.fromstring(view.get('arch', ''))
        candidates = [node for node in root.iter('button') if node.get('type') == 'object' and node.get('name') == definition.method] if not action_model else []
        if action_model:
            for node in root.iter('button'):
                if node.get('type') != 'action' or not str(node.get('name', '')).isdigit():
                    continue
                actions = self.ctx.call('ir.actions.act_window', 'search_read', [[('id', '=', int(node.get('name'))), ('res_model', '=', action_model)]], {'fields': ['id'], 'limit': 1})
                if actions:
                    candidates.append(node)
        if not candidates:
            raise Unsupported('Chưa xác minh được nút nghiệp vụ trên form của tài khoản.')
        parents = {child: parent for parent in root.iter() for child in parent}
        for button in candidates:
            # Include ancestor guards: a hidden/group-restricted container also hides its button.
            ancestor = parents.get(button)
            ancestor_blocked = False
            while ancestor is not None:
                if ancestor.get('attrs'):
                    ancestor_blocked = True
                    break
                groups = [g for g in ancestor.get('groups', '').split(',') if g]
                positives = [g for g in groups if not g.startswith('!')]
                if any(self.group(g[1:]) for g in groups if g.startswith('!')) or (positives and not any(self.group(g) for g in positives)):
                    ancestor_blocked = True
                    break
                expression = ancestor.get('invisible', 'False')
                names = {node.id for node in ast.walk(ast.parse(expression, mode='eval')) if isinstance(node, ast.Name)}
                if names - set(meta):
                    ancestor_blocked = True
                    break
                values = self.record(definition.model, record['id'], list(names)) if names else {}
                try:
                    if condition(expression, values):
                        ancestor_blocked = True
                        break
                except (BusinessError, TypeError):
                    ancestor_blocked = True
                    break
                ancestor = parents.get(ancestor)
            if ancestor_blocked:
                continue
            if any('approval' in key.lower() and value for key, value in button.attrib.items()):
                continue
            groups = [g for g in button.get('groups', '').split(',') if g]
            if any(self.group(g[1:]) for g in groups if g.startswith('!')):
                continue
            positives = [g for g in groups if not g.startswith('!')]
            if positives and not any(self.group(g) for g in positives):
                continue
            expression = button.get('invisible', 'False')
            names = {node.id for node in ast.walk(ast.parse(expression, mode='eval')) if isinstance(node, ast.Name)}
            if names - set(meta) - {'True', 'False', 'None'}:
                continue
            values = self.record(definition.model, record['id'], list(names))
            try:
                if not condition(expression, values):
                    if button.get('attrs'):
                        continue
                    return
            except (BusinessError, TypeError):
                continue
        raise OdooAccessError('Nút nghiệp vụ đang bị chặn hoặc điều kiện chưa được hỗ trợ.')

    def track_children(self, model, row):
        relations = {'sale.order': ('order_line', 'sale.order.line'), 'purchase.order': ('order_line', 'purchase.order.line'),
                     'stock.picking': ('move_ids', 'stock.move')}
        if model in relations:
            field, child = relations[model]
            parent = self.record(model, row['id'], [field])
            ids = parent.get(field, [])
            if len(ids) > 200:
                raise Unsupported('Chứng từ quá 200 dòng; chưa hỗ trợ thao tác qua agent.')
            child_meta = self.schema(child)
            fields = [f for f in ('product_id', 'name', 'product_uom_qty', 'product_qty', 'price_unit', 'tax_id', 'tax_ids', 'taxes_id', 'discount', 'customer_lead', 'date_planned', 'product_uom', 'product_uom_id', 'quantity', 'picked', 'move_line_ids') if f in child_meta]
            for record_id in ids:
                line = self.record(child, record_id, fields)
                for move_line in line.get('move_line_ids', []):
                    self.record('stock.move.line', move_line, ['product_id', 'quantity', 'qty_done', 'lot_id', 'location_id', 'location_dest_id', 'picked', 'package_id', 'result_package_id', 'owner_id'])

    def order_lines(self, definition, lines, parent=None):
        if not isinstance(lines, list) or not lines or len(lines) > 100:
            raise BusinessError('lines cần từ 1 đến 100 dòng.')
        sale = definition.module == 'sales'
        model = 'sale.order.line' if sale else 'purchase.order.line'
        self.access(model, 'create')
        meta = self.schema(model)
        qty = 'product_uom_qty' if sale else 'product_qty'
        uom = 'product_uom_id' if 'product_uom_id' in meta else 'product_uom'
        taxes = next((name for name in ('tax_ids', 'tax_id' if sale else 'taxes_id') if name in meta), None)
        if not taxes:
            raise Unsupported('Không xác minh được trường thuế của dòng hàng trên phiên bản Odoo hiện tại.')
        commands, preview = [], []
        for item in lines:
            if not isinstance(item, dict) or set(item) - {'id', 'remove', 'product_id', 'name', 'quantity', 'price_unit', 'price_confirmed', 'tax_ids', 'uom_id', 'date_planned', 'discount', 'customer_lead'}:
                raise BusinessError('Dòng hàng chứa trường chưa hỗ trợ.')
            line_id = item.get('id')
            if line_id:
                existing = self.record(model, line_id, ['order_id', 'product_id'])
                if not parent or identity(existing['order_id']) != parent['id']:
                    raise OdooAccessError('Dòng hàng không thuộc chứng từ.')
                self.access(model, 'unlink' if item.get('remove') else 'write')
                self.rules(model, [line_id], 'unlink' if item.get('remove') else 'write')
            if item.get('remove'):
                if not line_id:
                    raise BusinessError('Cần ID dòng cần xóa.')
                commands.append((2, line_id, 0))
                preview.append({'remove_line_id': line_id})
                continue
            if item.get('price_confirmed') is not True:
                raise BusinessError('Cần chốt đơn giá price_confirmed=true cho từng dòng.')
            product = self.record('product.product', item.get('product_id'), ['uom_id', 'sale_ok', 'purchase_ok', 'active'])
            if not product.get('active', True) or product.get('sale_ok' if sale else 'purchase_ok') is False:
                raise BusinessError('Sản phẩm không sử dụng được cho nghiệp vụ này.')
            chosen_uom = item.get('uom_id') or identity(product.get('uom_id'))
            self.record('uom.uom', chosen_uom)
            if not isinstance(item.get('tax_ids'), list):
                raise BusinessError('Cần chọn tax_ids; [] là xác nhận không áp thuế.')
            for tax in item['tax_ids']:
                tax_row = self.record('account.tax', tax, ['type_tax_use', 'active'])
                if tax_row.get('type_tax_use') != ('sale' if sale else 'purchase') or tax_row.get('active') is False:
                    raise BusinessError('Thuế không phù hợp nghiệp vụ.')
            values = {'product_id': product['id'], 'name': item.get('name') or product.get('display_name') or product.get('name'),
                      qty: number(item.get('quantity')), 'price_unit': number(item.get('price_unit')),
                      uom: chosen_uom, taxes: [(6, 0, item['tax_ids'])]}
            if sale and 'customer_lead' in meta:
                if item.get('customer_lead') is not None:
                    if type(item['customer_lead']) not in (int, float):
                        raise BusinessError('Thời gian giao hàng customer_lead cần số ngày, không phải ngày lịch.')
                    values['customer_lead'] = number(item['customer_lead'])
            if item.get('discount') is not None:
                if 'discount' not in meta or number(item['discount']) > 100:
                    raise BusinessError('Chiết khấu không phù hợp.')
                values['discount'] = item['discount']
            if not sale:
                if not item.get('date_planned'):
                    raise BusinessError('Cần ngày nhận dự kiến date_planned cho dòng mua.')
                planned, _ = self.values(model, {'date_planned': item['date_planned']}, {'date_planned'})
                values.update(planned)
            commands.append((1, line_id, values) if line_id else (0, 0, values))
            preview.append(dict(item, product=product.get('display_name')))
        return commands, preview

    def prepare(self, action, payload):
        if action not in ACTIONS:
            raise Unsupported('Thao tác không có trong catalog.')
        definition = ACTIONS[action]
        if not isinstance(payload, dict) or set(payload) - {'company_id', 'target_id', 'values', 'lines', 'options', 'target_kind', '_continuation'}:
            raise BusinessError('Payload chỉ nhận company_id, target_id, values, lines, options, target_kind.')
        payload = copy.deepcopy(payload)
        for container in ('values', 'options'):
            if container in payload and not isinstance(payload[container], dict):
                raise BusinessError(f'{container} phải là object.')
        if definition.kind not in ('create', 'update', 'stock_create', 'stock_update', 'method', 'lost') and payload.get('values'):
            raise BusinessError('Thao tác này không nhận thay đổi trường kèm theo.')
        if definition.kind not in ('create', 'update', 'stock_create', 'stock_update', 'validate', 'return') and payload.get('lines'):
            raise BusinessError('Thao tác này không nhận dòng hàng kèm theo.')
        self.company_id = self.ctx.selected_companies([positive_id(payload['company_id'])] if payload.get('company_id') else None)[0]
        payload['company_id'] = self.company_id
        if '_continuation' in payload and definition.kind != 'backorder':
            raise Unsupported('Bước tiếp theo không phù hợp thao tác.')
        model = definition.model
        if model == 'mail.thread':
            model = LOOKUPS.get(payload.get('target_kind'))
            if model not in TARGET_MODELS:
                raise BusinessError('Ghi chú cần target_kind thuộc năm phân hệ.')
        self.access(model, 'read')
        self.access(model, self.operation(definition) if definition.kind != 'note' else 'write')
        if definition.group and not self.group(definition.group):
            raise OdooAccessError('Không có nhóm quyền nghiệp vụ yêu cầu.')
        row = None
        missing = []
        if definition.kind not in ('create', 'stock_create') or definition.module == 'notes':
            if not payload.get('target_id'):
                missing.append('target_id')
            else:
                row = self.record(model, payload['target_id'], list(definition.fields))
                self.rules(model, [row['id']], self.operation(definition) if definition.kind != 'note' else 'write')
                if definition.states and row.get('state') not in definition.states:
                    raise BusinessError(f"Trạng thái {row.get('state')} không cho phép {definition.title}.")
                self.track_children(model, row)
        values, field_missing = {}, []
        preview = {'title': definition.title, 'company_id': self.company_id, 'target': row, 'changes': [], 'notice': 'Chỉ thực hiện sau xác nhận. Tổng/thuế chính thức do Odoo tính.'}
        if definition.kind in ('create', 'update'):
            if model == 'crm.lead' and definition.kind == 'update' and 'type' in payload.get('values', {}):
                raise Unsupported('Chuyển lead thành cơ hội phải qua wizard chuyển đổi, không sửa type trực tiếp.')
            values, field_missing = self.values(model, payload.get('values', {}), definition.fields, definition.kind == 'create')
            missing.extend(field_missing)
            if definition.module == 'products':
                if definition.kind == 'update' and set(values) & {'product_tmpl_id', 'product_template_attribute_value_ids'}:
                    raise Unsupported('Không thay cấu trúc biến thể của sản phẩm hiện có.')
                if values.get('product_tmpl_id'):
                    shared_fields = set(values) - {'product_tmpl_id', 'product_template_attribute_value_ids', 'default_code', 'barcode'}
                    if shared_fields:
                        raise Unsupported('Tạo biến thể của mẫu hiện có chỉ nhận mẫu, giá trị biến thể, mã và barcode; không sửa dữ liệu dùng chung trong cùng thao tác.')
                    for item in (payload.get('values') or {}).get('product_template_attribute_value_ids', []):
                        attribute = self.record('product.template.attribute.value', item, ['product_tmpl_id'])
                        if identity(attribute.get('product_tmpl_id')) != values['product_tmpl_id']:
                            raise BusinessError('Giá trị biến thể không thuộc mẫu sản phẩm đã chọn.')
            if definition.kind == 'update' and not values and 'lines' not in payload:
                missing.append('values')
            if definition.module in ('sales', 'purchases'):
                if definition.kind == 'create' and 'partner_id' not in values:
                    missing.append('values.partner_id')
                if definition.kind == 'create' and not payload.get('lines'):
                    missing.append('lines')
                if payload.get('lines'):
                    commands, line_preview = self.order_lines(definition, payload['lines'], row)
                    values['order_line'] = commands
                    preview['lines'] = line_preview
                if row and set(values) & {'partner_id', 'pricelist_id', 'currency_id', 'fiscal_position_id'} and 'order_line' not in values:
                    raise BusinessError('Đổi đối tác/bảng giá/tiền tệ cần chốt lại toàn bộ dòng hàng, giá và thuế.')
            # Preserve user text (HTML is escaped at the write boundary), freeze scalar defaults.
            incoming = payload.get('values', {})
            payload['values'] = {key: incoming[key] if key in incoming else value for key, value in values.items() if key != 'order_line'}
            for key, value in values.items():
                if self.schema(model).get(key, {}).get('type') == 'datetime':
                    payload['values'][key] = value + '+00:00'
            if definition.kind == 'create' and 'company_id' in self.schema(model) and not self.schema(model)['company_id'].get('readonly'):
                values['company_id'] = self.company_id
            if row:
                row = self.record(model, row['id'], list(values))
                if action == 'business.crm.stage':
                    lead = self.record(model, row['id'], ['type', 'active', 'won_status', 'team_id'])
                    if lead.get('type') != 'opportunity' or not lead.get('active') or lead.get('won_status') == 'lost':
                        raise BusinessError('Chỉ chuyển giai đoạn cơ hội đang hoạt động; cần mở lại cơ hội thua trước.')
                    stage_id = values.get('stage_id')
                    if not stage_id:
                        missing.append('values.stage_id')
                    else:
                        stage_meta = self.schema('crm.stage')
                        stage = self.record('crm.stage', stage_id, ['team_id', 'team_ids'])
                        teams = stage.get('team_ids', []) if 'team_ids' in stage_meta else [identity(stage['team_id'])] if stage.get('team_id') else []
                        if teams and identity(lead.get('team_id')) not in teams:
                            raise BusinessError('Giai đoạn không thuộc nhóm bán hàng của cơ hội.')
                if model == 'product.product' and set(values) & {'name', 'list_price', 'description_sale', 'description_purchase', 'sale_ok', 'purchase_ok', 'categ_id'}:
                    product = self.record(model, row['id'], ['product_tmpl_id'])
                    template = self.record('product.template', identity(product['product_tmpl_id']), ['product_variant_ids'])
                    self.access('product.template', 'write')
                    self.rules('product.template', [template['id']], 'write')
                    preview['notice'] += ' Các trường dùng chung sẽ áp dụng cho mọi biến thể của cùng mẫu sản phẩm.'
                    preview['affected_variant_ids'] = template.get('product_variant_ids', [])
            preview['changes'] = [{'field': name, 'label': self.schema(model).get(name, {}).get('string', name), 'before': row.get(name) if row else None, 'after': value} for name, value in values.items() if name != 'order_line']
        elif definition.kind in ('archive', 'restore'):
            if 'active' not in self.schema(model):
                raise Unsupported('Model không hỗ trợ lưu trữ.')
            values = {'active': definition.kind == 'restore'}
            preview['changes'] = [{'field': 'active', 'before': row.get('active') if row else None, 'after': values['active']}]
        elif definition.kind == 'method' and row:
            if action == 'business.sales.confirm':
                opts = payload.get('options', {})
                if set(opts) - {'customer_accepted'}:
                    raise BusinessError('Chỉ nhận xác nhận khách đã chấp thuận cho bước chuyển thành đơn bán.')
                if row.get('state') != 'sent':
                    raise BusinessError('Chỉ chuyển thành đơn bán sau khi đã gửi báo giá và khách chấp thuận.')
                if opts.get('customer_accepted') is not True:
                    missing.append('options.customer_accepted')
                preview['notice'] = 'Chuyển báo giá đã gửi thành đơn bán. Chỉ xác nhận nếu khách đã chấp thuận; duyệt nội bộ không đồng nghĩa khách chấp thuận.'
            self.button(definition, row)
            if action.endswith(('.cancel', '.reject')) and definition.module in ('sales', 'purchases', 'stock'):
                self.cancellation_dependencies(definition, row)
            if action.endswith('.lost'):
                reason_id = payload.get('values', {}).get('lost_reason_id')
                if not reason_id:
                    missing.append('values.lost_reason_id')
                else:
                    reason = self.record('crm.lost.reason', reason_id)
                    preview['lost_reason'] = reason.get('display_name')
            elif payload.get('values') or payload.get('lines'):
                raise BusinessError('Hành động trạng thái không nhận thay đổi trường kèm theo.')
        elif definition.kind not in ('delete', 'copy'):
            from app.business.adapters import prepare
            extra = prepare(self, definition, payload, row)
            missing.extend(extra.get('missing', []))
            preview.update(extra.get('preview', {}))
            values.update(extra.get('values', {}))
            if definition.kind in ('stock_create', 'stock_update'):
                incoming = payload.get('values', {})
                payload['values'] = {key: incoming[key] if key in incoming else value for key, value in values.items() if key not in {'move_ids', 'company_id'}}
                for key, value in values.items():
                    if self.schema(model).get(key, {}).get('type') == 'datetime':
                        payload['values'][key] = value + '+00:00'
        if definition.kind in ('delete', 'copy') and (payload.get('values') or payload.get('lines') or payload.get('options')):
            raise BusinessError('Xóa/sao chép không nhận thay đổi dữ liệu kèm theo.')
        if definition.kind == 'copy' and row:
            self.access(model, 'create')
        if definition.kind == 'delete':
            preview['notice'] = 'Xóa vĩnh viễn đúng bản ghi này; không tự xóa hoặc hủy chứng từ liên quan.'
        if definition.module == 'activities':
            from app.business.adapters import activity_target
            if definition.kind == 'create' and (not payload.get('values', {}).get('res_model_id') or not payload.get('values', {}).get('res_id')):
                missing.extend(name for name in ('values.res_model_id', 'values.res_id') if not payload.get('values', {}).get(name.split('.')[-1]))
            else:
                activity_target(self, payload, row)
        allowed_options = {'convert', 'email', 'validate', 'return', 'inventory', 'activity_done', 'note', 'backorder', 'quotation_approval'}
        if definition.kind not in allowed_options and action != 'business.sales.confirm' and payload.get('options'):
            raise BusinessError('Thao tác này không nhận options.')
        requirements = []
        meta = self.schema(model)
        for path in dict.fromkeys(missing):
            name = path.removeprefix('values.')
            spec = meta.get(name, {})
            requirements.append({'field': 'values.' + name if name in meta else path,
                                 'label': spec.get('string') or {'target_id': 'Chứng từ/bản ghi cần thao tác', 'lines': 'Các dòng hàng'}.get(path, path),
                                 'type': spec.get('type'), 'choices': spec.get('selection', [])})
        requirements = list({item['field']: item for item in requirements}.values())
        return {'status': 'needs_information' if missing else 'ready', 'action': action, 'payload': payload,
                'snapshot': list(self.snapshot.values()), 'preview': preview, 'missing': list(dict.fromkeys(missing)), 'requirements': requirements, '_values': values}

    def cancellation_dependencies(self, definition, row):
        model = definition.model
        fields = ['invoice_ids', 'picking_ids'] if definition.module in ('sales', 'purchases') else ['move_ids']
        source = self.record(model, row['id'], fields)
        for invoice_id in source.get('invoice_ids', []):
            invoice = self.record('account.move', invoice_id, ['state'])
            if invoice.get('state') == 'draft':
                raise Unsupported('Có hóa đơn nháp liên quan có thể bị hủy theo dây chuyền; cần xử lý trên Odoo vì kế toán ngoài phạm vi.')
        for picking_id in source.get('picking_ids', []):
            picking = self.record('stock.picking', picking_id, ['state'])
            if picking.get('state') not in ('done', 'cancel'):
                raise Unsupported('Có phiếu kho đang mở liên quan. Cần xem trước/hủy riêng phiếu kho trước, không tự hủy dây chuyền.')
        for move_id in source.get('move_ids', []):
            move = self.record('stock.move', move_id, ['move_dest_ids', 'propagate_cancel'])
            if move.get('propagate_cancel'):
                for destination in move.get('move_dest_ids', []):
                    other = self.record('stock.move', destination, ['state', 'picking_id'])
                    if other.get('state') not in ('done', 'cancel') and identity(other.get('picking_id')) != row['id']:
                        raise Unsupported('Có điều chuyển tiếp theo có thể bị hủy dây chuyền. Cần xử lý riêng trên Odoo.')

    def verify_snapshot(self, snapshot):
        for item in snapshot:
            current = self.record(item['model'], item['id'], list(item['values']), track=False)
            if current != item['values']:
                raise Conflict('Dữ liệu nguồn đã thay đổi sau bản xem trước; cần xem trước và xác nhận lại.')

    def execute(self, action, payload, snapshot, operation_id, checkpoint=None):
        self.company_id = self.ctx.selected_companies([payload['company_id']])[0]
        self.verify_snapshot(snapshot)
        prepared = self.prepare(action, payload)
        if prepared['status'] != 'ready':
            raise Conflict('Thông tin/quyền đã thay đổi; cần xem trước lại.')
        # Re-preparation may introduce a newly changed dependency. Compare both sets.
        before = {(item['model'], item['id']): item['values'] for item in snapshot}
        after = {(item['model'], item['id']): item['values'] for item in prepared['snapshot']}
        if before != after:
            raise Conflict('Dữ liệu hoặc điều kiện nghiệp vụ đã thay đổi; cần xem trước lại.')
        definition = ACTIONS[action]
        row = next((item['values'] for item in snapshot if item['model'] == definition.model and item['id'] == payload.get('target_id')), None)
        values = prepared['_values']
        model = definition.model
        def mark(stage, data):
            if checkpoint:
                checkpoint(stage, data)
        if definition.kind == 'create':
            result_id = self.ctx.call(model, 'create', [values])
            mark('created', {'model': model, 'id': result_id})
        elif definition.kind in ('update', 'archive', 'restore'):
            if definition.kind in ('archive', 'restore'):
                self.ctx.call(model, 'action_archive' if definition.kind == 'archive' else 'action_unarchive', [[row['id']]])
            else:
                self.ctx.call(model, 'write', [[row['id']], values])
            result_id = row['id']
            mark('written', {'model': model, 'id': result_id})
        elif definition.kind == 'delete':
            self.ctx.call(model, 'unlink', [[row['id']]])
            mark('deleted', {'model': model, 'id': row['id']})
            if self.ctx.call(model, 'search_count', [[('id', '=', row['id'])]], {'context': {'active_test': False}}):
                raise Conflict('Odoo chưa xác minh được kết quả xóa.')
            return {'status': 'completed', 'action': action, 'deleted_id': row['id'], 'message': 'Đã xóa bản ghi được xác nhận.'}
        elif definition.kind == 'copy':
            result_id = self.ctx.call(model, 'copy', [[row['id']]], {'default': {'state': 'draft'}})
            mark('copied', {'model': model, 'id': result_id})
        elif definition.kind == 'method':
            kwargs = {'lost_reason_id': payload['values']['lost_reason_id']} if action.endswith('.lost') else {}
            result = self.ctx.call(model, definition.method, [[row['id']]], kwargs)
            mark('method_returned', {'model': model, 'id': row['id'], 'response': result})
            if isinstance(result, dict) and result.get('type') == 'ir.actions.act_window':
                return {'status': 'awaiting_input', 'action': action, 'message': 'Odoo yêu cầu wizard bổ sung. Chưa tự xử lý; hãy mở chứng từ trên Odoo.', 'wizard': {'model': result.get('res_model'), 'id': result.get('res_id')}, 'record': self.readback(model, row['id'])}
            result_id = row['id']
        else:
            from app.business.adapters import execute
            return execute(self, definition, payload, row, operation_id, mark)
        record = self.readback(model, result_id, [key for key in values if key != 'order_line'])
        expected = {
            'business.sales.confirm': {'sale'}, 'business.sales.cancel': {'cancel'}, 'business.sales.draft': {'draft'},
            'business.purchases.confirm': {'purchase', 'to approve'}, 'business.purchases.approve': {'purchase'},
            'business.purchases.reject': {'cancel'}, 'business.purchases.cancel': {'cancel'}, 'business.purchases.draft': {'draft'},
            'business.stock.confirm': {'confirmed', 'waiting', 'assigned'}, 'business.stock.cancel': {'cancel'},
        }.get(action)
        if expected and record.get('state') not in expected:
            return {'status': 'check_required', 'action': action, 'record': record, 'message': 'Lệnh đã trả về nhưng trạng thái chưa đạt kết quả yêu cầu; cần kiểm tra trên Odoo. Không tự thực hiện lại.'}
        differences = []
        for key, value in values.items():
            if key in {'order_line', 'company_id'}:
                continue
            actual = record.get(key)
            if self.schema(model).get(key, {}).get('type') == 'many2one':
                actual = identity(actual)
            elif self.schema(model).get(key, {}).get('type') == 'many2many' and isinstance(value, list) and value and value[0][0] == 6:
                actual, value = sorted(actual or []), sorted(value[0][2])
            if actual != value:
                differences.append(key)
        if differences:
            return {'status': 'check_required', 'action': action, 'record': record, 'message': 'Odoo đã xử lý nhưng có giá trị khác bản xem trước: ' + ', '.join(differences) + '. Cần kiểm tra; không tự sửa lại.'}
        if action == 'business.purchases.confirm' and record.get('state') == 'to approve':
            return {'status': 'awaiting_approval', 'action': action, 'record': record, 'message': 'Đơn mua đã được đưa vào bước chờ duyệt; chưa coi là đã duyệt.'}
        return {'status': 'completed', 'action': action, 'record': record, 'message': 'Odoo đã thực hiện; dữ liệu dưới đây được đọc lại.'}

    def readback(self, model, record_id, extra_fields=None):
        meta = self.schema(model)
        fields = [f for f in ('name', 'state', 'active', 'partner_id', 'user_id', 'stage_id', 'type', 'email', 'phone', 'probability', 'lost_reason_id', 'amount_untaxed', 'amount_tax', 'amount_total', 'currency_id', 'order_line', 'move_ids', 'date_done', 'quantity', 'inventory_quantity') if f in meta]
        fields += [field for field in extra_fields or [] if field in meta and field not in fields]
        record = self.record(model, record_id, fields, track=False)
        record['url'] = f'{self.ctx.service.client.url}/web#id={record_id}&model={model}&view_type=form'
        return record
