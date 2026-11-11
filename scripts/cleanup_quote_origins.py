"""Migrate exact legacy quotation markers to Gateway links. --apply requires user authorization."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models import ActionOperation  # noqa: E402
from app.quotations.execution import QuoteExecutionJob  # noqa: E402
from app.quotations.service import QuotationService  # noqa: E402
from app.quotations.tracking import (  # noqa: E402
    QuotationRecordLink,
    begin_tracking,
    record_created,
)
from app.reporting.context import authenticated_context  # noqa: E402
from app.reporting.schemas import TelegramScope  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    settings = Settings()
    engine = create_engine(settings.DATABASE_URL)
    sf = sessionmaker(bind=engine)
    if args.apply:
        QuotationRecordLink.__table__.create(engine, checkfirst=True)
    with sf() as db:
        job = db.query(QuoteExecutionJob).order_by(QuoteExecutionJob.started_at.desc()).first()
        user_id, binding = job.user_id, TelegramScope.model_validate_json(job.scope_json)
        operations = [(op.operation_id, op.telegram_binding_id) for op in db.query(ActionOperation).filter_by(user_id=user_id, action='sales.quotation.create').all()]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=sf)))
    evidence = []
    journal = Path('data/quotation-origin-cleanup.jsonl')
    with authenticated_context(request, user_id, binding) as ctx:
        service = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
        _, companies = service._companies()
        for operation_id, operation_binding in operations:
            if operation_binding != binding.telegram_binding_id:
                continue
            marker = 'AGENT:' + operation_id
            rows = service.client.execute(service.uid, service.secret, 'sale.order', 'search_read',
                [[('origin', '=', marker), ('company_id', 'in', companies)]],
                {'fields': ['id', 'name', 'origin', 'company_id', 'state'], 'limit': 2, 'context': {'allowed_company_ids': companies}})
            if len(rows) != 1:
                continue
            row = rows[0]
            entry = {'operation_id': operation_id, 'quotation': row['name'], 'id': row['id'], 'before': row['origin'], 'after': None, 'applied': False}
            if args.apply:
                begin_tracking(sf, user_id, operation_id, ctx.user.tenant, ctx.user.database)
                record_created(sf, user_id, operation_id, row['id'], row['company_id'][0])
                with journal.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
                current = service._rpc('sale.order', 'read', [[row['id']]], row['company_id'][0], fields=['origin'])
                if not current or current[0].get('origin') != marker:
                    entry['skipped'] = 'origin_changed'
                else:
                    service._rpc('sale.order', 'write', [[row['id']], {'origin': False}], row['company_id'][0])
                    checked = service._rpc('sale.order', 'read', [[row['id']]], row['company_id'][0], fields=['origin'])
                    entry.update(applied=not checked[0].get('origin'), after=checked[0].get('origin'))
                    if not entry['applied']:
                        raise RuntimeError('Origin cleanup read-back mismatch')
                with journal.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
            evidence.append(entry)
        print(json.dumps({'mode': 'apply' if args.apply else 'read_only', 'quotations': evidence}, ensure_ascii=False), flush=True)
        # Exercise the revised preparation without creating or modifying a quotation.
        with sf() as db:
            latest = db.query(QuoteExecutionJob).filter_by(user_id=user_id, status='completed').order_by(QuoteExecutionJob.started_at.desc()).first()
            old = json.loads(latest.payload_json)
        payload = {'partner_id': old['partner_id'], 'lines': [{'product_id': old['lines'][0]['product_id'], 'quantity': 1, 'use_catalog_price': True, 'tax_ids': []}]}
        prepared = service.prepare(payload)
        output = Path('data/quote-revised-live-preview.json')
        output.write_text(json.dumps(prepared, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(json.dumps({'preparation_status': prepared['status'], 'missing': prepared['missing_fields'],
            'optional_fields': {row['name']: row['value'] for row in prepared['preview']['fields'] if row['name'] in ('origin', 'validity_date', 'payment_term_id', 'note', 'pricelist_id')}}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
