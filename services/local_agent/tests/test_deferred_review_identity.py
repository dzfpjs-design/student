"""A lost carryover receipt must never decide the next task or another child."""
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository, PlanInputError
from test_daily_learning_closure_audit import (
    db, learning_clock, session, set_state, finish, advance_day, CHILD, OTHER,
)


@pytest.fixture
def pending(db, learning_clock):
    repo = StudyRepository(db)
    sid = session(db)
    set_state(db, sid, 'homework_capture')
    for title in ('math page A', 'math page B'):
        repo.add_homework(sid, '数学', title, 10, 'written', '', None, None, None, None, str(uuid4()))
    plan = repo.generate_plan(sid, str(uuid4()))
    set_state(db, sid, 'study')
    for item in plan['plan_items']:
        if item['source_type'] == 'school_homework':
            repo.reschedule_plan_item(item['plan_item_id'], 'tomorrow', str(uuid4()))
    finish(db, sid)
    advance_day(learning_clock)
    target = session(db)
    set_state(db, target, 'homework_capture')
    candidates = repo.get_workspace(target)['carryover_candidates']
    assert len(candidates) == 2
    return db, target, candidates


def test_receipt_recovery_after_state_change_and_restart(pending):
    db, sid, rows = pending
    repo = StudyRepository(db)
    args = (sid, rows[0]['source_plan_item_id'], 'continue_today', 'continue original', 'carryover-request-1')
    saved = repo.review_deferred_task(*args)
    set_state(db, sid, 'plan_review')
    reopened = StudyRepository(Database(db.path))
    assert reopened.review_deferred_task(*args) == saved
    assert reopened.get_workspace(sid)['carryover_candidates'][0]['source_plan_item_id'] == rows[1]['source_plan_item_id']


@pytest.mark.parametrize('changed', ['task', 'decision', 'reason', 'child'])
def test_reused_key_cannot_authorize_another_request(pending, changed):
    db, sid, rows = pending
    repo = StudyRepository(db)
    args = [sid, rows[0]['source_plan_item_id'], 'continue_today', 'continue original', 'carryover-request-1']
    repo.review_deferred_task(*args)
    if changed == 'task':
        args[1] = rows[1]['source_plan_item_id']
    elif changed == 'decision':
        args[2] = 'cancelled'
    elif changed == 'reason':
        args[3] = 'changed reason'
    else:
        finish(db, sid)
        args[0] = session(db, OTHER)
        set_state(db, args[0], 'homework_capture')
    with pytest.raises(PlanInputError):
        repo.review_deferred_task(*args)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM deferred_task_review').fetchone()[0] == 1


def test_changed_original_requires_new_confirmation(pending):
    db, sid, rows = pending
    original = rows[0]
    version = original['source_version']
    with db.connect() as c:
        c.execute('UPDATE plan_item SET title=? WHERE plan_item_id=?', ('changed page', original['source_plan_item_id']))
        c.commit()
    with pytest.raises(PlanInputError):
        StudyRepository(db).review_deferred_task(sid, original['source_plan_item_id'], 'continue_today',
            'continue original', 'carryover-request-1', source_version=version)
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM deferred_task_review').fetchone()


def test_http_preserves_original_review_across_restart_and_rejects_stale_next_page(pending):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    db, sid, rows = pending
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    url = f"/v1/sessions/{sid}/deferred-tasks/{rows[0]['source_plan_item_id']}/review"
    payload = {'decision':'continue_today', 'reason':'continue original', 'client_action_id':'http-original-review',
        'source_version':rows[0]['source_version']}
    with TestClient(create_app(settings)) as client:
        saved = client.post(url, json=payload)
        assert saved.status_code == 200, saved.text
        remaining = client.get(f'/v1/sessions/{sid}').json()['carryover_candidates']
        assert len(remaining) == 1 and remaining[0]['source_plan_item_id'] == rows[1]['source_plan_item_id']
    set_state(db, sid, 'plan_review')
    with TestClient(create_app(settings)) as client:
        assert client.post(url, json=payload).json() == saved.json()
        other_url = f"/v1/sessions/{sid}/deferred-tasks/{rows[1]['source_plan_item_id']}/review"
        assert 400 <= client.post(other_url, json=payload).status_code < 500
        set_state(db, sid, 'homework_capture')
        with db.connect() as c:
            c.execute('UPDATE plan_item SET title=? WHERE plan_item_id=?', ('revised B', rows[1]['source_plan_item_id']))
            c.commit()
        stale = {**payload, 'client_action_id':'http-next-review', 'source_version':rows[1]['source_version']}
        refused = client.post(other_url, json=stale)
        assert refused.status_code == 409
        assert refused.json()['code'] == 'DEFERRED_TASK_SOURCE_CHANGED'
        details = refused.json()['details']
        assert details['not_applied'] is True
        assert details['client_action_id'] == stale['client_action_id']
        assert details['candidate']['source_plan_item_id'] == rows[1]['source_plan_item_id']
        assert details['candidate']['title'] == 'revised B'
        assert details['candidate']['source_version'] != rows[1]['source_version']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM deferred_task_review').fetchone()[0] == 1
        fresh = {**stale, 'client_action_id':'http-new-confirmation', 'source_version':details['candidate']['source_version']}
        confirmed = client.post(other_url, json=fresh)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()['source_plan_item_id'] == rows[1]['source_plan_item_id']
        assert client.post(other_url, json=fresh).json() == confirmed.json()
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM deferred_task_review').fetchone()[0] == 2
