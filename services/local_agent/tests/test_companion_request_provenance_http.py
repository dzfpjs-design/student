"""Synthetic PCM and controlled planner; real HTTP, SQLite and dispatch."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.model_gateway import ModelResult
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_voice_runtime_binding import capture, interpret


QUOTE = '我想站起来伸伸胳膊，带我活动一小会儿吧。'


def planner():
    value = Planner('companion.start_activity', {
        'activity_type': 'movement', 'topic': '伸展', 'opening': '我们站稳，轻轻伸展一下胳膊。'}, QUOTE,
        objective_type='companion')
    value.payload['after_execution'] = 'stop'
    return value


def resume_planner(gateway, quote):
    original = gateway.complete
    async def complete(request):
        if request.request_type == 'voice_interaction':
            gateway.requests.append(request)
            return ModelResult(content=json.dumps({'role': 'control', 'control': 'companion.activity_feedback',
                'quote': quote, 'confidence': .98, 'activity_feedback': {'operation': 'request_resume'}}),
                provider='test', model='controlled-intent', latency_ms=1)
        return await original(request)
    gateway.complete = complete


def activity(db):
    return CompanionActivityOutcomeService(db).list(CHILD)['items'][0]


def homework_task(client, db, sid):
    """Synthetic school plan only; starting and pausing use production receipts."""
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture' WHERE session_id=?", (sid,))
        c.commit()
    response = client.post(f'/v1/sessions/{sid}/homework', json={'subject': '数学',
        'title': '合成口算作业', 'expected_minutes': 10, 'client_action_id': 'homework-source'})
    assert response.status_code == 201, response.text
    hid = response.json()['homework_id']
    now = datetime.now(timezone.utc)
    with db.connect() as c:
        c.execute('''INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,
            total_minutes,generator,version,created_at,client_action_id)
            VALUES ('request-plan',?,?,?,'active',10,'synthetic-test',1,?,'request-plan')''',
            (CHILD, sid, now.date().isoformat(), now.isoformat()))
        c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,
            priority,status,position,source_type,source_ref,task_type)
            VALUES ('request-task','request-plan',?,'数学','合成口算作业',10,1,'pending',1,'school_homework',?,'written')''', (hid, hid))
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    response = client.post('/v1/plan-items/request-task/start', json={'client_action_id': 'start-original-task'})
    assert response.status_code == 200, response.text
    return hid


def planned(client, settings, monkeypatch, sid):
    voice = capture(client, settings, monkeypatch, sid, QUOTE)
    turn = interpret(client, sid, QUOTE, 'request-activity', voice)
    assert turn['action']['type'] == 'start_companion_activity', turn['action']
    return turn, voice


def test_actual_voice_request_survives_restart_and_revocation_without_autonomy(environment, monkeypatch):
    settings, db = environment
    gateway = planner()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        voice = capture(client, settings, monkeypatch, sid, QUOTE)
        turn = interpret(client, sid, QUOTE, 'request-activity', voice)
        assert turn['action']['type'] == 'start_companion_activity', (turn['action'], [r.request_type for r in gateway.requests])
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        proof = activity(db)['request_provenance']
        assert proof['classification'] == 'session_voice_request'
        assert proof['status'] == 'verified'
        assert proof['speaker_identity'] == 'unverified'
        assert proof['initiative'] == proof['autonomy'] == 'unknown'
        assert proof['binding']['voice_input_id'] == voice
        assert proof['binding']['agent_turn_id'] == turn['agent_turn_id']
        assert QUOTE not in json.dumps(proof, ensure_ascii=False)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert activity(db)['request_provenance'] == proof
        replay = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert replay.status_code == 200, replay.text
        assert activity(db)['request_provenance'] == proof
        assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code == 200
        assert activity(db)['request_provenance']['status'] == 'unknown'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM companion_activity_outcome_event WHERE kind='opened'").fetchone()[0] == 1
            stored = json.loads(c.execute('SELECT source_snapshot_json FROM companion_activity_outcome').fetchone()[0])
            assert stored['request_provenance_v1'] == proof['binding']
    with TestClient(create_app(settings, model_gateway=gateway)):
        assert activity(db)['request_provenance']['classification'] == 'unknown'


@pytest.mark.parametrize('mutation', ['voice_cancel', 'arrival_delete', 'wrong_turn', 'other_child', 'task_changed', 'homework_changed'])
def test_write_rechecks_original_request_and_task_without_creating_activity(environment, monkeypatch, mutation):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid = start(client)
        homework_task(client, db, sid)
        turn, voice = planned(client, settings, monkeypatch, sid)
        if mutation == 'voice_cancel':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code == 200
        else:
            with db.connect() as c:
                if mutation == 'arrival_delete':
                    c.execute("DELETE FROM learning_input_arrival WHERE client_action_id='request-activity'")
                elif mutation == 'wrong_turn':
                    c.execute("UPDATE agent_turn SET transcript='不同原话' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
                elif mutation == 'other_child':
                    c.execute('UPDATE agent_turn SET child_id=? WHERE agent_turn_id=?', (OTHER, turn['agent_turn_id']))
                elif mutation == 'task_changed':
                    c.execute("UPDATE plan_item SET title='替换后的任务' WHERE plan_item_id='request-task'")
                else:
                    c.execute("UPDATE homework_item SET range_text='替换后的题目范围' WHERE client_action_id='homework-source'")
                c.commit()
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code in {409, 422}, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM companion_activity_outcome').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('retry_change', [None, 'voice_cancel', 'arguments', 'cancelled', 'other_error'])
def test_activity_and_provenance_rollback_together_in_real_creation_transaction(environment, monkeypatch, retry_change):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner()), raise_server_exceptions=False) as client:
        sid = start(client)
        turn, _ = planned(client, settings, monkeypatch, sid)
        with db.connect() as c:
            c.execute('''CREATE TRIGGER fail_activity_open BEFORE INSERT ON companion_activity_outcome_event
                BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END''')
            c.commit()
        assert client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute").status_code == 500
        with db.connect() as c:
            for table in ('companion_activity', 'companion_activity_outcome', 'companion_activity_outcome_event'):
                assert c.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
            c.execute('DROP TRIGGER fail_activity_open')
            if retry_change == 'arguments':
                c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.topic','changed topic')")
            elif retry_change == 'cancelled':
                c.execute("UPDATE agent_plan SET status='cancelled'")
                c.execute("UPDATE agent_step SET status='cancelled'")
            elif retry_change == 'other_error':
                c.execute("UPDATE agent_turn SET tool_error_code='RuntimeError'")
            c.commit()
        if retry_change == 'voice_cancel':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code == 200
        retry = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        if retry_change is None:
            assert retry.status_code == 200, retry.text
            assert activity(db)['request_provenance']['status'] == 'verified'
        else:
            assert retry.status_code == 409, retry.text
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 0


@pytest.mark.parametrize('mismatch', ['missing_voice', 'other_child', 'changed_text'])
def test_actual_http_rejects_unowned_or_false_voice_reference(environment, monkeypatch, mismatch):
    settings, db = environment
    gateway = planner()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        voice = capture(client, settings, monkeypatch, sid, QUOTE)
        if mismatch == 'other_child':
            switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
                'target_child_id': OTHER, 'client_action_id': 'switch-other-session'})
            assert switched.status_code == 200, switched.text
            sid = switched.json()['session_id']
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '换过的原话' if mismatch == 'changed_text' else QUOTE,
            'client_action_id': 'invalid-reference', 'voice_input_id': 'invented' if mismatch == 'missing_voice' else voice})
        assert response.status_code == 409, response.text
        assert not gateway.requests
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 0


def test_borrowed_open_requires_real_transaction_and_does_not_backfill_legacy(environment):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid = start(client)
        runtime = AgentRuntimeStore(db)
        source = runtime.start_companion_activity(sid, 'movement', 'legacy', 'legacy-start')
        service = CompanionActivityOutcomeService(db)
        assert activity(db)['request_provenance']['status'] == 'unknown'
        with db.connect() as c:
            with pytest.raises(ValueError, match='ACTIVITY_TRANSACTION_REQUIRED'):
                service.open(CHILD, source_kind='companion', source_id=source['activity_id'],
                    intent='unused', success_description='unused', client_action_id='no-transaction', connection=c)
            saved = c.execute('SELECT source_snapshot_json FROM companion_activity_outcome').fetchone()[0]
        runtime.start_companion_activity(sid, 'movement', 'legacy', 'legacy-start')
        with db.connect() as c:
            assert c.execute('SELECT source_snapshot_json FROM companion_activity_outcome').fetchone()[0] == saved
            c.execute('''CREATE TRIGGER fail_next_open BEFORE INSERT ON companion_activity_outcome_event
                BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END''')
            c.commit()
        with pytest.raises(sqlite3.IntegrityError):
            runtime.start_companion_activity(sid, 'movement', 'new request', 'new-failed-activity')
        with db.connect() as c:
            assert c.execute('SELECT status FROM companion_activity WHERE activity_id=?',
                (source['activity_id'],)).fetchone()[0] == 'active'
            assert c.execute('SELECT count(*) FROM companion_activity').fetchone()[0] == 1


def test_request_task_resume_and_current_observation_reach_childmodel_separately(environment, monkeypatch):
    settings, db = environment
    gateway = planner()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        hid = homework_task(client, db, sid)
        turn, voice = planned(client, settings, monkeypatch, sid)
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        proof = activity(db)['request_provenance']
        assert proof['status'] == 'verified', proof
        assert proof['binding']['task_binding']['plan_item_id'] == 'request-task'
        assert proof['binding']['task_binding']['source']['homework_id'] == hid
        assert client.post(f'/v1/sessions/{sid}/companion-activities/end', json={'reason': 'turn_limit'}).status_code == 200
        quote = '我准备好继续刚才的学习了。'
        resume_planner(gateway, quote)
        resume_voice = capture(client, settings, monkeypatch, sid, quote, key='resume-voice')
        resume_turn = interpret(client, sid, quote, 'resume-learning', resume_voice)
        assert resume_turn['action']['type'] == 'record_companion_activity_feedback', resume_turn
        resumed = client.post(f"/v1/agent-turns/{resume_turn['agent_turn_id']}/execute")
        assert resumed.status_code == 200, resumed.text
        assert activity(db)['resume']['status'] == 'confirmed'
        headers = {'X-HSC-Parent': client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']}
        def balance():
            response = client.get(f'/v1/parent/children/{CHILD}/longitudinal-model', headers=headers)
            assert response.status_code == 200, response.text
            return response.json()['payload']['companion_balance']
        view = balance()
        assert view['task_resume_receipt_sessions'] == 1
        assert view['verified_child_return_sessions'] == 0
        assert view['outcome_evidence'][0]['request_provenance'] == proof
        # Calibrated synthetic observation metadata enters the real existing
        # observation producer/coverage service. It is not independent mastery.
        record_return_observation(db, sid)
        view = balance()
        assert view['verified_child_return_sessions'] == 1, view
        assert view['habit_success_sessions'] == 0
        assert view['outcome_evidence'][0]['return_verification']['independent'] is False
        assert view['outcome_evidence'][0]['request_provenance']['autonomy'] == 'unknown'
        for request in gateway.requests:
            payload = json.dumps(request.messages, ensure_ascii=False)
            assert proof['binding']['arrival_id'] not in payload
            assert proof['binding']['voice_input_id'] not in payload
            assert 'request_provenance_v1' not in payload
        assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code == 200
        view = balance()
        assert view['outcome_evidence'][0]['request_provenance']['status'] == 'unknown'
        assert view['task_resume_receipt_sessions'] == view['verified_child_return_sessions'] == 1
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
        value = client.get(f'/v1/parent/children/{CHILD}/longitudinal-model', headers={'X-HSC-Parent': token}).json()
        view = value['payload']['companion_balance']
        assert view['task_resume_receipt_sessions'] == view['verified_child_return_sessions'] == 1
        assert view['outcome_evidence'][0]['request_provenance']['status'] == 'unknown'
        with db.connect() as c:
            c.execute("UPDATE homework_item SET title='修订后的原作业' WHERE homework_id=?", (hid,))
            c.commit()
        value = client.get(f'/v1/parent/children/{CHILD}/longitudinal-model', headers={'X-HSC-Parent': token}).json()
        view = value['payload']['companion_balance']
        assert view['task_resume_receipt_sessions'] == 1
        assert view['verified_child_return_sessions'] == 0


def record_return_observation(db, sid):
    from home_study_agent.behavior_model import BehaviorModelService
    from home_study_agent.behavior_model.app_integration import BehaviorAppIntegration
    from home_study_agent.behavior_model.outcome_evidence import task_context_snapshot
    service = BehaviorModelService(db)
    integration = BehaviorAppIntegration(service, db)
    start_at = datetime.now(timezone.utc)
    service.begin_observation_stream(CHILD, sid, stream_epoch='request-return',
        at=start_at.isoformat(), client_action_id='request-return-start')
    with db.connect() as c:
        task = dict(c.execute("SELECT pi.*,dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE plan_item_id='request-task'").fetchone())
    context = {k: task[k] for k in ('plan_item_id', 'subject', 'task_type', 'cognitive_load', 'source_type', 'plan_version')}
    for i in range(2):
        at = datetime.now(timezone.utc)
        state_id = 'request-writing-' + str(i)
        integration.observation(sid, dict(observation_ready=True, shadow=False, state_event_id=state_id,
            state='writing_like', confidence=.9, observed_at=at.isoformat(), model_version='synthetic-calibrated'))
        service.record_observation_sample(CHILD, sid, dict(schema_version='observation_coverage_v1',
            child_id=CHILD, session_id=sid, stream_epoch='request-return', sequence=i,
            captured_at=at.isoformat(), received_at=at.isoformat(),
            capture_monotonic_ms=int((at-start_at).total_seconds()*1000),
            quality=dict(observation_ready=True, scene_quality='ready', person_baseline='established',
                shadow=False, analysis_skipped=False), task_context=task_context_snapshot(context),
            state='writing_like', confidence=.9, model_version='synthetic-calibrated',
            analysis_id='request-analysis-' + str(i), producer_epoch='producer', state_event_id=state_id))
