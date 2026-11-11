import base64

from app.odoo.client import OdooAccessError, OdooError

MAX_PDF_BYTES = 49 * 1024 * 1024
COMPOSER = 'mail.compose.message'


def render_quotation_pdf(ctx, result):
    """Use Odoo's quotation mail attachment renderer with the caller's API credential."""
    order_id = result['id']
    company_id = result['company_id'][0]
    ctx.selected_companies([company_id])
    orders = ctx.call('sale.order', 'search_read', [[('id', '=', order_id), ('company_id', '=', company_id)]],
                      {'fields': ['order_line'], 'limit': 1})
    if not orders:
        raise OdooAccessError('Không có quyền đọc báo giá trong công ty này.')
    lines = orders[0]['order_line']
    if lines and ctx.call('sale.order.line', 'search_count', [[('id', 'in', lines)]]) != len(lines):
        raise OdooAccessError('Không có quyền đọc đầy đủ các dòng báo giá.')

    model, report_id = ctx.call('ir.model.data', 'check_object_reference', ['sale', 'action_report_saleorder'])
    if model != 'ir.actions.report' or not report_id:
        raise OdooAccessError('Không tìm thấy mẫu in báo giá được phép sử dụng.')
    reports = ctx.call(model, 'search_read', [[('id', '=', report_id), ('model', '=', 'sale.order'),
                                              ('report_type', '=', 'qweb-pdf')]], {'fields': ['group_ids']})
    if not reports:
        raise OdooError('Mẫu in báo giá không phải là PDF bán hàng.')
    groups = reports[0]['group_ids']
    if groups:
        profile = ctx.call('res.users', 'read', [[ctx.user.odoo_uid]], {'fields': ['all_group_ids']})
        if not profile or not set(groups).intersection(profile[0]['all_group_ids']):
            raise OdooAccessError('Tài khoản không thuộc nhóm được phép in báo giá.')

    model, template_id = ctx.call('ir.model.data', 'check_object_reference', ['sale', 'email_template_edi_sale'])
    if model != 'mail.template' or not template_id:
        raise OdooAccessError('Không tìm thấy mẫu báo giá Odoo.')
    templates = ctx.call(model, 'read', [[template_id]], {'fields': ['model', 'report_template_ids', 'attachment_ids']})
    if not templates or templates[0]['model'] != 'sale.order' or templates[0]['report_template_ids'] != [report_id]:
        raise OdooError('Mẫu báo giá chưa được cấu hình để kết xuất đúng một PDF bán hàng.')

    # Creating the transient composer computes its report attachments. Never invoke action_send_mail.
    composer_id = ctx.call(COMPOSER, 'create', [{'model': 'sale.order', 'res_ids': repr([order_id]),
        'composition_mode': 'comment', 'template_id': template_id, 'body': False}])
    composers = ctx.call(COMPOSER, 'read', [[composer_id]], {'fields': ['attachment_ids']})
    static_ids = set(templates[0]['attachment_ids'])
    generated_ids = [item for item in composers[0]['attachment_ids'] if item not in static_ids] if composers else []
    attachments = ctx.call('ir.attachment', 'search_read', [[('id', 'in', generated_ids),
        ('res_model', '=', COMPOSER), ('res_id', '=', 0), ('create_uid', '=', ctx.user.odoo_uid),
        ('type', '=', 'binary'), ('mimetype', '=', 'application/pdf'), ('file_size', '<=', MAX_PDF_BYTES)]],
        {'fields': ['name', 'datas'], 'limit': 2, 'context': {'bin_size': False}})
    if len(attachments) != 1 or not attachments[0]['datas']:
        raise OdooError('Odoo không trả về một file PDF báo giá hợp lệ.')
    attachment = attachments[0]
    content = base64.b64decode(attachment['datas'], validate=True)
    if not content.startswith(b'%PDF-') or len(content) > MAX_PDF_BYTES:
        raise OdooError('Dữ liệu trả về không phải file PDF báo giá hợp lệ.')
    return attachment['name'] or f"{result['name']}.pdf", content
