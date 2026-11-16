"""A real shutdown transition has a durable system receipt, not child intent."""
import json
import sqlite3

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item
from test_goal_task_prompt import command


def start(execution):
    db, _, pid, _, _ = execution
    run_id, _ = db.begin_runtime()
    AgentCapabilityExecutor(db, StudyRepository(db)).execute(command(execution))
    return db, pid, run_id


def test_shutdown_records_exact_pause_once_and_preserves_time_across_restart(execution):
    db, pid, run_id = start(execution)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET active_seconds=12,active_segment_started_at=datetime('now','-2 seconds') WHERE plan_item_id=?", (pid,))
        segment = c.execute('SELECT active_segment_started_at FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0]
        run_started_at = c.execute('SELECT started_at FROM runtime_state').fetchone()[0]
        c.commit()
    db.end_runtime(run_id)
    db.end_runtime(run_id)
    reopened = Database(db.path)
    new_run, _ = reopened.begin_runtime()
    reopened.end_runtime(run_id)
    with reopened.connect() as c:
        row = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()
        assert row['status'] == 'paused' and row['active_seconds'] >= 14
        events = c.execute("SELECT * FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchall()
        assert len(events) == 1
        proof = json.loads(events[0]['payload_json'])
        assert proof == {'reason': 'runtime_shutdown', 'runtime_run_id': run_id, 'source': 'system',
                         'run_started_at': run_started_at, 'active_segment_started_at': segment}
        assert events[0]['client_action_id'] == f'runtime-shutdown:{run_id}:{pid}'
        assert c.execute('SELECT run_id,clean_shutdown FROM runtime_state').fetchone()[:] == (new_run, 0)


def test_pause_receipt_failure_rolls_back_task_and_runtime_and_retry_is_exact(execution):
    db, pid, run_id = start(execution)
    with db.connect() as c:
        original = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())
        c.execute("""CREATE TRIGGER fail_system_pause BEFORE INSERT ON plan_item_event
            WHEN json_extract(NEW.payload_json,'$.reason')='runtime_shutdown'
            BEGIN SELECT RAISE(ABORT,'synthetic journal write failure'); END""")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.end_runtime(run_id)
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()) == original
        assert c.execute('SELECT clean_shutdown FROM runtime_state').fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_system_pause')
        c.commit()
    db.end_runtime(run_id)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 1


def test_unclean_restart_records_system_pause_and_can_resume_repeatedly(execution):
    from home_study_agent.task_start_binding import TaskStartBinding
    db, sid, pid, _, _ = execution
    _, _, run_id = start(execution)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET active_seconds=12,active_segment_started_at=datetime('now','-1 day') WHERE plan_item_id=?", (pid,))
        c.commit()
    for index in range(3):
        reopened = Database(db.path)
        new_run, unclean = reopened.begin_runtime()
        assert unclean and new_run != run_id
        reopened.end_runtime(run_id)
        with reopened.connect() as c:
            item = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()
            assert item['status'] == 'paused' and item['active_seconds'] == 12
            assert item['active_segment_started_at'] is None
            paused = c.execute("SELECT * FROM plan_item_event WHERE plan_item_id=? AND event_type='paused' ORDER BY rowid", (pid,)).fetchall()
            assert len(paused) == index + 1
            payload = json.loads(paused[-1]['payload_json'])
            assert payload['reason'] == 'unclean_restart' and payload['source'] == 'system'
            assert payload['runtime_run_id'] == run_id and payload['recovery_run_id'] == new_run
            assert payload['uncheckpointed_duration_unknown'] is True
            candidate = TaskStartBinding.resume_candidates(c, sid, 'child-junior-3')
            assert len(candidate) == 1 and candidate[0]['plan_item_id'] == pid
            assert candidate[0]['pause_event_id'] == paused[-1]['event_id']
            assert candidate[0]['goal_prompt_binding']['plan_item_id'] == pid
        assert StudyRepository(reopened).resume_plan_item(pid, f'recover-{index}')['status'] == 'active'
        run_id = new_run


def test_unclean_recovery_event_failure_is_atomic_and_retryable(execution):
    db, pid, run_id = start(execution)
    with db.connect() as c:
        c.execute("""CREATE TRIGGER fail_recovery_pause BEFORE INSERT ON plan_item_event
            WHEN json_extract(NEW.payload_json,'$.reason')='unclean_restart'
            BEGIN SELECT RAISE(ABORT,'synthetic recovery journal failure'); END""")
        before = '\n'.join(c.iterdump())
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic recovery journal failure'):
        db.begin_runtime()
    with db.connect() as c:
        assert '\n'.join(c.iterdump()) == before
        c.execute('DROP TRIGGER fail_recovery_pause')
        c.commit()
    new_run, unclean = db.begin_runtime()
    assert unclean and new_run != run_id
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 1
