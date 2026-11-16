"""Report attribution follows the study day, not the delayed worker's clock."""
from datetime import datetime, timezone

import pytest

from home_study_agent import repository
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.reports import build_daily_report
from test_report_delivery import _pending_report_job


def test_parent_today_uses_family_midnight_not_database_host_clock(tmp_path, monkeypatch):
    db = Database(tmp_path / 'parent-today-clock.db')
    db.initialize()
    _pending_report_job(db)
    monkeypatch.setattr(repository, 'utc_now', lambda: '2026-09-13T16:30:00+00:00')
    repo = repository.StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET started_at='2026-09-13T16:00:00+00:00',ended_at='2026-09-13T16:20:00+00:00'")
        c.commit()
    today = repo.parent_today()
    primary = next(row for row in today['items'] if row['child']['child_id'] == 'child-primary-4')
    assert primary['session'] and primary['session']['session_id'] == 'pending-report-session'
    monkeypatch.setattr(repository, 'utc_now', lambda: '2026-09-14T16:00:00+00:00')
    after_midnight = repository.StudyRepository(Database(db.path)).parent_today()
    primary = next(row for row in after_midnight['items'] if row['child']['child_id'] == 'child-primary-4')
    assert primary['session'] is None


def test_delayed_report_selects_original_week_not_workers_week(tmp_path, monkeypatch):
    from home_study_agent.family_clock import family_today
    from home_study_agent.report_source_fingerprint import report_source_fingerprint
    from datetime import timedelta

    db = Database(tmp_path / 'report-week.db')
    db.initialize()
    _pending_report_job(db)
    LearningStateService(db).seed_catalog()
    sid, child = 'pending-report-session', 'child-primary-4'
    repo = repository.StudyRepository(db)
    current = family_today()
    original = current - timedelta(days=14)
    monday = original - timedelta(days=original.weekday())
    original_end = datetime.combine(original, datetime.min.time(), tzinfo=timezone.utc).replace(hour=10).isoformat()
    with db.connect() as c:
        c.execute('UPDATE daily_session SET started_at=?,ended_at=? WHERE session_id=?', (original_end, original_end, sid))
        c.commit()
    repo.weekly_goals(child, original)
    repo.weekly_goals(child, current)
    with db.connect() as c:
        c.execute("UPDATE weekly_goal SET target_minutes=17 WHERE child_id=? AND week_start=? AND track='pet'", (child, monday.isoformat()))
        c.execute("UPDATE weekly_goal SET target_minutes=29 WHERE child_id=? AND week_start!=? AND track='pet'", (child, monday.isoformat()))
        c.commit()
    evidence = repo.report_evidence(sid)
    assert 'PET周目标17分钟' in evidence['goal_context']
    assert 'PET周目标29分钟' not in evidence['goal_context']
    # Later work within that same week must not be presented as completed by
    # this report's earlier session end, even when the worker runs much later.
    from test_report_source_fingerprint import insert
    with db.connect() as c:
        insert(c, 'daily_session', session_id='later', child_id=child, status='completed', state='completed',
               started_at=original_end, ended_at=original_end.replace('T10:', 'T12:'),
               last_checkpoint_at=original_end, client_action_id='later')
        insert(c, 'daily_plan', plan_id='later-plan', child_id=child, session_id='later', plan_date=original.isoformat(),
               status='completed',total_minutes=11,generator='test',version=1,created_at=original_end,client_action_id='later-plan')
        insert(c, 'plan_item', plan_item_id='later-task', plan_id='later-plan', subject='英语',title='later PET',
               expected_minutes=11,actual_minutes=11,priority=1,status='completed',position=0,source_type='pet_daily',
               completed_at=original_end.replace('T10:', 'T12:'))
        c.commit()
    assert 'PET周目标17分钟，已记录0分钟' in repo.report_evidence(sid)['goal_context']
    with db.connect() as c:
        before = report_source_fingerprint(c, sid)
        c.execute("UPDATE weekly_goal SET target_minutes=19 WHERE child_id=? AND week_start=? AND track='pet'", (child, monday.isoformat()))
        assert report_source_fingerprint(c, sid) != before
        c.rollback()


@pytest.mark.parametrize('started,ended,plan_day,expected', [
    ('2026-09-13T16:30:00+00:00', '2026-09-13T17:00:00+00:00', None, '2026-09-14'),
    ('2026-09-13T15:00:00+00:00', '2026-09-13T16:10:00+00:00', None, '2026-09-13'),
    ('2026-09-13T15:30:00+00:00', '2026-09-13T17:10:00+00:00', '2026-09-14', '2026-09-14'),
])
def test_delayed_report_retains_original_learning_day_across_restart(tmp_path, monkeypatch, started, ended, plan_day, expected):
    db = Database(tmp_path / 'report-calendar.db')
    db.initialize()
    _pending_report_job(db)
    LearningStateService(db).seed_catalog()
    ReportDeliveryService(db, b'C' * 32).initialize()
    sid = 'pending-report-session'
    with db.connect() as c:
        c.execute('UPDATE daily_session SET started_at=?,ended_at=? WHERE session_id=?', (started, ended, sid))
        if plan_day:
            c.execute('''INSERT INTO daily_plan(plan_id, child_id, session_id, plan_date,
                status,total_minutes,generator,version,created_at,client_action_id)
                VALUES ('calendar-plan','child-primary-4',?,?,'completed',0,'test',1,?,'calendar-plan')''',
                (sid, plan_day, started))
        c.commit()
    monkeypatch.setattr(repository, 'utc_now', lambda: '2026-10-01T16:30:00+00:00')
    repo = repository.StudyRepository(db)
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    saved = repo.save_daily_report(sid, summary, coaching)
    assert saved['report_date'] == expected
    assert datetime.fromisoformat(saved['created_at']) == datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
    snapshot = ParentReportService(db).create_from_daily(saved)
    assert snapshot['content']['report_date'] == expected
    monkeypatch.setattr(repository, 'utc_now', lambda: '2026-11-01T00:30:00+00:00')
    restarted = repository.StudyRepository(Database(db.path))
    repeat = restarted.save_daily_report(sid, summary, coaching)
    assert repeat['report_id'] == saved['report_id']
    assert repeat['report_date'] == expected
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_report WHERE session_id=?', (sid,)).fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM report_delivery_outbox').fetchone()[0] == 0
