"""Report schedules use the family's Nanjing clock, not the host OS timezone."""
from datetime import datetime, timezone

import pytest

from home_study_agent.report_delivery import _next_quiet_end
from test_daily_report_evidence_versions import flow, CHILD


@pytest.mark.parametrize('utc_hour,quiet', [(12, False), (13, True), (22, True), (23, False)])
def test_quiet_hours_are_nanjing_on_a_utc_host(flow, monkeypatch, utc_hour, quiet):
    delivery = flow[3]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2026, 9, 11, utc_hour, 0, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr('home_study_agent.report_delivery.datetime', Clock)
    assert delivery._in_quiet_hours(CHILD) is quiet


@pytest.mark.parametrize('instant,expected', [
    ('2026-09-11T14:00:00+00:00', '2026-09-11T23:00:00+00:00'),
    ('2026-09-11T22:59:59+00:00', '2026-09-11T23:00:00+00:00'),
    ('2026-09-11T23:00:00+00:00', '2026-09-12T23:00:00+00:00'),
])
def test_next_quiet_end_is_persisted_as_utc_for_nanjing(instant, expected):
    assert _next_quiet_end('07:00', datetime.fromisoformat(instant)) == expected
