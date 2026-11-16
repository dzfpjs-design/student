"""Host router, voice execution and child-scoped lifecycle, without cloud media."""
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_companion_activity_outcome import activity
from test_companion_activity_voice import voice_turn
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService


class NoCloud:
    async def complete(self, *args, **kwargs):
        raise AssertionError('Activity feedback must stay local')


def family_rule(db):
    with db.connect() as c:
        c.execute("""INSERT OR IGNORE INTO family_rule(child_id,latest_end_time,max_learning_minutes,task_segment_minutes,
            break_minutes,pet_min_minutes,competition_minutes,version,updated_at)
            VALUES('child-game',NULL,90,25,5,10,15,1,datetime('now'))""")
        c.commit()


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_activity_export_deletion_and_restart_keep_child_boundary(activity, tmp_path, scope):
    db, _, _, oid, _, _ = activity
    turn = voice_turn(activity)
    CompanionActivityVoiceService(db).execute(turn)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    assert privacy.deletion_preview('child-game')['companion_activity_outcomes'] == 4
    with open(privacy.export_child('child-game')['path'], encoding='utf-8') as stream:
        exported = json.load(stream)
    assert exported['companion_activity_outcomes'][0]['outcome_id'] == oid
    assert len(exported['companion_activity_outcome_events']) == 2
    assert exported['companion_activity_voice_bindings'][0]['agent_turn_id'] == turn
    result = privacy.delete_child_scope('other', scope, '确认删除', str(uuid4()))
    assert result['status'] == 'completed'
    assert privacy.deletion_preview('child-game')['companion_activity_outcomes'] == 4
    result = privacy.delete_child_scope('child-game', scope, '确认删除', str(uuid4()))
    assert result['status'] == 'completed'
    assert privacy.deletion_preview('child-game')['companion_activity_outcomes'] == 0
    with db.connect() as c:
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('SELECT COUNT(*) FROM companion_activity_voice_binding').fetchone()[0] == 0


def test_real_app_activity_router_auth_restart_and_no_automatic_resume(activity, tmp_path):
    db, runtime, _, oid, session, item = activity
    family_rule(db)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    root = '/v1/parent/children/child-game/companion-activity-outcomes'
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        assert client.get(root).status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        response = client.get(root, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        body = {'expected_revision': 1, 'client_action_id': 'parent-feedback',
                'execution': 'partial', 'experience': 'fatigued', 'note': 'Only stretched twice',
                'evidence_basis': 'parent_observed'}
        response = client.post(f'{root}/{oid}/feedback', json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert not response.json()['activity']['habit_evidence_eligible']
        response = client.get(f'{root.replace("child-game", "other")}/{oid}', headers=headers)
        assert response.status_code == 404
        ended = client.post(f'/v1/sessions/{session}/companion-activities/end', json={'reason': 'child_requested'})
        assert ended.status_code == 200, ended.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item,)).fetchone()[0] == 'paused'
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        response = client.get(f'{root}/{oid}', headers={'X-HSC-Parent': token})
        assert response.status_code == 200, response.text
        assert response.json()['activity']['revision'] == 2
        assert response.json()['activity']['feedback']['payload']['note'] == body['note']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['feedback', 'request_resume'])
def test_real_dispatch_records_voice_and_resumes_only_on_explicit_request(activity, operation):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from home_study_agent.repository import StudyRepository
    from home_study_agent.database import Database
    db, runtime, _, _, session, item = activity
    family_rule(db)
    runtime.end_companion_activity(session, 'child_requested')
    key = voice_turn(activity, operation=operation, quote='我准备好继续刚才的学习了。' if operation == 'request_resume' else '刚才只做了两次，现在累了。')
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='planned' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (key,))
        c.commit()
    first = AgentCapabilityExecutor(db, StudyRepository(db)).execute(key)
    assert first['result']['cloud_transmission_allowed'] is False
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item,)).fetchone()[0] == ('active' if operation == 'request_resume' else 'paused')
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == (1 if operation == 'request_resume' else 0)
        assert c.execute("SELECT COUNT(*) FROM companion_activity_voice_binding").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    reopened = Database(db.path)
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(key) == first


@pytest.mark.parametrize('change', ['revised_feedback', 'cancelled_plan'])
def test_real_http_rejects_stale_activity_command_without_resuming(activity, tmp_path, change):
    db, runtime, book, oid, session, item = activity
    family_rule(db)
    runtime.end_companion_activity(session, 'child_requested')
    turn = voice_turn(activity, operation='request_resume', quote='现在继续刚才的学习。')
    with db.connect() as c:
        c.execute("UPDATE agent_step SET status='planned' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        if change == 'cancelled_plan':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn,))
            c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        c.commit()
    if change == 'revised_feedback':
        book.feedback('child-game', oid, expected_revision=1, client_action_id='parent-revision',
                      execution='partial', experience='fatigued', note='Still resting',
                      evidence_basis='parent_observed', actor='parent')
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        response = client.post(f'/v1/agent-turns/{turn}/execute')
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code']
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item,)).fetchone()[0] == 'paused'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
        if change == 'cancelled_plan':
            assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (turn,)).fetchone()[0] == 'cancelled'
            assert c.execute('SELECT status FROM agent_step WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (turn,)).fetchone()[0] == 'cancelled'
    assert book.get('child-game', oid)['activity']['revision'] == (2 if change == 'revised_feedback' else 1)


def test_duplicate_http_while_worker_runs_does_not_fail_its_turn(activity, tmp_path):
    db, _, _, _, _, _ = activity
    family_rule(db)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        turn = voice_turn(activity)
        response = client.post(f'/v1/agent-turns/{turn}/execute')
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'AGENT_STEP_ALREADY_EXECUTING'
    with db.connect() as c:
        assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (turn,)).fetchone()[0] == 'running'
        assert c.execute('SELECT status FROM agent_step WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (turn,)).fetchone()[0] == 'running'
        assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (turn,)).fetchone()[0] == 'pending'
