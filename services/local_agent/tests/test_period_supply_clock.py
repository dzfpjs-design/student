from datetime import date, datetime, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.repository import StudyRepository


@pytest.mark.parametrize('instant,expected_week,expected_month', [
    ('2026-08-31T16:01:00+00:00', '2026-08-31', '2026-09-01'),
    ('2026-09-06T16:01:00+00:00', '2026-09-07', '2026-09-01'),
    ('2026-12-31T16:01:00+00:00', '2026-12-28', '2027-01-01'),
])
def test_default_period_calendar_is_nanjing_and_history_override_survives(tmp_path, monkeypatch, instant, expected_week, expected_month):
    from home_study_agent import period_reports
    fixed = datetime.fromisoformat(instant)
    class HostDate(date):
        @classmethod
        def today(cls):
            return fixed.astimezone(timezone.utc).date()
    monkeypatch.setattr(period_reports, 'date', HostDate)
    monkeypatch.setattr(period_reports, 'utc_now', lambda: instant)
    db = Database(tmp_path/'period-clock.db'); db.initialize()
    StudyRepository(db).seed_family(); LearningStateService(db).seed_catalog()
    service = PeriodReportService(db)
    refreshed = service.refresh_current('child-primary-4', require_evidence=False)
    assert not refreshed['failures'], refreshed
    by_kind = {r['report_type']:r for r in refreshed['reports']}
    assert by_kind['weekly']['period_start'] == expected_week
    assert by_kind['monthly']['period_start'] == expected_month
    assert service.generate('child-primary-4', 'monthly')['period_start'] == expected_month
    old = service.generate('child-primary-4', 'monthly', date(2026, 7, 12))
    assert old['period_start'] == '2026-07-01' and old['content']['as_of_date'] == '2026-07-12'
