from home_study_agent.report_source_fingerprint import report_source_fingerprint
from home_study_agent.parent_supply_followup import report_supply_facts
from test_parent_supply_followup_http import supplied, daytime, prepared
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.repository import StudyRepository
import json


def test_unselected_original_is_part_of_save_authority_not_today_followup(supplied, monkeypatch):
    case = supplied
    sid, root, report, snapshot, service, attempts = prepared(case, monkeypatch)
    with case['db'].connect() as c:
        c.execute('BEGIN')
        before = report_source_fingerprint(c, case['sid'])
        facts = report_supply_facts(c, 'child-primary-4', case['sid'])
        c.execute('UPDATE plan_constraint_decision SET reason=? WHERE decision_id=?',
            ('历史来源经本地修订后说明不同', root['decision_id']))
        assert report_supply_facts(c, 'child-primary-4', case['sid']) != facts
        assert report_source_fingerprint(c, case['sid']) != before
        c.rollback()
        c.execute('BEGIN')
        assert report_source_fingerprint(c, case['sid']) == before
        # Today's derived follow-up is not a fact of the closed original day.
        c.execute('UPDATE plan_constraint_decision SET reason=? WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?)',
            ('今天新的安排说明', sid))
        assert report_supply_facts(c, 'child-primary-4', case['sid']) == facts
        assert report_source_fingerprint(c, case['sid']) == before
        c.rollback()
        for invalid in ('{broken', 'null', '[]', '{"supply_carryover":[1]}',
                        '{"planning_evidence":[1]}'):
            c.execute('BEGIN')
            c.execute('UPDATE plan_constraint_decision SET metadata_json=? WHERE decision_id=?',
                (invalid, root['decision_id']))
            assert report_source_fingerprint(c, case['sid']) != before
            c.rollback()


def test_old_approval_cannot_authorize_revised_historical_obligation(supplied, monkeypatch):
    case = supplied
    sid, root, report, snapshot, service, attempts = prepared(case, monkeypatch)
    service.approve(snapshot['snapshot_id'])
    with case['db'].connect() as c:
        c.execute('BEGIN')
        old = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        assert ParentReportService.snapshot_source_current(c, old)
        c.execute('UPDATE plan_constraint_decision SET reason=? WHERE decision_id=?',
            ('历史原事项经核对后修订说明', root['decision_id']))
        c.commit()
    with case['db'].connect() as c:
        c.execute('BEGIN')
        assert not ParentReportService.snapshot_source_current(c, old)
        assert c.execute('SELECT content_json FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()[0] == old['content_json']
    current = StudyRepository(case['db']).get_daily_report(report['report_id'])
    new_snapshot = service.create_from_daily(current)
    assert new_snapshot['snapshot_id'] != snapshot['snapshot_id'] and new_snapshot['status'] == 'draft'
    assert service.get(snapshot['snapshot_id'])['status'] == 'revoked'
    assert current['summary']['supply_obligations_as_of'] != json.loads(old['content_json'])['summary']['supply_obligations_as_of']
