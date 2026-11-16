"""Actual next-day voice/runtime/prompt writes with synthetic audio and model."""
import json
from synthetic_oral_app_lifecycle import fresh_oral_case, parent_headers

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_oral_supply_cross_day_source import test_cross_day_source_uses_original_goal_and_published_unseen_question as journey
from test_voice_runtime_binding import capture,interpret


@pytest.mark.parametrize('subject',['数学','物理'])
def test_voice_resume_delivers_new_question_with_old_goal_lineage(tmp_path,monkeypatch,subject,after_delivery=None,before_execute=None):
    from home_study_agent import supply_gap_resume
    import traceback
    failures=[]
    original_require=supply_gap_resume._require
    def require(value,code='SUPPLY_RESUME_SOURCE_CHANGED'):
        if not value:
            failures.append([(frame.name,frame.lineno) for frame in traceback.extract_stack(limit=5)])
        return original_require(value,code)
    monkeypatch.setattr(supply_gap_resume,'_require',require)
    from home_study_agent import oral_supply_execution
    original_oral=oral_supply_execution.require
    def oral_require(value):
        if not value:
            failures.append([(frame.name,frame.lineno) for frame in traceback.extract_stack(limit=4)])
        return original_oral(value)
    monkeypatch.setattr(oral_supply_execution,'require',oral_require)
    def resume(case):
        client,db,sid,child = case['client'],case['db'],case['next_sid'],case['child']
        response=client.post(f'/v1/sessions/{sid}/transition',json=dict(expected_state='plan_review',target_state='study',
            reason='今天继续未交付的新题。',client_action_id='cross-oral-study'))
        assert response.status_code==200,(response.text,[f[-2] for f in failures[-4:]])
        quote='继续昨天缺的那道'+subject+'口述新题，我现在自己试试。'
        case['command_state'].update(control='task.resume',quote=quote,
            selector={'subject':'math' if subject=='数学' else 'physics','task_kind':'oral','relative_order':None})
        voice=capture(client,case['settings'],monkeypatch,sid,quote,key='cross-resume-voice',child=child)
        turn=interpret(client,sid,quote,'cross-resume',voice)
        assert turn.get('runtime_capability')=='task.resume',turn
        if before_execute:
            if before_execute({**case,'resume_turn':turn}) is False:
                return
        response=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code==200,(response.text,[f[-2] for f in failures[-4:]])
        executed=response.json()['execution']
        assert executed['result']['status']=='active',executed
        prompt=executed['result']['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id']==case['published']['diagnostic_item_id']
        assert prompt['origin_session_id']==sid and prompt['prompt_id']!=case['prompt']['prompt_id']
        with db.connect() as c:
            root=c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(case['commitment']['commitment_id'],)).fetchone()
            payload=json.loads(root['payload_json'])
            assert root['status']=='completed' and not payload['resolution']['learning_completed']
            assert payload['resolution']['session_id']==sid and payload['resolution']['prompt_id']==prompt['prompt_id']
            plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
            bound=json.loads(plan['plan_json'])['local_learning_binding']
            assert bound['root_objective_id']==case['commitment']['objective_id']
            assert bound['objective_id']!=bound['root_objective_id'] and bound['session_id']==sid
            assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',(case['attempt']['attempt_id'],)).fetchone()[0]==case['quote']
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(child,)).fetchone()[0]==1
            item_id=executed['result']['plan_item_id']
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(item_id,)).fetchone()[0]==1
        repeat=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert repeat.status_code==200,repeat.text
        detail=client.get(case['followup_path'], headers=parent_headers(case)).json()
        assert detail['status']=='ready' and detail['current_session_id']==sid,detail
        with fresh_oral_case(case, TestClient, client=('127.0.0.1', 41984)) as fresh_case:
            case = fresh_case
            client = restarted = case['client']
            restored=restarted.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
            assert restored.status_code==200,restored.text
            assert restored.json()['status']=='waiting_answer',restored.json()
            assert restored.json()['prompt']['prompt_id']==prompt['prompt_id']
            sent=json.dumps(case['posts'],ensure_ascii=False)
            assert case['commitment']['commitment_id'] not in sent
            assert case['second_source']['prompt'] not in sent
            detail=client.get(case['followup_path'], headers=parent_headers(case))
            assert detail.status_code==200,detail.text
            assert detail.json()['current_session_id']==sid and detail.json()['status']=='paused',detail.json()
            assert detail.json()['plan_item_id']==item_id
            voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,key='cross-after-restart-voice',child=child)
            resumed_turn=interpret(client,sid,quote,'cross-after-restart',voice)
            assert resumed_turn.get('runtime_capability')=='task.resume',resumed_turn
            resumed=client.post('/v1/agent-turns/'+resumed_turn['agent_turn_id']+'/execute')
            assert resumed.status_code==200,resumed.text
            assert resumed.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
            assert client.get(case['followup_path'], headers=parent_headers(case)).json()['status']=='ready'
            obligations=client.get(f'/v1/sessions/{sid}').json()['supply_obligations']
            linked=[r for r in obligations if r.get('root_obligation_id')==case['commitment']['commitment_id']]
            assert len(linked)==1 and linked[0]['status']=='active' and linked[0]['plan_item_id']==item_id,linked
            if after_delivery:
                after_delivery({**case,'new_prompt':prompt,'new_item_id':item_id,'resume_turn':turn})
    journey(tmp_path,monkeypatch,subject,after_ready=resume)


def test_new_oral_answer_gets_current_goal_feedback_and_persists(tmp_path,monkeypatch,before_answer=None,after_answer=None,delivered_case=None,after_completion=None):
    def answer(case):
        if before_answer:
            before_answer(case)
        client,sid,child,prompt=case['client'],case['next_sid'],case['child'],case['new_prompt']
        from test_writing_question_authorization import LICENSE
        path=f"/v1/parent/children/{child}/writing-question-authorizations/{prompt['diagnostic_item_id']}"
        auth=client.get(path, headers=parent_headers(case)).json()
        granted=client.post(path+'/grant',json={**{k:auth[k] for k in ('item_version','source_version','curriculum_version')},
            'expected_revision':auth['revision'],'client_action_id':'cross-new-cloud-source','license_basis':LICENSE}, headers=parent_headers(case))
        assert granted.status_code==200,granted.text
        quote='a是4，b是4，c是1，判别式等于4的平方减去4乘4乘1，等于0，所以这个方程有两个相等的实数根。'
        case['command_state'].update(control='answer',quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,'cross-new-answer-audio',child=child)
        heard=interpret(client,sid,quote,'cross-new-answer-intent',voice)
        assert heard['action']['type']=='submit_diagnostic_answer',heard
        request=dict(
            prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=quote,
            client_action_id='cross-new-answer',authorized_turn_id=heard['agent_turn_id'])
        case['new_answer_request']=request
        submitted=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json=request)
        assert submitted.status_code==200,submitted.text
        result=submitted.json()
        assert result['attempt']['submitted_answer']==quote and result['attempt']['correctness'] is None
        if after_answer and after_answer(case,result) is False:
            return
        follow=result['local_learning_evaluation']
        assert follow['status']=='feedback_required',follow
        evaluated=client.post('/v1/agent-turns/'+follow['feedback_turn_id']+'/execute')
        assert evaluated.status_code==200,evaluated.text
        assert evaluated.json()['execution']['result']['formative_only'] is True
        assert case['evaluations'][-1]['response']==quote
        assert case['evaluations'][-1]['question']==case['second_source']['prompt']
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(child,)).fetchone()[0]==2
            assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',
                (case['attempt']['attempt_id'],)).fetchone()[0]==case['quote']
        detail=client.get(case['followup_path'], headers=parent_headers(case)).json()
        assert detail['status']=='practice_completed',detail
        quote='这道题我已经回答并核对反馈了，结束这个任务。'
        case['command_state'].update(control='task.complete',quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,'cross-new-complete-audio',child=child)
        complete=interpret(client,sid,quote,'cross-new-complete-intent',voice)
        assert complete.get('runtime_capability')=='task.complete',complete
        completed=client.post('/v1/agent-turns/'+complete['agent_turn_id']+'/execute')
        assert completed.status_code==200,completed.text
        with case['db'].connect() as c:
            task=c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case['new_item_id'],)).fetchone()
            assert task['status']=='completed'
        with fresh_oral_case(case, TestClient, client=('127.0.0.1', 41984)) as fresh_case:
            case = fresh_case
            client = reopened = case['client']
            token=reopened.post('/v1/parent/unlock',json={'pin':'8264'})
            assert token.status_code==200,token.text
            case['headers'].update({'X-HSC-Parent':token.json()['parent_token']})
            saved=reopened.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']}).json()
            assert saved['status']=='answered' and saved['attempt']['attempt_id']==result['attempt']['attempt_id'],saved
            response=reopened.get(case['followup_path'], headers=parent_headers(case))
            assert response.status_code==200,response.text
            detail=response.json()
            assert detail['status']=='practice_completed' and detail['plan_item_id']==case['new_item_id'],detail
            if after_completion:
                after_completion({**case,'new_answer':result})
    if delivered_case is None:
        test_voice_resume_delivers_new_question_with_old_goal_lineage(tmp_path,monkeypatch,'数学',after_delivery=answer)
    else:
        answer(delivered_case)


def test_delivery_write_failure_rolls_back_new_work_and_retries_same_voice(tmp_path,monkeypatch):
    def fail(case):
        db=case['db']
        def snapshot(c):
            return {table:[tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('agent_commitment','agent_objective','diagnostic_prompt','diagnostic_attempt',
                    'daily_plan','plan_item','plan_item_event','plan_constraint_decision')}
        with db.connect() as c:
            before=snapshot(c)
            c.execute('''CREATE TRIGGER deny_cross_delivery BEFORE UPDATE ON agent_commitment
                WHEN NEW.status='completed' AND json_extract(NEW.payload_json,'$.origin')='oral_question_supply_v1'
                BEGIN SELECT RAISE(ABORT,'synthetic cross delivery failure'); END''')
            c.commit()
        failed=case['client'].post('/v1/agent-turns/'+case['resume_turn']['agent_turn_id']+'/execute')
        assert failed.status_code>=500,failed.text
        with db.connect() as c:
            assert snapshot(c)==before,'Partial task, goal, prompt, timer, or delivery survived rollback.'
            c.execute('DROP TRIGGER deny_cross_delivery')
            c.commit()
    test_voice_resume_delivers_new_question_with_old_goal_lineage(tmp_path,monkeypatch,'数学',before_execute=fail)


def _take_break(case,monkeypatch):
    client,sid=case['client'],case['next_sid']
    for control,quote,state in [('break.begin','我想休息一下，先暂停学习。','break'),
            ('break.resume','休息好了，继续刚才的题。','study')]:
        case['command_state'].update(control=control,quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid,quote,case['new_prompt'],control+'-audio',child=case['child'])
        turn=interpret(client,sid,quote,control+'-intent',voice)
        assert turn.get('runtime_capability')==control,turn
        result=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert result.status_code==200,result.text
        assert client.get(f'/v1/sessions/{sid}').json()['session']['state']==state
    restored=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':case['new_prompt']['prompt_id']}).json()
    assert restored['status']=='waiting_answer',restored
    assert client.get(case['followup_path'], headers=parent_headers(case)).json()['status']=='ready'


def test_voice_break_returns_to_same_delivered_question(tmp_path,monkeypatch):
    test_voice_resume_delivers_new_question_with_old_goal_lineage(tmp_path,monkeypatch,'数学',
        after_delivery=lambda case:_take_break(case,monkeypatch))


def test_break_then_answer_feedback_and_settlement_keep_original_question(tmp_path,monkeypatch):
    test_new_oral_answer_gets_current_goal_feedback_and_persists(tmp_path,monkeypatch,
        before_answer=lambda case:_take_break(case,monkeypatch))


@pytest.mark.parametrize('fault',['timer_event','transition','task_scope','other_child','result_owner'])
def test_break_history_tampering_blocks_feedback_but_keeps_answer(tmp_path,monkeypatch,fault):
    def alter(case):
        _take_break(case,monkeypatch)
        with case['db'].connect() as c:
            receipt=c.execute("SELECT * FROM break_control_receipt WHERE session_id=? AND operation='begin'",
                (case['next_sid'],)).fetchone()
            if fault=='timer_event':
                c.execute('DELETE FROM plan_item_event WHERE client_action_id=?',(receipt['client_action_id']+':pause',))
            elif fault=='transition':
                c.execute("UPDATE session_transition SET from_state='break' WHERE transition_id=?",(receipt['transition_id'],))
            elif fault=='task_scope':
                payload=json.loads(receipt['binding_json'])
                payload['resume_item_id']='different-synthetic-task'
                c.execute('UPDATE break_control_receipt SET binding_json=? WHERE client_action_id=?',
                    (json.dumps(payload),receipt['client_action_id']))
            elif fault=='other_child':
                c.execute("UPDATE break_control_receipt SET child_id='child-primary-4' WHERE client_action_id=?",
                    (receipt['client_action_id'],))
            else:
                proof=json.loads(receipt['result_json'])
                proof['child_id']='child-primary-4'
                encoded=json.dumps(proof)
                c.execute('UPDATE break_control_receipt SET result_json=? WHERE client_action_id=?',
                    (encoded,receipt['client_action_id']))
                c.execute("UPDATE agent_step SET result_json=json_set(result_json,'$.result.break_receipt',json(?)) WHERE idempotency_key=?",
                    (encoded,receipt['client_action_id']))
            c.commit()
        case['grades_before_answer']=len(case['evaluations'])
    def verify(case,result):
        follow=result['local_learning_evaluation']
        assert follow['status']=='blocked',(follow.get('reason'),follow)
        assert len(case['evaluations'])==case['grades_before_answer']
        repeated=case['client'].post(f"/v1/sessions/{case['next_sid']}/diagnostic-prompt/answer",json=case['new_answer_request'])
        assert repeated.status_code==200,repeated.text
        assert repeated.json()['attempt']['attempt_id']==result['attempt']['attempt_id']
        with case['db'].connect() as c:
            saved=c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',
                (result['attempt']['attempt_id'],)).fetchone()
            assert saved[0]==result['attempt']['submitted_answer']
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE child_id=?',(case['child'],)).fetchone()[0]==2
        return False
    test_new_oral_answer_gets_current_goal_feedback_and_persists(tmp_path,monkeypatch,
        before_answer=alter,after_answer=verify)


@pytest.mark.parametrize('fault',['source','plan','answer'])
def test_late_changes_block_actual_delivery_without_new_work(tmp_path,monkeypatch,fault):
    def change(case):
        with case['db'].connect() as c:
            if fault=='source':
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",
                    (case['published']['diagnostic_item_id'],))
            elif fault=='plan':
                c.execute('UPDATE daily_plan SET version=version+1 WHERE session_id=?',(case['next_sid'],))
            else:
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='changed synthetic answer' WHERE attempt_id=?",
                    (case['attempt']['attempt_id'],))
            c.commit()
            before={table:[tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('agent_commitment','agent_objective','diagnostic_prompt','plan_item','plan_item_event')}
        result=case['client'].post('/v1/agent-turns/'+case['resume_turn']['agent_turn_id']+'/execute')
        assert result.status_code==409,(fault,result.text)
        with case['db'].connect() as c:
            after={table:[tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in before}
            assert after==before
        return False
    test_voice_resume_delivers_new_question_with_old_goal_lineage(tmp_path,monkeypatch,'数学',before_execute=change)
