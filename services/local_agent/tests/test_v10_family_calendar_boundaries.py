"""Stored SQLite UTC is explicit; host timezone never defines the learning day."""
from datetime import datetime, timezone
import os
import time

import pytest

from home_study_agent.database import Database
from home_study_agent.family_clock import family_time
from home_study_agent.report_calendar import study_day
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_input_replan import ReplanConflict
from test_learning_input_replan import schedule, inputs, flow, CHILD, revised


@pytest.fixture
def los_angeles_host():
    previous = os.environ.get('TZ')
    os.environ['TZ'] = 'America/Los_Angeles'
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


@pytest.mark.parametrize('stamp,expected', [
    ('2026-09-09 15:59:59', '2026-09-09'),
    ('2026-09-09 16:00:00', '2026-09-10'),
    ('2026-09-09T16:00:00Z', '2026-09-10'),
    ('2026-09-09T09:00:00-07:00', '2026-09-10'),
])
def test_report_day_reads_legacy_sqlite_utc_without_rewriting_history(tmp_path, los_angeles_host, stamp, expected):
    db = Database(tmp_path / 'legacy-session.db')
    db.initialize()
    StudyRepository(db).seed_family()
    with db.connect() as c:
        c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('legacy',?,'completed','completed',?,?,'legacy')''', (CHILD, stamp, stamp))
        c.commit()
    for current in (db, Database(db.path)):
        with current.connect() as c:
            c.execute('PRAGMA query_only=ON')
            assert study_day(c, 'legacy') == expected
            assert c.execute("SELECT started_at FROM daily_session WHERE session_id='legacy'").fetchone()[0] == stamp
            assert c.total_changes == 0


@pytest.mark.parametrize('stamp', ['2026-09-09T16:00:00', '2026-09-09', 'not-a-date', '2026-09-09 16:00'])
def test_unknown_naive_history_is_not_assigned_an_invented_timezone(tmp_path, stamp):
    db = Database(tmp_path / 'unknown.db')
    db.initialize()
    StudyRepository(db).seed_family()
    with db.connect() as c:
        c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('unknown',?,'completed','completed',?,?,'unknown')''', (CHILD, stamp, stamp))
        with pytest.raises(ValueError):
            study_day(c, 'unknown')
    with pytest.raises(ValueError):
        family_time(datetime(2026,9,9,16))


@pytest.mark.parametrize('hour,accepted', [(0, True), (15, True), (16, False), (23, False)])
def test_replan_uses_original_nanjing_day_not_host_day(schedule, los_angeles_host, hour, accepted):
    source, service, clock = schedule
    clock[0] = datetime(2026,9,7,hour,tzinfo=timezone.utc)
    event = revised(schedule)
    db = source[0]
    with db.connect() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')]
    if accepted:
        result = service.execute_replan(CHILD, event, 'boundary-replan')
        assert result['receipt']['budget']['planning_date'] == '2026-09-07'
        assert result['receipt']['budget']['cutoff_at'] == '2026-09-07T21:30:00+08:00'
        assert service.execute_replan(CHILD,event,'boundary-replan') == result
        if hour == 0:
            assert result['receipt']['allocated_minutes'] == 30
        else:
            assert result['receipt']['allocated_minutes'] == 0
            assert result['unplaced'][0]['reason'] == 'latest_end_capacity'
    else:
        with pytest.raises(ReplanConflict, match='REPLAN_DAY_CHANGED'):
            service.execute_replan(CHILD,event,'boundary-replan')
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')] == before
            assert c.execute('SELECT COUNT(*) FROM learning_input_replan_receipt').fetchone()[0] == 0
