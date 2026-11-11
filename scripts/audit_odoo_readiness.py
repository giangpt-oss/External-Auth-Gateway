"""Read-only live audit. Never calls create/write/unlink or business buttons."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.business.catalog import ACTIONS
from app.business.service import enabled
from app.config import Settings
from app.quotations.jobs import QuotePreparationJob
from app.reporting.context import authenticated_context
from app.reporting.engine import capability, generate
from app.reporting.schemas import ReportRequest, TelegramScope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', action='store_true', help='Read real report datasets without writing jobs or sending files.')
    parser.add_argument('--verify-exports', action='store_true', help='Also exercise existing Word/Excel exporters and reopen the bytes in memory; no delivery.')
    args = parser.parse_args()
    settings = Settings()
    sf = sessionmaker(bind=create_engine(settings.DATABASE_URL))
    with sf() as db:
        job = db.query(QuotePreparationJob).order_by(QuotePreparationJob.created_at.desc()).first()
        if not job:
            raise RuntimeError('No verified quotation session available for audit')
        uid, scope = job.user_id, TelegramScope.model_validate_json(job.scope_json)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=sf)))
    result = {'business_catalog': dict(Counter(d.module for d in ACTIONS.values())),
              'enabled_business_actions': [d.name for d in ACTIONS.values() if enabled(settings, d)],
              'flags': {key: value for key, value in settings.model_dump().items() if key.startswith('ENABLE_')},
              'models': {}, 'writes': 0}
    models = {
        'sale.order': ['state', 'validity_date', 'date_order', 'pricelist_id'],
        'sale.order.line': ['tax_id', 'tax_ids', 'product_uom', 'product_uom_id', 'customer_lead', 'discount'],
        'purchase.order': ['state', 'date_order', 'date_planned', 'picking_type_id'],
        'purchase.order.line': ['tax_id', 'tax_ids', 'taxes_id', 'product_uom', 'product_uom_id', 'date_planned'],
        'crm.lead': ['type', 'stage_id', 'lost_reason_id', 'probability', 'active'],
        'res.partner': ['type', 'parent_id', 'is_company', 'active'],
        'product.product': ['type', 'tracking', 'list_price', 'product_template_attribute_value_ids'],
        'stock.picking': ['state', 'move_ids', 'move_ids_without_package', 'move_line_ids'],
        'stock.move': ['product_uom', 'product_uom_id', 'quantity', 'product_uom_qty', 'state'],
        'stock.move.line': ['quantity', 'qty_done', 'lot_id', 'picked'],
        'stock.quant': ['inventory_quantity', 'quantity', 'inventory_quantity_set'],
    }
    with authenticated_context(request, uid, scope) as ctx:
        for model, names in models.items():
            try:
                fields = ctx.read_fields(model)
                rights = {op: bool(ctx.call(model, 'check_access_rights', [op, False])) for op in ('read', 'create', 'write', 'unlink')}
                result['models'][model] = {'rights_for_current_account': rights,
                    'fields': {name: {key: fields[name].get(key) for key in ('type', 'required', 'readonly', 'selection')} for name in names if name in fields},
                    'absent_fields': [name for name in names if name not in fields],
                    'studio_approval_fields': [name for name in fields if name.startswith('x_') and 'approval' in name]}
                if model in ('sale.order', 'purchase.order', 'crm.lead', 'res.partner', 'stock.picking'):
                    # Read form metadata to compare methods and widgets; do not call them.
                    view = ctx.call(model, 'get_view', [], {'view_type': 'form'})
                    from xml.etree import ElementTree as ET
                    root = ET.fromstring(view['arch'])
                    result['models'][model]['form_buttons'] = [{'name': b.get('name'), 'label': b.get('string'),
                        'type': b.get('type'), 'groups': b.get('groups')} for b in root.findall('.//button')]
            except Exception as exc:
                result['models'][model] = {'error': type(exc).__name__ + ': ' + str(exc).splitlines()[-1][:300]}
            print(model + ': read-only audit complete', flush=True)
        try:
            result['report_capabilities'] = capability(ctx)
        except Exception as exc:
            result['report_capabilities'] = {'error': type(exc).__name__}
        # Confirm the already-created record without creating anything else.
        result['quotation_evidence'] = ctx.call('sale.order', 'search_read', [[('id', '=', 83), ('origin', '=', 'AGENT:op_3f7b5acbf4d276556a643689')]],
            {'fields': ['id', 'name', 'state', 'amount_total', 'currency_id', 'validity_date'], 'limit': 1})
        if args.reports or args.verify_exports:
            result['report_generation_checks'] = {}
            for kind in ('sales', 'quotations', 'purchases', 'purchase_rfqs', 'crm', 'contacts', 'inventory', 'transfers'):
                try:
                    period = {} if kind in ('crm', 'contacts', 'inventory') else {'date_from': '2026-10-01', 'date_to': '2026-11-03'}
                    req = ReportRequest(**scope.model_dump(), report_type=kind, **period)
                    data = generate(ctx, req, lambda *a, **kw: None)
                    result['report_generation_checks'][kind] = {'status': 'read_success',
                        'sections': [{'type': s['type'], 'count': s['count']} for s in data['sections']],
                        'warnings': data['warnings'], 'artifact_render_and_delivery': 'not_tested_in_this_audit'}
                    if args.verify_exports:
                        from io import BytesIO

                        from docx import Document
                        from openpyxl import load_workbook

                        from app.reporting.export import export_xlsx
                        from app.reporting.word_export import export_docx
                        docx_bytes, xlsx_bytes = export_docx(data), export_xlsx(data)
                        document = Document(BytesIO(docx_bytes))
                        workbook = load_workbook(BytesIO(xlsx_bytes), read_only=True)
                        assert document.tables and workbook.sheetnames
                        result['report_generation_checks'][kind]['exports'] = {
                            'docx_bytes': len(docx_bytes), 'word_tables': len(document.tables),
                            'xlsx_bytes': len(xlsx_bytes), 'sheets': workbook.sheetnames,
                            'reopened': True, 'visual_render': 'not_checked', 'delivery': 'not_attempted'}
                        workbook.close()
                except Exception as exc:
                    result['report_generation_checks'][kind] = {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc).splitlines()[-1][:300]}
                print('report ' + kind + ': ' + result['report_generation_checks'][kind]['status'], flush=True)
    output = Path(__file__).resolve().parents[1] / 'data' / 'odoo-readiness-audit.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('Read-only evidence: ' + str(output))


if __name__ == '__main__':
    main()
