import json
from datetime import date

from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from test_parent_supply_followup_http import supplied, daytime, prepared
from test_supply_gap_resume_http import CHILD


def test_valid_json_list_history_is_controlled_unavailable_and_retry_keeps_original(supplied, monkeypatch):
    case = supplied
    sid, *_ = prepared(case, monkeypatch)
    with case['db'].connect() as c:
        day = c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0]
    report = PeriodReportService(case['db']).generate(CHILD, 'weekly', date.fromisoformat(day))
    snapshot = ParentReportService(case['db']).create_from_period(report)
    path = f"/v1/parent/children/{CHILD}/period-supply-followups/{snapshot['snapshot_id']}"
    assert case['client'].get(path).status_code == 200
    with case['db'].connect() as c:
        old = c.execute('SELECT content_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (snapshot['snapshot_id'],)).fetchone()[0]
        bad = json.loads(old)
        bad['summary']['supply_history'] = []
        c.execute('UPDATE parent_report_snapshot SET content_json=? WHERE snapshot_id=?',
            (json.dumps(bad), snapshot['snapshot_id']))
        c.commit()
    response = case['client'].get(path)
    assert response.status_code == 503, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['detail']['code'] == 'WORK_SOURCE_UNAVAILABLE'
    with case['db'].connect() as c:
        assert json.loads(c.execute('SELECT content_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (snapshot['snapshot_id'],)).fetchone()[0])['summary']['supply_history'] == []
        c.execute('UPDATE parent_report_snapshot SET content_json=? WHERE snapshot_id=?',
            (old, snapshot['snapshot_id']))
        c.commit()
    assert case['client'].get(path).status_code == 200
