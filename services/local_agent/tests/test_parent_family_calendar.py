"""Parent-facing availability and persisted days agree with Nanjing planning."""
from datetime import date, datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent import family_clock, curriculum_supply_http, report_followup, specialized_program
from home_study_agent.database import Database
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.report_followup import ReportFollowupService
from home_study_agent.specialized_program import SpecializedProgramService
from test_curriculum_supply import CHILD, prepared
from test_curriculum_supply_http import BASE, application
from test_daily_report_evidence_versions import flow
from test_report_followup_host import yesterday_feedback
from test_specialized_program import setup_database


def clock(monkeypatch, hour):
    instant = datetime(2026, 9, 9, hour, tzinfo=timezone.utc)

    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    class HostDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 9)

    monkeypatch.setattr(family_clock, 'datetime', Clock)
    for module in (curriculum_supply_http, report_followup, specialized_program):
        monkeypatch.setattr(module, 'date', HostDate, raising=False)
    monkeypatch.setattr(specialized_program, '_now', lambda: instant.isoformat())
    return instant


@pytest.mark.parametrize('hour,first,last,available', [
    (15, None, '2026-09-09', True),
    (16, None, '2026-09-09', False),
    (16, '2026-09-10', None, True),
])
def test_parent_material_options_use_family_day(tmp_path, monkeypatch, hour, first, last, available):
    db, _, _ = prepared(tmp_path)
    with db.connect() as c:
        source = c.execute("SELECT source_id FROM knowledge_source WHERE title LIKE '隔离验收教材页%'").fetchone()[0]
        c.execute('UPDATE knowledge_source SET effective_from=?,effective_to=? WHERE source_id=?', (first, last, source))
        c.commit()
    auth = ParentAuth(db)
    token = auth.setup('7391')
    clock(monkeypatch, hour)
    with TestClient(application(db, auth), client=('127.0.0.1', 50000)) as client:
        response = client.get(BASE + '/options', headers={'X-HSC-Parent': token})
        assert response.status_code == 200, response.text
        row = next(s for s in response.json()['sources'] if s['source_id'] == source)
        assert row['available'] is available, row
        assert ('CURRICULUM_TEXTBOOK_EXPIRED' in row['blockers']) is (not available)
        assert response.headers['cache-control'] == 'no-store'


def test_parent_guard_read_agrees_with_plan_and_preserves_history(flow, monkeypatch):
    db, _, _, _ = flow
    snapshot = yesterday_feedback(flow, monkeypatch)
    service = ReportFollowupService(db)
    proposed = service.propose_plan_adjustment(CHILD, snapshot, 1, 'daily_total_minutes_cap', 30,
        '2026-09-09', '2026-09-09', 'calendar-proposal')
    aid = proposed['adjustment_id']
    service.review_plan_adjustment(CHILD, aid, 1, 'approve', 'calendar-approve')
    with db.connect() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM report_plan_adjustment_event ORDER BY rowid')]
    clock(monkeypatch, 15)
    assert service.get_adjustment(CHILD, aid)['effective_as_guard'] is True
    clock(monkeypatch, 16)
    restarted = ReportFollowupService(Database(db.path))
    result = restarted.get_adjustment(CHILD, aid)
    assert result['effective_as_guard'] is False
    assert result['learning_effect_verified'] is False
    assert restarted.project_for_plan(CHILD, '2026-09-10')['constraints'] == []
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM report_plan_adjustment_event ORDER BY rowid')] == before
    with pytest.raises(KeyError):
        restarted.get_adjustment('child-junior-3', aid)


def test_specialized_countdown_and_snapshots_share_generation_day(tmp_path, monkeypatch):
    db = setup_database(tmp_path / 'calendar.db')
    from home_study_agent.goal_program import GoalProgramService
    GoalProgramService(db).seed_defaults()
    with db.connect() as c:
        goal = c.execute("SELECT * FROM goal_program WHERE child_id=? AND track='pet_path'", (CHILD,)).fetchone()
        horizon = json.loads(goal['target_horizon_json'])
        horizon['target_date'] = '2026-09-11'
        c.execute('UPDATE goal_program SET target_horizon_json=? WHERE program_id=?', (json.dumps(horizon), goal['program_id']))
        c.commit()
    service = SpecializedProgramService(db)
    clock(monkeypatch, 15)
    first = service.roadmap(CHILD, 'pet')
    assert first['target']['days_remaining'] == 2
    clock(monkeypatch, 16)
    second = service.roadmap(CHILD, 'pet')
    assert second['target']['days_remaining'] == 1
    # Delayed persistence belongs to the original generation day, not the write day.
    service._save_snapshot(first)
    with Database(db.path).connect() as c:
        rows = c.execute('SELECT snapshot_date,payload_json FROM specialized_program_snapshot WHERE child_id=? AND track=? ORDER BY snapshot_date', (CHILD, 'pet')).fetchall()
    assert [r['snapshot_date'] for r in rows] == ['2026-09-09', '2026-09-10']
    assert [json.loads(r['payload_json'])['target']['days_remaining'] for r in rows] == [2, 1]
