"""Actual ASR -> role -> saved response -> source-bounded coaching HTTP flow."""
import json
from dataclasses import replace
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from test_content_free_voice_control_http import ControlGateway
from test_tutor_check_response_http import lesson
from test_voice_runtime_binding import capture,interpret

TEXT='我先找出三个系数，再代入公式。'
COACH='你说到了先找系数这一步。我们先检查公式分子里负号的位置，你能说一说为什么要注意它吗？'

class CoachingGateway(ControlGateway):
    def __init__(self):
        super().__init__(TEXT,None,role='answer')
        self.coaching=[]
        self.hook=None
    async def complete(self,request):
        if request.request_type=='voice_interaction':return await super().complete(request)
        self.coaching.append(request)
        if self.hook:self.hook()
        return ModelResult(content=json.dumps({'blackboard':{'title':'检查这一步','segments':[{'narration':COACH,'blocks':[{'kind':'question','text':'为什么要注意负号？'}]}]}},ensure_ascii=False),provider='test',model='checked-coaching',latency_ms=1)


def heard_check(tmp_path,monkeypatch,gateway):
    client,db,settings,_,sid,parent,_=lesson(tmp_path,scored=True);client.__exit__(None,None,None)
    client=TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token});client.__enter__()
    target=parent['output_envelope']['local_teaching_control']['question_target']['binding']
    voice=capture(client,settings,monkeypatch,sid,TEXT,{'kind':'diagnostic_help','binding':{k:target[k] for k in ('prompt_id','item_version')}},key='coached-check-capture',child='child-junior-3')
    heard=interpret(client,sid,TEXT,'coached-check-reply',voice)
    assert heard['action']['type']=='submit_tutor_check_response'
    return client,db,settings,sid,parent,heard


def test_saved_voice_check_gets_actual_bound_coaching_and_replays_without_new_model(tmp_path,monkeypatch):
    gateway=CoachingGateway();client,db,settings,sid,parent,heard=heard_check(tmp_path,monkeypatch,gateway)
    try:
        execute=f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        result=client.post(execute)
        assert result.status_code==200,result.text
        value=result.json()['execution']
        assert value.get('coaching_status')=='completed',value
        taught=value['tutor_turn']
        assert taught['question']==parent['question'] and taught['parent_turn_id']==parent['turn_id']
        assert taught['answer']==COACH and value['response_text']==COACH
        envelope=taught['output_envelope']
        assert envelope['local_only'] is True and envelope['cloud_transmission_allowed'] is False
        assert envelope['independent_evidence'] is False and envelope['mastery_update_allowed'] is False
        assert envelope['check_coaching_binding']['feedback_id']==value['result']['response']['feedback_id']
        assert len(gateway.coaching)==1
        wire=json.dumps(gateway.coaching[0].messages,ensure_ascii=False)
        system=gateway.coaching[0].messages[0]['content']
        assert '不得预设孩子已经回答' not in system
        assert '不要重复已经回答的检查' in system
        assert '向下一小步推进' in system
        assert TEXT in wire and parent['answer'] in wire and parent['question'] in wire
        for private in [sid,parent['turn_id'],value['result']['response']['feedback_id'],'child-junior-3']:
            assert private not in wire
        replay=client.post(execute)
        assert replay.status_code==200,replay.text
        assert replay.json()['execution']['tutor_turn']['turn_id']==taught['turn_id'] and len(gateway.coaching)==1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
        client.__exit__(None,None,None);client=None
        with TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token}) as restarted:
            again=restarted.post(execute)
            assert again.status_code==200,again.text
            assert again.json()['execution']['tutor_turn']['turn_id']==taught['turn_id'] and len(gateway.coaching)==1
    finally:
        if client:client.__exit__(None,None,None)


import pytest

@pytest.mark.parametrize('change',['feedback','withdraw','consent_cycle'])
def test_coaching_result_race_cannot_attach_to_changed_evidence(tmp_path,monkeypatch,change):
    gateway=CoachingGateway();client,db,_,_,_,heard=heard_check(tmp_path,monkeypatch,gateway)
    def mutate():
        with db.connect() as c:
            if change=='feedback':c.execute("UPDATE tutor_feedback SET response_text='changed original speech'")
            elif change=='withdraw':c.execute("UPDATE curriculum_first_source SET state='withdrawn'")
            else:
                original=c.execute("SELECT * FROM consent_record WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
                cols=list(original.keys());values=[original[k] for k in cols];values[cols.index('consent_id')]='changed-consent-cycle'
                c.execute('INSERT INTO consent_record ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')',values)
            c.commit()
    gateway.hook=mutate
    try:
        response=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code==200,response.text
        value=response.json()['execution']
        assert value['coaching_status']=='failed' and 'tutor_turn' not in value
        assert len(gateway.coaching)==1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
    finally:client.__exit__(None,None,None)


def test_unknown_coaching_keeps_original_response_and_never_automatically_resubmits(tmp_path,monkeypatch):
    from home_study_agent.model_gateway import ModelResponseError
    gateway=CoachingGateway();client,db,_,_,_,heard=heard_check(tmp_path,monkeypatch,gateway)
    def uncertain():raise ModelResponseError('MODEL_TIMEOUT',fallback_eligible=False)
    gateway.hook=uncertain
    try:
        endpoint=f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        first=client.post(endpoint);assert first.status_code==200,first.text
        receipt=first.json()['execution'];assert receipt['coaching_status']=='failed'
        assert receipt['result']['response']['response_text']==TEXT
        again=client.post(endpoint);assert again.status_code==200,again.text
        assert again.json()['execution']['result']['response']==receipt['result']['response']
        assert len(gateway.coaching)==1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
    finally:client.__exit__(None,None,None)


def test_physical_provider_receives_only_current_source_check_and_response(tmp_path,monkeypatch):
    import httpx
    from home_study_agent.model_gateway import ModelGateway
    posts=[]
    class PhysicalGateway(CoachingGateway):
        async def complete(self,request):
            if request.request_type=='voice_interaction':return await super().complete(request)
            return await self.physical.complete(request)
    gateway=PhysicalGateway();client,db,settings,sid,parent,heard=heard_check(tmp_path,monkeypatch,gateway)
    def provider(request):
        posts.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':COACH}}],'usage':{'prompt_tokens':19,'completion_tokens':31}})
    gateway.physical=ModelGateway(settings,transport=httpx.MockTransport(provider))
    try:
        with db.connect() as c:
            c.execute("UPDATE child_profile SET nickname='私密孩子名字',curriculum_region='私密地区' WHERE child_id='child-junior-3'");c.commit()
            baseline=[dict(x) for x in c.execute("SELECT * FROM mastery_update_event WHERE child_id='child-junior-3'")]
        response=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code==200,response.text
        value=response.json()['execution'];assert value['coaching_status']=='completed',value
        assert len(posts)==1
        wire=json.dumps(posts,ensure_ascii=False)
        assert TEXT in wire and parent['answer'] in wire and parent['question'] in wire
        for secret in ['私密孩子名字','私密地区',sid,parent['turn_id'],value['result']['response']['feedback_id'],'exact_answers','source_ref','child-junior-3']:
            assert secret not in wire
        with db.connect() as c:
            assert [dict(x) for x in c.execute("SELECT * FROM mastery_update_event WHERE child_id='child-junior-3'")]==baseline
    finally:client.__exit__(None,None,None)


def test_saved_coaching_playback_revalidates_source_and_does_not_survive_withdrawal(tmp_path,monkeypatch):
    gateway=CoachingGateway();client,db,_,sid,_,heard=heard_check(tmp_path,monkeypatch,gateway)
    try:
        response=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute");assert response.status_code==200,response.text
        turn=response.json()['execution']['tutor_turn']
        base=f'/v1/sessions/{sid}/tutor-playback'
        started=client.post(base+'/begin',json={'turn_id':turn['turn_id'],'client_action_id':'coached-playback-begin'})
        assert started.status_code==200,started.text
        with db.connect() as c:c.execute("UPDATE curriculum_first_source SET state='withdrawn'");c.commit()
        state=client.get(base).json()['playback']
        assert state['status']=='invalidated'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
    finally:client.__exit__(None,None,None)


@pytest.mark.parametrize('text',['请拨19900000001联系我。','初三同学的私密家庭信息。','字'*1201])
def test_local_response_does_not_grant_unbounded_or_identified_coaching_export(tmp_path,text):
    from home_study_agent.tutor_check_coaching import action_id
    client,db,_,gateway,sid,parent,view=lesson(tmp_path)
    try:
        saved=client.post(f'/v1/sessions/{sid}/tutor-check/responses',json={'binding':{'turn_id':parent['turn_id'],'source_version':view['source_version']},'response_text':text,'client_action_id':'private-or-long-check-response'})
        assert saved.status_code==200,saved.text
        fid=saved.json()['response']['feedback_id']
        result=client.post(f'/v1/sessions/{sid}/tutor',json={'subject':parent['subject'],'question':parent['question'],'parent_turn_id':parent['turn_id'],'hint_level':1,'check_response_id':fid,'client_action_id':action_id(sid,fid)})
        assert result.status_code==409,result.text
        assert len(gateway.requests)==1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0]==1
    finally:client.__exit__(None,None,None)


def test_replay_cannot_read_response_while_child_scope_is_being_deleted(tmp_path,monkeypatch):
    gateway=CoachingGateway();client,db,_,_,_,heard=heard_check(tmp_path,monkeypatch,gateway)
    try:
        endpoint=f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        assert client.post(endpoint).status_code==200
        with db.connect() as c:
            c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,status,created_at,updated_at) VALUES ('owned-test-delete','child-junior-3','all_history','pending','2026-10-07T00:00:00+00:00','2026-10-07T00:00:00+00:00')");c.commit()
        rejected=client.post(endpoint)
        assert rejected.status_code==409,rejected.text
        assert len(gateway.coaching)==1
    finally:client.__exit__(None,None,None)
