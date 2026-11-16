from datetime import date, datetime, timedelta
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService, PeriodReportSourceError
from home_study_agent.repository import StudyRepository
from test_pet_task_execution import CHILD, generated
from test_writing_stage_review_consumers import advance_clock


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_clock_only_refresh_keeps_approval_but_real_deferral_changes_history(tmp_path, monkeypatch, kind):
    db, sid, pid, *_ = generated(tmp_path, 'reading')
    with db.connect() as c:
        day = date.fromisoformat(c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0])
    service, parents = PeriodReportService(db), ParentReportService(db)
    first = service.generate(CHILD, kind, day)
    snapshot = parents.create_from_period(first)
    parents.approve(snapshot['snapshot_id'])
    old_asof = first['content']['supply_history']['as_of']
    clock = advance_clock(monkeypatch, datetime.fromisoformat(old_asof)+timedelta(minutes=5))
    next_report = PeriodReportService(Database(db.path)).generate(CHILD, kind, day)
    renewed = ParentReportService(Database(db.path)).create_from_period(next_report)
    assert renewed['snapshot_id'] == snapshot['snapshot_id'], 'Clock-only refresh silently superseded reviewed period'
    assert renewed['status'] == 'approved'
    assert next_report['content']['supply_history']['as_of'] == old_asof
    clock.current += timedelta(minutes=5)
    StudyRepository(db).reschedule_plan_item(pid, '今天明确延期原阅读任务', 'real-period-defer')
    changed = service.generate(CHILD, kind, day)
    replacement = parents.create_from_period(changed)
    assert changed['content']['supply_history']['as_of'] != old_asof
    assert any(r['historical_status']=='explicitly_deferred' for r in changed['content']['supply_history']['items'])
    assert replacement['snapshot_id'] != snapshot['snapshot_id'] and replacement['status'] == 'draft'
    assert parents.get(snapshot['snapshot_id'])['status'] == 'revoked'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('damage', [
    'invalid_json', 'list_content', 'list_history', 'missing_asof',
    'invalid_asof', 'null_asof', 'naive_asof',
])
def test_optional_old_cutoff_never_blocks_existing_corrupt_cache_recovery(tmp_path, damage):
    db, sid, *_ = generated(tmp_path, 'reading')
    with db.connect() as c:
        day = date.fromisoformat(c.execute('SELECT plan_date FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0])
    service, parents = PeriodReportService(db), ParentReportService(db)
    first = service.generate(CHILD, 'weekly', day)
    snapshot = parents.create_from_period(first)
    parents.approve(snapshot['snapshot_id'])
    with db.connect() as c:
        frozen = c.execute('SELECT content_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (snapshot['snapshot_id'],)).fetchone()[0]
        broken = json.loads(c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
            (first['period_report_id'],)).fetchone()[0])
        if damage == 'list_content':
            broken = []
        elif damage == 'list_history':
            broken['supply_history'] = []
        elif damage == 'missing_asof':
            broken['supply_history'].pop('as_of')
        elif damage in {'invalid_asof', 'null_asof', 'naive_asof'}:
            broken['supply_history']['as_of'] = {'invalid_asof':'broken', 'null_asof':None,
                'naive_asof':'2026-09-01T12:00:00'}[damage]
        serialized = '{broken' if damage == 'invalid_json' else json.dumps(broken)
        c.execute('UPDATE period_report SET content_json=? WHERE period_report_id=?',
            (serialized, first['period_report_id']))
        c.commit()
    try:
        regenerated = service.generate(CHILD, 'weekly', day)
    except PeriodReportSourceError:
        # The pre-existing top-level source-issue contract is intentionally
        # retained; an optional cutoff optimization must not broaden it.
        assert damage in {'invalid_json', 'list_content'}
        with db.connect() as c:
            assert c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
                (first['period_report_id'],)).fetchone()[0] == serialized
    else:
        assert damage not in {'invalid_json', 'list_content'}
        assert datetime.fromisoformat(regenerated['content']['supply_history']['as_of']).tzinfo is not None
        assert regenerated['content']['supply_history']['status'] == 'available'
    with db.connect() as c:
        assert c.execute('SELECT content_json FROM parent_report_snapshot WHERE snapshot_id=?',
            (snapshot['snapshot_id'],)).fetchone()[0] == frozen
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
