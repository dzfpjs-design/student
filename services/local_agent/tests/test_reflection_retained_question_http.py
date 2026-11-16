"""Synthetic ASR + real HTTP/Runtime/draft; no external model or family data."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from home_study_agent.agent_orchestrator import AgentOrchestrator
from test_app import client_for
from test_voice_runtime_binding import capture, interpret

CHILD='child-primary-4'
class UnknownControl:
    configured=True
    def __init__(self,text):self.text=text;self.requests=[]
    async def complete(self,request):
        self.requests.append(request)
        return ModelResult(content=json.dumps({'role':'unknown','control':None,'quote':self.text,'confidence':.9}),
                           provider='test',model='synthetic-unknown-control',latency_ms=1)

def retained_original(client):
    repo=client.app.state.repository;db=client.app.state.agent_orchestrator.database
    sid=repo.start_session(CHILD,'retained-reflection-session')['session_id']
    with db.connect() as c:c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,));c.commit()
    p=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={
        'preferred_item_id':'diag-p4-arithmetic-closed-1','client_action_id':'retained-original'}).json()['prompt']
    answered=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={
        'prompt_id':p['prompt_id'],'item_version':p['item_version'],'answer':'12','client_action_id':'retained-answer'})
    assert answered.status_code==200,answered.text
    assert answered.json()['status']=='answered'
    entered=client.post(f'/v1/sessions/{sid}/transition',json={
        'expected_state':'study','target_state':'reflection','reason':'settled','client_action_id':'enter-reflection'})
    assert entered.status_code==200,entered.text
    return sid,p

def original_rows(db):
    with db.connect() as c:
        return {table:[tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('diagnostic_prompt','diagnostic_attempt','mistake','mastery_evidence')
                if c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone()}

def finish_saved(client,turn):
    # The client bridge completes only after the actual full draft PUT below.
    receipt={'execution_receipt':{'capability':'reflection.update','execution_location':'client_device_bridge',
        'action_type':'update_reflection','status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),
        'output':{'synthetic_client_flow':True}}}
    r=client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",json={
        'final_response':turn['response_text'],'tool_status':'completed','result':receipt})
    assert r.status_code==200,r.text

@pytest.mark.parametrize('text,field,value',[
    ('今天学会了把面积分成两个长方形。','learned','把面积分成两个长方形'),
    ('今天学会了用图解释面积。','learned','用图解释面积'),
    ('今天学会了为什么通分后才能相加。','learned','为什么通分后才能相加'),
    ('还不确定题目条件够不够。','difficult','题目条件够不够'),
    ('明天先把容易混淆的条件写下来。','next_action','把容易混淆的条件写下来'),
])
def test_retained_answered_question_cannot_steal_current_reflection_save(tmp_path,monkeypatch,text,field,value):
    gateway=UnknownControl(text)
    with client_for(tmp_path,model_gateway=gateway,agent_runtime_enabled=True) as client:
        sid,p=retained_original(client);db=client.app.state.agent_orchestrator.database
        settings=client.app.state.agent_orchestrator.settings;before=original_rows(db)
        voice=capture(client,settings,monkeypatch,sid,text,p,'reflection-recording',child=CHILD)
        turn=interpret(client,sid,text,'current-reflection-answer',voice)
        assert turn['action']['type']=='update_reflection',turn['action'].get('source')
        assert turn['action']['field']==field and turn['action']['value']==value
        assert turn['action']['voice_input_id']==voice
        assert turn['action']['voice_input_binding']['question_reference']['binding']=={
            'prompt_id':p['prompt_id'],'item_version':p['item_version']}
        assert turn['runtime_capability']=='reflection.update'
        payload={'learned_text':'已有自述，未当掌握','difficult_text':'原不确定项','next_action':''}
        payload[{'learned':'learned_text','difficult':'difficult_text','next_action':'next_action'}[field]]=value
        saved=client.put(f'/v1/sessions/{sid}/drafts/reflection',json={'payload':payload});assert saved.status_code==200
        finish_saved(client,turn)
        assert client.get(f'/v1/sessions/{sid}').json()['drafts']['reflection']['payload']==payload
        assert original_rows(db)==before
        assert not gateway.requests
        with db.connect() as c:
            steps=c.execute('SELECT s.capability_name,s.status FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',(turn['agent_turn_id'],)).fetchall()
            assert [tuple(x) for x in steps]==[('reflection.update','completed')]
            assert c.execute('SELECT count(*) FROM session_draft WHERE session_id=? AND section="reflection"',(sid,)).fetchone()[0]==1
    with TestClient(create_app(settings,model_gateway=gateway)) as restarted:
        replay=interpret(restarted,sid,text,'current-reflection-answer',voice)
        assert replay['agent_turn_id']==turn['agent_turn_id'] and replay['action']==turn['action']
        assert restarted.get(f'/v1/sessions/{sid}').json()['drafts']['reflection']['payload']==payload
        assert original_rows(db)==before and not gateway.requests

@pytest.mark.parametrize('state,text',[('reflection','今天学会了通分，为什么还要约分？'),
                                     ('reflection','为什么通分后才能相加？'),
                                     ('study','今天学会了用图解释面积。')])
def test_real_question_and_study_keep_tutor_intent(state,text):
    intent,_,action=AgentOrchestrator._classify(state,text,5)
    assert intent=='tutor_question' and action['type']=='ask_tutor'

@pytest.mark.parametrize('state,text',[
    ('study','今天学会了把面积分成两个长方形。'),
    ('reflection','这个地方再想一想。'),
])
def test_stage_or_unknown_reply_keeps_original_control_gate(tmp_path,monkeypatch,state,text):
    gateway=UnknownControl(text)
    with client_for(tmp_path,model_gateway=gateway,agent_runtime_enabled=True) as client:
        sid,p=retained_original(client);db=client.app.state.agent_orchestrator.database;before=original_rows(db)
        with db.connect() as c:c.execute('UPDATE daily_session SET state=? WHERE session_id=?',(state,sid));c.commit()
        voice=capture(client,client.app.state.agent_orchestrator.settings,monkeypatch,sid,text,p,'unknown-recording',child=CHILD)
        turn=interpret(client,sid,text,'not-reflection-permission',voice)
        assert turn['action']['type']=='none'
        assert turn['action']['source']=='content_free_control_clarification'
        assert len(gateway.requests)==1
        assert client.get(f'/v1/sessions/{sid}').json()['drafts'].get('reflection') is None
        assert original_rows(db)==before

@pytest.mark.parametrize('invalid','cancelled cross_child'.split())
def test_saved_recording_cannot_borrow_current_reflection_owner(tmp_path,monkeypatch,invalid):
    text='还不确定面积单位怎么换。';gateway=UnknownControl(text)
    with client_for(tmp_path,model_gateway=gateway,agent_runtime_enabled=True) as client:
        sid,p=retained_original(client);db=client.app.state.agent_orchestrator.database;before=original_rows(db)
        voice=capture(client,client.app.state.agent_orchestrator.settings,monkeypatch,sid,text,p,'invalid-recording',child=CHILD)
        target=sid
        if invalid=='cancelled':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/invalid-recording/cancel').status_code==200
        else:
            # Corrupt one saved owner pointer; opening a second household
            # session would itself be forbidden before this binding check.
            with db.connect() as c:
                c.execute("UPDATE voice_input SET child_id='child-junior-3' WHERE voice_input_id=?",(voice,));c.commit()
        result=client.post(f'/v1/sessions/{target}/agent-turn',json={
            'transcript':text,'client_action_id':'unowned-reflection','voice_input_id':voice,'cloud_text_allowed':True})
        assert result.status_code==409,result.text
        assert client.get(f'/v1/sessions/{target}').json()['drafts'].get('reflection') is None
        assert original_rows(db)==before and not gateway.requests

def test_same_action_id_cannot_replace_saved_reflection_payload(tmp_path,monkeypatch):
    text='还不确定面积单位怎么换。';gateway=UnknownControl(text)
    with client_for(tmp_path,model_gateway=gateway,agent_runtime_enabled=True) as client:
        sid,p=retained_original(client);db=client.app.state.agent_orchestrator.database;settings=client.app.state.agent_orchestrator.settings
        voice=capture(client,settings,monkeypatch,sid,text,p,'original-reflection-recording',child=CHILD)
        turn=interpret(client,sid,text,'durable-reflection-action',voice)
        assert turn['action']['type']=='update_reflection'
        original={'learned_text':'','difficult_text':turn['action']['value'],'next_action':''}
        assert client.put(f'/v1/sessions/{sid}/drafts/reflection',json={'payload':original}).status_code==200
        finish_saved(client,turn)
        altered='明天先查一查面积单位。'
        newvoice=capture(client,settings,monkeypatch,sid,altered,p,'changed-recording',child=CHILD)
        result=client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':altered,'client_action_id':'durable-reflection-action','voice_input_id':newvoice,'cloud_text_allowed':True})
        assert result.status_code==409,result.text
        assert client.get(f'/v1/sessions/{sid}').json()['drafts']['reflection']['payload']==original
        assert not gateway.requests
