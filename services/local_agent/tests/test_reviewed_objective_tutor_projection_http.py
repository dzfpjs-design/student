"""Only the reviewed exact-source objective family can join D015 Tutor projection."""
import json

import pytest
import httpx
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_unseen_diagnostic_http import ready, CHILD, BASE
from test_tutor_child_model_controls import RecordingGateway


def objective_client(tmp_path,gateway):
    old,db,sid,first,second=ready(tmp_path)
    old.__exit__(None,None,None)
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,session_token='objective-source-test',
        model_provider='deepseek',model_api_key='synthetic-key',model_base_url='https://primary.invalid',
        fallback_model_provider='qwen',fallback_model_api_key='synthetic-verifier',fallback_model_base_url='https://verifier.invalid',fallback_model_name='synthetic-verifier')
    if callable(gateway):gateway=gateway(settings)
    client=TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token})
    client.__enter__()
    r=client.post('/v1/parent/unlock',json={'pin':'7391'});assert r.status_code==200,r.text
    client.headers['X-HSC-Parent']=r.json()['parent_token']
    assert client.put('/v1/parent/privacy',json={'cloud_model_text_allowed':True}).status_code==200
    r=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json=dict(track='school',preferred_item_id=second['diagnostic_item_id'],client_action_id='objective-source-prompt'))
    assert r.status_code==200,r.text
    prompt=r.json()['prompt']
    payload=dict(subject=prompt['item']['subject'],question=prompt['item']['prompt'],prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'],client_action_id='objective-source-help')
    return client,db,settings,sid,first,second,payload


def test_reviewed_second_original_tutor_projects_only_question_replays_and_restarts(tmp_path):
    gateway=RecordingGateway()
    client,db,settings,sid,first,second,payload=objective_client(tmp_path,gateway)
    try:
        r=client.post(f'/v1/sessions/{sid}/tutor',json=payload)
        assert r.status_code==200,r.text
        turn=r.json();assert turn['status']=='completed',turn
        assert len(gateway.requests)==1
        request=gateway.requests[0];sent=json.dumps(request.messages,ensure_ascii=False)
        assert payload['question'] in sent
        for private in ('3,4',sid,CHILD,second['candidate_id'],second['diagnostic_item_id'],payload['prompt_id'],payload['item_version']):
            assert private not in sent
        assert request.context_manifest['privacy_transform']['source_projection']['fields']==['subject','question']
        assert turn['output_envelope']['local_only'] is True
        assert turn['output_envelope']['cloud_transmission_allowed'] is False
        assert client.post(f'/v1/sessions/{sid}/tutor',json=payload).json()['turn_id']==turn['turn_id']
        client.__exit__(None,None,None);client=None
        with TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),headers={'X-HSC-Session':settings.session_token}) as restarted:
            replay=restarted.post(f'/v1/sessions/{sid}/tutor',json=payload)
            assert replay.status_code==200,replay.text
            assert replay.json()['turn_id']==turn['turn_id']
        assert len(gateway.requests)==1
    finally:
        if client:client.__exit__(None,None,None)


@pytest.mark.parametrize('mutation',['root_withdrawn','package_withdrawn','validator_changed','consent_withdrawn'])
def test_second_original_source_or_permission_change_blocks_dispatch(tmp_path,mutation):
    gateway=RecordingGateway()
    client,db,_,sid,first,second,payload=objective_client(tmp_path,gateway)
    try:
        if mutation in {'root_withdrawn','package_withdrawn'}:
            candidate=first if mutation=='root_withdrawn' else second
            r=client.post(BASE+f"/{candidate['candidate_id']}/withdraw",json=dict(action_id='objective-withdraw-source',expected_version=4,note='隔离源撤回'))
            assert r.status_code==200,r.text
        elif mutation=='consent_withdrawn':
            assert client.put('/v1/parent/privacy',json={'cloud_model_text_allowed':False}).status_code==200
        else:
            with db.connect() as c:
                c.execute("UPDATE curriculum_supply_package SET payload_json=json_set(payload_json,'$.validator','unreviewed-model-variant') WHERE candidate_id=?",(second['candidate_id'],));c.commit()
        r=client.post(f'/v1/sessions/{sid}/tutor',json=payload)
        assert r.status_code in ({200,403,409} if mutation=='consent_withdrawn' else {200,409}),r.text
        if r.status_code==200:assert r.json()['status']!='completed',r.json()
        assert not gateway.requests
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM tutor_turn WHERE status='completed'").fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)


def test_second_original_withdrawn_during_model_cannot_persist_or_play(tmp_path):
    class ChangingGateway(RecordingGateway):
        async def complete(self,request):
            result=await super().complete(request)
            with db.connect() as c:
                c.execute("UPDATE curriculum_supply_package SET state='withdrawn' WHERE candidate_id=?",(second['candidate_id'],));c.commit()
            return result
    gateway=ChangingGateway()
    client,db,_,sid,_,second,payload=objective_client(tmp_path,gateway)
    try:
        r=client.post(f'/v1/sessions/{sid}/tutor',json=payload)
        assert r.status_code in {200,409},r.text
        if r.status_code==200:assert r.json()['status']!='completed',r.json()
        assert len(gateway.requests)==1
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM tutor_turn WHERE status='completed'").fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)


def test_second_original_actual_provider_body_excludes_keys_identity_and_package(tmp_path):
    posts=[]
    def provider(request):
        posts.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'VERIFIED' if request.url.host=='verifier.invalid' else '先看方程的一般形式，你能指出二次项系数吗？'}}],
            'usage':{'prompt_tokens':15,'completion_tokens':20}})
    client,db,_,sid,first,second,payload=objective_client(tmp_path,lambda settings:ModelGateway(settings,transport=httpx.MockTransport(provider)))
    try:
        r=client.post(f'/v1/sessions/{sid}/tutor',json=payload)
        assert r.status_code==200,r.text
        assert posts
        wire=json.dumps(posts,ensure_ascii=False)
        assert payload['question'] in wire
        for private in ('3,4',CHILD,sid,first['candidate_id'],second['candidate_id'],second['diagnostic_item_id'],payload['prompt_id'],payload['item_version'],'source_snapshot_json','reviewed_payload_hash'):
            assert private not in wire
        turn=r.json()
        initial=client.post(f'/v1/sessions/{sid}/tutor-playback/begin',json=dict(turn_id=turn['turn_id'],client_action_id='objective-before-withdrawal-playback'))
        assert initial.status_code==200,initial.text
        with db.connect() as c:
            c.execute("UPDATE curriculum_supply_package SET state='withdrawn' WHERE candidate_id=?",(second['candidate_id'],));c.commit()
        playback=client.post(f'/v1/sessions/{sid}/tutor-playback/begin',json=dict(turn_id=turn['turn_id'],client_action_id='objective-withdrawn-playback'))
        assert playback.status_code==409,playback.text
        restored=client.get(f'/v1/sessions/{sid}/tutor-playback')
        assert restored.status_code==200,restored.text
        assert restored.json()['playback']['status']=='invalidated',restored.json()
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)
