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
