"""Arrival identity for real generated goal tasks; no family data or cloud calls."""
import json

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.learning_input_revision import LearningInputRevisionService
from home_study_agent.repository import StudyRepository
from test_goal_task_prompt import command
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item, OTHER


@pytest.fixture
def active_goal(execution):
    db, sid, pid, _, _ = execution
    LearningInputRevisionService(db).initialize()
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(command(execution))
    assert result['result']['status'] == 'active', result
    return db, sid, pid


def capture(active_goal, key='goal-break-request'):
    db, sid, _ = active_goal
    receipt = LearningInputRevisionService(db).capture_arrival(sid, key, '我想先休息一小会儿')
    with db.connect() as c:
        raw = c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE arrival_id=?',
                        (receipt['arrival_id'],)).fetchone()[0]
    return receipt, json.loads(raw), raw


def goal_entry(items):
    return next(item for item in items if item['kind'] == 'task_execution_arrival')


def test_actual_started_goal_captures_original_task_plan_and_segment(active_goal):
    db, sid, pid = active_goal
    receipt, items, raw = capture(active_goal)
    entry = goal_entry(items)
    assert entry['local_only'] and entry['cloud_transmission_allowed'] is False
    assert len(entry['plans']) == 1
    task = entry['plans'][0]
    with db.connect() as c:
        current = c.execute('''SELECT pi.*,dp.version AS daily_plan_version FROM plan_item pi
            JOIN daily_plan dp USING(plan_id) WHERE plan_item_id=?''', (pid,)).fetchone()
        for field in ('plan_item_id', 'plan_id', 'title', 'subject', 'task_type', 'source_type',
                      'source_ref', 'active_segment_started_at', 'active_seconds', 'daily_plan_version'):
            assert task[field] == current[field], field
        assert task['source_type'] == 'junior_exam_goal'
        assert task['status'] == 'active' and task['active_segment_started_at']
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
    assert set(receipt) == {'arrival_id', 'pending', 'local_only', 'cloud_transmission_allowed'}
    assert receipt['cloud_transmission_allowed'] is False
    assert sid not in json.dumps(receipt) and pid not in json.dumps(receipt)
    reopened = (Database(db.path), sid, pid)
    assert capture(reopened) == (receipt, items, raw)


def test_old_arrival_never_backfills_new_state_or_plan_revision(active_goal):
    db, _, pid = active_goal
    original = capture(active_goal)
    assert goal_entry(original[1])['plans'][0]['status'] == 'active'
    with db.connect() as c:
        c.execute("UPDATE plan_item SET status='paused',active_segment_started_at=NULL WHERE plan_item_id=?", (pid,))
        c.execute('UPDATE daily_plan SET version=version+1')
        c.commit()
    assert capture(active_goal) == original
    assert all(item['kind'] != 'task_execution_arrival' for item in capture(active_goal, 'later')[1])


@pytest.mark.parametrize('mutation', [
    "UPDATE plan_item SET status='pending' WHERE plan_item_id=?",
    "UPDATE plan_item SET status='paused' WHERE plan_item_id=?",
    "UPDATE plan_item SET status='completed' WHERE plan_item_id=?",
    "UPDATE plan_item SET source_type='planned_break' WHERE plan_item_id=?",
    "UPDATE daily_plan SET status='cancelled' WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)",
    "UPDATE daily_plan SET child_id='child-primary-4' WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)",
    "UPDATE daily_plan SET session_id=(SELECT session_id FROM daily_session WHERE child_id='child-primary-4' LIMIT 1) WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)",
])
def test_excludes_inactive_break_other_child_and_other_session_tasks(active_goal, mutation):
    db, _, pid = active_goal
    with db.connect() as c:
        c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,
            ended_at,last_checkpoint_at,client_action_id) VALUES('other-session',?,'completed',
            'completed',datetime('now'),datetime('now'),datetime('now'),'other-arrival')''', (OTHER,))
        c.execute(mutation, (pid,))
        c.commit()
    assert all(item['kind'] != 'task_execution_arrival' for item in capture(active_goal)[1])


def test_homework_snapshot_not_duplicated_or_reclassified(active_goal):
    db, _, pid = active_goal
    with db.connect() as c:
        c.execute("UPDATE plan_item SET status='paused',active_segment_started_at=NULL WHERE plan_item_id=?", (pid,))
        c.execute("UPDATE plan_item SET status='active',active_segment_started_at=datetime('now') WHERE source_type='school_homework'")
        c.commit()
    _, items, _ = capture(active_goal)
    assert not any(item['kind'] == 'task_execution_arrival' for item in items)
    active = [p for item in items for p in item.get('plans', []) if p['status'] == 'active']
    assert len(active) == 1 and active[0]['source_type'] == 'school_homework'
