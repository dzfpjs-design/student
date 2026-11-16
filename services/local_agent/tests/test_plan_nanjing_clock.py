"""Initial planning and later admission share the family's Nanjing day/cutoff."""
from datetime import date, datetime
import os
import time

import pytest

from home_study_agent import repository
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService


@pytest.mark.parametrize('instant, expected_day, maximum, blocked', [
    ('2026-09-09T16:30:00+00:00', '2026-09-10', 120, False),
    ('2026-09-09T13:25:00+00:00', '2026-09-09', 5, False),
    ('2026-09-09T13:30:00+00:00', '2026-09-09', 0, True),
])
def test_plan_uses_one_nanjing_clock_not_host_wall_clock(tmp_path, monkeypatch, instant, expected_day, maximum, blocked):
    monkeypatch.setattr(repository, 'utc_now', lambda: instant)
    db = Database(tmp_path / 'nanjing-clock.db')
    db.initialize()
    repo = repository.StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    with db.connect() as c:
        child = c.execute("SELECT child_id FROM child_profile WHERE grade='四年级'").fetchone()[0]
        c.execute("UPDATE family_rule SET latest_end_time='21:30',max_learning_minutes=120,competition_minutes=0 WHERE child_id=?", (child,))
        c.commit()
    sid = repo.start_session(child, 'fixed-clock-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (sid,))
        c.commit()
    previous = os.environ.get('TZ')
    try:
        os.environ['TZ'] = 'America/Los_Angeles'
        if hasattr(time, 'tzset'):
            time.tzset()
        if blocked:
            with pytest.raises(repository.PlanInputError, match='最晚结束时间'):
                repo.generate_plan(sid, 'fixed-clock-plan')
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 0
        else:
            result = repo.generate_plan(sid, 'fixed-clock-plan')
            assert result['plan']['plan_date'] == expected_day
            assert result['plan']['total_minutes'] <= maximum
            assert repo.generate_plan(sid, 'fixed-clock-plan')['plan']['plan_id'] == result['plan']['plan_id']
            assert repository.StudyRepository(Database(db.path)).generate_plan(sid, 'fixed-clock-plan')['plan']['plan_id'] == result['plan']['plan_id']
            with pytest.raises(repository.PlanInputError, match='原学习会话不匹配'):
                repo.generate_plan('another-session', 'fixed-clock-plan')
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM plan_item_event').fetchone()[0] == 0
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        if hasattr(time, 'tzset'):
            time.tzset()


def test_weekly_goal_read_uses_same_nanjing_week_but_keeps_explicit_history(tmp_path, monkeypatch):
    class HostSunday(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 13)

    monkeypatch.setattr(repository, 'date', HostSunday)
    monkeypatch.setattr(repository, 'utc_now', lambda: '2026-09-13T16:30:00+00:00')
    db = Database(tmp_path / 'weekly-clock.db')
    db.initialize()
    repo = repository.StudyRepository(db)
    repo.seed_family()
    with db.connect() as c:
        child = c.execute("SELECT child_id FROM child_profile WHERE grade='四年级'").fetchone()[0]
    old = repo.weekly_goals(child, date(2026, 9, 13))
    assert old and {goal['week_start'] for goal in old} == {'2026-09-07'}
    current = repo.weekly_goals(child)
    assert current and {goal['week_start'] for goal in current} == {'2026-09-14'}
    restarted = repository.StudyRepository(Database(db.path))
    assert {goal['weekly_goal_id'] for goal in restarted.weekly_goals(child)} == {goal['weekly_goal_id'] for goal in current}
    assert restarted.weekly_goals(child, date(2026, 9, 13)) == old
