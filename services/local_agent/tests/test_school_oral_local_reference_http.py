"""Saved speech -> local content check -> new answer -> authorized feedback.

Actual HTTP and SQLite; disposable sources, controlled ASR and model transport.
"""
import json
from synthetic_oral_app_lifecycle import fresh_oral_case

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_open_response_spoken_runtime_http import test_natural_spoken_process_asr_bound_answer_formative_and_restart as run_journey
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('fault', [None,'early_retry','early_retry_parent_changed','raced_replay','cloud_off',
    'source','late_source','voice','capture','new_input','goal','arguments','forged_outcome','forged_receipt'])
def test_local_oral_reference_without_question_upload_permission(tmp_path,monkeypatch,fault):
    def inspect(j):
        c, follow = j['client'], j['follow']
        assert follow['status']=='local_feedback_required',follow
        assert follow['reason'] and not follow['reason'].startswith('WRITING_WRITTEN')
        tid=follow['local_feedback_turn_id']
        assert tid and tid!=follow['feedback_turn_id']
        with j['db'].connect() as db:
            count=db.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
            goal=db.execute("SELECT * FROM agent_objective WHERE child_id=? AND status='active'",(j['child'],)).fetchone()
            assert count==1 and goal
            original_goal=dict(goal)
            if fault=='source':
                db.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(j['prompt']['diagnostic_item_id'],))
            elif fault=='voice':
                db.execute("UPDATE voice_input SET transcript='not the submitted answer' WHERE voice_input_id=?",(j['voice'],))
            elif fault=='capture':
                db.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE voice_input_id=?",(j['voice'],))
            elif fault=='goal':
                db.execute("UPDATE agent_objective SET status='paused' WHERE objective_id=?",(goal['objective_id'],))
            elif fault=='arguments':
                db.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','你已经掌握了') WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(tid,))
            db.commit()
        if fault=='new_input':
            capture(c,j['settings'],monkeypatch,j['sid'],'先等一下',j['prompt'],'newer-voice',child=j['child'])
        if fault=='cloud_off':
            privacy=c.put('/v1/parent/privacy',headers=j['headers'],json={'cloud_model_text_allowed':False,'raw_media_storage_allowed':False})
            assert privacy.status_code==200,privacy.text
        before=len(j['posts'])
        if fault=='late_source':
            executor=c.app.state.agent_capability_executor
            dispatch=executor._dispatch
            def withdraw_after_check(step):
                result=dispatch(step)
                with j['db'].connect() as db:
                    db.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(j['prompt']['diagnostic_item_id'],))
                    db.commit()
                return result
            monkeypatch.setattr(executor,'_dispatch',withdraw_after_check)
        if fault in {'forged_outcome','forged_receipt'}:
            with j['db'].connect() as db:
                canonical=db.execute('SELECT response_text FROM agent_turn WHERE agent_turn_id=?',(tid,)).fetchone()[0]
            forged=c.put('/v1/agent-turns/'+tid+'/outcome',json={'tool_status':'completed',
                'final_response':canonical if fault=='forged_receipt' else '已经掌握，可以跳过复习。','result':{}})
            assert forged.status_code==409,forged.text
            with j['db'].connect() as db:
                assert db.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',(tid,)).fetchone()[0]=='pending'
            return
        reply=c.post('/v1/agent-turns/'+tid+'/execute')
        assert len(j['posts'])==before
        if fault and fault not in {'cloud_off','early_retry','early_retry_parent_changed','raced_replay'}:
            assert reply.status_code==409,reply.text
            with j['db'].connect() as db:
                assert db.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
                assert db.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchone()[0]==j['quote']
                assert db.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
            return
        assert reply.status_code==200,reply.text
        execution=reply.json()['execution']
        assert execution['capability']=='conversation.respond'
        assert '口头回答' in execution['response_text'] and '原稿' not in execution['response_text']
        result=execution['result']
        assert result['feedback_delivery_allowed'] and result['feedback_status']=='ready'
        assert result['local_only'] and not result['cloud_transmission_allowed']
        assert not result['objective_update_applied']
        oral=result['oral_feedback']
        assert oral['status']=='content_self_check' and oral['response_text']==j['quote']
        assert oral['prompt_id']==j['prompt']['prompt_id'] and not oral['mastery_claim_allowed']
        with j['db'].connect() as db:
            current=dict(db.execute('SELECT * FROM agent_objective WHERE objective_id=?',(goal['objective_id'],)).fetchone())
            for key in ('status','phase','success_criteria_json'):
                assert current[key]==original_goal[key]
            assert db.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
            assert not db.execute('SELECT 1 FROM voice_input WHERE local_path IS NOT NULL').fetchone()
        with fresh_oral_case({**j,'client_options':{}},TestClient,client=('testclient',50000)) as restarted_case:
            restarted=restarted_case['client']
            # Model another request finishing between the optimistic read and
            # the transaction's authoritative status check, without real threads.
            with monkeypatch.context() as racing:
                if fault=='raced_replay':
                    executor=restarted.app.state.agent_capability_executor
                    load=executor._load
                    racing.setattr(executor,'_load',lambda turn_id:{**load(turn_id),'status':'planned','result_json':None})
                replay=restarted.post('/v1/agent-turns/'+tid+'/execute')
            assert replay.status_code==200,replay.text
            assert replay.json()['execution']['replay_audio_allowed'] is False
            assert replay.json()['execution']['result']==result
            assert len(j['posts'])==before
            if fault in {'early_retry','early_retry_parent_changed'}:
                j['command_state'].update(control='oral.retry',quote='我想换个说法，从已知条件重新解释一次')
                voice=capture(restarted,j['settings'],monkeypatch,j['sid'],j['command_state']['quote'],j['prompt'],'local-retry-voice',child=j['child'])
                turn=interpret(restarted,j['sid'],j['command_state']['quote'],'local-retry',voice)
                assert turn['runtime_capability']=='oral.retry',turn
                retried=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
                assert retried.status_code==200,retried.text
                next_prompt=retried.json()['execution']['result']['prompt']
                j['command_state'].clear()
                voice=capture(restarted,j['settings'],monkeypatch,j['sid'],j['quote'],next_prompt,'local-retry-answer',child=j['child'])
                turn=interpret(restarted,j['sid'],j['quote'],'local-retry-answer-turn',voice)
                saved=restarted.post(f"/v1/sessions/{j['sid']}/diagnostic-prompt/answer",json={
                    'prompt_id':next_prompt['prompt_id'],'item_version':next_prompt['item_version'],
                    'answer':j['quote'],'client_action_id':'local-retry-save','authorized_turn_id':turn['agent_turn_id']})
                assert saved.status_code==200,saved.text
                followup=saved.json()['local_learning_evaluation']
                assert followup['status']=='local_feedback_required',followup
                if fault=='early_retry_parent_changed':
                    with j['db'].connect() as db:
                        db.execute("UPDATE voice_input SET transcript='changed ancestor, not the new answer' WHERE voice_input_id=?",(j['voice'],))
                        db.commit()
                checked=restarted.post('/v1/agent-turns/'+followup['local_feedback_turn_id']+'/execute')
                if fault=='early_retry_parent_changed':
                    assert checked.status_code==409,checked.text
                    assert not j['evaluations']
                    return
                assert checked.status_code==200,checked.text
                assert checked.json()['execution']['result']['oral_feedback']['prompt_id']==next_prompt['prompt_id']
                assert not j['evaluations']
                wire=json.dumps(j['posts'],ensure_ascii=False)
                assert tid not in wire and 'oral_local_reference' not in wire
                assert execution['response_text'] not in wire and j['prompt']['item']['prompt'] not in wire
                with j['db'].connect() as db:
                    assert db.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
                    assert db.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
                    hint=db.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=?',
                        (saved.json()['attempt']['evidence_id'],)).fetchone()[0]
                    assert hint>=1
                return
            # Permission recovery must not require another copy of the answer.
            token=restarted.post('/v1/parent/unlock',json={'pin':'8264'}).json()['parent_token']
            path=f"/v1/parent/children/{j['child']}/writing-question-authorizations/{j['prompt']['diagnostic_item_id']}"
            headers={'X-HSC-Parent':token}
            if fault=='cloud_off':
                assert restarted.put('/v1/parent/privacy',headers=headers,json={
                    'cloud_model_text_allowed':True,'raw_media_storage_allowed':False}).status_code==200
            spec=restarted.get(path,headers=headers).json()
            from test_writing_question_authorization import LICENSE
            approved=restarted.post(path+'/grant',headers=headers,json={
                **{k:spec[k] for k in ('item_version','source_version','curriculum_version')},
                'expected_revision':0,'client_action_id':'restore-question-text','license_basis':LICENSE})
            assert approved.status_code==200,approved.text
            saved=restarted.post(f"/v1/sessions/{j['sid']}/diagnostic-prompt/answer",json=j['payload'])
            assert saved.status_code==200,saved.text
            restored=saved.json()['local_learning_evaluation']
            assert restored['status']=='feedback_required',restored
            assert restored['feedback_turn_id']==follow['feedback_turn_id']
            graded=restarted.post('/v1/agent-turns/'+restored['feedback_turn_id']+'/execute')
            assert graded.status_code==200,graded.text
            assert graded.json()['execution']['result']['formative_only']
            assert len(j['evaluations'])==1 and j['evaluations'][0]['response']==j['quote']
            j['command_state'].update(control='oral.retry',quote='我想换个说法，从已知条件重新解释一次')
            voice=capture(restarted,j['settings'],monkeypatch,j['sid'],j['command_state']['quote'],j['prompt'],'retry-voice',child=j['child'])
            turn=interpret(restarted,j['sid'],j['command_state']['quote'],'retry-after-local',voice)
            assert turn['runtime_capability']=='oral.retry',turn
            retry=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert retry.status_code==200,retry.text
            next_prompt=retry.json()['execution']['result']['prompt']
            assert next_prompt['prompt_id']!=j['prompt']['prompt_id']
            assert next_prompt['diagnostic_item_id']==j['prompt']['diagnostic_item_id']
            j['command_state'].clear()
            voice=capture(restarted,j['settings'],monkeypatch,j['sid'],j['quote'],next_prompt,'retry-answer',child=j['child'])
            turn=interpret(restarted,j['sid'],j['quote'],'retry-answer-turn',voice)
            submitted=restarted.post(f"/v1/sessions/{j['sid']}/diagnostic-prompt/answer",json={
                'prompt_id':next_prompt['prompt_id'],'item_version':next_prompt['item_version'],
                'answer':j['quote'],'client_action_id':'retry-answer-save','authorized_turn_id':turn['agent_turn_id']})
            assert submitted.status_code==200,submitted.text
            assert submitted.json()['local_learning_evaluation']['status']=='feedback_required',submitted.text
            second= submitted.json()['local_learning_evaluation']['feedback_turn_id']
            graded=restarted.post('/v1/agent-turns/'+second+'/execute')
            assert graded.status_code==200,graded.text
            assert graded.json()['execution']['result']['formative_only']
            assert len(j['evaluations'])==2
            with j['db'].connect() as db:
                assert db.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
                assert not db.execute('SELECT 1 FROM diagnostic_attempt WHERE correctness IS NOT NULL').fetchone()
            with fresh_oral_case(restarted_case,TestClient,client=('testclient',50000)) as recovered_case:
                recovered=recovered_case['client']
                original=recovered.get(f"/v1/sessions/{j['sid']}/diagnostic-prompt",params={'prompt_id':j['prompt']['prompt_id']})
                revised=recovered.get(f"/v1/sessions/{j['sid']}/diagnostic-prompt",params={'prompt_id':next_prompt['prompt_id']})
                assert original.status_code==revised.status_code==200
                assert original.json()['status']==revised.json()['status']=='answered'
                assert original.json()['attempt']['attempt_id']!=revised.json()['attempt']['attempt_id']
                assert revised.json()['attempt']['attempt_id']==submitted.json()['attempt']['attempt_id']
                assert revised.json()['attempt']['submitted_answer']==j['quote']
                assert len(j['evaluations'])==2
        wire=json.dumps(j['posts'],ensure_ascii=False)
        assert 'oral_local_reference' not in wire and 'local_audit' not in wire and tid not in wire
    run_journey(tmp_path,monkeypatch,'数学',None,True,None,raw_mode='never_store',
        question_authorized=False,after_saved=inspect)
