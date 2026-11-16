import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.repository import StudyRepository
from test_learning_runtime_http import NoCloud
from test_pet_task_execution import generated, CHILD, command
from test_writing_stage_review_consumers import advance_clock


def test_real_original_answer_completed_is_not_historical_ready_debt_after_restart(tmp_path, monkeypatch):
    case = generated(tmp_path, 'reading')
    db, sid, pid, _, original = case
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 43900)) as http:
        setup = http.post('/v1/parent/setup', json={'pin':'7391'})
        assert setup.status_code == 200, setup.text
        http.headers['X-HSC-Parent'] = setup.json()['parent_token']
        with db.connect() as c:
            day = c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0]
        periods = PeriodReportService(db)
        report = periods.generate(CHILD, 'weekly', date.fromisoformat(day))
        snapshot = ParentReportService(db).create_from_period(report)
        path = f"/v1/parent/children/{CHILD}/period-supply-followups/{snapshot['snapshot_id']}"
        response = http.get(path)
        assert response.status_code == 200, response.text
        before = response.json()
        row = next(r for r in before['items'] if (r['current'] or {}).get('plan_item_id') == pid)
        root = row['historical']['root_obligation_id']
        assert row['historical']['historical_status'] == 'unfulfilled'
        frozen = report['content']['supply_history']
        turn = command(case, child_id=CHILD)
        execution = http.post(f'/v1/agent-turns/{turn}/execute', json={})
        assert execution.status_code == 200, execution.text
        prompt = execution.json()['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id'] == original['diagnostic_item_id']
        answer = http.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id':prompt['prompt_id'], 'item_version':prompt['item_version'],
            'answer':'I am not sure', 'client_action_id':'period-original-answer'})
        assert answer.status_code == 200 and answer.json()['status'] == 'answered', answer.text
        completed = http.post(f'/v1/plan-items/{pid}/complete', json={
            'actual_minutes':1, 'client_action_id':'period-practice-complete'})
        assert completed.status_code == 200, completed.text
        current = http.get(path).json()
        now = next(r for r in current['items'] if r['historical']['root_obligation_id'] == root)
        assert now['current']['status'] == 'practice_completed'
        assert now['historical'] == row['historical']
        assert current['unfulfilled_count'] == before['unfulfilled_count'] - 1
        assert periods.get(report['period_report_id'])['content']['supply_history'] == frozen
        with db.connect() as c:
            original_events = [tuple(r) for r in c.execute('SELECT * FROM plan_item_event WHERE plan_item_id=?', (pid,))]
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 43901)) as http:
        token = http.post('/v1/parent/unlock', json={'pin':'7391'}).json()['parent_token']
        http.headers['X-HSC-Parent'] = token
        loaded = http.get(path)
        assert loaded.status_code == 200, loaded.text
        now = next(r for r in loaded.json()['items'] if r['historical']['root_obligation_id'] == root)
        assert now['current']['status'] == 'practice_completed'
        assert now['historical']['historical_status'] == 'unfulfilled'
        assert now['current']['mastery_claim_allowed'] is False
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM plan_item_event WHERE plan_item_id=?', (pid,))] == original_events
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 1


def test_period_expiry_legacy_unknown_and_corrupt_source_are_not_zero(tmp_path, monkeypatch):
    case = generated(tmp_path, 'reading')
    db, sid, pid, _, _ = case
    from home_study_agent.parent_work_queue import ParentWorkQueueService
    service = PeriodReportService(db)
    with db.connect() as c:
        day = c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0]
    report = service.generate(CHILD, 'monthly', date.fromisoformat(day))
    snapshot = ParentReportService(db).create_from_period(report)
    queue = ParentWorkQueueService(db)
    current = queue.period_supply_followups(CHILD, snapshot['snapshot_id'])
    root = next(r['historical']['root_obligation_id'] for r in current['items'] if (r['current'] or {}).get('plan_item_id') == pid)
    advance_clock(monkeypatch, datetime.now(timezone.utc) + timedelta(days=3))
    current = queue.period_supply_followups(CHILD, snapshot['snapshot_id'])
    row = next(r for r in current['items'] if r['historical']['root_obligation_id'] == root)
    assert row['current']['status'] == 'needs_plan'
    assert current['unfulfilled_count'] is not None and current['unfulfilled_count'] > 0
    assert service.get(report['period_report_id'])['content']['supply_history'] == report['content']['supply_history']
    with db.connect() as c:
        c.execute('BEGIN')
        c.execute('UPDATE plan_constraint_decision SET metadata_json=? WHERE decision_id=?', ('{broken',root))
        assert ParentReportService.source_qualification_fingerprint(c, CHILD, 'period_report', report['period_report_id']) is None
        c.rollback()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0


def test_period_legacy_missing_history_and_failed_refresh_preserve_old_snapshot(tmp_path):
    case = generated(tmp_path, 'reading')
    db, sid, pid, _, _ = case
    with db.connect() as c:
        day = c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0]
    service = PeriodReportService(db)
    report = service.generate(CHILD, 'monthly', date.fromisoformat(day))
    snapshots = ParentReportService(db)
    # A serialized legacy aggregate has no original-root claim. Do not backfill
    # its frozen snapshot by guessing today's rows or turn absence into zero.
    legacy = {**report, 'content':{k:v for k,v in report['content'].items() if k!='supply_history'}}
    # Restore the actual legacy source, not a modified caller copy of a newer
    # aggregate. Freezing a copy that differs from storage must be rejected.
    with db.connect() as c:
        c.execute('UPDATE period_report SET content_json=? WHERE period_report_id=?',
            (json.dumps(legacy['content']), report['period_report_id']))
        c.commit()
    old = snapshots.create_from_period(legacy)
    from home_study_agent.parent_work_queue import ParentWorkQueueService
    queue = ParentWorkQueueService(db)
    unknown = queue.period_supply_followups(CHILD, old['snapshot_id'])
    assert unknown['status'] == 'unavailable' and unknown['unfulfilled_count'] is None
    with db.connect() as c:
        before = dict(c.execute('SELECT * FROM period_report WHERE period_report_id=?', (report['period_report_id'],)).fetchone())
        c.execute("CREATE TRIGGER fail_period_refresh BEFORE UPDATE ON period_report BEGIN SELECT RAISE(ABORT,'synthetic-write-failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic-write-failure'):
        service.generate(CHILD, 'monthly', date.fromisoformat(day))
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM period_report WHERE period_report_id=?', (report['period_report_id'],)).fetchone()) == before
        assert c.execute('SELECT status FROM period_report_refresh_state WHERE child_id=? AND report_type=?', (CHILD,'monthly')).fetchone()[0] == 'failed'
        c.execute('DROP TRIGGER fail_period_refresh'); c.commit()
    retried = service.generate(CHILD, 'monthly', date.fromisoformat(day))
    assert retried['period_report_id'] == report['period_report_id']
    assert queue.period_supply_followups(CHILD, old['snapshot_id'])['status'] == 'unavailable'


@pytest.mark.parametrize('status,event,expected', [('cancelled','cancelled','cancelled'),('rescheduled','rescheduled','explicitly_deferred')])
def test_recorded_terminal_or_deferral_event_stays_historical_after_reopen(tmp_path, status, event, expected):
    from home_study_agent.parent_supply_followup import period_supply_facts
    from home_study_agent.database import Database
    case = generated(tmp_path, 'reading')
    db, sid, pid, _, _ = case
    now = datetime.now(timezone.utc).isoformat()
    # Isolated historical-event fixture, not a claim that a child completed work.
    with db.connect() as c:
        c.execute('UPDATE plan_item SET status=? WHERE plan_item_id=?', (status,pid))
        StudyRepository._insert_plan_event(c,pid,event,now,{'reason':'原事项明确取消或延期'},'recorded-'+event)
        day = c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0]
        c.commit()
    with Database(db.path).connect() as c:
        c.execute('BEGIN'); before=c.total_changes
        rows = period_supply_facts(c,CHILD,day,day,now)['items']
        matched = [r for r in rows if r['historical_status']==expected]
        assert len(matched)==1 and matched[0]['mastery_claim_allowed'] is False
        assert c.total_changes == before
