import pytest
from pydantic import ValidationError

from app.reporting.schemas import ReportDeliveryRequest, ReportRequest


@pytest.mark.parametrize('unsupported', [{'metric': 'profit'}, {'top_n': 10}, {'custom_columns': ['margin']}])
def test_unsupported_report_requirements_are_rejected(unsupported):
    with pytest.raises(ValidationError, match='extra_forbidden'):
        ReportRequest(telegram_user_id=7, chat_id=7, telegram_binding_id='fixture', report_type='sales',
            date_from='2026-10-01', date_to='2026-10-31', **unsupported)


def test_redelivery_cannot_silently_accept_new_filter():
    with pytest.raises(ValidationError, match='extra_forbidden'):
        ReportDeliveryRequest(telegram_user_id=7, chat_id=7, telegram_binding_id='fixture', partner_id=999)


def test_employee_weekly_report_defaults_and_exports():
    from app.reporting.export import employee_weekly_chat_summary, export_xlsx
    from app.reporting.word_export import export_docx

    req = ReportRequest(
        telegram_user_id=1, chat_id=1, telegram_binding_id='fixture',
        report_type='employee_weekly', employee_name='Nguyễn Văn Nam',
    )
    assert req.date_from is not None
    assert req.date_to is not None
    assert req.employee_name == 'Nguyễn Văn Nam'

    data = {
        'report_type': 'employee_weekly',
        'parameters': req.model_dump(mode='json'),
        'company_ids': [1],
        'started_at': '2026-11-11T00:00:00Z',
        'finished_at': '2026-11-11T00:00:05Z',
        'warnings': [],
        'sections': [
            {
                'type': 'employee_profile',
                'title': 'Thông tin nhân sự',
                'model': 'hr.employee',
                'count': 1,
                'summary': [{
                    'name': 'Nguyễn Văn Nam',
                    'job_title': 'Chuyên viên kinh doanh',
                    'department': 'Phòng Kinh doanh',
                    'manager': 'Trần Văn Sếp',
                    'email': 'nam.nv@congty.com',
                    'phone': '0912345678',
                }],
                'rows': [],
            },
            {
                'type': 'tasks',
                'title': 'Công việc & Dự án',
                'model': 'project.task',
                'count': 2,
                'summary': [{'total_tasks': 2, 'done_count': 1, 'overdue_count': 1, 'inprogress_count': 0, 'done_rate': '50.0%'}],
                'rows': [{'name': 'Task 1', 'project_id': [1, 'Dự án A'], 'stage_id': [1, 'Mới']}],
            },
        ],
    }
    chat = employee_weekly_chat_summary(data)
    assert 'BÁO CÁO CÔNG VIỆC TUẦN CỦA NHÂN VIÊN' in chat
    assert 'Nguyễn Văn Nam' in chat

    docx_bytes = export_docx(data)
    assert len(docx_bytes) > 1000

    xlsx_bytes = export_xlsx(data)
    assert len(xlsx_bytes) > 1000

