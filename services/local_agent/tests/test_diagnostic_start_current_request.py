"""Original Gateway/HTTP/Runtime/SQLite; all speech and providers synthetic."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from normal_provider_owner import normal_provider_owner
from home_study_agent.app import create_app
from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import CapabilityRegistry, InvalidAgentPlanError, parse_agent_plan_output
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_agent_plan_json_fallback import settings_for
from test_cloud_first_agent import Planner
from test_daily_planning_state_contract import capture_session
from test_voice_runtime_binding import capture

HOMEWORK = '数学练习是做家长选好的三道原创合成练习。'
PUBLIC_TRIAL_VERB = '数学练习试做家长选好的三道原创合成练习。'


def payload(capability, quote, *, role='task_request', binding_quote=None):
    args = {'subject': '数学', 'title': quote} if capability == 'homework.add' else {'track': 'school'}
    body = Planner(capability, args, quote, role=role,
                   objective_type='execution' if capability == 'homework.add' else 'learning').payload
    body['utterance_binding']['quote'] = quote if binding_quote is None else binding_quote
    body['next_step']['expected_evidence'] = ['homework_saved' if capability == 'homework.add' else 'diagnostic_item_presented']
    return body


def setup(tmp_path, monkeypatch, responses):
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path/'gateway.db'))
    settings = settings_for(tmp_path, fallback=False)
    db = Database(settings.database_path); db.initialize()
    privacy = PrivacyLifecycleService(db, settings.data_dir); privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    posts = []
    def handler(request):
        assert request.url.host == 'primary.invalid'
        index = len(posts); posts.append(json.loads(request.read()))
        assert index < len(responses), 'UNEXPECTED_PHYSICAL_MODEL_POST'
        return httpx.Response(200, request=request, json={'choices':[{'finish_reason':'stop',
            'message':{'content':json.dumps(responses[index],ensure_ascii=False)}}],
            'usage':{'prompt_tokens':10,'completion_tokens':20}})
    return settings, db, ModelGateway(settings, transport=httpx.MockTransport(handler)), posts


@pytest.mark.parametrize('quote', [HOMEWORK, PUBLIC_TRIAL_VERB], ids=['public_expected','public_one_char_trial_variant'])
def test_homework_answer_cannot_start_diagnostic_and_next_voice_can_save(tmp_path, monkeypatch, quote):
    settings, db, gateway, posts = setup(tmp_path, monkeypatch, [
        payload('diagnostic.start', quote, role='answer'), payload('homework.add', quote, role='answer')])
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = capture_session(client)
        voice = capture(client, settings, monkeypatch, sid, quote, key='first-homework-voice')
        first = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript':quote,
            'voice_input_id':voice,'cloud_text_allowed':True,'client_action_id':'local-asr-'+voice})
        assert first.status_code == 200
        denied = first.json()
        assert denied['action']['type'] == 'none', 'DECLARATIVE_HOMEWORK_MUST_NOT_START_DIAGNOSTIC'
        assert denied['action']['source'] == 'cloud_planner_unavailable'
        assert denied['action']['retryable'] is True
        assert '保留' in denied['response_text'] and '没有执行' in denied['response_text']
        with db.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0] == 'homework_capture'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM agent_plan WHERE session_id=?',(sid,)).fetchone()[0] == 0
            assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?',(voice,)).fetchone()[0] == quote
            assert c.execute('SELECT utterance FROM learning_input_arrival WHERE client_action_id=?',('local-asr-'+voice,)).fetchone()[0] == quote
            first_turn = c.execute('SELECT transcript,tool_status FROM agent_turn WHERE agent_turn_id=?',(denied['agent_turn_id'],)).fetchone()
            assert first_turn['transcript'] == quote
            calls = list(c.execute('SELECT status,error_code FROM model_call WHERE session_id=? ORDER BY rowid',(sid,)))
            assert len(calls) == 1 and calls[0]['status'] == 'failed'
            assert calls[0]['error_code'] == 'MODEL_DIAGNOSTIC_CURRENT_REQUEST_REQUIRED'
        completed = client.put('/v1/agent-turns/'+denied['agent_turn_id']+'/outcome',
            json={'final_response':denied['response_text'],'tool_status':'completed'})
        assert completed.status_code == 200
        # A new, explicit child turn uses the existing voice/arrival/Runtime path;
        # the denied turn is never retried or reinterpreted automatically.
        next_voice = capture(client, settings, monkeypatch, sid, quote, key='clarified-homework-voice')
        second = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript':quote,
            'voice_input_id':next_voice,'cloud_text_allowed':True,'client_action_id':'local-asr-'+next_voice})
        assert second.status_code == 200
        saved = second.json()
        assert saved['action']['capability'] == 'homework.add'
        executed = client.post('/v1/agent-turns/'+saved['agent_turn_id']+'/execute')
        assert executed.status_code == 200, executed.json().get('detail')
        assert len(posts) == 2
        # Protected captured speech remains local history. The next explicit
        # whole child utterance is independently admitted; no old text is exported.
        second_context = json.loads(posts[1]['messages'][1]['content'])
        assert second_context['context']['recent_dialogue'] == []
        assert second_context['context']['session']['state'] == 'homework_capture'
        assert second_context['child_utterance'] == quote
    reopened = Database(settings.database_path)
    with reopened.connect() as c:
        homework = list(c.execute('SELECT child_id,title FROM homework_item WHERE session_id=?',(sid,)))
        assert len(homework) == 1 and homework[0]['title'] == quote and homework[0]['child_id'] == 'child-primary-4'
        assert c.execute('SELECT state FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0] == 'homework_capture'
        assert c.execute('SELECT COUNT(*) FROM homework_item WHERE child_id=?',('child-junior-3',)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_input_arrival WHERE session_id=?',(sid,)).fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM voice_input WHERE session_id=?',(sid,)).fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM agent_turn WHERE session_id=?',(sid,)).fetchone()[0] == 2


@pytest.mark.parametrize('quote', ['现在开始测评。', '做这张卷。'])
def test_explicit_full_current_request_remains_schedulable(tmp_path, monkeypatch, quote):
    settings, db, gateway, posts = setup(tmp_path, monkeypatch, [payload('diagnostic.start', quote)])
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = capture_session(client)
        voice = capture(client, settings, monkeypatch, sid, quote, key='explicit-diagnostic-voice')
        heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript':quote,
            'voice_input_id':voice,'cloud_text_allowed':True,'client_action_id':'local-asr-'+voice})
        assert heard.status_code == 200
        assert heard.json()['action']['type'] == 'start_diagnostic'
        assert heard.json()['action']['capability'] == 'diagnostic.start'
        assert len(posts) == 1
    with Database(settings.database_path).connect() as c:
        row = c.execute('SELECT s.capability_name FROM agent_plan p JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=?',
                        (heard.json()['agent_turn_id'],)).fetchone()
        assert row[0] == 'diagnostic.start'
        assert c.execute('SELECT COUNT(*) FROM homework_item WHERE session_id=?',(sid,)).fetchone()[0] == 0


@pytest.mark.parametrize('role', ['answer','acknowledgement','correction','question','companionship'])
def test_diagnostic_requires_task_request_role(role):
    quote='做这张卷。'; body=payload('diagnostic.start',quote,role=role)
    decision=parse_agent_plan_output(json.dumps(body,ensure_ascii=False),'homework_capture',CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError,match='DIAGNOSTIC_CURRENT_REQUEST_REQUIRED'):
        AgentOrchestrator._validate_cloud_utterance_binding(json.dumps(body,ensure_ascii=False),quote,decision,None,original_transcript=quote)


@pytest.mark.parametrize('quote,binding,original', [
    ('请现在做这张卷。','做这张卷。','请现在做这张卷。'),
    ('做这张卷。','做这张卷。','原始全文：做这张卷。'),
])
def test_diagnostic_does_not_authorize_substring_or_changed_local_text(quote,binding,original):
    body=payload('diagnostic.start',quote,binding_quote=binding)
    decision=parse_agent_plan_output(json.dumps(body,ensure_ascii=False),'homework_capture',CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError,match='DIAGNOSTIC_CURRENT_REQUEST_REQUIRED'):
        AgentOrchestrator._validate_cloud_utterance_binding(json.dumps(body,ensure_ascii=False),quote,decision,None,original_transcript=original)
