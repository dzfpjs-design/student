"""Shared daily reservations via actual planning/deferral; no real family data."""
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.learning_input_replan import LearningInputReplanService
from test_learning_runtime_http import NoCloud
from test_writing_stage_review_consumers import advance_clock
from test_goal_roadmap import flow, TARGET

CHILD = 'child-primary-4'


@pytest.fixture
def mixed_day(tmp_path, monkeypatch, flow):
    advance_clock(monkeypatch, datetime(2026, 9, 14, 8, tzinfo=timezone.utc))
    settings = Settings(data_dir=tmp_path, database_path=flow[0].path, maintenance_enabled=False,
        data_encryption_key=b'8' * 32)
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(settings.database_path))
    with ExitStack() as original_client_lifecycle:
        client = original_client_lifecycle.enter_context(TestClient(create_app(settings, model_gateway=NoCloud())))
        def post(path, body):
            response = client.post(path, json=body)
            assert response.status_code in (200, 201), response.text
            return response.json()
        token = post('/v1/parent/setup', {'pin': '8264'})['parent_token']
        response = client.put(f'/v1/parent/children/{CHILD}/family-rule', headers={'X-HSC-Parent': token}, json=dict(
            latest_end_time='21:00', max_learning_minutes=90, task_segment_minutes=25, break_minutes=5,
            pet_min_minutes=15, competition_minutes=15, observation_mode='shadow', reminder_budget_per_10m=0))
        assert response.status_code == 200, response.text
        # Publish an existing self-authored math task through the real local
        # supply service. This is source qualification, not a learner score.
        from test_mistake_retention_goal_path import qualified_target
        qualified_target(client.app.state.repository.database)
        with flow[0].connect() as c:
            # This fixture covers one explicit arithmetic prerequisite path,
            # not coverage of every competition domain in the shipped catalog.
            c.execute("UPDATE knowledge_edge SET status='inactive' WHERE target_node_id=? AND edge_id!='roadmap-prerequisite'", (TARGET,))
            c.commit()
        sid = post('/v1/sessions', dict(child_id=CHILD, client_action_id='mixed-session'))['session_id']
        response = client.put(f'/v1/sessions/{sid}/planning-context', json=dict(energy_level='medium',
            fixed_commitment_minutes=0, client_action_id='mixed-context'))
        assert response.status_code == 200, response.text
        post(f'/v1/sessions/{sid}/transition', dict(expected_state='check_in', target_state='classroom_recall',
            reason='说今天的学习内容。', client_action_id='mixed-recall'))
        post(f'/v1/sessions/{sid}/recalls', dict(subject='数学', summary='今天学习了数量关系和四则运算。',
            client_action_id='mixed-recall-math'))
        post(f'/v1/sessions/{sid}/transition', dict(expected_state='classroom_recall', target_state='homework_capture',
            reason='登记作业。', client_action_id='mixed-homework'))
        post(f'/v1/sessions/{sid}/homework', dict(subject='数学', title='完成学校的口算练习', expected_minutes=20,
            client_action_id='mixed-school-math'))
        plan = post(f'/v1/sessions/{sid}/plan/generate', dict(client_action_id='mixed-plan'))
        yield dict(client=client, db=client.app.state.repository.database, sid=sid, settings=settings, plan=plan, post=post,
                   finish_client=original_client_lifecycle.close)


def current_budget(case, at=None):
    with case['db'].connect() as c:
        c.execute('BEGIN')
        plan = c.execute("SELECT * FROM daily_plan WHERE session_id=? AND status='active'", (case['sid'],)).fetchone()
        session = c.execute('SELECT ds.*,cp.grade FROM daily_session ds JOIN child_profile cp USING(child_id) WHERE session_id=?', (case['sid'],)).fetchone()
        rule = c.execute('SELECT * FROM family_rule WHERE child_id=?', (CHILD,)).fetchone()
        context = c.execute('SELECT * FROM session_planning_context WHERE session_id=?', (case['sid'],)).fetchone()
        items = [dict(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=?', (plan['plan_id'],))]
        info, remaining, reason = LearningInputReplanService(case['db'])._budget(
            c, CHILD, session, plan, items, rule, context, {}, at or datetime(2026, 9, 14, 8, tzinfo=timezone.utc))
        assert reason is None, reason
        return info, remaining


@pytest.mark.parametrize('minutes_worked', [0, 5, 25])
def test_explicit_deferral_releases_only_unspent_school_time(mixed_day, monkeypatch, minutes_worked):
    case = mixed_day
    items = case['plan']['plan_items']
    sources = {r['source_type'] for r in items}
    assert {'school_homework', 'pet_daily', 'competition_math'} <= sources, [
        (r['obligation_type'], r['decision'], r['reason']) for r in case['plan']['constraint_decisions']]
    school = next(r for r in items if r['source_type'] == 'school_homework')
    at = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)
    if minutes_worked:
        case['post'](f"/v1/sessions/{case['sid']}/transition", dict(expected_state='plan_review', target_state='study',
            reason='先做校内作业。', client_action_id='mixed-study'))
        case['post']('/v1/plan-items/' + school['plan_item_id'] + '/start', dict(client_action_id='mixed-school-start'))
        at += timedelta(minutes=minutes_worked)
        advance_clock(monkeypatch, at)
        case['post']('/v1/plan-items/' + school['plan_item_id'] + '/pause', dict(client_action_id='mixed-school-pause'))
    before, capacity = current_budget(case, at)
    deferred = case['post']('/v1/plan-items/' + school['plan_item_id'] + '/reschedule', dict(
        reason='这项作业明天继续，今天先完成英语和数学思维练习。', client_action_id='mixed-defer-school'))
    assert deferred['status'] == 'rescheduled'
    after, available = current_budget(case, at)
    released = max(0, school['expected_minutes'] * 60 - before['actual_spent_seconds'])
    assert after['protected_remaining_seconds'] == before['protected_remaining_seconds'] - released
    assert available == capacity + released
    assert after['actual_spent_seconds'] == before['actual_spent_seconds']
    assert abs(after['actual_spent_seconds'] - minutes_worked * 60) <= 1
    assert after['pet_reserved_seconds'] == before['pet_reserved_seconds']
    workspace = case['client'].get(f"/v1/sessions/{case['sid']}").json()
    for original in items:
        if original['plan_item_id'] == school['plan_item_id']:
            continue
        current = next(r for r in workspace['plan_items'] if r['plan_item_id'] == original['plan_item_id'])
        assert current['status'] == original['status'] and current['expected_minutes'] == original['expected_minutes']
    with case['db'].connect() as c:
        event = dict(c.execute("SELECT * FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled'",
            (school['plan_item_id'],)).fetchone())
        homework = dict(c.execute('SELECT * FROM homework_item WHERE homework_id=?', (school['homework_id'],)).fetchone())
        assert homework['status'] == 'pending'
    case['finish_client']()
    with TestClient(create_app(case['settings'], model_gateway=NoCloud())) as reopened:
        replay = reopened.post('/v1/plan-items/' + school['plan_item_id'] + '/reschedule', json=dict(
            reason='这项作业明天继续，今天先完成英语和数学思维练习。', client_action_id='mixed-defer-school'))
        assert replay.status_code == 200, replay.text
        restored = dict(case, db=reopened.app.state.repository.database)
        assert current_budget(restored, at) == (after, available)
        with restored['db'].connect() as c:
            events = [dict(r) for r in c.execute("SELECT * FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled'",
                (school['plan_item_id'],))]
            assert events == [event]
            assert dict(c.execute('SELECT * FROM homework_item WHERE homework_id=?', (school['homework_id'],)).fetchone()) == homework


def test_deferral_does_not_extend_evening_cutoff(mixed_day):
    case = mixed_day
    school = next(r for r in case['plan']['plan_items'] if r['source_type'] == 'school_homework')
    case['post']('/v1/plan-items/' + school['plan_item_id'] + '/reschedule', dict(
        reason='明天继续。', client_action_id='cutoff-defer'))
    info, capacity = current_budget(case, datetime(2026, 9, 14, 13, tzinfo=timezone.utc))
    assert capacity == 0 and info['wall_remaining_seconds'] == 0
    assert info['total_budget_remaining_seconds'] > 0
    assert info['limiting_reason'] == 'latest_end_capacity'


def test_deferred_pet_minimum_cannot_be_spent_on_other_work(mixed_day):
    case = mixed_day
    pet = next(r for r in case['plan']['plan_items'] if r['source_type'] == 'pet_daily')
    before, available = current_budget(case)
    case['post']('/v1/plan-items/' + pet['plan_item_id'] + '/reschedule', dict(
        reason='英语练习稍后重新安排。', client_action_id='pet-defer'))
    after, capacity = current_budget(case)
    assert after['pet_reserved_seconds'] == 15 * 60
    assert before['pet_reserved_seconds'] == 0
    assert after['protected_remaining_seconds'] == before['protected_remaining_seconds'] - pet['expected_minutes'] * 60
    assert capacity - available == max(0, pet['expected_minutes'] - 15) * 60
    assert after['actual_spent_seconds'] == 0
