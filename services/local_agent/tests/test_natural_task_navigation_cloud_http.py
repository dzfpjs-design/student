"""Actual ModelGateway transport and host; synthetic utterance, no paid network."""
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_diagnostic_prompt_http import environment
from test_natural_task_navigation_http import setup_case, hear, execute
from test_voice_runtime_binding import prepare
from test_cloud_first_agent import Planner


def application(settings, db, requests, *, capability='task.start', fail=False, hook=None, confidence=None):
    settings = replace(settings, model_api_key='synthetic-not-a-key', model_provider='qwen',
                       model_name='isolated-navigation', voice_intent_model_name='isolated-navigation')
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        data = json.loads(body['messages'][-1]['content'])
        if hook:
            hook()
        if fail:
            return httpx.Response(403, json={'error':{'message':'isolated denial'}})
        if set(data) == {'transcript','state'}:
            assert all(type(x) is bool for x in data['state'].values())
            output = {'role':'control','control':capability,'quote':data['transcript'],'confidence':.99}
            if capability == 'task.resume':
                output['selector'] = {'subject':None,'task_kind':None,'relative_order':None}
        else:
            # The general planner remains the producer for ordinary unbound speech.
            quote = data['child_utterance']
            arguments = ({'start_selector': {
                'subject': None, 'task_kind': None, 'source_type': None,
                'target_quote': quote,
            }} if capability == 'task.start' else {})
            output = Planner(capability, arguments, quote).payload
        if confidence is not None:
            output['confidence'] = confidence
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(output)}}]})
    return create_app(settings,model_gateway=ModelGateway(settings,transport=httpx.MockTransport(transport)))


@pytest.mark.parametrize('bound', [False,True])
def test_cloud_failure_is_not_replaced_by_local_start(environment, monkeypatch, bound):
    settings,db=environment
    db.initialize()
    requests=[]
    with TestClient(application(settings,db,requests,fail=True)) as client:
        sid,repo=setup_case(client,db,'completed')
        prompt=prepare(client,sid) if bound else None
        heard,_=hear(client,settings,monkeypatch,sid,'开始下一项',cloud=True,prompt=prompt)
        assert requests, heard
        assert heard['action']['type']=='none', heard
        assert ('语言理解服务暂时不可用' if bound else '这次没能确认你的意思') in heard['response_text']
        if not bound:
            assert '等服务恢复' not in heard['response_text']
        assert '这次没有执行操作' in heard['response_text']
        assert '你再说明一下' not in heard['response_text']
        assert repo._get_plan_item_by_id('control-task-2')['status']=='pending'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_unconfigured_control_model_is_not_presented_as_unclear_child_speech(environment, monkeypatch):
    settings, db = environment
    db.initialize()
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = replace(settings, model_provider='qwen', model_api_key='')
    requests = []
    def deny_network(request):
        requests.append(True)
        raise AssertionError('Unconfigured model must not send a request')
    gateway = ModelGateway(settings, transport=httpx.MockTransport(deny_network))
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo = setup_case(client, db, 'completed')
        prompt = prepare(client, sid)
        heard, _ = hear(client, settings, monkeypatch, sid, '暂停学习', cloud=True, prompt=prompt)
        assert heard['action']['type'] == 'none'
        assert '语言理解服务还没有配置好' in heard['response_text']
        assert '这次没有执行操作' in heard['response_text']
        assert '你再说明一下' not in heard['response_text']
        assert heard['action']['understanding_failure_code'] == 'MODEL_UNAVAILABLE'
        assert heard['action']['agent_runtime']['requires_child_reply'] is False
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        assert requests == []


@pytest.mark.parametrize('quote', ['别开始','他说开始任务','上一项没做好'])
def test_even_model_start_does_not_authorize_denial_or_reported_words(environment, monkeypatch, quote):
    settings,db=environment
    db.initialize()
    requests=[]
    with TestClient(application(settings,db,requests)) as client:
        sid,repo=setup_case(client,db,'completed')
        heard,_=hear(client,settings,monkeypatch,sid,quote,cloud=True)
        assert requests
        assert heard['action']['type']=='none', heard
        assert repo._get_plan_item_by_id('control-task-2')['status']=='pending'


def test_bound_plan_question_uses_content_free_query_without_starting(environment, monkeypatch):
    settings,db=environment
    db.initialize()
    requests=[]
    with TestClient(application(settings,db,requests,capability='planning.describe')) as client:
        sid,repo=setup_case(client,db,'active')
        prompt=prepare(client,sid)
        heard,_=hear(client,settings,monkeypatch,sid,'接下来做什么？',cloud=True,prompt=prompt)
        assert heard['runtime_capability']=='planning.describe', heard
        execute(client,heard)
        assert len(requests)==1
        body=requests[0]['messages']
        for secret in (prompt['item']['prompt'],prompt['prompt_id'],prompt['item_version'],sid,'control-task-1'):
            assert secret not in json.dumps(body,ensure_ascii=False)
        assert repo._get_plan_item_by_id('control-task-1')['status']=='active'


def test_unbound_model_start_freezes_arrival_before_transport_changes_plan(environment, monkeypatch):
    settings,db=environment
    db.initialize()
    requests=[]
    def changed():
        with db.connect() as c:
            c.execute("UPDATE plan_item SET source_ref='replacement' WHERE plan_item_id='control-task-2'")
            c.commit()
    with TestClient(application(settings,db,requests,hook=changed),raise_server_exceptions=False) as client:
        sid,repo=setup_case(client,db,'completed')
        from test_voice_runtime_binding import capture
        quote = '接着安排好的下一段来吧'
        voice = capture(client, settings, monkeypatch, sid, quote, None, 'changed-arrival-voice')
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'client_action_id': 'changed-arrival', 'voice_input_id': voice,
            'cloud_text_allowed': True})
        if response.status_code == 200:
            heard = response.json()
            assert heard['runtime_capability'] == 'task.start', heard
            response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'TASK_START_ARRIVAL_CHANGED'
        assert repo._get_plan_item_by_id('control-task-2')['status']=='pending'


def test_completed_answer_capture_can_request_next_without_regrading(environment,monkeypatch):
    settings,db=environment
    db.initialize()
    requests=[]
    with TestClient(application(settings,db,requests)) as client:
        sid,repo=setup_case(client,db,'active')
        prompt=prepare(client,sid)
        from test_voice_runtime_binding import submit
        answer,_=hear(client,settings,monkeypatch,sid,'90',key='actual-answer',prompt=prompt)
        result=submit(client,sid,prompt,answer)
        assert result.status_code==200 and result.json()['status']=='answered',result.text
        repo.complete_plan_item('control-task-1',1,'after-original-answer')
        heard,_=hear(client,settings,monkeypatch,sid,'开始下一项',key='next-after-answer',cloud=True,prompt=prompt)
        assert heard['runtime_capability']=='task.start',heard
        result=execute(client,heard)
        assert result['execution']['result']['plan_item_id']=='control-task-2'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
        assert len(requests)==1
        assert prompt['item']['prompt'] not in json.dumps(requests[0]['messages'],ensure_ascii=False)


def test_low_confidence_reply_saves_words_and_progress_after_application_reopen(environment, monkeypatch):
    from home_study_agent.database import Database
    settings,db=environment
    db.initialize()
    requests=[]
    quote='开始下一项'
    key='low-confidence-durable-words'
    expected='这次没能确认你的意思。你的话和学习进度已保留，这次没有执行操作。你可以再说一遍，或稍后继续。'
    def progress(database,sid):
        with database.connect()as c:
            return ([dict(r)for r in c.execute('SELECT i.*FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE p.session_id=? ORDER BY i.plan_item_id',(sid,))],
                    [dict(r)for r in c.execute('SELECT *FROM daily_plan WHERE session_id=? ORDER BY plan_id',(sid,))],
                    c.execute('SELECT count(*)FROM plan_item_event').fetchone()[0],
                    c.execute('SELECT count(*)FROM diagnostic_attempt').fetchone()[0])
    with TestClient(application(settings,db,requests,confidence=.5))as client:
        sid,_=setup_case(client,db,'completed')
        before=progress(db,sid)
        heard,voice=hear(client,settings,monkeypatch,sid,quote,key=key,cloud=True)
        assert heard['response_text']==expected
        assert heard['intent']=='cloud_understanding_unavailable'
        assert heard['action']['type']=='none'and heard['action']['source']=='cloud_planner_unavailable'
        assert len(requests)==1
        with db.connect()as c:
            row=c.execute('SELECT transcript,response_text,action_json FROM agent_turn WHERE agent_turn_id=?',(heard['agent_turn_id'],)).fetchone()
            assert tuple(row[:2])==(quote,expected)
            assert json.loads(row['action_json'])['type']=='none'
            assert not c.execute('SELECT 1 FROM agent_plan WHERE agent_turn_id=?',(heard['agent_turn_id'],)).fetchone()
            assert tuple(c.execute("SELECT error_code,verification_status FROM model_call WHERE purpose='agent_plan' ORDER BY rowid DESC LIMIT 1").fetchone())==('MODEL_AGENT_PLAN_LOW_CONFIDENCE','rejected')
        assert progress(db,sid)==before
    reopened=Database(db.path)
    assert progress(reopened,sid)==before
    with TestClient(application(settings,reopened,requests,confidence=.5))as client:
        replay=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,'client_action_id':key,'voice_input_id':voice,'cloud_text_allowed':True})
        assert replay.status_code==200,replay.text
        assert replay.json()['agent_turn_id']==heard['agent_turn_id']
        assert replay.json()['response_text']==expected
        assert len(requests)==1
        assert progress(reopened,sid)==before
