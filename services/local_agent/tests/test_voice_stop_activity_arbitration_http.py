"""Real HTTP/stores/dispatcher; synthetic text and controlled model, no devices or network."""
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult, ModelResponseError
from home_study_agent.repository import utc_now
from test_diagnostic_prompt_http import environment
from test_content_free_voice_control_http import task_fixture
from test_content_free_voice_control_http import ControlGateway


class ActivityIntent:
    configured = True

    def __init__(self, operation='feedback', hook=None):
        self.operation, self.hook, self.requests = operation, hook, []

    async def complete(self, request):
        self.requests.append(request)
        assert request.request_type == 'voice_interaction'
        body = json.loads(request.messages[-1]['content'])
        assert set(body) == {'transcript', 'state'}
        assert all(type(v) is bool for v in body['state'].values())
        if self.hook:
            self.hook()
        feedback = {'operation': self.operation}
        if self.operation == 'feedback':
            feedback.update(execution='partial', experience='fatigued')
        return ModelResult(content=json.dumps({'role': 'control', 'control': 'companion.activity_feedback',
            'quote': body['transcript'], 'confidence': .99, 'activity_feedback': feedback}),
            provider='test', model='controlled', latency_ms=1)


class FailingCloud:
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        raise ModelResponseError('MODEL_TIMEOUT', fallback_eligible=False)


def session(client, db, stage='study'):
    r = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'voice-stage'})
    assert r.status_code == 201, r.text
    sid = r.json()['session_id']
    with db.connect() as c:
        c.execute('UPDATE daily_session SET state=? WHERE session_id=?', (stage, sid))
        c.commit()
    return sid


def say(client, sid, text, key):
    r = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id': key, 'cloud_text_allowed': True})
    assert r.status_code == 200, r.text
    return r.json()


def open_activity(client, sid, key='activity-first'):
    r = client.post(f'/v1/sessions/{sid}/companion-activities/start', json={
        'activity_type': 'movement', 'topic': 'PRIVATE_ACTIVITY_SOURCE', 'client_action_id': key})
    assert r.status_code == 200, r.text
    ended = client.post(f'/v1/sessions/{sid}/companion-activities/end', json={'reason': 'child_requested'})
    assert ended.status_code == 200, ended.text
    return r.json()


@pytest.mark.parametrize('stage', ['study', 'break', 'reflection', 'check_in'])
def test_stop_camera_before_question_without_cloud_or_page_read(environment, monkeypatch, stage):
    settings, db = environment
    gateway = FailingCloud()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        r = client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id': 'monitor-start'})
        assert r.status_code == 200, r.text
        with db.connect() as c:
            c.execute('UPDATE daily_session SET state=? WHERE session_id=?', (stage, sid))
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            c.commit()
        reader = Mock(side_effect=AssertionError('A stop must not read the page'))
        monkeypatch.setattr(client.app.state.agent_orchestrator, '_local_document_context', reader)
        turn = say(client, sid, '暂停高拍仪，我刚才写了什么？', 'stop-before-question')
        assert turn['action']['type'] == 'control_document_monitor'
        assert turn['action']['command'] == 'pause'
        assert '正在' in turn['response_text']
        assert turn['requires_control_admission'] is True
        assert not gateway.requests and not reader.called
        tid = turn['agent_turn_id']
        admission = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission')
        assert admission.status_code == 200, admission.text
        assert client.put(f'/v1/agent-turns/{tid}/outcome', json={
            'final_response': '已停', 'tool_status': 'completed'}).status_code == 409
        # Device acknowledgement is synthetic; the server monitor write and validation are real.
        paused = client.post(f'/v1/sessions/{sid}/document-monitor/pause', json={
            'reason': 'child_requested', 'client_action_id': 'monitor-pause'})
        assert paused.status_code == 200, paused.text
        receipt = {'capability': 'document.monitor_control', 'execution_location': 'client_device_bridge',
            'action_type': 'control_document_monitor', 'status': 'completed', 'completed_at': utc_now(),
            'output': {'device_type': 'document_camera', 'device_status': 'paused', 'frame_confirmed': False,
                       'lease_confirmed': True, 'persisted_status': 'paused'}}
        complete = client.put(f'/v1/agent-turns/{tid}/outcome', json={
            'final_response': '高拍仪已停止。', 'tool_status': 'completed', 'result': {'execution_receipt': receipt}})
        assert complete.status_code == 200, complete.text
        assert not gateway.requests and not reader.called
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = say(client, sid, '暂停高拍仪，我刚才写了什么？', 'stop-before-question')
        assert replay['agent_turn_id'] == tid and replay['tool_status'] == 'completed'


@pytest.mark.parametrize('operation', ['feedback', 'request_resume'])
def test_activity_response_has_its_own_focus_not_task_resume_and_restarts(environment, operation):
    settings, db = environment
    gateway = ActivityIntent(operation)
    quote = '刚才只活动了一小会儿，现在胳膊有点累' if operation == 'feedback' else '我准备好了，回去接着刚才的学习吧'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        task_fixture(db, sid)
        opened = open_activity(client, sid)
        turn = say(client, sid, quote, 'activity-response')
        assert turn['action']['type'] == 'record_companion_activity_feedback'
        r = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert r.status_code == 200, r.text
        assert r.json()['execution']['capability'] == 'companion.activity_feedback'
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='control-task-1'").fetchone()[0] == ('active' if operation == 'request_resume' else 'paused')
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert len(gateway.requests) == 1
        messages = json.dumps(gateway.requests[0].messages)
        for private in (sid, opened['activity_id'], 'PRIVATE_ACTIVITY_SOURCE', 'control-task-1'):
            assert private not in messages
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = say(client, sid, quote, 'activity-response')
        assert replay['agent_turn_id'] == turn['agent_turn_id']
        r = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert r.status_code == 200, r.text
        assert len(gateway.requests) == 1


def test_generic_task_resume_cannot_bypass_pending_activity(environment):
    settings, db = environment
    quote = '我准备好了，继续刚才的学习吧'
    gateway = ControlGateway(quote, 'task.resume')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        task_fixture(db, sid)
        open_activity(client, sid)
        turn = say(client, sid, quote, 'wrong-resume-label')
        assert turn['action']['type'] == 'none'
        assert '活动' in turn['response_text']
        r = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert r.status_code == 200, r.text
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='control-task-1'").fetchone()[0] == 'paused'
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
        assert len(gateway.requests) == 1


def test_explicit_different_task_does_not_get_rewritten_as_activity_resume(environment):
    settings, db = environment
    quote = '先继续英语那项，刚才的活动以后再说'
    class EnglishIntent(ControlGateway):
        async def complete(self, request):
            value = await super().complete(request)
            body = json.loads(value.content)
            body['selector']['subject'] = 'english'
            return ModelResult(content=json.dumps(body), provider='test', model='controlled-intent', latency_ms=1)
    gateway = EnglishIntent(quote, 'task.resume')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        repo = task_fixture(db, sid)
        open_activity(client, sid)
        with db.connect() as c:
            c.execute("UPDATE plan_item SET subject='英语' WHERE plan_item_id='control-task-2'")
            c.commit()
        repo.start_plan_item('control-task-2', 'start-other-task')
        repo.pause_plan_item('control-task-2', 'pause-other-task')
        turn = say(client, sid, quote, 'select-other-paused-task')
        assert turn['action']['type'] == 'resume_paused_task'
        r = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert r.status_code == 200, r.text
        with db.connect() as c:
            rows = dict(c.execute('SELECT plan_item_id,status FROM plan_item'))
            assert rows['control-task-1'] == 'paused' and rows['control-task-2'] == 'active'
            # The feedback journal is lazy-created only when that service runs.
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='companion_activity_voice_binding'").fetchone():
                assert c.execute('SELECT count(*) FROM companion_activity_voice_binding').fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM agent_step WHERE capability_name='companion.activity_feedback'").fetchone()[0] == 0


def test_cancelled_arrival_cannot_complete_late_stop_receipt(environment):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=FailingCloud())) as client:
        sid = session(client, db)
        client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id': 'monitor-start'})
        turn = say(client, sid, '暂停高拍仪', 'pending-stop')
        tid = turn['agent_turn_id']
        r = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission')
        assert r.status_code == 200, r.text
        say(client, sid, '先别执行刚才的操作', 'cancel-previous-stop')
        # Simulate a delayed device return, not a new authorized device operation.
        client.post(f'/v1/sessions/{sid}/document-monitor/pause', json={'client_action_id': 'late-physical-pause'})
        receipt = {'capability': 'document.monitor_control', 'execution_location': 'client_device_bridge',
            'action_type': 'control_document_monitor', 'status': 'completed', 'completed_at': utc_now(),
            'output': {'device_type': 'document_camera', 'device_status': 'paused', 'frame_confirmed': False,
                       'lease_confirmed': True, 'persisted_status': 'paused'}}
        r = client.put(f'/v1/agent-turns/{tid}/outcome', json={'final_response': '已经停好，继续读页',
            'tool_status': 'completed', 'result': {'execution_receipt': receipt}})
        assert r.status_code == 409, r.text
        with db.connect() as c:
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (tid,)).fetchone()[0] != 'completed'


@pytest.mark.parametrize('change', ['new_utterance', 'feedback_revision', 'other_child'])
def test_activity_command_rechecks_original_binding_before_real_dispatch(environment, change):
    from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
    settings, db = environment
    gateway = ActivityIntent('request_resume')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        task_fixture(db, sid)
        open_activity(client, sid)
        turn = say(client, sid, '回到刚才的学习吧', 'bound-activity-resume')
        assert turn['action']['type'] == 'record_companion_activity_feedback'
        with db.connect() as c:
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                           (turn['agent_turn_id'],)).fetchone()[0])
        b = manifest['companion_activity_outcome_binding']
        if change == 'new_utterance':
            say(client, sid, '先别执行刚才的操作', 'withdraw-activity-resume')
        elif change == 'feedback_revision':
            CompanionActivityOutcomeService(db).feedback(b['child_id'], b['outcome_id'], expected_revision=b['expected_revision'],
                client_action_id='newer-activity-feedback', execution='partial', experience='fatigued',
                note='Still resting', evidence_basis='parent_observed', actor='parent')
        else:
            # Corrupt only the authority proof after a real planned request.
            with db.connect() as c:
                b['child_id'] = 'child-middle-9'
                c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',
                          (json.dumps(manifest), turn['agent_turn_id']))
                c.commit()
        r = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert r.status_code == 409, r.text
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='control-task-1'").fetchone()[0] == 'paused'
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0


def test_multiple_activities_offer_local_choice_then_feedback_after_restart(environment):
    settings, db = environment
    gateway = ActivityIntent('feedback')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        task_fixture(db, sid)
        open_activity(client, sid)
        open_activity(client, sid, 'activity-second')
        turn = say(client, sid, '刚才只做了一部分，现在有点累', 'ambiguous-activities')
        assert turn['action']['type'] == 'none' and '第2项' in turn['response_text']
        r = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert r.status_code == 200, r.text
        with db.connect() as c:
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                           (turn['agent_turn_id'],)).fetchone()[0])
            selected = manifest['companion_activity_selection']['candidates'][1]['binding']['outcome_id']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        choice = say(client, sid, '我说的是第二个', 'select-second-activity')
        assert len(gateway.requests) == 1
        assert client.post(f"/v1/agent-turns/{choice['agent_turn_id']}/execute").status_code == 200
        feedback = say(client, sid, '那次只做了一会儿，现在累了', 'feedback-selected-activity')
        r = client.post(f"/v1/agent-turns/{feedback['agent_turn_id']}/execute")
        assert r.status_code == 200, r.text
        assert r.json()['execution']['result']['activity']['outcome_id'] == selected
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='control-task-1'").fetchone()[0] == 'paused'


def test_legacy_no_plan_blocked_receipt_does_not_become_success_on_replay(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=FailingCloud())) as client:
        sid = session(client, db)
        turn = say(client, sid, '我说的话还没整理好', 'unplanned-legacy-turn')
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
        def forbidden(*args, **kwargs):
            pytest.fail('A durable blocked receipt must not run success hooks')
        monkeypatch.setattr(client.app.state.agent_orchestrator.agent_runtime, 'finalize_diagnostic_turn', forbidden)
        route = f"/v1/agent-turns/{turn['agent_turn_id']}/outcome"
        r = client.put(route, json={'final_response': '这次没有执行。', 'tool_status': 'completed',
                                   'result': {'status': 'blocked', 'reason': 'unavailable'}})
        assert r.status_code == 200, r.text
        r = client.put(route, json={'final_response': '不应覆盖原回执', 'tool_status': 'completed',
                                   'result': {'status': 'completed'}})
        assert r.status_code == 200, r.text
        assert r.json()['response_text'] == '这次没有执行。'
