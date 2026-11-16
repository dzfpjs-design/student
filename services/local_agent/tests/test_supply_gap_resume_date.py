"""Shanghai-day admission, using real generated plans in the isolated copy."""
import os
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from home_study_agent import repository, supply_gap_resume
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.task_start_binding import TaskStartBindingError


@pytest.fixture
def shanghai_plan(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 9, 16, 30, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock[0]
            return value.astimezone(tz) if tz else value.astimezone().replace(tzinfo=None)

    class HostDate(date):
        @classmethod
        def today(cls):
            return clock[0].astimezone(ZoneInfo('America/Los_Angeles')).date()

    monkeypatch.setattr(supply_gap_resume, 'datetime', Clock)
    monkeypatch.setattr(supply_gap_resume, 'date', HostDate, raising=False)
    monkeypatch.setattr(repository, 'utc_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(repository, 'datetime', Clock)
    db = Database(tmp_path / 'date.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    child = 'child-primary'
    with db.connect() as c:
        child = c.execute("SELECT child_id FROM child_profile WHERE grade='四年级'").fetchone()[0]
        c.execute("UPDATE family_rule SET latest_end_time='21:30',max_learning_minutes=120,competition_minutes=0 WHERE child_id=?", (child,))
        c.commit()
    sid = repo.start_session(child, 'date-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.commit()
    previous = os.environ.get('TZ')
    try:
        # Generate with the actual application's Nanjing host date, then test
        # admission under the review host's LA date. No plan rows are fabricated.
        os.environ['TZ'] = 'Asia/Shanghai'
        time.tzset()
        repo.generate_plan(sid, 'date-plan')
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()
    with db.connect() as c:
        plan = dict(c.execute('SELECT * FROM daily_plan WHERE session_id=?', (sid,)).fetchone())
    assert plan['plan_date'] == '2026-09-10'
    return db, sid, child, clock, plan


def test_utc_midnight_does_not_expire_same_nanjing_day(shanghai_plan):
    db, sid, child, clock, plan = shanghai_plan
    observations = []
    for instant in ('2026-09-09T23:30:00+00:00', '2026-09-10T00:30:00+00:00'):
        clock[0] = datetime.fromisoformat(instant)
        with db.connect() as c:
            admitted = supply_gap_resume._plan(c, sid, child)
            assert admitted is not None, 'LA/UTC date must not expire the Nanjing plan'
            assert admitted['plan_id'] == plan['plan_id']
            receipt = supply_gap_resume.budget(c, dict(child_id=child, session_id=sid,
                plan_item_id='not-yet-placed', expected_minutes=12,
                source_type='pet_daily', source_ref='pet-daily'))
            assert receipt['planning_date'] == '2026-09-10'
            assert receipt['cutoff_at'] == '2026-09-10T21:30:00+08:00'
            observations.append(receipt['wall_remaining_seconds'])
    assert observations[0] - observations[1] == 3600


def test_actual_nanjing_next_day_rejects_old_plan_without_rollforward(shanghai_plan):
    db, sid, child, clock, plan = shanghai_plan
    clock[0] = datetime.fromisoformat('2026-09-10T16:01:00+00:00')
    with db.connect() as c:
        assert supply_gap_resume._plan(c, sid, child) is None
        with pytest.raises(TaskStartBindingError, match='SUPPLY_RESUME_PLAN_CHANGED'):
            supply_gap_resume.budget(c, dict(child_id=child, session_id=sid,
                plan_item_id='not-yet-placed', expected_minutes=12))
        assert dict(c.execute('SELECT * FROM daily_plan WHERE plan_id=?', (plan['plan_id'],)).fetchone()) == plan
