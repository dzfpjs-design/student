"""Actual local persistence and recovery; synthetic providers, never child proof."""
from datetime import datetime,timezone
from contextlib import contextmanager, ExitStack
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_pending_evaluation_voice_capture_http import inline_host
from test_pending_evaluation_control import facts
from test_foreground_independent_dialogue import foreground_models

QUOTE = '等评价的时候，你能告诉我有没有PET学习资料吗？'


@contextmanager
def context(tmp_path):
    with ExitStack() as original_client_lifecycle:
        settings,db,sid,client=original_client_lifecycle.enter_context(inline_host(tmp_path))
        privacy=PrivacyLifecycleService(db,tmp_path);privacy.initialize()
        privacy.update_preferences({'cloud_model_text_allowed':True})
        gateway=client.app.state.model_gateway;gateway.requests=[]
        yield {'settings':settings,'db':db,'sid':sid,'client':client,'gateway':gateway,'privacy':privacy,'finish_client':original_client_lifecycle.close}


def ask(j, quote=QUOTE, key='pending-independent', *, reference='eval'):
    started=j['client'].post(f"/v1/sessions/{j['sid']}/voice/realtime/begin",json={
        'client_action_id':key,'model_label':'synthetic-asr',
        'captured_at_ms':int(datetime.now(timezone.utc).timestamp()*1000),
        'capture_reference':{'child_id':'child-runtime','pending_evaluation_turn_id':'eval'}})
    assert started.status_code==200,started.text
    complete=j['client'].post(f"/v1/sessions/{j['sid']}/voice/realtime/complete",json={
        'client_action_id':key,'transcript':quote})
    assert complete.status_code==200,complete.text
    body={'transcript':quote,'voice_input_id':complete.json()['voice_input_id'],
        'pending_evaluation_turn_id':reference,'client_action_id':key+'-turn','cloud_text_allowed':True}
    return j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn",json=body),body


def resources(j, monkeypatch, **kwargs):
    return foreground_models(j,monkeypatch,QUOTE,capability='system.inspect_learning',
                             arguments={'scope':'resources','track':'pet'},**kwargs)


def test_independent_resources_execute_and_replay_without_original_evaluation_or_media_export(tmp_path,monkeypatch):
    with context(tmp_path) as j:
        resources(j,monkeypatch);before=facts(j['db'])
        planned,body=ask(j)
        assert planned.status_code==200,planned.text
        turn=planned.json();assert turn['action']['type']=='inspect_learning_system',turn['action']
        executed=j['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code==200,executed.text
        assert executed.json()['execution']['result']['cloud_transmission_allowed'] is False
        assert facts(j['db'])==before
        j['finish_client']()
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            replay=restarted.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
            assert replay.status_code==200 and replay.json()['agent_turn_id']==turn['agent_turn_id'],replay.text
            receipt=restarted.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert receipt.status_code==200 and receipt.json()['execution']==executed.json()['execution'],receipt.text
        assert facts(j['db'])==before
        assert [r.request_type for r in j['gateway'].requests]==['voice_interaction','agent_plan']
        classifier_prompt=j['gateway'].requests[0].messages[0]['content']
        assert '原作答已提交并等待评价' in classifier_prompt
        assert '原题仍等待作答' not in classifier_prompt
        for request in j['gateway'].requests:
            exported=json.dumps({'messages':request.messages,'manifest':request.context_manifest,'evidence':request.evidence_refs},ensure_ascii=False)
            for source in ['child-runtime',j['sid'],'正弦','eval-action',body['voice_input_id']]:
                assert source not in exported
        planner=json.loads(j['gateway'].requests[1].messages[1]['content'])
        assert set(planner['context'])=={'session','child','conversation_state'}
        assert {c['name'] for c in planner['available_capabilities']}=={'system.inspect_learning', 'learning.explain'}
        with j['db'].connect() as c:
            assert c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0] is None


@pytest.mark.parametrize('role,scope,confidence', [('question','current',.97),('question','unknown',.97),
    ('question',None,.97),('mixed',None,.97),('answer',None,.97),('unknown',None,.97),('question','independent',.2)])
def test_original_mixed_and_uncertain_inputs_do_not_gain_independent_execution(tmp_path,monkeypatch,role,scope,confidence):
    with context(tmp_path) as j:
        resources(j,monkeypatch,role=role,scope=scope,confidence=confidence);before=facts(j['db'])
        result,_=ask(j)
        assert result.status_code==200,result.text
        assert result.json()['action']['type']=='none'
        assert [r.request_type for r in j['gateway'].requests]==['voice_interaction']
        assert facts(j['db'])==before


@pytest.mark.parametrize('role,quote', [('greeting','小知你好，今天见到你很开心。'),
    ('companionship','上了一天课好累，想和你聊一会儿。'),('topic_mention','一元二次方程')])
def test_pending_evaluation_can_have_non_teaching_companionship(tmp_path,monkeypatch,role,quote):
    with context(tmp_path) as j:
        foreground_models(j,monkeypatch,quote,role=role,arguments={'response':'我在这里陪着你。'})
        before=facts(j['db']);planned,_=ask(j,quote)
        assert planned.status_code==200,planned.text
        execution=j['client'].post(f"/v1/agent-turns/{planned.json()['agent_turn_id']}/execute")
        assert execution.status_code==200,execution.text
        assert execution.json()['execution']['response_text']=='我在这里陪着你。',execution.text
        assert facts(j['db'])==before


@pytest.mark.parametrize('capability',['conversation.respond','learning.evaluate_response'])
def test_model_cannot_expand_independent_question_to_unverified_facts_or_regrading(tmp_path,monkeypatch,capability):
    with context(tmp_path) as j:
        arguments=({'question':QUOTE,'subject':'数学'} if capability=='learning.explain' else
            {'response':QUOTE,'subject':'数学'} if capability=='learning.evaluate_response' else {'response':'这是题目答案。'})
        foreground_models(j,monkeypatch,QUOTE,capability=capability,arguments=arguments)
        before=facts(j['db']);result,_=ask(j)
        assert result.status_code==200,result.text
        assert result.json()['action']['type']=='none'
        assert facts(j['db'])==before


def mutate(j, change):
    if change=='consent':
        j['privacy'].update_preferences({'cloud_model_text_allowed':False});return
    with j['db'].connect() as c:
        if change=='cancel':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
        else:
            c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','changed-original') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')")
        c.commit()


@pytest.mark.parametrize('change',['journal','objective','reply','capability','own_cancel','missing_voice','voice_manifest'])
def test_persisted_scope_tampering_cannot_dispatch_an_independent_step(tmp_path,monkeypatch,change):
    with context(tmp_path) as j:
        resources(j,monkeypatch);planned,_=ask(j)
        assert planned.status_code==200,planned.text
        turn=planned.json()['agent_turn_id']
        with j['db'].connect() as c:
            if change=='journal':
                c.execute("UPDATE voice_control_intent_request SET decision_json=json_set(decision_json,'$.role','answer')")
            elif change=='own_cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(turn,))
            elif change=='capability':
                c.execute("UPDATE agent_step SET capability_name='learning.evaluate_response' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(turn,))
            elif change=='missing_voice':
                c.execute("UPDATE agent_turn SET action_json=json_remove(action_json,'$.voice_input_id','$.voice_input_binding') WHERE agent_turn_id=?",(turn,))
            elif change=='voice_manifest':
                c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.voice_input_binding.voice_input_id','different-input') WHERE agent_turn_id=?",(turn,))
            else:
                value='new' if change=='objective' else True
                row=c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?',(turn,)).fetchone()
                data=json.loads(row[0])
                if change=='objective':data['objective']['mode']=value
                else:data['requires_child_reply']=value
                c.execute('UPDATE agent_plan SET plan_json=? WHERE agent_turn_id=?',(json.dumps(data),turn))
            c.commit()
        before=facts(j['db'])
        result=j['client'].post(f'/v1/agent-turns/{turn}/execute')
        assert result.status_code in {409,422},result.text
        assert facts(j['db'])==before and len(j['gateway'].requests)==2


def test_unavailable_independent_planner_does_not_repeat_after_restart(tmp_path,monkeypatch):
    with context(tmp_path) as j:
        resources(j,monkeypatch,fail_plan=True);before=facts(j['db'])
        result,body=ask(j)
        assert result.status_code==200 and result.json()['action']['type']=='none',result.text
        assert len(j['gateway'].requests)==2
        j['finish_client']()
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            saved=restarted.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
            assert saved.status_code==200 and saved.json()['agent_turn_id']==result.json()['agent_turn_id'],saved.text
        assert facts(j['db'])==before and len(j['gateway'].requests)==2


def test_original_question_text_stays_local_even_if_classifier_would_say_independent(tmp_path,monkeypatch):
    with context(tmp_path) as j:
        with j['db'].connect() as c:
            question=json.loads(c.execute("SELECT arguments_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')").fetchone()[0])['question_asked']
        foreground_models(j,monkeypatch,question,capability='system.inspect_learning',arguments={'scope':'resources','track':'pet'})
        before=facts(j['db']);result,_=ask(j,question)
        assert result.status_code==200 and result.json()['action']['type']=='none',result.text
        assert not j['gateway'].requests and facts(j['db'])==before


@pytest.mark.parametrize('change',['cancel','source','consent'])
@pytest.mark.parametrize('when',['planning','execution'])
def test_cancel_change_and_withdrawal_do_not_admit_late_independent_result(tmp_path,monkeypatch,change,when):
    with context(tmp_path) as j:
        resources(j,monkeypatch)
        if when=='planning':
            original=j['gateway'].complete
            async def changed(request):
                if request.request_type=='agent_plan':mutate(j,change)
                return await original(request)
            monkeypatch.setattr(j['gateway'],'complete',changed)
        planned,_=ask(j)
        if when=='planning':
            assert planned.status_code in {409,422},planned.text
            with j['db'].connect() as c:
                assert c.execute("SELECT count(*) FROM agent_turn WHERE orchestrator_version='pending_evaluation_independent_v1'").fetchone()[0]==0
        else:
            assert planned.status_code==200,planned.text
            mutate(j,change)
            execution=j['client'].post(f"/v1/agent-turns/{planned.json()['agent_turn_id']}/execute")
            assert execution.status_code in {409,422},execution.text


def test_original_evaluation_can_finish_while_independent_receipt_remains_read_only(tmp_path,monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from test_learning_outcome_evaluator import evaluation_fixture
    db,sid,evaluator,evaluation_gateway,_=evaluation_fixture(tmp_path)
    settings=inline_settings(tmp_path,db)
    from test_learning_runtime_http import NoCloud
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        privacy=PrivacyLifecycleService(db,tmp_path);privacy.initialize()
        privacy.update_preferences({'cloud_model_text_allowed':True})
        gateway=client.app.state.model_gateway;gateway.requests=[]
        j={'settings':settings,'db':db,'sid':sid,'client':client,'gateway':gateway,'privacy':privacy}
        resources(j,monkeypatch)
        entered,released=Event(),Event();original=evaluation_gateway.complete
        async def held(request):
            entered.set()
            for _ in range(2000):
                if released.is_set():return await original(request)
                await asyncio.sleep(.01)
            raise AssertionError('synthetic evaluation release deadline')
        evaluation_gateway.complete=held
        with ThreadPoolExecutor(max_workers=1) as pool:
            running=pool.submit(asyncio.run,evaluator.evaluate('eval'))
            try:
                assert entered.wait(10),'original evaluation did not start'
                before=facts(db);planned,body=ask(j)
                assert planned.status_code==200,planned.text
                turn=planned.json();execution=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
                assert execution.status_code==200,execution.text
                assert facts(db)==before
            finally:
                released.set()
            original_result=running.result(timeout=15)
        assert original_result['evaluation']['certification_status']=='candidate'
        after=facts(db)
        replay=client.post(f'/v1/sessions/{sid}/agent-turn',json=body)
        assert replay.status_code==200 and replay.json()['agent_turn_id']==turn['agent_turn_id'],replay.text
        saved=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert saved.status_code==200 and saved.json()['execution']==execution.json()['execution'],saved.text
        assert facts(db)==after and len(evaluation_gateway.requests)==1 and len(gateway.requests)==2


def inline_settings(tmp_path,db):
    from home_study_agent.config import Settings
    return Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False)


def test_pending_independent_receipt_survives_actual_tcp_sigkill(tmp_path):
    from contextlib import closing
    import os
    from pathlib import Path
    import select
    import signal
    import subprocess
    import sys
    import time
    import httpx
    quote='我们先不看这张纸，你有PET学习资料可以给我介绍吗？'
    processes=[]
    with context(tmp_path) as j:
        # Capture before starting the subprocess; its synthetic model is fixed.
        key='pending-process'
        started=j['client'].post(f"/v1/sessions/{j['sid']}/voice/realtime/begin",json={
            'client_action_id':key,'model_label':'synthetic-asr','captured_at_ms':int(time.time()*1000),
            'capture_reference':{'child_id':'child-runtime','pending_evaluation_turn_id':'eval'}})
        assert started.status_code==200,started.text
        completed=j['client'].post(f"/v1/sessions/{j['sid']}/voice/realtime/complete",json={
            'client_action_id':key,'transcript':quote})
        assert completed.status_code==200,completed.text
        body={'transcript':quote,'voice_input_id':completed.json()['voice_input_id'],
            'pending_evaluation_turn_id':'eval','client_action_id':key+'-turn','cloud_text_allowed':True}
        fixture=Path(__file__).parent/'fixtures/document_dialogue_process.py'
        source=Path(__file__).parents[1]/'src'
        def launch():
            log=(tmp_path/f'pending-process-{len(processes)}.log').open('w')
            process=subprocess.Popen([sys.executable,str(fixture),'--data-root',str(tmp_path),'--database-name','evaluation.db'],
                stdout=subprocess.PIPE,stderr=log,text=True,env={**os.environ,'PYTHONPATH':str(source),'PYTHONDONTWRITEBYTECODE':'1'})
            processes.append((process,log))
            assert select.select([process.stdout],[],[],15)[0],'process startup deadline'
            info=json.loads(process.stdout.readline());assert info['synthetic_models']
            client=httpx.Client(base_url=f"http://127.0.0.1:{info['port']}",timeout=10,trust_env=False)
            for _ in range(200):
                try:
                    if client.get('/openapi.json').status_code==200:return process,client
                except httpx.TransportError:pass
                time.sleep(.05)
            client.close();raise AssertionError('HTTP startup deadline')
        try:
            before=facts(j['db']);process,client=launch()
            with closing(client):
                planned=client.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
                assert planned.status_code==200,planned.text
                turn=planned.json();assert turn['action']['type']=='inspect_learning_system',turn['action']
                executed=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
                assert executed.status_code==200,executed.text
                saved=executed.json()['execution']
            process.kill();process.wait(timeout=5);assert process.returncode==-signal.SIGKILL
            process,client=launch()
            with closing(client):
                replay=client.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
                assert replay.status_code==200 and replay.json()['agent_turn_id']==turn['agent_turn_id'],replay.text
                receipt=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
                assert receipt.status_code==200 and receipt.json()['execution']==saved,receipt.text
            assert facts(j['db'])==before
            with j['db'].connect() as c:
                assert c.execute("SELECT count(*) FROM model_call WHERE purpose IN ('voice_interaction','agent_plan')").fetchone()[0]==2
        finally:
            for process,log in processes:
                if process.poll() is None:process.kill();process.wait(timeout=5)
                process.stdout.close();log.close()


def test_pending_evaluation_does_not_swallow_a_separate_resource_query(tmp_path,monkeypatch):
    with inline_host(tmp_path) as (settings,db,sid,client):
        privacy=PrivacyLifecycleService(db,tmp_path);privacy.initialize()
        privacy.update_preferences({'cloud_model_text_allowed':True})
        gateway=client.app.state.model_gateway;gateway.requests=[]
        j={'settings':settings,'db':db,'sid':sid,'client':client,'gateway':gateway}
        quote='等评价的时候，你能告诉我有没有PET学习资料吗？'
        foreground_models(j,monkeypatch,quote,capability='system.inspect_learning',arguments={'scope':'resources','track':'pet'})
        before=facts(db)
        key='pending-evaluation-independent-speech'
        started=client.post(f'/v1/sessions/{sid}/voice/realtime/begin',json={
            'client_action_id':key,'model_label':'synthetic-asr',
            'captured_at_ms':int(datetime.now(timezone.utc).timestamp()*1000),
            'capture_reference':{'child_id':'child-runtime','pending_evaluation_turn_id':'eval'}})
        assert started.status_code==200,started.text
        complete=client.post(f'/v1/sessions/{sid}/voice/realtime/complete',json={
            'client_action_id':key,'transcript':quote})
        assert complete.status_code==200,complete.text
        result=client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':quote,'voice_input_id':complete.json()['voice_input_id'],
            'pending_evaluation_turn_id':'eval','client_action_id':key+'-turn','cloud_text_allowed':True})
        assert result.status_code==200,result.text
        assert facts(db)==before
        with db.connect() as c:
            details = {'action': result.json()['action'],
                'semantic': [r[0] for r in c.execute('SELECT decision_json FROM voice_control_intent_request')],
                'models': [tuple(r) for r in c.execute('SELECT purpose,status,error_code FROM model_call')]}
        assert not any(r[2] for r in details['models']), details['models']
        assert result.json()['action']['type']=='inspect_learning_system',details
