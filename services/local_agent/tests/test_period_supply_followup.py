import json
from datetime import date

import pytest

from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from test_parent_supply_followup_http import supplied, daytime, prepared
from test_supply_gap_resume_http import CHILD


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_period_has_exact_root_history_and_separate_current_read(supplied, monkeypatch, kind):
    case = supplied
    sid, root, daily, _, _, attempts = prepared(case, monkeypatch)
    with case['db'].connect() as c:
        today = c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0]
    periods = PeriodReportService(case['db'])
    report = periods.generate(CHILD, kind, date.fromisoformat(today))
    history = report['content'].get('supply_history')
    assert history and history['status'] == 'available', 'Period report drops exact original obligations'
    original = [r for r in history['items'] if r['root_obligation_id'] == root['decision_id']]
    assert len(original) == 1, 'Do not sum each daily carryover as a new obligation'
    assert original[0]['original_date'] < today
    assert original[0]['original_reason'] == root['reason']
    assert len([r for r in history['items'] if '写作' in r['title']]) >= 4
    snapshot = ParentReportService(case['db']).create_from_period(report)
    path = f"/v1/parent/children/{CHILD}/period-supply-followups/{snapshot['snapshot_id']}"
    response = case['client'].get(path)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    data = response.json()
    current = next(r['current'] for r in data['items'] if r['historical']['root_obligation_id'] == root['decision_id'])
    assert current['status'] == 'ready'
    assert 'current' not in report['content'] and 'ready' not in json.dumps(history)
    assert data['mastery_claim_allowed'] is False and data['local_only'] is True
    assert case['client'].get(path.replace(CHILD, 'child-junior-3')).status_code == 404
    token = case['client'].headers.pop('X-HSC-Parent')
    assert case['client'].get(path).status_code == 401
    case['client'].headers['X-HSC-Parent'] = token
    before = json.dumps(snapshot['content'], sort_keys=True)
    result = case['client'].post(case['base'] + f"/{case['published']['candidate_id']}/withdraw", json={
        'expected_version': 4, 'action_id': 'period-withdraw-' + kind, 'note': '合成原题来源撤回'})
    assert result.status_code == 200, result.text
    changed = case['client'].get(path).json()
    now = next(r['current'] for r in changed['items'] if r['historical']['root_obligation_id'] == root['decision_id'])
    assert now['status'] in {'missing_source', 'source_changed'}
    assert now['version'] != current['version']
    with case['db'].connect() as c:
        content = json.loads(c.execute('SELECT content_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (snapshot['snapshot_id'],)).fetchone()[0])
        assert json.dumps(content, sort_keys=True) == before
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
    assert periods.get(report['period_report_id'])['content']['supply_history'] == history
