"""Read-only Studio approval discovery for the current linked quotation account."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402
from app.quotations.execution import QuoteExecutionJob  # noqa: E402
from app.reporting.context import authenticated_context  # noqa: E402
from app.reporting.schemas import TelegramScope  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-existing', action='store_true')
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    settings = Settings()
    sf = sessionmaker(bind=create_engine(settings.DATABASE_URL))
    with sf() as db:
        job = db.query(QuoteExecutionJob).order_by(QuoteExecutionJob.started_at.desc()).first()
        user_id = job.user_id
        binding = TelegramScope.model_validate_json(job.scope_json)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=sf)))
    with authenticated_context(request, user_id, binding) as ctx:
        client = ctx.service.client
        if args.prepare:
            from app.business.service import BusinessService
            result = BusinessService(ctx).prepare('business.sales.request_approval', {'target_id': 85})
            Path('data/quotation-approval-live-prepare.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'status': result['status'], 'action': result['action'], 'missing': result['missing'],
                              'target': result['preview']['target']['name'],
                              'approval_state': result['preview'].get('approval_state')}, ensure_ascii=True))
            return
        if args.sample_existing:
            entries = ctx.call('studio.approval.entry', 'search_read', [[('rule_id', '=', 14)]],
                               {'fields': ['res_id'], 'limit': 1, 'order': 'id desc'})
            if entries:
                result = client.studio_approval_spec(ctx.user.odoo_uid, ctx.secret, entries[0]['res_id'],
                                                    'action_confirm', ctx.default_company_id)
                print(json.dumps(result, ensure_ascii=True))
            return
        response = httpx.post(client.url + '/jsonrpc', json={
            'jsonrpc': '2.0', 'method': 'call', 'id': 1,
            'params': {'service': 'object', 'method': 'execute_kw', 'args': [
                client.db, ctx.user.odoo_uid, ctx.secret, 'studio.approval.rule',
                'get_approval_spec', [[{'model': 'sale.order', 'method': 'action_confirm', 'res_id': 85}]],
                {'context': {'allowed_company_ids': [ctx.default_company_id]}}]}}, timeout=30)
        response.raise_for_status()
        result = response.json()
        Path('data/sales-approval-spec.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
