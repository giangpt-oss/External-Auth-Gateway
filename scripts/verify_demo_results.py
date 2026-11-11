"""Read back the latest completed demo quote; never replay a provider write."""
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
from app.reporting.context import authenticated_context  # noqa: E402
from app.reporting.models import ReportArtifact, ReportJob  # noqa: E402
from app.reporting.schemas import TelegramScope  # noqa: E402


def main():
    settings = Settings()
    sf = sessionmaker(bind=create_engine(settings.DATABASE_URL))
    with sf() as db:
        job = db.query(QuoteExecutionJob).filter_by(status='completed').order_by(QuoteExecutionJob.started_at.desc()).first()
        if not job:
            raise RuntimeError('No completed quotation to verify')
        scope = TelegramScope.model_validate_json(job.scope_json)
        user_id, operation_id = job.user_id, job.operation_id
        operations = db.query(ActionOperation).filter_by(user_id=user_id, operation_id=operation_id).all()
        assert len(operations) == 1 and operations[0].status == 'completed'
        stored = json.loads(operations[0].result_json)['result']
        evidence = {'operation_id': operation_id, 'ledger_count': len(operations), 'notification': job.notified,
            'stored_verification': stored.get('verification'), 'stored_differences': stored.get('differences'), 'reports': []}
        for report_id in ('rpt_3a36f654c26d77149639838c', 'rpt_71752859ae304f37ebd543a5'):
            report = db.query(ReportJob).filter_by(id=report_id, user_id=user_id, binding_id=scope.telegram_binding_id).one()
            files = db.query(ReportArtifact).filter_by(report_id=report_id, user_id=user_id, binding_id=scope.telegram_binding_id).all()
            assert report.status == 'completed' and report.delivery_status == 'delivered'
            evidence['reports'].append({'report_id': report.id, 'status': report.status, 'delivery': report.delivery_status,
                'files': [{'filename': item.filename, 'bytes': len(item.content)} for item in files]})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=sf)))
    with authenticated_context(request, user_id, scope) as ctx:
        service = QuotationService(ctx.service.client, ctx.user.odoo_uid, ctx.secret)
        result = service.reconcile(operation_id)
        assert result['status'] == 'completed' and result['result']['id'] == stored['id']
        evidence['odoo'] = result['result']
        evidence['unique_record_for_operation'] = True  # reconcile requires exactly one matching origin.
    output = Path(__file__).resolve().parents[1] / 'data' / 'demo-final-evidence.json'
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'quotation': evidence['odoo']['name'], 'id': evidence['odoo']['id'],
        'verification': evidence['stored_verification'], 'unique_record': True,
        'notification': evidence['notification'], 'reports_delivered': len(evidence['reports'])}))


if __name__ == '__main__':
    main()
