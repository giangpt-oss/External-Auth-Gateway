from typing import Any


class OdooModelConfig:
    def __init__(
        self,
        model: str,
        display_name: str,
        allowed_fields: list[str],
        default_fields: list[str],
        default_order: str = "id desc",
        max_limit: int = 50,
        default_limit: int = 10,
        allowed_search_fields: list[str] | None = None,
    ):
        self.model = model
        self.display_name = display_name
        self.allowed_fields = set(allowed_fields)
        self.default_fields = default_fields
        self.default_order = default_order
        self.max_limit = max_limit
        self.default_limit = default_limit
        self.allowed_search_fields = set(allowed_search_fields or default_fields)


ALLOWED_OPERATORS = {
    "=",
    "!=",
    ">",
    "<",
    ">=",
    "<=",
    "in",
    "not in",
    "like",
    "ilike",
    "=?",
}

ODOO_CATALOG: dict[str, OdooModelConfig] = {
    "crm.lead": OdooModelConfig(
        model="crm.lead",
        display_name="Cơ hội & Khách hàng tiềm năng (CRM)",
        allowed_fields=[
            "id",
            "name",
            "partner_id",
            "stage_id",
            "expected_revenue",
            "probability",
            "user_id",
            "email_from",
            "phone",
            "create_date",
            "write_date",
            "active",
        ],
        default_fields=["id", "name", "partner_id", "stage_id", "expected_revenue", "user_id"],
        default_order="write_date desc, id desc",
        allowed_search_fields=["name", "email_from", "phone", "partner_id", "stage_id", "user_id"],
    ),
    "res.partner": OdooModelConfig(
        model="res.partner",
        display_name="Đối tác & Khách hàng / Nhà cung cấp",
        allowed_fields=[
            "id",
            "name",
            "display_name",
            "email",
            "phone",
            "mobile",
            "is_company",
            "customer_rank",
            "supplier_rank",
            "city",
            "country_id",
            "parent_id",
            "active",
        ],
        default_fields=["id", "name", "email", "phone", "is_company", "city"],
        default_order="name asc, id desc",
        allowed_search_fields=["name", "email", "phone", "city"],
    ),
    "product.product": OdooModelConfig(
        model="product.product",
        display_name="Sản phẩm (Biến thể)",
        allowed_fields=[
            "id",
            "name",
            "default_code",
            "barcode",
            "list_price",
            "standard_price",
            "qty_available",
            "uom_id",
            "categ_id",
            "active",
        ],
        default_fields=["id", "name", "default_code", "list_price", "qty_available", "uom_id"],
        default_order="name asc, id desc",
        allowed_search_fields=["name", "default_code", "barcode"],
    ),
    "product.template": OdooModelConfig(
        model="product.template",
        display_name="Sản phẩm (Mẫu)",
        allowed_fields=[
            "id",
            "name",
            "default_code",
            "barcode",
            "list_price",
            "standard_price",
            "qty_available",
            "uom_id",
            "categ_id",
            "active",
        ],
        default_fields=["id", "name", "default_code", "list_price", "qty_available", "uom_id"],
        default_order="name asc, id desc",
        allowed_search_fields=["name", "default_code", "barcode"],
    ),
    "sale.order": OdooModelConfig(
        model="sale.order",
        display_name="Đơn bán hàng & Báo giá",
        allowed_fields=[
            "id",
            "name",
            "partner_id",
            "date_order",
            "amount_total",
            "amount_untaxed",
            "state",
            "user_id",
            "currency_id",
            "validity_date",
        ],
        default_fields=["id", "name", "partner_id", "date_order", "amount_total", "state"],
        default_order="date_order desc, id desc",
        allowed_search_fields=["name", "partner_id", "state", "user_id"],
    ),
    "purchase.order": OdooModelConfig(
        model="purchase.order",
        display_name="Đơn mua hàng",
        allowed_fields=[
            "id",
            "name",
            "partner_id",
            "date_order",
            "amount_total",
            "amount_untaxed",
            "state",
            "user_id",
            "currency_id",
            "date_planned",
        ],
        default_fields=["id", "name", "partner_id", "date_order", "amount_total", "state"],
        default_order="date_order desc, id desc",
        allowed_search_fields=["name", "partner_id", "state", "user_id"],
    ),
    "stock.picking": OdooModelConfig(
        model="stock.picking",
        display_name="Phiếu điều chuyển kho",
        allowed_fields=[
            "id",
            "name",
            "picking_type_id",
            "partner_id",
            "scheduled_date",
            "date_done",
            "state",
            "origin",
            "location_id",
            "location_dest_id",
        ],
        default_fields=["id", "name", "picking_type_id", "origin", "state", "scheduled_date"],
        default_order="scheduled_date desc, id desc",
        allowed_search_fields=["name", "origin", "state", "partner_id"],
    ),
    "stock.quant": OdooModelConfig(
        model="stock.quant",
        display_name="Tồn kho thực tế theo địa điểm",
        allowed_fields=[
            "id",
            "product_id",
            "location_id",
            "quantity",
            "reserved_quantity",
            "inventory_quantity_auto_apply",
        ],
        default_fields=["id", "product_id", "location_id", "quantity", "reserved_quantity"],
        default_order="quantity desc, id desc",
        allowed_search_fields=["product_id", "location_id"],
    ),
    "account.move": OdooModelConfig(
        model="account.move",
        display_name="Hóa đơn & Chứng từ kế toán",
        allowed_fields=[
            "id",
            "name",
            "partner_id",
            "invoice_date",
            "invoice_date_due",
            "amount_total",
            "amount_residual",
            "state",
            "payment_state",
            "move_type",
            "currency_id",
        ],
        default_fields=["id", "name", "partner_id", "invoice_date", "amount_total", "payment_state", "state"],
        default_order="invoice_date desc, id desc",
        allowed_search_fields=["name", "partner_id", "state", "payment_state", "move_type"],
    ),
    "hr.employee": OdooModelConfig(
        model="hr.employee",
        display_name="Danh bạ nhân viên",
        allowed_fields=[
            "id",
            "name",
            "job_title",
            "department_id",
            "work_email",
            "work_phone",
            "mobile_phone",
            "parent_id",
            "user_id",
            "active",
        ],
        default_fields=["id", "name", "job_title", "department_id", "work_email", "work_phone"],
        default_order="name asc, id desc",
        allowed_search_fields=["name", "work_email", "department_id", "job_title"],
    ),
    "hr.department": OdooModelConfig(
        model="hr.department",
        display_name="Phòng ban",
        allowed_fields=[
            "id",
            "name",
            "manager_id",
            "parent_id",
            "active",
        ],
        default_fields=["id", "name", "manager_id", "parent_id"],
        default_order="name asc, id desc",
        allowed_search_fields=["name", "manager_id"],
    ),
}


def get_catalog_overview() -> list[dict[str, Any]]:
    """Trả về tổng quan catalog cho client/agent nắm được các model và field được phép."""
    result = []
    for model_name, cfg in ODOO_CATALOG.items():
        result.append({
            "model": model_name,
            "display_name": cfg.display_name,
            "default_fields": cfg.default_fields,
            "allowed_fields": sorted(cfg.allowed_fields),
            "allowed_search_fields": sorted(cfg.allowed_search_fields),
            "max_limit": cfg.max_limit,
        })
    return result


def validate_catalog_query(
    model: str,
    fields: list[str] | None = None,
    domain: list[Any] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    order: str | None = None,
) -> tuple[str, list[str], list[Any], int, int, str]:
    """Kiểm tra và chuẩn hóa câu truy vấn theo Whitelist Catalog.

    Ném ValueError nếu model không thuộc catalog, field ngoài whitelist, hoặc domain độc hại.
    """
    if not model or model not in ODOO_CATALOG:
        raise ValueError(f"Mô hình '{model}' không nằm trong danh mục truy vấn được phép của Gateway.")

    cfg = ODOO_CATALOG[model]

    # 1. Kiểm tra fields
    if fields:
        sanitized_fields = []
        for f in fields:
            if not isinstance(f, str) or not f.strip():
                continue
            f_clean = f.strip()
            if f_clean not in cfg.allowed_fields:
                raise ValueError(f"Trường '{f_clean}' không được phép truy vấn trong mô hình '{model}'.")
            sanitized_fields.append(f_clean)
        final_fields = sanitized_fields if sanitized_fields else cfg.default_fields
    else:
        final_fields = cfg.default_fields

    if "id" not in final_fields:
        final_fields = ["id"] + final_fields

    # 2. Kiểm tra domain
    final_domain: list[Any] = []
    if domain:
        if not isinstance(domain, list):
            raise ValueError("Domain phải là danh sách điều kiện (list).")

        for leaf in domain:
            if leaf in ("|", "&", "!"):
                final_domain.append(leaf)
                continue

            if not isinstance(leaf, list | tuple) or len(leaf) != 3:
                raise ValueError(f"Điều kiện domain '{leaf}' không hợp lệ (phải là bộ 3 phần tử [field, op, val]).")

            f_name, op, val = leaf[0], leaf[1], leaf[2]
            if not isinstance(f_name, str):
                raise ValueError(f"Tên trường trong domain '{f_name}' không hợp lệ.")

            f_name_clean = f_name.strip()
            if f_name_clean not in cfg.allowed_fields and f_name_clean not in cfg.allowed_search_fields:
                raise ValueError(f"Trường lọc '{f_name_clean}' không được phép trong domain của '{model}'.")

            if not isinstance(op, str) or op.strip().lower() not in ALLOWED_OPERATORS:
                raise ValueError(f"Toán tử '{op}' không được hỗ trợ trong domain.")

            final_domain.append((f_name_clean, op.strip().lower(), val))

    # 3. Phân trang và sắp xếp
    final_limit = min(max(1, limit or cfg.default_limit), cfg.max_limit)
    final_offset = max(0, offset or 0)
    final_order = (order or cfg.default_order).strip()

    return model, final_fields, final_domain, final_limit, final_offset, final_order
