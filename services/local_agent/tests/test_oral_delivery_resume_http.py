"""A child's explicit next-day continuation reaches the same question, not a demo."""
import json
from synthetic_oral_app_lifecycle import parent_headers
from datetime import datetime,timedelta
import pytest

from test_oral_delivered_multiday import test_unanswered_delivery_survives_closure_and_third_plan as retained_journey
from test_voice_runtime_binding import capture, interpret
from test_writing_stage_review_consumers import advance_clock


def test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,after_start=None,before_start=None,before_deferral=None):
    def resume(case):
        client,db,sid,child = case['client'],case['db'],case['sid3'],case['child']
        def post(path,body=None):
            r=client.post(path,json=body)
            assert r.status_code in (200,201),r.text
            return r.json()
        case['headers']['X-HSC-Parent']=post('/v1/parent/unlock',{'pin':'8264'})['parent_token']
        post(f'/v1/sessions/{sid}/transition',dict(expected_state='plan_review',target_state='study',
            reason='继续昨天没回答的题。',client_action_id='third-continue-study'))
        quote='继续昨天没回答完的数学口述题。'
        case['command_state'].update(control='task.resume',quote=quote,
            selector={'subject':'math','task_kind':'oral','relative_order':None})
        voice=capture(client,case['settings'],monkeypatch,sid,quote,key='third-continue-voice',child=child)
        turn=interpret(client,sid,quote,'third-continue-intent',voice)
        assert turn.get('runtime_capability')=='task.resume',turn
        if before_start and before_start({**case,'continuation_turn':turn}) is False:
            return
        with db.connect() as c:
            original=dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(case['new_prompt']['prompt_id'],)).fetchone())
            root=dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(case['commitment']['commitment_id'],)).fetchone())
        execution=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')['execution']
        assert '接着上次还没回答的这道题' in execution['response_text'],execution
        result=execution['result']
        assert result['status']=='active',result
        prompt=result['diagnostic_prompt']['prompt']
        assert prompt['prompt_id']!=original['prompt_id'] and prompt['origin_session_id']==sid
        assert prompt['diagnostic_item_id']==original['diagnostic_item_id'] and prompt['item_version']==original['item_version']
        replay=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')['execution']['result']
        assert replay['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(original['prompt_id'],)).fetchone())==original
            assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(root['commitment_id'],)).fetchone())==root
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==1
            plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
            receipt=json.loads(plan['context_manifest_json'])['oral_delivery_continuation_receipt']
            assert receipt['previous_resolution']['prompt_id']==original['prompt_id']
            assert receipt['resolution']['prompt_id']==prompt['prompt_id']
        detail=client.get(case['followup_path'], headers=parent_headers(case))
        assert detail.status_code==200,detail.text
        assert detail.json()['status']=='ready' and detail.json()['current_session_id']==sid,detail.json()
        current=client.get(f'/v1/sessions/{sid}').json()
        linked=[v for v in current['supply_obligations'] if v.get('root_obligation_id')==root['commitment_id']]
        assert len(linked)==1 and linked[0]['status']=='active' and linked[0]['plan_item_id']==result['plan_item_id'],linked
        if after_start:
            after_start({**case,'next_sid':sid,'new_prompt':prompt,'new_item_id':result['plan_item_id'],
                'continuation_turn':turn,'previous_prompt':original})
    retained_journey(tmp_path,monkeypatch,'数学',after_retention=resume,before_deferral=before_deferral)


def test_continued_answer_feedback_voice_completion_and_restart(tmp_path,monkeypatch):
    from test_oral_supply_cross_day_execute import test_new_oral_answer_gets_current_goal_feedback_and_persists as answer_journey
    def answer(case):
        answer_journey(tmp_path,monkeypatch,delivered_case=case)
        transmitted=json.dumps(case['posts'],ensure_ascii=False)
        assert case['previous_prompt']['prompt_id'] not in transmitted
        assert case['commitment']['commitment_id'] not in transmitted
        assert 'oral_delivery_continuation_receipt' not in transmitted
    test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,after_start=answer)


def test_continuation_break_then_answer_keeps_current_question(tmp_path,monkeypatch):
    from test_oral_supply_cross_day_execute import test_new_oral_answer_gets_current_goal_feedback_and_persists as answer_journey, _take_break
    def answer(case):
        answer_journey(tmp_path,monkeypatch,delivered_case=case,before_answer=lambda current:_take_break(current,monkeypatch))
    test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,after_start=answer)


def test_prior_question_help_survives_day_boundary_in_answer_evidence(tmp_path,monkeypatch):
    from test_oral_supply_cross_day_execute import test_new_oral_answer_gets_current_goal_feedback_and_persists as answer_journey
    def help_fixture(case):
        # Persisted tutor result is a fixture, not a claim of live model teaching.
        helped_at=datetime.fromisoformat(case['new_prompt']['created_at'])+timedelta(seconds=1)
        advance_clock(monkeypatch,helped_at)
        with case['db'].connect() as c:
            item=c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',(case['new_prompt']['diagnostic_item_id'],)).fetchone()
            c.execute("""INSERT INTO tutor_turn(turn_id,session_id,child_id,plan_item_id,subject,question,answer,
                provider,model,status,hint_level,created_at,client_action_id)
                VALUES('prior-continuation-help',?,?,?,'数学',?,'先看各项系数。','fixture','fixture','completed',1,?,'prior-continuation-help')""",
                (case['next_sid'],case['child'],case['new_item_id'],item['prompt'],helped_at.isoformat()))
            c.commit()
    def answer(case):
        def verify(current,result):
            with current['db'].connect() as c:
                evidence=c.execute('SELECT hint_level,independent FROM learning_evidence WHERE evidence_id=?',
                    (result['attempt']['evidence_id'],)).fetchone()
                assert evidence['hint_level']>=1 and not evidence['independent']
        answer_journey(tmp_path,monkeypatch,delivered_case=case,after_answer=verify)
    test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,after_start=answer,before_deferral=help_fixture)


@pytest.mark.parametrize('fault',['source','plan','previous_answer','deferral_event'])
def test_late_change_rejects_continuation_without_allocating_new_work(tmp_path,monkeypatch,fault):
    def alter(case):
        db,client=case['db'],case['client']
        with db.connect() as c:
            if fault=='source':
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(case['new_prompt']['diagnostic_item_id'],))
            elif fault=='plan':
                c.execute('UPDATE daily_plan SET version=version+1 WHERE plan_id=?',(case['workspace3']['plan']['plan_id'],))
            elif fault=='previous_answer':
                c.execute("UPDATE diagnostic_prompt SET status='answer_saved',answer_json='{}' WHERE prompt_id=?",(case['new_prompt']['prompt_id'],))
            else:
                c.execute("DELETE FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled'",(case['new_item_id'],))
            c.commit()
            before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')]
                for t in ('plan_item','daily_plan','diagnostic_prompt','diagnostic_attempt','agent_commitment','plan_constraint_decision')}
        r=client.post('/v1/agent-turns/'+case['continuation_turn']['agent_turn_id']+'/execute')
        assert r.status_code==409,r.text
        with db.connect() as c:
            assert {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in before}==before
        return False
    test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,before_start=alter)


def test_receipt_write_failure_rolls_back_task_prompt_and_budget_then_retries(tmp_path,monkeypatch):
    def fail_once(case):
        db,client=case['db'],case['client']
        with db.connect() as c:
            before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')]
                for t in ('plan_item','daily_plan','diagnostic_prompt','diagnostic_attempt','agent_objective','agent_commitment','plan_constraint_decision')}
            c.execute("""CREATE TRIGGER reject_continuation BEFORE UPDATE ON agent_plan
                WHEN json_type(NEW.context_manifest_json,'$.oral_delivery_continuation_receipt')='object'
                BEGIN SELECT RAISE(ABORT,'synthetic continuation write failure'); END""")
            c.commit()
        r=client.post('/v1/agent-turns/'+case['continuation_turn']['agent_turn_id']+'/execute')
        assert r.status_code>=400,r.text
        with db.connect() as c:
            assert {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in before}==before
            c.execute('DROP TRIGGER reject_continuation')
            c.commit()
    test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,before_start=fail_once)


@pytest.mark.parametrize('fault',['missing','changed'])
def test_missing_or_forged_continuation_receipt_cannot_authorize_answer(tmp_path,monkeypatch,fault):
    def corrupt(case):
        from home_study_agent.oral_delivery_resume import resolve_execution,validate_source
        with case['db'].connect() as c:
            c.execute('BEGIN')
            target=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                (case['continuation_turn']['agent_turn_id'],)).fetchone()[0])['supply_resume_receipt']
            target['supply_item']['planning_evidence']['root_objective_id']='foreign-goal'
            with pytest.raises(ValueError):
                validate_source(c,target)
            c.rollback()
            if fault=='missing':
                c.execute("UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.oral_delivery_continuation_receipt') WHERE agent_turn_id=?",
                    (case['continuation_turn']['agent_turn_id'],))
            else:
                c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.oral_delivery_continuation_receipt.resolution.objective_id','another-goal') WHERE agent_turn_id=?",
                    (case['continuation_turn']['agent_turn_id'],))
            c.commit()
            c.execute('BEGIN')
            with pytest.raises(ValueError):
                resolve_execution(c,case['next_sid'],case['new_item_id'])
        r=case['client'].get(f"/v1/sessions/{case['next_sid']}/diagnostic-prompt",params={'prompt_id':case['new_prompt']['prompt_id']})
        assert r.status_code==200,r.text
        assert r.json()['status']=='invalidated',r.json()
        r=case['client'].get(case['followup_path'], headers=parent_headers(case))
        assert r.status_code==200 and r.json()['status']=='source_changed',r.text
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(case['child'],)).fetchone()[0]==1
    test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,after_start=corrupt)


def test_fourth_day_continues_latest_unanswered_presentation_then_answers(tmp_path,monkeypatch):
    from test_oral_supply_cross_day_execute import test_new_oral_answer_gets_current_goal_feedback_and_persists as answer_journey
    def fourth(case):
        client,db,sid,child=case['client'],case['db'],case['next_sid'],case['child']
        def post(path,body=None):
            r=client.post(path,json=body)
            assert r.status_code in (200,201),r.text
            return r.json()
        for control,quote in [('task.pause','暂停这道题。'),('task.reschedule','这道题延期到明天。')]:
            case['command_state'].update(control=control,quote=quote)
            voice=capture(client,case['settings'],monkeypatch,sid,quote,case['new_prompt'],key='fourth-'+control,child=child)
            turn=interpret(client,sid,quote,'fourth-intent-'+control,voice)
            assert turn.get('runtime_capability')==control,turn
            post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        for item in client.get(f'/v1/sessions/{sid}').json()['plan_items']:
            if item['status'] in ('pending','paused'):
                post(f"/v1/plan-items/{item['plan_item_id']}/reschedule",dict(reason='今天先结束。',client_action_id='fourth-defer-'+item['plan_item_id']))
        post(f'/v1/sessions/{sid}/transition',dict(expected_state='study',target_state='reflection',reason='明天继续。',client_action_id='fourth-reflect'))
        post(f'/v1/sessions/{sid}/reflection/complete',dict(learned_text='',difficult_text='',client_action_id='fourth-close'))
        with db.connect() as c:
            ended=c.execute('SELECT ended_at FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0]
        advance_clock(monkeypatch,datetime.fromisoformat(ended)+timedelta(days=1))
        case['headers']['X-HSC-Parent']=post('/v1/parent/unlock',dict(pin='8264'))['parent_token']
        next_sid=post('/v1/sessions',dict(child_id=child,client_action_id='fourth-session'))['session_id']
        r=client.put(f'/v1/sessions/{next_sid}/planning-context',json=dict(energy_level='medium',fixed_commitment_minutes=0,client_action_id='fourth-context'))
        assert r.status_code==200,r.text
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?",(next_sid,))
            c.commit()
        planned=post(f'/v1/sessions/{next_sid}/plan/generate',dict(client_action_id='fourth-plan'))
        kept=[d for d in planned['constraint_decisions'] if d['obligation_type']=='oral_delivered_continuation']
        assert len(kept)==1
        assert json.loads(kept[0]['metadata_json'])['oral_delivered_continuation']['prompt_id']==case['new_prompt']['prompt_id']
        post(f'/v1/sessions/{next_sid}/transition',dict(expected_state='plan_review',target_state='study',reason='继续未答的题。',client_action_id='fourth-study'))
        quote='继续上次没答完的数学口述题。'
        case['command_state'].update(control='task.resume',quote=quote,selector={'subject':'math','task_kind':'oral','relative_order':None})
        voice=capture(client,case['settings'],monkeypatch,next_sid,quote,key='fourth-resume-voice',child=child)
        turn=interpret(client,next_sid,quote,'fourth-resume',voice)
        assert turn.get('runtime_capability')=='task.resume',turn
        result=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')['execution']['result']
        prompt=result['diagnostic_prompt']['prompt']
        with db.connect() as c:
            receipt=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0])['oral_delivery_continuation_receipt']
            assert receipt['previous_resolution']['prompt_id']==case['new_prompt']['prompt_id']
            assert c.execute("SELECT COUNT(*) FROM agent_plan WHERE json_type(context_manifest_json,'$.oral_delivery_continuation_receipt')='object'").fetchone()[0]==2
        answer_journey(tmp_path,monkeypatch,delivered_case={**case,'next_sid':next_sid,'new_prompt':prompt,'new_item_id':result['plan_item_id']})
    test_third_day_voice_continuation_delivers_same_question_once(tmp_path,monkeypatch,after_start=fourth)
