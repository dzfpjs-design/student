"""Habit support must never become an academic completion condition."""
import json
from datetime import datetime, timezone

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_goal_task_prompt import command
from test_parent_report_habit_dispatch import active_habit


CHILD = 'child-primary-4'
LEGACY_REASON = '附着到第一项真实学习任务，不额外增加孩子负担。'


def prepared(tmp_path):
    db = Database(tmp_path / 'habit-plan.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    behavior = active_habit(db)
    habit = behavior.active_habit_experiment(CHILD)['experiment']
    sid = repo.start_session(CHILD, 'habit-plan-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture' WHERE session_id=?", (sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=0,competition_minutes=0 WHERE child_id=?''', (CHILD,))
        c.commit()
    repo.add_homework(sid, '语文', '朗读课文', 10, 'reading', None, None, None, None, None, 'reading')
    repo.add_homework(sid, '数学', '计算练习', 10, 'written', None, None, None, None, None, 'written')
    workspace = repo.generate_plan(sid, 'habit-plan')
    task = next(item for item in workspace['plan_items'] if item['source_type'] == 'school_homework')
    return db, repo, behavior, habit, sid, task


def legacy(case):
    db, repo, _, habit, sid, task = case
    base = task['completion_standard'].removesuffix('；本轮习惯实验：' + habit['child_action'])
    raw = base + '；本轮习惯实验：' + habit['child_action']
    with db.connect() as c:
        c.execute('UPDATE plan_item SET completion_standard=? WHERE plan_item_id=?', (raw, task['plan_item_id']))
        c.execute('''UPDATE plan_constraint_decision SET reason=?,metadata_json=?
            WHERE source_ref=? AND obligation_type='habit_experiment' ''',
            (LEGACY_REASON, json.dumps({'title': habit['title'], 'success_signal': habit['success_signal']}), habit['experiment_id']))
        c.commit()
    return base, raw


def test_active_support_is_separate_from_every_academic_task(tmp_path):
    db, repo, _, habit, sid, _ = prepared(tmp_path)
    result = repo.get_workspace(sid)
    assert all(habit['child_action'] not in row['completion_standard'] for row in result['plan_items'])
    support = next(row for row in result['constraint_decisions'] if row['obligation_type'] == 'habit_experiment')
    assert support['metadata']['support_role'] == 'contextual_optional'
    assert support['metadata']['completion_condition'] is False
    assert 'plan_item_id' not in support['metadata']
    assert repo.generate_plan(sid, 'habit-plan')['plan_items'] == result['plan_items']


def test_stopped_legacy_support_is_not_spoken_on_real_task_start_or_after_restart(tmp_path):
    case = prepared(tmp_path)
    db, repo, behavior, habit, sid, task = case
    base, raw = legacy(case)
    behavior.stop_habit_experiment(habit['experiment_id'], CHILD, datetime.now(timezone.utc).isoformat(), 'stop-habit')
    turn = command((db, sid, task['plan_item_id'], None, None), child_id=CHILD)
    result = AgentCapabilityExecutor(db, repo).execute(turn)
    assert habit['child_action'] not in result['response_text']
    assert base in result['response_text']
    reopened = StudyRepository(Database(db.path))
    assert reopened._get_plan_item_by_id(task['plan_item_id'])['completion_standard'] == base
    assert next(row for row in reopened.get_workspace(sid)['plan_items'] if row['plan_item_id'] == task['plan_item_id'])['completion_standard'] == base
    with db.connect() as c:
        assert c.execute('SELECT completion_standard FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == raw
        assert c.execute('SELECT status FROM habit_experiment WHERE experiment_id=?', (habit['experiment_id'],)).fetchone()[0] == 'stopped'


@pytest.mark.parametrize('guard', ['other_child', 'other_generator', 'no_decision', 'new_optional_decision',
                                 'changed_task_source', 'ambiguous_support', 'corrupt_support'])
def test_literal_teacher_requirement_is_not_stripped_without_exact_legacy_provenance(tmp_path, guard):
    case = prepared(tmp_path)
    db, repo, _, habit, sid, task = case
    _, raw = legacy(case)
    with db.connect() as c:
        if guard == 'other_child':
            c.execute("UPDATE habit_experiment SET child_id='child-junior-3' WHERE experiment_id=?", (habit['experiment_id'],))
        elif guard == 'other_generator':
            c.execute("UPDATE daily_plan SET generator='imported' WHERE session_id=?", (sid,))
        elif guard == 'no_decision':
            c.execute("DELETE FROM plan_constraint_decision WHERE obligation_type='habit_experiment'")
        elif guard == 'changed_task_source':
            c.execute("UPDATE plan_item SET source_ref='different-homework' WHERE plan_item_id=?", (task['plan_item_id'],))
        elif guard == 'ambiguous_support':
            c.execute('''INSERT INTO plan_constraint_decision SELECT 'ambiguous',plan_id,obligation_type,
                source_ref,decision,reason,requested_minutes,allocated_minutes,metadata_json,created_at
                FROM plan_constraint_decision WHERE obligation_type='habit_experiment' ''')
        elif guard == 'corrupt_support':
            c.execute("UPDATE plan_constraint_decision SET metadata_json='[]' WHERE obligation_type='habit_experiment'")
        else:
            c.execute('''UPDATE plan_constraint_decision SET metadata_json=? WHERE obligation_type='habit_experiment' ''',
                      (json.dumps({'support_role': 'contextual_optional', 'completion_condition': False}),))
        c.commit()
    assert repo._get_plan_item_by_id(task['plan_item_id'])['completion_standard'] == raw


def test_legacy_projection_uses_original_placement_even_after_reorder(tmp_path):
    case = prepared(tmp_path)
    db, repo, _, _, sid, task = case
    base, _ = legacy(case)
    with db.connect() as c:
        c.execute('UPDATE plan_item SET position=position+100 WHERE plan_item_id=?', (task['plan_item_id'],))
        c.commit()
    assert repo._get_plan_item_by_id(task['plan_item_id'])['completion_standard'] == base


def test_real_http_stop_resume_and_restart_preserve_academic_requirement_and_history(tmp_path):
    from test_behavior_goal_http import host
    case = prepared(tmp_path)
    db, repo, _, habit, sid, task = case
    base, raw = legacy(case)
    with host(db, tmp_path) as client:
        turn = command((db, sid, task['plan_item_id'], None, None), child_id=CHILD)
        start = client.post(f'/v1/agent-turns/{turn}/execute')
        assert start.status_code == 200, start.text
        assert base in start.json()['execution']['response_text']
        paused = client.post(f"/v1/plan-items/{task['plan_item_id']}/pause", json={'client_action_id': 'pause-habit-task'})
        assert paused.status_code == 200, paused.text
        stop_url = f"/v1/children/{CHILD}/behavior/habit-experiments/{habit['experiment_id']}/stop"
        assert client.post(stop_url, json={'client_action_id': 'stop-http'}).status_code == 401
        parent = client.post('/v1/parent/setup', json={'pin': '8362'})
        assert parent.status_code == 200, parent.text
        stopped = client.post(stop_url, json={'client_action_id': 'stop-http'},
                              headers={'X-HSC-Parent': parent.json()['parent_token']})
        assert stopped.status_code == 200, stopped.text
        resumed_turn = command((db, sid, task['plan_item_id'], None, None), child_id=CHILD)
        resumed = client.post(f'/v1/agent-turns/{resumed_turn}/execute')
        assert resumed.status_code == 200, resumed.text
        spoken = resumed.json()['execution']['response_text']
        assert '现在继续' in spoken and base in spoken and habit['child_action'] not in spoken
    with host(Database(db.path), tmp_path) as client:
        result = client.get(f'/v1/sessions/{sid}')
        assert result.status_code == 200, result.text
        current = next(row for row in result.json()['plan_items'] if row['plan_item_id'] == task['plan_item_id'])
        assert current['completion_standard'] == base and current['status'] == 'paused'
        assert current['active_segment_started_at'] is None
        restart_turn = command((db, sid, task['plan_item_id'], None, None), child_id=CHILD)
        again = client.post(f'/v1/agent-turns/{restart_turn}/execute')
        assert again.status_code == 200, again.text
        assert base in again.json()['execution']['response_text']
        assert habit['child_action'] not in again.json()['execution']['response_text']
        with db.connect() as c:
            assert [r[0] for r in c.execute('SELECT event_type FROM plan_item_event WHERE plan_item_id=? ORDER BY rowid',
                                          (task['plan_item_id'],))] == ['started', 'paused', 'resumed', 'paused', 'resumed']
    with db.connect() as c:
        assert c.execute('SELECT completion_standard FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == raw
        assert [r[0] for r in c.execute('SELECT event_type FROM plan_item_event WHERE plan_item_id=? ORDER BY rowid',
                                      (task['plan_item_id'],))] == ['started', 'paused', 'resumed', 'paused', 'resumed', 'paused']
