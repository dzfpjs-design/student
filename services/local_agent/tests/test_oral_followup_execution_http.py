"""Actual HTTP writers and synthetic captured speech; no paid model or device proof."""
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_pet_oral_supply_http import setup,post,BASE,OTHER
from test_oral_runtime_feedback_http import OralGateway,begin_oral,ANSWER
from test_voice_runtime_binding import capture,interpret,submit


def answer(client,settings,monkeypatch,sid,prompt,gateway,quote,key):
    key='oral-answer-'+key
    gateway.quote=quote
    gateway.requested_control=None
    voice=capture(client,settings,monkeypatch,sid,quote,prompt,key+'-audio')
    heard=interpret(client,sid,quote,key,voice)
    if heard['intent']=='pending_answer_clarification':
        post(client,f"/v1/agent-turns/{heard['agent_turn_id']}/execute",{})
        gateway.quote='刚才那句就是我的答案'
        confirmed=capture(client,settings,monkeypatch,sid,gateway.quote,prompt,key+'-confirm-audio')
        heard=interpret(client,sid,gateway.quote,key+'-confirm',confirmed)
    assert heard['action']['type']=='submit_diagnostic_answer',heard
    assert heard['action']['source_voice_input_id']==voice
    saved=submit(client,sid,prompt,heard,answer=quote,key=key+'-submit')
    assert saved.status_code==200,saved.text
    return saved.json(),voice


@pytest.mark.parametrize('fault',['none','cancel','source','child','transaction_revoke','storage_restart','prepared_cancel','conversation','late_cancel'])
def test_actual_retry_new_capture_replay_and_write_boundaries(tmp_path,monkeypatch,fault):
    initial,settings=setup(tmp_path)
    settings=replace(settings,data_encryption_key=b'R'*32)
    db=Database(initial.path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    gateway=OralGateway()
    headers={'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings,model_gateway=gateway),headers=headers,client=('127.0.0.1',41947),
                    raise_server_exceptions=fault=='late_cancel') as client:
        client.headers['X-HSC-Parent']=post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True,
            'cloud_model_text_allowed':True,'cloud_audio_allowed':False}).status_code==200
        sid,prompt,original,published=begin_oral(client,db,gateway)
        saved,voice=answer(client,settings,monkeypatch,sid,prompt,gateway,ANSWER,'first')
        assert saved['local_learning_evaluation']['status']=='feedback_ready'
        if fault=='conversation':
            gateway.ordinary_chat=True
            gateway.quote='我今天上体育课很开心'
            chat=interpret(client,sid,gateway.quote,'oral-ordinary-chat',None)
            assert chat['runtime_capability']=='conversation.respond',chat
            post(client,f"/v1/agent-turns/{chat['agent_turn_id']}/execute",{})
            with db.connect() as c:
                row=c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(chat['agent_turn_id'],)).fetchone()
                assert row[0] is None,'A neutral chat cannot become an oral learning execution.'
            gateway.ordinary_chat=False
        gateway.quote='我想按刚才的建议补充一下，再说一次'
        gateway.requested_control='oral.retry'
        retry_voice=capture(client,settings,monkeypatch,sid,gateway.quote,prompt,'retry-audio')
        command=interpret(client,sid,gateway.quote,'retry-command',retry_voice)
        assert command['runtime_capability']=='oral.retry',command
        tid=command['agent_turn_id']
        path=f'/v1/agent-turns/{tid}/execute'
        calls=len(gateway.requests)
        if fault=='late_cancel':
            from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
            dispatch=AgentCapabilityExecutor._dispatch
            def cancel_after_actual_prepare(executor,step):
                result=dispatch(executor,step)
                if step['capability_name']=='oral.retry':
                    # A separate request portal, without a second lifespan:
                    # calling the outer TestClient in its own event loop is
                    # invalid and would manufacture a fixture RuntimeError.
                    concurrent=TestClient(client.app,headers=client.headers,client=('127.0.0.1',41949))
                    try:
                        post(concurrent,f'/v1/sessions/{sid}/agent-turn',dict(transcript='取消刚才的请求',
                            client_action_id='late-oral-cancel',cloud_text_allowed=False))
                    finally:
                        concurrent.close()
                return result
            monkeypatch.setattr(AgentCapabilityExecutor,'_dispatch',cancel_after_actual_prepare)
        if fault=='cancel':
            post(client,f'/v1/sessions/{sid}/agent-turn',dict(transcript='取消刚才的请求',client_action_id='oral-cancel',cloud_text_allowed=False))
        elif fault=='source':
            post(client,BASE+'/'+published['candidate_id']+'/withdraw',dict(action_id='withdraw',expected_version=published['version'],note='Synthetic withdrawal.'))
        elif fault=='child':
            post(client,f'/v1/sessions/{sid}/switch-child',dict(target_child_id=OTHER,client_action_id='other-child'))
        elif fault in {'transaction_revoke','storage_restart'}:
            with db.connect() as c:
                if fault=='transaction_revoke':
                    # Fires after all preflight reads, in the same real prompt
                    # INSERT transaction. The in-transaction bind must reject.
                    c.execute('''CREATE TRIGGER revoke_retry AFTER INSERT ON diagnostic_prompt
                        WHEN json_extract(NEW.request_json,'$.oral_authorized_turn_id') IS NOT NULL
                        BEGIN UPDATE voice_control_intent_request SET status='cancelled'; END''')
                else:
                    c.execute('''CREATE TRIGGER fail_retry BEFORE INSERT ON diagnostic_prompt
                        WHEN json_extract(NEW.request_json,'$.oral_authorized_turn_id') IS NOT NULL
                        BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END''')
                c.commit()
        response=client.post(path,json={})
        if fault not in {'none','prepared_cancel','conversation'}:
            assert response.status_code==(500 if fault=='storage_restart' else 409),response.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==(2 if fault=='late_cancel' else 1)
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
                if fault=='late_cancel':
                    active=c.execute("SELECT COUNT(*) FROM agent_objective WHERE status='active'").fetchone()[0]
                    assert active==0
                    status=c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?',(tid,)).fetchone()[0]
                    assert status=='cancelled'
                    from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                    with pytest.raises(InvalidAgentPlanError,match='ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED'):
                        AgentRuntimeStore(db).complete_turn_execution(c,tid,False,'OperationalError')
                    assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?',(tid,)).fetchone()[0]=='cancelled'
                if fault=='transaction_revoke':
                    assert c.execute("SELECT COUNT(*) FROM voice_control_intent_request WHERE status='cancelled'").fetchone()[0]==0
                if fault=='storage_restart':
                    c.execute('DROP TRIGGER fail_retry')
                    c.commit()
            if fault!='storage_restart':
                return
        else:
            assert response.status_code==200,response.text
            first=response.json()['execution']['result']
            assert first['status']=='waiting_answer'
            assert first['prompt']['prompt_id']!=prompt['prompt_id']
            assert first['prompt']['diagnostic_item_id']==prompt['diagnostic_item_id']
    with TestClient(create_app(settings,model_gateway=gateway),headers=headers,client=('127.0.0.1',41948)) as client:
        resumed=post(client,path,{})
        value=resumed['execution']['result']
        new_prompt=value['prompt']
        assert len(gateway.requests)==calls
        assert post(client,path,{})['execution']['result']['prompt']['prompt_id']==new_prompt['prompt_id']
        assert value['required_hint_level']==1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
        if fault=='prepared_cancel':
            post(client,f'/v1/sessions/{sid}/agent-turn',dict(transcript='取消刚才的请求',
                client_action_id='cancel-prepared-oral',cloud_text_allowed=False))
            current=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':new_prompt['prompt_id']})
            assert current.status_code==200,current.text
            assert current.json()['status'] in {'cancelled','invalidated'},current.text
            replay=client.post(path,json={})
            assert replay.status_code==409,replay.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
                assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()[0]=='answered'
            return
        quote='I would visit the garden with my sister because we enjoy looking at colourful flowers together.'
        second,new_voice=answer(client,settings,monkeypatch,sid,new_prompt,gateway,quote,'second')
        assert new_voice not in {voice,retry_voice}
        assert second['attempt']['submitted_answer']==quote
        assert second['attempt']['score_status']=='needs_review'
        assert second['local_learning_evaluation']['status']=='feedback_ready',second
        feedback=client.get(f"/v1/agent-turns/{second['local_learning_evaluation']['feedback_turn_id']}/execution-status").json()['execution']['execution']
        assert feedback['result']['feedback_delivery_allowed'] is True
        assert feedback['result']['oral_feedback']['mastery_claim_allowed'] is False
        with db.connect() as c:
            row=c.execute('SELECT hint_level,independent FROM learning_evidence WHERE evidence_id=?',(second['attempt']['evidence_id'],)).fetchone()
            assert row['hint_level']>=1 and not row['independent']
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
            assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',(saved['attempt']['attempt_id'],)).fetchone()[0]==ANSWER
        for request in gateway.requests:
            if request.request_type=='voice_interaction' and 'oral.retry' in json.dumps(request.messages):
                payload=json.loads(request.messages[-1]['content'])
                assert set(payload)=={'transcript','state'}
                assert all(type(v) is bool for v in payload['state'].values())
                assert prompt['prompt_id'] not in json.dumps(request.messages)
                assert original['task_points'][0] not in json.dumps(request.messages)
