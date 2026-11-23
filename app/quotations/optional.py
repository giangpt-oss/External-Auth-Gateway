"""Sale order options verified against the live company schema before use."""

OPTIONAL_FIELDS = {
    'user_id': ('Nhân viên sales', 'many2one', 'res.users'),
    'team_id': ('Bộ phận sales', 'many2one', 'crm.team'),
    'require_signature': ('Chữ ký online', 'boolean', None),
    'require_payment': ('Thanh toán online', 'boolean', None),
    'client_order_ref': ('Tham chiếu khách hàng', 'char', None),
    'tag_ids': ('Thẻ', 'many2many', 'crm.tag'),
    'x_studio_thoi_gian_bao_tri': ('Thời gian bảo trì', 'selection', None),
    'warehouse_id': ('Kho hàng', 'many2one', 'stock.warehouse'),
    'incoterm': ('Incoterm', 'many2one', 'account.incoterms'),
    'incoterm_location': ('Địa điểm Incoterm', 'char', None),
    'picking_policy': ('Chính sách vận chuyển', 'selection', None),
    'commitment_date': ('Ngày giao hàng', 'datetime', None),
    'fiscal_position_id': ('Vị trí tài chính', 'many2one', 'account.fiscal.position'),
    'preferred_payment_method_line_id': ('Phương thức thanh toán', 'many2one', 'account.payment.method.line'),
    'origin': ('Chứng từ gốc', 'char', None),
    'opportunity_id': ('Cơ hội', 'many2one', 'crm.lead'),
    'campaign_id': ('Chiến dịch', 'many2one', 'utm.campaign'),
    'medium_id': ('Phương tiện', 'many2one', 'utm.medium'),
    'source_id': ('Nguồn', 'many2one', 'utm.source'),
}
