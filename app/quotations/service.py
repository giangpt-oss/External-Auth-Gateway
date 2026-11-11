"""Quotation RPC boundary. Never invokes form onchange or private Odoo methods."""

import math
import re
from datetime import date
from typing import Any

from app.odoo.client import OdooAccessError, OdooClient, OdooError, OdooNetworkError
from app.quotations.approval import approval_control


class QuotationValidationError(ValueError):
    """The submitted quotation cannot be safely prepared or created."""


LOOKUPS = {
    "partners": "res.partner",
    "products": "product.product",
    "taxes": "account.tax",
    "companies": "res.company",
    "currencies": "res.currency",
    "pricelists": "product.pricelist",
    "payment_terms": "account.payment.term",
    "uoms": "uom.uom",
    "fiscal_positions": "account.fiscal.position",
}
ORDER_FIELDS = {
    "partner_id", "partner_invoice_id", "partner_shipping_id", "company_id",
    "currency_id", "pricelist_id", "payment_term_id", "validity_date", "note",
    "fiscal_position_id", "client_order_ref", "origin",
}
LINE_FIELDS = {"product_id", "name", "product_uom_qty", "product_uom", "product_uom_id", "price_unit", "tax_id", "tax_ids", "customer_lead"}
SAFE_TYPES = {"char", "text", "html", "integer", "float", "monetary", "boolean", "date", "selection", "many2one"}
ATTRIBUTES = ["string", "type", "required", "readonly", "store", "selection", "relation", "compute"]


def _id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QuotationValidationError(f"{label} phải là ID số nguyên dương đã được tra cứu.")
    return value


def _number(value: Any, label: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, float | int) or not math.isfinite(value):
        raise QuotationValidationError(f"{label} phải là số hữu hạn.")
    if value < 0 or (positive and value == 0):
        raise QuotationValidationError(f"{label} không hợp lệ.")
    return float(value)


def _m2o(value: Any) -> int | None:
    return value[0] if isinstance(value, list | tuple) and value else None


class QuotationService:
    def __init__(self, client: OdooClient, uid: int, secret: str):
        self.client, self.uid, self.secret = client, uid, secret
        self._schemas: dict[str, dict] = {}
        self._records: dict = {}
        self._company_cache: dict = {}
        self._create_checked = False

    def _rpc(self, model: str, method: str, args: list, company_id: int | None = None, **kwargs):
        if company_id:
            kwargs["context"] = {"allowed_company_ids": [company_id], "force_company": company_id}
        return self.client.execute(self.uid, self.secret, model, method, args, kwargs)

    def _schema(self, model: str) -> dict:
        if model not in self._schemas:
            self._schemas[model] = self._rpc(model, "fields_get", [], attributes=ATTRIBUTES)
        return self._schemas[model]

    def _companies(self, company_id: int | None = None) -> tuple[int, list[int]]:
        if company_id in self._company_cache:
            return self._company_cache[company_id]
        rows = self._rpc("res.users", "read", [[self.uid]], fields=["company_id", "company_ids"])
        if not rows:
            raise OdooAccessError("Không đọc được công ty của tài khoản.")
        allowed = rows[0].get("company_ids") or []
        chosen = _id(company_id, "company_id") if company_id is not None else _m2o(rows[0].get("company_id"))
        if not chosen or chosen not in allowed:
            raise OdooAccessError("Công ty không thuộc phạm vi tài khoản.")
        self._company_cache[company_id] = (chosen, allowed)
        return chosen, allowed

    def _record(self, model: str, record_id: int, company_id: int, fields: list[str] | None = None) -> dict:
        record_id = _id(record_id, model)
        if model == "res.company" and record_id != company_id:
            raise OdooAccessError("Quan hệ công ty phải trùng công ty của báo giá.")
        schema = self._schema(model)
        requested = [f for f in (fields or ["id", "display_name", "company_id", "active"]) if f in schema or f == "id"]
        for f in ("company_id", "active"):
            if f in schema and f not in requested:
                requested.append(f)
        cached = self._records.get((model, record_id, company_id))
        if cached and set(requested).issubset(cached):
            return dict(cached)
        rows = self._rpc(model, "search_read", [[("id", "=", record_id)]], company_id, fields=requested, limit=1)
        if not rows:
            raise OdooAccessError(f"Không tìm thấy hoặc không có quyền đọc {model} #{record_id}.")
        row = rows[0]
        if row.get("active") is False:
            raise QuotationValidationError(f"{model} #{record_id} đã bị lưu trữ.")
        if row.get("company_id") and _m2o(row["company_id"]) != company_id:
            raise OdooAccessError(f"{model} #{record_id} thuộc công ty khác.")
        self._records[(model, record_id, company_id)] = row
        return row

    def _check_create(self):
        if self._create_checked:
            return
        for model in ("sale.order", "sale.order.line"):
            for operation in ("read", "create"):
                if not self._rpc(model, "check_access_rights", [operation, False]):
                    raise OdooAccessError(f"Không có quyền {operation} {model}.")
        self._create_checked = True

    def _editable(self, model: str) -> dict:
        base = ORDER_FIELDS if model == "sale.order" else LINE_FIELDS
        return {name: spec for name, spec in self._schema(model).items()
                if (name in base or name.startswith("x_")) and not spec.get("readonly")
                and not spec.get("compute") and not approval_control(name)}

    def metadata(self, company_id: int | None = None) -> dict:
        company_id, allowed = self._companies(company_id)
        self._check_create()
        result = {"company_id": company_id, "allowed_company_ids": allowed, "models": {},
                  "pricing": "user_confirmed", "totals": "Odoo tính chính thức sau khi tạo nháp."}
        for model in ("sale.order", "sale.order.line"):
            editable = self._editable(model)
            defaults = self._rpc(model, "default_get", [list(editable)], company_id)
            fields, unsupported = {}, []
            for name, spec in editable.items():
                supported = spec.get("type") in SAFE_TYPES or name == "tax_id"
                if spec.get("type") == "many2one" and spec.get("relation") not in LOOKUPS.values():
                    supported = False
                fields[name] = {k: v for k, v in spec.items() if k != "compute"}
                fields[name]["supported"] = supported
                if spec.get("required") and not supported and not defaults.get(name):
                    unsupported.append(name)
            # Unknown mandatory writable standard fields cannot silently disappear.
            extra = [name for name, spec in self._schema(model).items()
                     if spec.get("required") and not spec.get("readonly") and not spec.get("compute")
                     and name not in editable and name not in {"order_id", "order_line", "name", "state", "date_order", "user_id"}]
            extra_defaults = self._rpc(model, "default_get", [extra], company_id) if extra else {}
            unsupported.extend(name for name in extra if not extra_defaults.get(name))
            result["models"][model] = {"fields": fields, "defaults": defaults, "unsupported_required_fields": unsupported}
        return result

    def lookup(self, kind: str, query: str = "", company_id: int | None = None, limit: int = 10) -> dict:
        company_id, allowed = self._companies(company_id)
        if kind not in LOOKUPS:
            raise QuotationValidationError("Loại tra cứu báo giá không được hỗ trợ.")
        model = LOOKUPS[kind]
        schema = self._schema(model)
        fields = [f for f in ["id", "display_name", "name", "default_code", "list_price", "uom_id", "currency_id", "company_id", "amount", "amount_type", "price_include", "type_tax_use"] if f in schema or f == "id"]
        domain = []
        if "company_id" in schema:
            domain += ["|", ("company_id", "=", False), ("company_id", "=", company_id)]
        if kind == "companies":
            domain.append(("id", "in", allowed))
        if query.strip():
            domain.append(("display_name", "ilike", query.strip()[:150]))
        if kind == "taxes":
            domain.append(("type_tax_use", "in", ["sale", "none"]))
        if kind == "products" and "sale_ok" in schema:
            domain.append(("sale_ok", "=", True))
        rows = self._rpc(model, "search_read", [domain], company_id, fields=fields, order="id asc", limit=max(1, min(int(limit), 20)))
        return {"kind": kind, "company_id": company_id, "items": rows}

    def _custom(self, model: str, data: Any, company_id: int) -> dict:
        if not isinstance(data, dict):
            raise QuotationValidationError("custom_fields phải là một object.")
        editable = self._editable(model)
        clean = {}
        for name, value in data.items():
            if approval_control(name):
                raise QuotationValidationError('Trường phê duyệt phải được xử lý bằng thao tác duyệt riêng, không được điền trong báo giá.')
            spec = editable.get(name)
            if not name.startswith("x_") or not spec:
                raise QuotationValidationError(f"Không được ghi trường {name}.")
            kind = spec.get("type")
            if kind not in SAFE_TYPES:
                raise QuotationValidationError(f"Chưa hỗ trợ kiểu trường {name} ({kind}).")
            if kind == "many2one":
                relation = spec.get("relation")
                if relation not in LOOKUPS.values():
                    raise QuotationValidationError(f"Chưa hỗ trợ quan hệ {name}.")
                self._record(relation, _id(value, name), company_id)
            elif kind == "boolean":
                if not isinstance(value, bool):
                    raise QuotationValidationError(f"{name} phải là true/false.")
            elif kind == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise QuotationValidationError(f"{name} phải là số nguyên.")
            elif kind in {"float", "monetary"}:
                if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                    raise QuotationValidationError(f"{name} phải là số hữu hạn.")
            elif kind == "selection":
                if value not in [s[0] for s in spec.get("selection", [])]:
                    raise QuotationValidationError(f"Lựa chọn {name} không hợp lệ.")
            elif kind == "date":
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError) as exc:
                    raise QuotationValidationError(f"{name} phải có dạng YYYY-MM-DD.") from exc
            elif not isinstance(value, str) or len(value) > 10000:
                raise QuotationValidationError(f"{name} phải là văn bản tối đa 10000 ký tự.")
            clean[name] = value
        return clean

    def prepare(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise QuotationValidationError("Dữ liệu báo giá không hợp lệ.")
        unknown = set(payload) - ORDER_FIELDS - {"lines", "custom_fields"}
        if unknown:
            raise QuotationValidationError("Trường báo giá chưa hỗ trợ: " + ", ".join(sorted(unknown)))
        company_id, _ = self._companies(payload.get("company_id"))
        meta = self.metadata(company_id)
        missing, values = [], {"company_id": company_id}
        unsupported = {m: d["unsupported_required_fields"] for m, d in meta["models"].items() if d["unsupported_required_fields"]}
        if unsupported:
            raise QuotationValidationError(f"Odoo yêu cầu trường chưa hỗ trợ: {unsupported}.")
        order_fields = self._schema("sale.order")
        partner = None
        if not payload.get("partner_id"):
            missing.append("partner_id")
        else:
            partner = self._record("res.partner", payload["partner_id"], company_id,
                                   ["id", "display_name", "property_product_pricelist", "property_payment_term_id", "property_account_position_id"])
            values["partner_id"] = partner["id"]
            for field in ('partner_invoice_id', 'partner_shipping_id'):
                if field in order_fields:
                    if payload.get(field):
                        self._record('res.partner', payload[field], company_id)
                        values[field] = payload[field]
                    elif not order_fields[field].get('required'):
                        values[field] = False
        relations = {"pricelist_id": ("product.pricelist", "property_product_pricelist"),
                     "payment_term_id": ("account.payment.term", "property_payment_term_id"),
                     "fiscal_position_id": ("account.fiscal.position", "property_account_position_id")}
        for field, (model, _partner_field) in relations.items():
            if field not in order_fields:
                continue
            value = payload.get(field)
            if value:
                record = self._record(model, value, company_id, ["id", "display_name", "currency_id", "tax_ids"])
                values[field] = value
                if field == "pricelist_id":
                    currency_id = _m2o(record.get("currency_id"))
                    if payload.get("currency_id") and payload["currency_id"] != currency_id:
                        raise QuotationValidationError("Tiền tệ không khớp bảng giá. Chọn bảng giá có tiền tệ mong muốn.")
                    if currency_id:
                        values["currency_id"] = currency_id
            elif not order_fields[field].get('required'):
                values[field] = False
        if not values.get("currency_id"):
            company = self._record("res.company", company_id, company_id, ["id", "currency_id"])
            currency_id = _m2o(company.get("currency_id"))
            if payload.get("currency_id") and payload["currency_id"] != currency_id:
                raise QuotationValidationError("Tiền tệ không khớp công ty; hãy chọn bảng giá có tiền tệ mong muốn.")
            if currency_id:
                values["currency_id"] = currency_id
        for name in ['validity_date', 'note', 'client_order_ref', 'origin']:
            if payload.get(name) not in (None, '', False):
                if name not in order_fields or not isinstance(payload[name], str):
                    raise QuotationValidationError(f"Trường {name} không hợp lệ.")
                if name == "validity_date":
                    try:
                        date.fromisoformat(payload[name])
                    except ValueError as exc:
                        raise QuotationValidationError("Hiệu lực báo giá phải có dạng YYYY-MM-DD.") from exc
                elif len(payload[name]) > 10000:
                    raise QuotationValidationError("Ghi chú quá dài.")
                values[name] = payload[name]
            elif name in order_fields and not order_fields[name].get('required'):
                values[name] = False
        custom_input = payload.get("custom_fields", {})
        if not isinstance(custom_input, dict):
            raise QuotationValidationError("custom_fields phải là một object.")
        values.update(self._custom('sale.order', custom_input, company_id))
        for name, spec in meta['models']['sale.order']['fields'].items():
            if name.startswith('x_') and spec.get('supported') and not spec.get('required') and name not in values:
                values[name] = False
        raw_lines = payload.get("lines", [])
        if not isinstance(raw_lines, list) or len(raw_lines) > 100:
            raise QuotationValidationError("Báo giá phải có tối đa 100 dòng hàng.")
        if not raw_lines:
            missing.append("lines")
        commands, preview_lines, normalized_lines = [], [], []
        line_schema = self._schema("sale.order.line")
        tax_field = 'tax_ids' if 'tax_ids' in line_schema else 'tax_id'
        if tax_field not in line_schema:
            raise QuotationValidationError('Không xác minh được trường thuế dòng báo giá trên Odoo.')
        uom_field = "product_uom_id" if "product_uom_id" in line_schema else "product_uom"
        for index, line in enumerate(raw_lines):
            prefix = f"lines.{index}"
            if not isinstance(line, dict):
                raise QuotationValidationError(f"Dòng {index + 1} không hợp lệ.")
            unknown = set(line) - {"product_id", "name", "quantity", "price_unit", "price_confirmed", "use_catalog_price", "tax_ids", "uom_id", "custom_fields", "customer_lead"}
            if unknown:
                raise QuotationValidationError(f"Trường dòng hàng chưa hỗ trợ: {', '.join(sorted(unknown))}")
            if not line.get("product_id"):
                missing.append(f"{prefix}.product_id")
                continue
            product = self._record("product.product", line["product_id"], company_id,
                                   ["id", "display_name", "uom_id", "taxes_id", "sale_ok", "list_price", "currency_id"])
            if product.get("sale_ok") is False:
                raise QuotationValidationError("Sản phẩm không được phép bán.")
            vals = {"product_id": product["id"], "name": str(line.get("name") or product.get("display_name") or product["id"])[:2000]}
            if line.get("quantity") is None:
                missing.append(f"{prefix}.quantity")
            else:
                vals["product_uom_qty"] = _number(line["quantity"], "Số lượng", True)
            if "customer_lead" in line:
                if "customer_lead" not in meta["models"]["sale.order.line"]["fields"]:
                    raise QuotationValidationError('Odoo không hỗ trợ thời gian giao hàng trên dòng báo giá.')
                vals['customer_lead'] = _number(line['customer_lead'], 'Thời gian giao hàng (số ngày, không phải ngày lịch)')
            if 'use_catalog_price' in line and not isinstance(line['use_catalog_price'], bool):
                raise QuotationValidationError('Lựa chọn giá niêm yết phải là boolean.')
            if line.get('use_catalog_price') is True:
                if line.get('uom_id') and line['uom_id'] != _m2o(product.get('uom_id')):
                    raise QuotationValidationError('Giá niêm yết chỉ dùng trực tiếp với đơn vị gốc của sản phẩm.')
                product_currency = _m2o(product.get('currency_id'))
                if product_currency and product_currency != values.get('currency_id'):
                    raise QuotationValidationError('Giá niêm yết khác tiền tệ báo giá; cần chốt giá quy đổi trước.')
                vals['price_unit'] = _number(product.get('list_price'), 'Giá niêm yết')
            elif line.get("price_unit") is None or line.get("price_confirmed") is not True:
                missing.append(f"{prefix}.price_unit_confirmed")
            else:
                vals["price_unit"] = _number(line["price_unit"], "Đơn giá")
            uom_id = line.get("uom_id") or _m2o(product.get("uom_id"))
            if uom_id:
                selected_uom = self._record('uom.uom', uom_id, company_id, ['id', 'category_id', 'display_name'])
                product_uom = self._record("uom.uom", _m2o(product.get("uom_id")), company_id, ["id", "category_id"])
                if (selected_uom.get("category_id") != product_uom.get("category_id")
                        or (uom_id != _m2o(product.get("uom_id")) and not selected_uom.get("category_id"))):
                    raise QuotationValidationError("Đơn vị tính khác nhóm đơn vị của sản phẩm.")
                vals[uom_field] = uom_id
            else:
                missing.append(f"{prefix}.uom_id")
            tax_ids = line.get('tax_ids')
            tax_labels = []
            # Explicit [] means the user selected no tax. Never guess a fiscal-position mapping.
            if tax_ids is None:
                missing.append(f"{prefix}.tax_ids")
            elif not isinstance(tax_ids, list) or len(tax_ids) > 20:
                raise QuotationValidationError("Danh sách thuế không hợp lệ.")
            else:
                for tax_id in tax_ids:
                    tax = self._record('account.tax', tax_id, company_id, ['id', 'type_tax_use', 'display_name'])
                    tax_labels.append(tax.get('display_name') or str(tax_id))
                    if tax.get("type_tax_use") not in {"sale", "none"}:
                        raise QuotationValidationError("Thuế không áp dụng cho bán hàng.")
                vals[tax_field] = [[6, 0, list(dict.fromkeys(tax_ids))]]
            line_custom = line.get("custom_fields", {})
            if not isinstance(line_custom, dict):
                raise QuotationValidationError("custom_fields của dòng hàng phải là một object.")
            vals.update(self._custom('sale.order.line', line_custom, company_id))
            for name, spec in meta['models']['sale.order.line']['fields'].items():
                if name.startswith('x_') and spec.get('supported') and not spec.get('required') and name not in vals:
                    vals[name] = False
            for name, spec in meta["models"]["sale.order.line"]["fields"].items():
                # Standard Odoo computes this on creation; required ORM metadata
                # does not make it a mandatory user input (nor an expiry date).
                if name == 'customer_lead':
                    continue
                if spec.get('required') and name not in vals:
                    missing.append(f"{prefix}.custom_fields.{name}" if name.startswith("x_") else f"{prefix}.{name}")
            commands.append([0, 0, vals])
            normalized_lines.append(dict(line, uom_id=uom_id, **({'customer_lead': vals['customer_lead']} if 'customer_lead' in vals else {}),
                                         custom_fields=dict(line_custom)))
            if line.get('use_catalog_price') is True:
                normalized_lines[-1].pop('use_catalog_price', None)
                normalized_lines[-1].update(price_unit=vals['price_unit'], price_confirmed=True)
            preview_lines.append({'name': vals.get('name'), "product": product.get("display_name"), "quantity": vals.get("product_uom_qty"),
                                  "price_unit": vals.get("price_unit"), "uom_id": uom_id, "tax_ids": tax_ids,
                                  'tax_labels': tax_labels, 'uom_name': selected_uom.get('display_name') if uom_id else None,
                                  'custom_fields': [{'label': spec.get('string') or name, 'value': line_custom.get(name)} for name, spec in meta['models']['sale.order.line']['fields'].items() if name.startswith('x_')],
                                  "suggested_price": product.get("list_price"), "customer_lead": vals.get('customer_lead')})
        for name, spec in meta["models"]["sale.order"]["fields"].items():
            if spec.get('required') and name not in values:
                missing.append(f"custom_fields.{name}" if name.startswith("x_") else name)
        values["order_line"] = commands
        normalized = dict(payload, company_id=company_id)
        # Preserve blanks; only technical context and explicitly requested data are frozen.
        normalized.update({k: v for k, v in values.items() if k in ORDER_FIELDS and v is not False})
        normalized['custom_fields'] = dict(custom_input)
        if len(normalized_lines) == len(raw_lines):
            normalized["lines"] = normalized_lines
        fields = self.preview_fields(payload, values, company_id, meta)
        return {"status": "needs_information" if missing else "ready", "payload": normalized,
                "missing_fields": list(dict.fromkeys(missing)), "values": values, "metadata": meta,
                "preview": {"fields": fields, "customer": partner.get("display_name") if partner else None,
                            "company_id": company_id, "currency_id": values.get("currency_id"), "lines": preview_lines,
                            "totals_provisional": True, "notice": "Đơn giá do người dùng chốt; tổng và thuế chính thức do Odoo tính khi tạo nháp."}}

    def preview_fields(self, payload, values, company_id, meta):
        labels = {'name': 'Số báo giá', 'company_id': 'Công ty', 'partner_id': 'Khách hàng',
            'partner_invoice_id': 'Địa chỉ xuất hóa đơn', 'partner_shipping_id': 'Địa chỉ giao hàng',
            'date_order': 'Ngày báo giá', 'validity_date': 'Ngày hết hạn', 'pricelist_id': 'Bảng giá',
            'currency_id': 'Tiền tệ', 'payment_term_id': 'Điều khoản thanh toán',
            'fiscal_position_id': 'Vị thế tài chính', 'client_order_ref': 'Tham chiếu khách hàng',
            'origin': 'Chứng từ gốc', 'note': 'Điều khoản và ghi chú'}
        relations = {'partner_id': 'res.partner', 'partner_invoice_id': 'res.partner', 'partner_shipping_id': 'res.partner',
            'company_id': 'res.company', 'currency_id': 'res.currency', 'pricelist_id': 'product.pricelist',
            'payment_term_id': 'account.payment.term', 'fiscal_position_id': 'account.fiscal.position'}
        rows = []
        custom = payload.get('custom_fields', {})
        for name in [*labels, *sorted(k for k in meta['models']['sale.order']['fields'] if k.startswith('x_'))]:
            value = custom.get(name) if name.startswith('x_') else values.get(name)
            explicit_boolean = name in custom and meta['models']['sale.order']['fields'].get(name, {}).get('type') == 'boolean'
            if value is None or (value is False and not explicit_boolean):
                value = None
            elif name in relations:
                record = self._record(relations[name], value, company_id, ['id', 'display_name'])
                value = record.get('display_name') or str(record['id'])
            rows.append({'name': name, 'label': labels.get(name) or meta['models']['sale.order']['fields'][name].get('string', name),
                'value': value, 'source': 'context' if name in ('company_id', 'currency_id') and name not in payload else 'user' if value is not None else 'blank'})
        return rows

    def _readback(self, order_id: int, company_id: int) -> dict:
        fields = [f for f in dict.fromkeys(['id', 'name', 'state', *sorted(ORDER_FIELDS), 'date_order', 'amount_untaxed', 'amount_tax', 'amount_total', 'order_line']) if f in self._schema('sale.order') or f == 'id']
        order = self._record("sale.order", order_id, company_id, fields)
        line_fields = [f for f in ["id", "product_id", "name", "product_uom_qty", "price_unit", "tax_id", "tax_ids", "price_subtotal", "price_total", "product_uom", "product_uom_id", "customer_lead"] if f in self._schema("sale.order.line") or f == "id"]
        order["lines"] = self._rpc("sale.order.line", "read", [order.get("order_line", [])], company_id, fields=line_fields) if order.get("order_line") else []
        order["url"] = f"{self.client.url}/web#id={order_id}&model=sale.order&view_type=form"
        return order

    def status_by_name(self, name, company_id=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise QuotationValidationError('Số báo giá không hợp lệ.')
        company_id, _ = self._companies(company_id)
        rows = self._rpc('sale.order', 'search_read', [[('name', '=', name.strip()), ('company_id', '=', company_id)]],
            company_id, fields=['id', 'name', 'company_id', 'state', 'amount_total', 'currency_id'], limit=2)
        if not rows:
            return {'status': 'not_found', 'message': 'Không tìm thấy báo giá trong công ty và phạm vi tài khoản được phép xem.'}
        if len(rows) != 1:
            return {'status': 'needs_selection', 'message': 'Có nhiều chứng từ cùng số; cần xác định chính xác chứng từ trước.', 'items': rows}
        result = dict(rows[0])
        if getattr(self.client, 'url', None):
            result['url'] = f"{self.client.url}/web#id={result['id']}&model=sale.order&view_type=form"
        return {'status': 'OK', 'result': result}

    def reconcile(self, operation_id: str) -> dict:
        self._operation(operation_id)
        _, allowed = self._companies()
        rows = self.client.execute(self.uid, self.secret, "sale.order", "search_read",
                                   [[("origin", "=", f"AGENT:{operation_id}"), ("company_id", "in", allowed)]],
                                   {"fields": ["id", "company_id"], "limit": 2,
                                    "context": {"allowed_company_ids": allowed}})
        if len(rows) != 1:
            return {"status": "unknown_result", "message": "Chưa tìm thấy một báo giá duy nhất; không tự tạo lại, cần kiểm tra Odoo."}
        result = self._readback(rows[0]["id"], _m2o(rows[0]["company_id"]))
        result["verification"] = "check_required"
        result["notice"] = "Đã tìm thấy chứng từ theo mã thao tác. Hãy đối chiếu trạng thái và dòng hàng với bản xem trước trước khi gửi khách hàng."
        return {"status": "completed", "result": result}

    @staticmethod
    def _operation(operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", operation_id):
            raise QuotationValidationError("operation_id không hợp lệ.")

    def create(self, operation_id: str, payload: dict, on_created=None) -> dict:
        self._operation(operation_id)
        prepared = self.prepare(payload)
        if prepared["status"] != "ready":
            raise QuotationValidationError("Còn thiếu thông tin: " + ", ".join(prepared["missing_fields"]))
        values = prepared["values"]
        values["state"] = "draft"
        company_id = values["company_id"]
        order_id = self._rpc("sale.order", "create", [values], company_id)
        self._records.clear()  # Read-back must observe fresh provider state after the write.
        if not isinstance(order_id, int) or isinstance(order_id, bool):
            raise OdooNetworkError("Odoo không trả ID rõ ràng sau create; phải đối soát trước khi thử lại.")
        if on_created:
            try:
                on_created(order_id, company_id)
            except Exception as exc:
                raise OdooNetworkError('Odoo đã trả ID nhưng chưa lưu được liên kết theo dõi; không tự tạo lại.') from exc
        try:
            result = self._readback(order_id, company_id)
        except (OdooError, ValueError) as exc:
            raise OdooNetworkError("Đã gửi create nhưng chưa đọc lại được báo giá; cần đối soát.") from exc
        differences = []
        if result.get("state") != "draft":
            differences.append("state")
        if _m2o(result.get("partner_id")) != values["partner_id"]:
            differences.append("partner_id")
        if _m2o(result.get("company_id")) != company_id:
            differences.append("company_id")
        actual_lines = result.get("lines", [])
        if len(actual_lines) != len(values["order_line"]):
            differences.append("line_count")
        else:
            for index, (actual, command) in enumerate(zip(actual_lines, values["order_line"], strict=True)):
                expected = command[2]
                uom_field = "product_uom_id" if "product_uom_id" in expected else "product_uom"
                tax_field = 'tax_ids' if 'tax_ids' in expected else 'tax_id'
                for name in ("product_id", "product_uom_qty", "price_unit", tax_field, uom_field, *(['customer_lead'] if 'customer_lead' in expected else [])):
                    actual_value = actual.get(name)
                    expected_value = expected.get(name)
                    if name in {"product_id", "product_uom", "product_uom_id"}:
                        actual_value = _m2o(actual_value)
                    elif name in {"tax_id", "tax_ids"}:
                        actual_value = sorted(actual_value or [])
                        expected_value = sorted(expected_value[0][2])
                    elif name in {"price_unit", "product_uom_qty", "customer_lead"}:
                        if math.isclose(float(actual_value or 0), expected_value, rel_tol=1e-9, abs_tol=1e-6):
                            continue
                    if actual_value != expected_value:
                        differences.append(f"lines.{index}.{name}")
        result["verification"] = "check_required" if differences else "matched"
        result["differences"] = differences
        result["notice"] = (
            "Báo giá nháp đã tạo; tổng tiền chính thức lấy từ Odoo. Kiểm tra khác biệt trước khi gửi khách hàng."
            if result.get("state") == "draft" else
            "Chứng từ đã được tạo nhưng trạng thái đọc lại không phải nháp. Hãy kiểm tra trên Odoo trước khi thực hiện tiếp."
        )
        return result
