"""Scheduled writing: voice pause/resume after the revision prompt exists."""
from home_study_agent.formative_evaluation_projection import build_formative_projection
from test_scheduled_writing_natural_runtime_http import scheduled, native_answer, post
from test_voice_runtime_binding import capture, interpret


def test_revision_prompt_then_pause_resume_keeps_formal_feedback(tmp_path,monkeypatch):
    with scheduled(tmp_path) as case:
        answer,_=native_answer(case)
        feedback=answer['local_learning_evaluation']['feedback_turn_id']
        post(case['client'],f'/v1/agent-turns/{feedback}/execute',{})
        case['transport'].control='writing.revise_original'
        quote='我想按刚才的反馈修改这份作文'
        voice=capture(case['client'],case['settings'],monkeypatch,case['sid'],quote,case['prompt'],'revision-voice')
        turn=interpret(case['client'],case['sid'],quote,'revision-command',voice)
        revised=post(case['client'],f'/v1/agent-turns/{turn["agent_turn_id"]}/execute',{})
        prompt=revised['execution']['result']['prompt']
        assert prompt['prompt_id']!=case['prompt']['prompt_id']
        with case['db'].connect() as c:
            original=c.execute("SELECT plan_id,context_manifest_json FROM agent_plan WHERE json_extract(context_manifest_json,'$.scheduled_writing_goal.prompt_id')=?",
                (case['prompt']['prompt_id'],)).fetchone()
            origin_id,origin_manifest=original
            assert c.execute("SELECT count(*) FROM agent_plan WHERE json_extract(context_manifest_json,'$.scheduled_writing_goal.prompt_id')=?",
                (prompt['prompt_id'],)).fetchone()[0]==0
        for control,quote in [('task.pause','第二稿写到一半了先暂停'),('task.resume','继续写刚才的第二稿')]:
            case['transport'].control=control
            voice=capture(case['client'],case['settings'],monkeypatch,case['sid'],quote,prompt,control+'-voice')
            turn=interpret(case['client'],case['sid'],quote,control+'-turn',voice)
            post(case['client'],f'/v1/agent-turns/{turn["agent_turn_id"]}/execute',{})
        second,_=native_answer({**case,'prompt':prompt},key='second')
        follow=second['local_learning_evaluation']
        assert follow['status']=='feedback_required',{key:follow.get(key) for key in ('status','reason')}
        with case['db'].connect() as c:
            plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
            args=dict(child_id=plan['child_id'],session_id=case['sid'],objective_id=plan['objective_id'],
                prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])
            faults=[
                ("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_binding.prompt_id',?) WHERE agent_turn_id=?",(case['prompt']['prompt_id'],turn['agent_turn_id'])),
                ("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.scheduled_writing_goal.prompt_id',?) WHERE plan_id=?",(prompt['prompt_id'],origin_id)),
                ("UPDATE writing_followup SET binding_json=json_set(binding_json,'$.parent_prompt_id','unrelated') WHERE prompt_id=?",(prompt['prompt_id'],)),
                ("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_binding.child_id','other-child') WHERE agent_turn_id=?",(turn['agent_turn_id'],)),
                ("UPDATE diagnostic_prompt SET created_at='2999-01-01T00:00:00+00:00' WHERE prompt_id=?",(prompt['prompt_id'],)),
                ("UPDATE agent_step SET status='failed' WHERE plan_id=?",(plan['plan_id'],)),
                ("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_binding.session_id','other-session') WHERE agent_turn_id=?",(turn['agent_turn_id'],)),
            ]
            for sql,values in faults:
                c.execute('SAVEPOINT fault');c.execute(sql,values)
                projection=build_formative_projection(c,**args)
                assert projection['status']=='local_required',(sql,projection.get('reason'))
                c.execute('ROLLBACK TO fault');c.execute('RELEASE fault')
            assert c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?',(origin_id,)).fetchone()[0]==origin_manifest
        post(case['client'],f'/v1/agent-turns/{follow["feedback_turn_id"]}/execute',{})
        with case['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==2
