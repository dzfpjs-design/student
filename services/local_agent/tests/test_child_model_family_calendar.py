"""The agent must not carry yesterday's expired family goals into a new week."""
from datetime import date, datetime, timezone
import json

import pytest

from home_study_agent import child_model
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService
from test_goal_program import goal_db
from test_goal_nanjing_calendar import freeze


@pytest.mark.parametrize('child_id', ['child-primary-4', 'child-junior-3'])
def test_family_week_rollover_updates_live_context_and_durable_snapshot(goal_db, monkeypatch, child_id):
    clock = [datetime(2026, 9, 13, 15, 59, 59, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(child_model, 'datetime', Clock)
    freeze(monkeypatch, clock[0].isoformat())
    goals = GoalProgramService(goal_db)
    goals.cycles(child_id, date(2026, 9, 13))
    goals.cycles(child_id, date(2026, 9, 14))
    with goal_db.connect() as c:
        for day, end in [('2026-09-07', '2026-09-13'), ('2026-09-14', '2026-09-20')]:
            c.execute('''INSERT INTO weekly_goal(weekly_goal_id,child_id,week_start,week_end,
                track,target_minutes,target_sessions,status,generator,knowledge_version,created_at,updated_at)
                VALUES (?,?,?,?,'pet',30,2,'not_started','calendar-test','test',?,?)''',
                (child_id + day, child_id, day, end, clock[0].isoformat(), clock[0].isoformat()))
        c.commit()
        journals = {table: [tuple(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in ('weekly_goal', 'goal_cycle')}
    service = ChildModelService(goal_db)
    sunday = service.snapshot(child_id)
    assert {r['week_end'] for r in sunday['payload']['goals']} == {'2026-09-13', '2026-09-20'}
    clock[0] = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
    live = service.compact_for_agent(child_id)
    assert {r['week_end'] for r in live['goal_progress']} == {'2026-09-20'}
    assert {r['goal_kind'] for r in live['goal_progress']} == {'legacy_weekly', 'program_cycle'}
    monday = service.snapshot(child_id)
    assert monday['snapshot_id'] != sunday['snapshot_id']
    assert monday['payload']['goals'] == live['goal_progress']
    restarted = ChildModelService(Database(goal_db.path))
    assert restarted.snapshot(child_id)['snapshot_id'] == monday['snapshot_id']
    assert restarted.compact_for_agent(child_id)['goal_progress'] == monday['payload']['goals']
    with goal_db.connect() as c:
        assert {table: [tuple(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in journals} == journals
        old_payload = c.execute('SELECT payload_json FROM child_model_snapshot WHERE snapshot_id=?',
                                (sunday['snapshot_id'],)).fetchone()[0]
        assert json.loads(old_payload) == sunday['payload']
