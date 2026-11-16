"""Actual TCP report/restart at Nanjing midnight; score admission is synthetic."""
from datetime import datetime, timedelta, timezone

from home_study_agent.family_clock import FAMILY_ZONE, family_today
from home_study_agent.period_reports import PeriodReportService
from test_period_written_observations_http import report_case, generate
from test_writing_calibrated_gate import authority
from test_writing_stage_review_consumers import advance_clock
import test_school_open_reviews as school


def test_monday_original_reaches_new_week_over_http_and_survives_full_backend_restart(report_case, monkeypatch):
    db, client = report_case[:2]
    today = family_today()
    monday = today + timedelta(days=7 - today.weekday())
    advance_clock(monkeypatch, datetime.combine(monday, datetime.min.time(), tzinfo=FAMILY_ZONE).astimezone(timezone.utc))
    token = client.post('/v1/parent/unlock', json={'pin': '8264'})
    assert token.status_code == 200, token.text
    headers = {'X-HSC-Parent': token.json()['parent_token']}
    evidence, _, _, _ = authority(monkeypatch, report_case, 0)
    report = generate(client, headers, 'weekly')
    observed = report['content']['learning_summary']['qualified_observations']
    assert report['period_start'] == monday.isoformat()
    assert len(observed) == 1 and observed[0]['date'] == monday.isoformat()
    assert observed[0]['passed'] is False and observed[0]['mastery_claim_allowed'] is False
    previous = PeriodReportService(db).generate(school.CHILD, 'weekly', monday - timedelta(days=1))
    assert previous['content']['learning_summary']['qualified_observations'] == []
    client = client.school_reopen()
    token = client.post('/v1/parent/unlock', json={'pin': '8264'})
    assert token.status_code == 200, token.text
    headers = {'X-HSC-Parent': token.json()['parent_token']}
    response = client.get(f'/v1/children/{school.CHILD}/period-reports', headers=headers)
    assert response.status_code == 200, response.text
    reopened = next(r for r in response.json()['items'] if r['period_report_id'] == report['period_report_id'])
    assert reopened['content']['learning_summary']['qualified_observations'] == observed
    with db.connect() as c:
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?', (evidence,)).fetchone()[0] is None
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
