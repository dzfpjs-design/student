"""One postcommit/pre-finalize fault, followed by real cold-start recovery."""
import json
import test_native_saved_resume_http as native_case
from test_native_writing_receipt_http import _process_client


def test_postcommit_pre_finalize_restart_recovers_original_feedback_without_post(tmp_path,monkeypatch):
    import home_study_agent.native_writing_resume as module
    with native_case.saved_answer(tmp_path/'data',monkeypatch) as value:
        case=value['case']; request=value['request']; base=value['base']
        original=module.resume_saved
        def break_before_route_finalizer(*args,**kwargs):
            packet,replayed=original(*args,**kwargs)
            assert not replayed and packet['state']=='answered'
            raise ValueError('synthetic_exit_after_attempt_and_link_before_complete_prompt_result')
        with monkeypatch.context() as fault:
            fault.setattr(module,'resume_saved',break_before_route_finalizer)
            response=value['client'].post(base+'/native-writing-resume',json=request)
            assert response.status_code==409,response.text
        def feedback(c,evidence):
            return [dict(r) for r in c.execute('''SELECT t.agent_turn_id,t.tool_status,s.capability_name,s.status,
                p.context_manifest_json,p.plan_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
                JOIN agent_step s USING(plan_id) WHERE t.session_id=? AND t.child_id=?
                AND (json_extract(p.context_manifest_json,'$.evidence_id')=?
                    OR json_extract(p.context_manifest_json,'$.writing_reference.evidence_id')=?)''',
                (case['sid'],value['body']['child_id'],evidence,evidence))]
        with case['db'].connect() as c:
            row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(value['body']['prompt_id'],)).fetchone()
            attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(row['attempt_id'],)).fetchone()
            assert row['status']=='answered'
            assert feedback(c,attempt['evidence_id'])==[]
            hit=c.execute('''SELECT count(*) FROM agent_plan plan WHERE plan.child_id=? AND plan.session_id=?
                AND (json_extract(plan.plan_json,'$.local_learning_binding.prompt_id')=?
                  OR json_extract(plan.context_manifest_json,'$.goal_prompt_binding.prompt_id')=?)''',
                (row['child_id'],case['sid'],row['prompt_id'],row['prompt_id'])).fetchone()[0]
            assert hit>0
        # This helper denies every provider request, including during lifespan.
        with _process_client(case) as (client,pid):
            with case['db'].connect() as c:
                restored=feedback(c,attempt['evidence_id'])
                assert len(restored)==1,restored
                assert restored[0]['capability_name']=='learning.evaluate_response'
                assert restored[0]['status']=='planned' and restored[0]['tool_status']=='pending'
                manifest=json.loads(restored[0]['context_manifest_json'])
                assert manifest['execution_method']=='formative_evaluation'
                assert manifest['original_learning_binding']['prompt_id']==row['prompt_id']
                assert manifest['original_learning_binding']['item_version']==row['item_version']
                before=list(c.iterdump())
            q={**value['query'],'resume_action_id':request['client_action_id']}
            receipt=client.get(base+'/native-writing-receipt',params=q)
            replay=client.post(base+'/native-writing-resume',json=request)
            assert receipt.status_code==replay.status_code==200
            assert receipt.json()['result']['attempt']['attempt_id']==attempt['attempt_id']
            assert replay.json()['result']['attempt']['attempt_id']==attempt['attempt_id']
            with case['db'].connect() as c:
                assert list(c.iterdump())==before
                assert feedback(c,attempt['evidence_id'])==restored
            status=client.get('/v1/agent-turns/'+restored[0]['agent_turn_id']+'/execution-status')
            assert status.status_code==200,status.text
        (tmp_path/'postcommit-finalize.json').write_text(json.dumps(dict(
            interruption='after attempt/link commit, before complete_prompt_result',interrupted_http=409,
            prompt_id=row['prompt_id'],evidence_id=attempt['evidence_id'],attempt_id=attempt['attempt_id'],
            startup_query_hits=hit,feedback_before=0,feedback_after=1,feedback_turn_id=restored[0]['agent_turn_id'],
            feedback_capability=restored[0]['capability_name'],feedback_status=restored[0]['status'],
            formative_runtime_plan_restored=True,receipt_and_replay_readonly=True,
            restart_pid=pid,owned_processes_remaining=0,provider_posts=0),indent=2))
