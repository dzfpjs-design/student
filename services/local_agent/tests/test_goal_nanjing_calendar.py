"""Goal calendar, execution eligibility and evidence cutoff use Nanjing days."""
from datetime import date, datetime, timezone

import pytest

from home_study_agent import goal_program, goal_roadmap
from home_study_agent.goal_program import GoalProgramError, GoalProgramService
from home_study_agent.family_clock import calendar_as_of, family_time
from home_study_agent.repository import StudyRepository
from test_goal_program import goal_db


def freeze(monkeypatch, instant):
    now = datetime.fromisoformat(instant)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(goal_roadmap, 'datetime', Clock)
    monkeypatch.setattr(goal_program, 'datetime', Clock)
    monkeypatch.setattr(goal_program, '_now', lambda: now.isoformat())


@pytest.mark.parametrize('instant, expected', [
    ('2026-09-13T15:59:59+00:00', '2026-09-13'),
    ('2026-09-13T16:00:00+00:00', '2026-09-14'),
])
def test_roadmap_default_day_follows_family_midnight(monkeypatch, instant, expected):
    freeze(monkeypatch, instant)
    today, feedback_now = goal_roadmap._as_of(None)
    assert today.isoformat() == expected
    assert feedback_now == datetime.fromisoformat(instant)


def test_historical_day_ends_in_nanjing_and_future_does_not_admit_future_evidence(monkeypatch):
    freeze(monkeypatch, '2026-09-14T10:00:00+00:00')
    day, cutoff = goal_roadmap._as_of(date(2026, 9, 13))
    assert day == date(2026, 9, 13)
    assert cutoff == datetime(2026, 9, 13, 15, 59, 59, 999999, tzinfo=timezone.utc)
    _, future_cutoff = goal_roadmap._as_of(date(2026, 9, 15))
    assert future_cutoff == datetime(2026, 9, 14, 10, tzinfo=timezone.utc)


def test_cycles_roll_to_family_week_and_closed_week_uses_original_cutoff(goal_db, monkeypatch):
    freeze(monkeypatch, '2026-09-13T16:30:00+00:00')
    service = GoalProgramService(goal_db)
    current = service.cycles('child-junior-3')
    assert current and {item['week_start'] for item in current} == {'2026-09-14'}
    historical = service.cycles('child-junior-3', date(2026, 9, 13))
    assert {item['week_start'] for item in historical} == {'2026-09-07'}
    assert {item['learning_outcome']['as_of'] for item in historical} == {'2026-09-13T15:59:59.999999+00:00'}
    assert GoalProgramService(goal_db).cycles('child-junior-3') == current


def test_new_goal_rejects_yesterdays_family_deadline(goal_db, monkeypatch):
    freeze(monkeypatch, '2026-09-13T16:30:00+00:00')
    with pytest.raises(GoalProgramError, match='目标日期不能早于今天'):
        GoalProgramService(goal_db).configure_program('child-junior-3', 'junior_exam_readiness',
            target_statement=None, target_date='2026-09-13', target_score=None, target_score_label=None,
            event_name=None, event_rules_url=None, event_version=None, award_target=None,
            registration_deadline=None, weekly_minutes=None, weekly_sessions=None,
            client_action_id='yesterday-in-family-zone')
    with goal_db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM goal_program_revision').fetchone()[0] == 0


@pytest.mark.parametrize('instant, expected_day, window', [
    ('2026-09-13T16:30:00+00:00', '2026-09-14', 120),
    ('2026-09-14T13:25:00+00:00', '2026-09-14', 5),
])
def test_unplanned_work_and_fixed_time_remain_on_family_day(goal_db, monkeypatch, instant, expected_day, window):
    from home_study_agent import repository

    freeze(monkeypatch, instant)
    monkeypatch.setattr(repository, 'utc_now', lambda: instant)
    repo = StudyRepository(goal_db)
    sid = repo.start_session('child-junior-3', 'family-unplanned-load')['session_id']
    repo.save_planning_context(sid, 'medium', 13, 'Synthetic fixed time', None, None, 0, 'fixed-load')
    with goal_db.connect() as c:
        c.execute("UPDATE family_rule SET max_learning_minutes=120,latest_end_time='21:30' WHERE child_id='child-junior-3'")
        c.execute("UPDATE daily_session SET state='homework_capture' WHERE session_id=?", (sid,))
        c.commit()
    repo.add_homework(sid, '数学', 'Synthetic unplanned homework', 20, 'written', None, None, None, None, None, 'unplanned-load')
    service = GoalProgramService(goal_db)
    service.seed_defaults()
    route = service.roadmap('child-junior-3', 'junior_exam_readiness')
    first = route['daily_decomposition']['days'][0]
    assert first['date'] == expected_day
    assert first['fixed_commitment_minutes'] == 13
    assert first['unplanned_school_minutes'] == 20
    assert first['time_window_minutes'] == window
    assert first['available_minutes'] == min(87, window)
    assert not route['daily_decomposition']['actual_assignments_created']
    with goal_db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_plan').fetchone()[0] == 0


def test_naive_instants_and_datetime_as_calendar_date_are_rejected():
    with pytest.raises(ValueError, match='aware'):
        family_time(datetime(2026, 9, 14, 10))
    with pytest.raises(ValueError, match='reference_date'):
        calendar_as_of(datetime(2026, 9, 14), datetime(2026, 9, 14, tzinfo=timezone.utc))
