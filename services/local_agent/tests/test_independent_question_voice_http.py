"""New-question voice control keeps the old answer and binds a fresh source."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_unseen_diagnostic_http import ready, CHILD, BASE
from test_content_free_voice_control_http import ControlGateway
from test_voice_runtime_binding import capture, interpret

QUOTE = '再给我一道校内题，我要独立做。'


def context(tmp_path, monkeypatch, *, answered=True):
    old, db, sid, first, second = ready(tmp_path,with_spoken_tuple=True)
    old.__exit__(None, None, None)
    settings = Settings(data_dir=tmp_path, database_path=db.path,
                        maintenance_enabled=False, session_token='isolated-first-source')
    gateway = ControlGateway(QUOTE, 'learning.independent_check')
    client = TestClient(create_app(settings, model_gateway=gateway),
                        headers={'X-HSC-Session':settings.session_token})
    client.__enter__()
    unlocked=client.post('/v1/parent/unlock',json={'pin':'7391'})
    assert unlocked.status_code==200,unlocked.text
    client.headers['X-HSC-Parent']=unlocked.json()['parent_token']
    assert client.put('/v1/parent/privacy',json={'cloud_model_text_allowed':True}).status_code==200
    states=['check_in','classroom_recall','homework_capture','plan_review','study']
    for index,(before,after) in enumerate(zip(states,states[1:])):
        if before=='classroom_recall':
            assert client.post(f'/v1/sessions/{sid}/recalls/none',json={'client_action_id':'fresh-no-recall'}).status_code==200
        if before=='homework_capture':
            assert client.post(f'/v1/sessions/{sid}/homework/none',json={'client_action_id':'fresh-no-homework'}).status_code==200
        r=client.post(f'/v1/sessions/{sid}/transition',json=dict(expected_state=before,target_state=after,reason='owned synthetic independent flow',client_action_id=f'fresh-study-{index}'))
        assert r.status_code==200,r.text
    base=f'/v1/sessions/{sid}/diagnostic-prompt'
    r=client.post(base+'/prepare',json=dict(track='school',preferred_item_id=first['diagnostic_item_id'],client_action_id='voice-original-question'))
    assert r.status_code==200,r.text
    original=r.json()['prompt']
    if answered:
        r=client.post(base+'/answer',json=dict(prompt_id=original['prompt_id'],item_version=original['item_version'],answer='1,2',client_action_id='voice-original-answer'))
        assert r.status_code==200,r.text
    voice=capture(client,settings,monkeypatch,sid,QUOTE,{'kind':'diagnostic_help','binding':{k:original[k] for k in ('prompt_id','item_version')}},child=CHILD)
    return client,db,settings,gateway,sid,original,second,voice


@pytest.mark.parametrize('spoken',['3,4','三、四。'])
def test_new_question_voice_uses_existing_diagnostic_and_durable_independent_score(tmp_path,monkeypatch,spoken):
    client,db,settings,gateway,sid,original,second,voice=context(tmp_path,monkeypatch)
    try:
        turn=interpret(client,sid,QUOTE,'voice-request-independent',voice)
        assert turn['action']['type']=='start_diagnostic',turn
        assert turn['action']['track']=='school' and turn['action']['require_unexposed'] is True
        assert turn['runtime_capability']=='diagnostic.start'
        projection=json.loads(gateway.requests[0].messages[-1]['content'])
        assert set(projection)=={'transcript','state'} and projection['transcript']==QUOTE
        assert all(type(v) is bool for v in projection['state'].values())
        sent=json.dumps(gateway.requests[0].messages,ensure_ascii=False)
        for private in (sid,original['prompt_id'],original['item_version'],original['item']['prompt'],'2,3'):
            assert private not in sent
        admitted=client.post(f'/v1/sessions/{sid}/agent-turns/{turn["agent_turn_id"]}/control-admission')
        assert admitted.status_code==200,admitted.text
        base=f'/v1/sessions/{sid}/diagnostic-prompt'
        r=client.post(base+'/prepare',json=dict(track=turn['action']['track'],require_unexposed=True,subject=turn['action']['subject'],knowledge_node_id=turn['action']['knowledge_node_id'],
            preferred_item_id=second['diagnostic_item_id'],client_action_id='voice-diagnostic:'+turn['agent_turn_id']))
        assert r.status_code==200,r.text
        prepared=r.json();new=prepared['prompt']
        assert new['diagnostic_item_id']==second['diagnostic_item_id']
        outcome=dict(tool_status='completed',final_response='新原题已经准备，等待独立作答。',result={'execution_receipt':dict(
            capability='diagnostic.start',execution_location='client_device_bridge',action_type='start_diagnostic',
            status='completed',completed_at=datetime.now(timezone.utc).isoformat(),output=prepared)})
        r=client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",json=outcome)
        assert r.status_code==200,r.text
        assert r.json()['tool_status']=='completed',r.json()
        with db.connect() as c:
            binding=json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0])['local_learning_binding']
            assert binding['prompt_id']==new['prompt_id'] and binding['item_version']==new['item_version']
        gateway.quote=spoken;gateway.control=None;gateway.role='answer'
        answer_voice=capture(client,settings,monkeypatch,sid,spoken,new,key='voice-new-answer',child=CHILD)
        answer_turn=interpret(client,sid,spoken,'voice-new-answer-turn',answer_voice)
        assert answer_turn['action']['type']=='none',answer_turn
        candidate=answer_turn['action']['pending_answer_candidate']['answer']
        assert candidate==spoken
        assert client.post(f"/v1/agent-turns/{answer_turn['agent_turn_id']}/execute").status_code==200
        gateway.quote='刚才那句就是我的答案';gateway.role='control';gateway.control='learning.independent_check'
        confirmation=capture(client,settings,monkeypatch,sid,gateway.quote,new,key='voice-confirm-new-answer',child=CHILD)
        answer_turn=interpret(client,sid,gateway.quote,'voice-confirm-new-answer-turn',confirmation)
        assert answer_turn['action']['type']=='submit_diagnostic_answer',answer_turn
        assert answer_turn['action']['answer']==candidate
        body=dict(prompt_id=new['prompt_id'],item_version=new['item_version'],answer=candidate,independent=True,hint_level=0,
            client_action_id='voice-new-answer-write',authorized_turn_id=answer_turn['agent_turn_id'])
        scored=client.post(base+'/answer',json=body)
        assert scored.status_code==200,scored.text
        assert scored.json()['attempt']['score_status']=='verified_local'
        assert scored.json()['attempt']['correctness']==1
        with db.connect() as c:
            rows=c.execute('SELECT a.diagnostic_item_id,e.independent,e.hint_level FROM diagnostic_attempt a JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id AND e.session_id=a.session_id WHERE a.session_id=? ORDER BY a.rowid',(sid,)).fetchall()
            assert len(rows)==2 and tuple(rows[1])==(second['diagnostic_item_id'],1,0)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id!=?',(CHILD,)).fetchone()[0]==0
        calls=len(gateway.requests)
        client.__exit__(None,None,None);client=None
        with TestClient(create_app(settings,model_gateway=gateway),headers={'X-HSC-Session':settings.session_token}) as restarted:
            assert interpret(restarted,sid,QUOTE,'voice-request-independent',voice)['agent_turn_id']==turn['agent_turn_id']
            assert restarted.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",json=outcome).status_code==200
            assert restarted.post(base+'/answer',json=body).json()['attempt']['attempt_id']==scored.json()['attempt']['attempt_id']
        assert len(gateway.requests)==calls
    finally:
        if client:client.__exit__(None,None,None)


@pytest.mark.parametrize('quote',['不要给我新题，我还没想好','她说“再给我一道题”，不是我在请求','如果我让你出另一道题，会怎样','先别换题','再来一道题，然后写出答案'])
def test_new_question_refusal_quote_hypothesis_or_answer_leak_overrides_model_label(quote):
    result=VoiceControlIntent.parse(json.dumps(dict(role='control',control='learning.independent_check',quote=quote,confidence=.99)),quote)
    assert result['status']=='clarify'


def test_waiting_original_cannot_be_discarded_by_independent_request(tmp_path,monkeypatch):
    client,db,_,_,sid,original,_,voice=context(tmp_path,monkeypatch,answered=False)
    try:
        result=interpret(client,sid,QUOTE,'waiting-original-new-request',voice)
        assert result['action']['type']=='none',result
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)


@pytest.mark.parametrize('stage',['admission','completion'])
@pytest.mark.parametrize('mutation',['original','consent','voice','unexposed_flag','node_scope'])
def test_changed_authority_cannot_complete_new_question(tmp_path,monkeypatch,stage,mutation):
    client,db,_,_,sid,original,second,voice=context(tmp_path,monkeypatch)
    try:
        turn=interpret(client,sid,QUOTE,'voice-mutation-independent',voice)
        assert turn['action']['type']=='start_diagnostic',turn
        path=f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission"
        if stage=='completion':
            assert client.post(path).status_code==200
            r=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json=dict(track='school',require_unexposed=True,subject=turn['action']['subject'],knowledge_node_id=turn['action']['knowledge_node_id'],
                preferred_item_id=second['diagnostic_item_id'],client_action_id='voice-diagnostic:'+turn['agent_turn_id']))
            assert r.status_code==200,r.text
            prepared=r.json()
        if mutation=='consent':
            assert client.put('/v1/parent/privacy',json={'cloud_model_text_allowed':False}).status_code==200
        elif mutation=='voice':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code==200
        else:
            with db.connect() as c:
                if mutation=='original':
                    c.execute("UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?",(original['prompt_id'],))
                elif stage=='admission':
                    raw=json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0])
                    if mutation=='node_scope':raw['knowledge_node_id']='other-learning-node'
                    else:raw['require_unexposed']=False
                    c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(raw),turn['agent_turn_id']))
                else:
                    if mutation=='node_scope':
                        c.execute("UPDATE diagnostic_prompt SET request_json=json_set(request_json,'$.knowledge_node_id','other-learning-node') WHERE prompt_id=?",(prepared['prompt']['prompt_id'],))
                    else:c.execute("UPDATE diagnostic_prompt SET request_json=json_remove(request_json,'$.require_unexposed') WHERE prompt_id=?",(prepared['prompt']['prompt_id'],))
                c.commit()
        if stage=='admission':
            rejected=client.post(path)
        else:
            rejected=client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",json=dict(tool_status='completed',final_response='不能提前确认',result={'execution_receipt':dict(
                capability='diagnostic.start',execution_location='client_device_bridge',action_type='start_diagnostic',status='completed',
                completed_at=datetime.now(timezone.utc).isoformat(),output=prepared)}))
        assert rejected.status_code==409,rejected.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0]!='completed'
    finally:client.__exit__(None,None,None)
