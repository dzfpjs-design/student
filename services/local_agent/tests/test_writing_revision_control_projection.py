"""Real scheduled planner/TCP: timer recovery must retain its original prompt."""
from test_scheduled_writing_natural_runtime_http import scheduled, native_answer, post
from test_voice_runtime_binding import capture, interpret
import pytest
from home_study_agent.repository import utc_now


@pytest.mark.parametrize('device_resume',[False,True])
def test_historical_timer_control_does_not_bind_itself_to_later_revision(tmp_path,monkeypatch,device_resume):
    with scheduled(tmp_path) as case:
        answer,_=native_answer(case)
        feedback=answer['local_learning_evaluation']['feedback_turn_id']
        post(case['client'],f'/v1/agent-turns/{feedback}/execute',{})
        for control,quote in [('task.pause','这项先暂停一下'),('task.resume','继续刚才的写作任务')]:
            case['transport'].control=control
            voice=capture(case['client'],case['settings'],monkeypatch,case['sid'],quote,case['prompt'],control+'-voice')
            turn=interpret(case['client'],case['sid'],quote,control+'-turn',voice)
            post(case['client'],f'/v1/agent-turns/{turn["agent_turn_id"]}/execute',{})
        case['transport'].control='writing.revise_original'
        quote='我想按刚才的反馈修改这份作文'
        voice=capture(case['client'],case['settings'],monkeypatch,case['sid'],quote,case['prompt'],'revision-voice')
        turn=interpret(case['client'],case['sid'],quote,'revision-command',voice)
        revised=post(case['client'],f'/v1/agent-turns/{turn["agent_turn_id"]}/execute',{})
        new_prompt=revised['execution']['result']['prompt']
        assert new_prompt['prompt_id']!=case['prompt']['prompt_id']
        if device_resume:
            case['transport'].control='document.resume'
            quote='恢复高拍仪'
            voice=capture(case['client'],case['settings'],monkeypatch,case['sid'],quote,new_prompt,'document-voice')
            turn=interpret(case['client'],case['sid'],quote,'document-command',voice)
            assert turn['action']['type']=='control_document_monitor'
            post(case['client'],f'/v1/sessions/{case["sid"]}/agent-turns/{turn["agent_turn_id"]}/control-admission',{})
            post(case['client'],f'/v1/sessions/{case["sid"]}/document-monitor/start',{'client_action_id':'real-monitor-start'})
            post(case['client'],f'/v1/sessions/{case["sid"]}/devices/register',{
                'device_type':'document_camera','system_id_hash':'a'*64,'capabilities':{'synthetic':True}})
            receipt={'capability':'document.monitor_control','execution_location':'client_device_bridge',
                'action_type':'control_document_monitor','status':'completed','completed_at':utc_now(),
                'output':{'device_type':'document_camera','device_status':'active','frame_confirmed':True,
                    'lease_confirmed':True,'persisted_status':'active'}}
            response=case['client'].put(f'/v1/agent-turns/{turn["agent_turn_id"]}/outcome',json={
                'final_response':'高拍仪已恢复','tool_status':'completed','result':{'client_bridge_receipt':receipt}})
            assert response.status_code==200,response.text
        second,_=native_answer({**case,'prompt':new_prompt},key='second')
        follow=second['local_learning_evaluation']
        assert follow['status']=='feedback_required',{key:follow.get(key) for key in ('status','reason')}
        if device_resume:
            from home_study_agent.formative_evaluation_projection import build_formative_projection
            with case['db'].connect() as c:
                plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
                step=c.execute('SELECT step_id FROM agent_step WHERE plan_id=?',(plan['plan_id'],)).fetchone()[0]
                args={'child_id':plan['child_id'],'session_id':case['sid'],'objective_id':plan['objective_id'],
                    'prompt_id':new_prompt['prompt_id'],'item_version':new_prompt['item_version']}
                faults=[
                    ("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_binding.prompt_id','other') WHERE agent_turn_id=?",(turn['agent_turn_id'],)),
                    ("UPDATE agent_step SET status='failed' WHERE step_id=?",(step,)),
                    ("UPDATE agent_step SET result_json=json_set(result_json,'$.client_bridge_receipt.output.frame_confirmed',0) WHERE step_id=?",(step,)),
                    ("UPDATE agent_step SET result_json=json_set(result_json,'$.client_bridge_receipt.output.frame_confirmed',1) WHERE step_id=?",(step,)),
                    ("UPDATE agent_step SET result_json=json_set(result_json,'$.extra_document_text','private content') WHERE step_id=?",(step,)),
                    ("UPDATE agent_step SET result_json=json_set(result_json,'$.client_bridge_receipt.completed_at','2999-01-01T00:00:00+00:00') WHERE step_id=?",(step,)),
                    ("UPDATE voice_input SET transcript='different sentence' WHERE voice_input_id=?",(voice,)),
                    ("DELETE FROM voice_control_intent_request WHERE request_key IN (SELECT json_extract(context_manifest_json,'$.voice_control_binding.request_key') FROM agent_plan WHERE plan_id=?)",(plan['plan_id'],)),
                    ("UPDATE sensor_lease SET opened_at='2999-01-01T00:00:00+00:00' WHERE session_id=? AND device_type='document_camera'",(case['sid'],)),
                ]
                for sql,values in faults:
                    c.execute('SAVEPOINT fault');c.execute(sql,values)
                    projection=build_formative_projection(c,**args)
                    assert projection['status']=='local_required',(sql,projection.get('reason'))
                    c.execute('ROLLBACK TO fault');c.execute('RELEASE fault')
        post(case['client'],f'/v1/agent-turns/{follow["feedback_turn_id"]}/execute',{})
        with case['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==2
