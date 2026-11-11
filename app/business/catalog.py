"""Closed operation catalog. Model/method/context never come from the model."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    model: str
    title: str
    kind: str
    fields: tuple[str, ...] = ()
    method: str | None = None
    states: tuple[str, ...] = ()
    group: str | None = None

    @property
    def module(self):
        return self.name.split('.')[1]


CONTACT = ('name', 'email', 'phone', 'mobile', 'is_company', 'parent_id', 'type', 'street', 'street2', 'city', 'zip', 'country_id', 'state_id', 'vat', 'user_id', 'lang', 'comment')
PRODUCT = ('name', 'default_code', 'barcode', 'list_price', 'description_sale', 'description_purchase', 'sale_ok', 'purchase_ok', 'categ_id', 'product_tmpl_id', 'product_template_attribute_value_ids')
CRM = ('name', 'partner_id', 'contact_name', 'partner_name', 'email_from', 'phone', 'user_id', 'team_id', 'expected_revenue', 'probability', 'date_deadline', 'description', 'type')
SALE = ('partner_id', 'partner_invoice_id', 'partner_shipping_id', 'pricelist_id', 'payment_term_id', 'validity_date', 'note', 'client_order_ref', 'user_id', 'team_id', 'fiscal_position_id')
PURCHASE = ('partner_id', 'partner_ref', 'currency_id', 'payment_term_id', 'date_order', 'date_planned', 'notes', 'user_id', 'picking_type_id', 'fiscal_position_id')
PICKING = ('partner_id', 'picking_type_id', 'location_id', 'location_dest_id', 'scheduled_date', 'origin', 'user_id', 'note', 'move_type')
ACTIONS: dict[str, ActionDefinition] = {}


def add(module, model, op, title, kind, fields=(), method=None, states=(), group=None):
    name = f'business.{module}.{op}'
    ACTIONS[name] = ActionDefinition(name, model, title, kind, tuple(fields), method, tuple(states), group)


for module, model, fields, caption in (
    ('contacts', 'res.partner', CONTACT, 'liên hệ'), ('products', 'product.product', PRODUCT, 'sản phẩm'),
    ('crm', 'crm.lead', CRM, 'lead/cơ hội'), ('sales', 'sale.order', SALE, 'báo giá bán'),
    ('purchases', 'purchase.order', PURCHASE, 'RFQ mua'), ('stock', 'stock.picking', PICKING, 'phiếu kho'),
):
    for op, title in (('create', 'Tạo'), ('update', 'Sửa')):
        add(module, model, op, f'{title} {caption}', f'stock_{op}' if module == 'stock' else op, fields,
            states=('draft', 'sent') if module in ('sales', 'purchases') else ('draft',) if module == 'stock' else ())
    if module in ('contacts', 'products', 'crm'):
        for op, title in (('archive', 'Lưu trữ'), ('restore', 'Khôi phục')):
            add(module, model, op, f'{title} {caption}', op)
    if module != 'products' and module != 'stock':
        add(module, model, 'delete', f'Xóa {caption}', 'delete', states=('draft', 'cancel') if module in ('sales', 'purchases') else ())

for module, model, specs in (
    ('sales', 'sale.order', (
        ('copy', 'Sao chép báo giá', 'copy', ('draft', 'sent', 'sale', 'cancel')),
        ('confirm', 'Xác nhận đơn bán', 'action_confirm', ('draft', 'sent')),
        ('cancel', 'Hủy đơn bán', 'action_cancel', ('draft', 'sent', 'sale')),
        ('draft', 'Đưa báo giá về nháp', 'action_draft', ('cancel',)),
        ('lock', 'Khóa đơn bán', 'action_lock', ('sale',)), ('unlock', 'Mở khóa đơn bán', 'action_unlock', ('sale',)),
    )),
    ('purchases', 'purchase.order', (
        ('copy', 'Sao chép RFQ', 'copy', ('draft', 'sent', 'purchase', 'cancel')),
        ('confirm', 'Xác nhận đơn mua', 'button_confirm', ('draft', 'sent')),
        ('approve', 'Duyệt đơn mua', 'button_approve', ('to approve',)),
        ('reject', 'Từ chối đơn mua', 'button_cancel', ('to approve',)),
        ('cancel', 'Hủy đơn mua', 'button_cancel', ('draft', 'sent', 'to approve', 'purchase')),
        ('draft', 'Đưa RFQ về nháp', 'button_draft', ('cancel',)),
    )),
    ('stock', 'stock.picking', (
        ('confirm', 'Xác nhận phiếu kho', 'action_confirm', ('draft',)),
        ('reserve', 'Giữ hàng', 'action_assign', ('confirmed', 'waiting', 'assigned')),
        ('unreserve', 'Bỏ giữ hàng', 'do_unreserve', ('confirmed', 'waiting', 'assigned')),
        ('cancel', 'Hủy phiếu kho', 'action_cancel', ('draft', 'confirmed', 'waiting', 'assigned')),
    )),
):
    for op, title, method, states in specs:
        add(module, model, op, title, 'copy' if op == 'copy' else 'method', method=method, states=states,
            group='purchase.group_purchase_manager' if op in ('approve', 'reject') else
            'stock.group_stock_user' if module == 'stock' else None)

for op, fields, method in (
    ('assign', ('user_id', 'team_id'), None), ('stage', ('stage_id',), None),
    ('won', (), 'action_set_won_rainbowman'), ('lost', ('lost_reason_id',), None),
    ('reopen', (), 'action_restore'),
):
    add('crm', 'crm.lead', op, {'assign': 'Phân công CRM', 'stage': 'Chuyển giai đoạn CRM', 'won': 'Đánh dấu thắng', 'lost': 'Đánh dấu thua', 'reopen': 'Mở lại CRM'}[op], 'lost' if op == 'lost' else 'method' if method else 'update', fields, method)
add('crm', 'crm.lead', 'convert', 'Chuyển lead thành cơ hội', 'convert')
for module, model in (('sales', 'sale.order'), ('purchases', 'purchase.order')):
    add(module, model, 'send', 'Gửi báo giá/RFQ qua email', 'email', states=('draft', 'sent'))
add('sales', 'sale.order', 'export', 'Xuất tài liệu báo giá Odoo', 'export')
for op, title in (('request_approval', 'Yêu cầu cấp trên duyệt báo giá'),
                  ('grant_approval', 'Duyệt nội bộ báo giá'), ('reject_approval', 'Từ chối duyệt báo giá')):
    add('sales', 'sale.order', op, title, 'quotation_approval', states=('draft', 'sent'))
add('stock', 'stock.picking', 'validate', 'Hoàn tất phiếu kho', 'validate', states=('confirmed', 'waiting', 'assigned'), group='stock.group_stock_user')
add('stock', 'stock.picking', 'return', 'Tạo phiếu trả hàng', 'return', states=('done',), group='stock.group_stock_user')
add('stock', 'stock.quant', 'inventory', 'Áp dụng kiểm kê', 'inventory', group='stock.group_stock_manager')
add('stock', 'stock.picking', 'backorder', 'Xử lý phần hàng còn lại', 'backorder', states=('confirmed', 'waiting', 'assigned'), group='stock.group_stock_user')
add('stock', 'stock.lot', 'lot_create', 'Tạo lô/serial', 'create', ('name', 'product_id', 'ref'), group='stock.group_stock_user')
add('activities', 'mail.activity', 'create', 'Tạo hoạt động', 'create', ('activity_type_id', 'user_id', 'date_deadline', 'summary', 'note', 'res_model_id', 'res_id'))
add('activities', 'mail.activity', 'update', 'Sửa hoạt động', 'update', ('activity_type_id', 'user_id', 'date_deadline', 'summary', 'note'))
add('activities', 'mail.activity', 'done', 'Hoàn tất hoạt động', 'activity_done')
add('notes', 'mail.thread', 'create', 'Thêm ghi chú nội bộ', 'note')

LOOKUPS = {
    'contacts': 'res.partner', 'products': 'product.product', 'sales': 'sale.order', 'purchases': 'purchase.order',
    'crm': 'crm.lead', 'stock': 'stock.picking', 'quants': 'stock.quant', 'activities': 'mail.activity',
    'warehouses': 'stock.warehouse', 'locations': 'stock.location', 'picking_types': 'stock.picking.type',
    'lots': 'stock.lot', 'users': 'res.users', 'stages': 'crm.stage', 'teams': 'crm.team',
    'lost_reasons': 'crm.lost.reason', 'taxes': 'account.tax', 'uoms': 'uom.uom', 'currencies': 'res.currency',
    'pricelists': 'product.pricelist', 'payment_terms': 'account.payment.term', 'activity_types': 'mail.activity.type',
    'countries': 'res.country', 'states': 'res.country.state', 'categories': 'product.category',
    'models': 'ir.model', 'attachments': 'ir.attachment', 'move_lines': 'stock.move.line', 'moves': 'stock.move',
    'product_templates': 'product.template', 'variant_values': 'product.template.attribute.value',
}
TARGET_MODELS = {'sale.order', 'purchase.order', 'crm.lead', 'res.partner', 'stock.picking', 'product.product'}
