"""Real parent withdrawal/replacement transaction, synthetic voice and sources."""
import json
from synthetic_oral_app_lifecycle import fresh_oral_case, parent_headers
from datetime import timedelta, timezone

import pytest

from test_oral_answer_daily_followup_http import test_next_day_check_uses_distinct_published_question_not_model_prompt as daily
from test_oral_supply_cross_day_source import publish
from test_voice_runtime_binding import capture,interpret
from test_writing_stage_review_consumers import advance_clock


def test_withdrawal_keeps_old_answer_and_replaces_original_daily_duty(tmp_path,monkeypatch,fault=None):
    from home_study_agent.knowledge_base import KnowledgeBaseService
    from home_study_agent import agent_runtime
    importer=KnowledgeBaseService.import_source
    def fourth(service,child,values):
        if values.get('source_ref')=='isolated-spoken-source':
            rows=[json.loads(s) for s in values['content_text'].splitlines()]
            extra={**rows[-1],'prompt':'不直接求根，口头判断 3x²+2x-1=0 的实数根情况并说明依据。'}
            fifth={**extra,'prompt':'不直接求根，口头判断 5x²+6x-2=0 的实数根情况并说明依据。'}
            values={**values,'content_text':values['content_text']+'\n'+json.dumps(extra,ensure_ascii=False)+'\n'+json.dumps(fifth,ensure_ascii=False)}
        return importer(service,child,values)
    monkeypatch.setattr(KnowledgeBaseService,'import_source',fourth)

    def replace(case):
        client,db,child,sid=case['client'],case['db'],case['child'],case['daily_sid']
        old,old_task=case['daily_prompt'],case['daily_task']
        root=old_task['source_ref']
        def post(path,body, *, headers=None):
            r=client.post(path,json=body,headers=headers)
            assert r.status_code in (200,201),r.text
            return r.json()
        def say(quote,key,prompt=None):
            voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,key=key+'-voice',child=child)
            return interpret(client,sid,quote,key,voice)
        def answer(prompt,key):
            from test_writing_question_authorization import LICENSE
            path=f"/v1/parent/children/{child}/writing-question-authorizations/{prompt['diagnostic_item_id']}"
            auth=client.get(path, headers=parent_headers(case)).json()
            post(path+'/grant',{**{k:auth[k] for k in ('item_version','source_version','curriculum_version')},
                'expected_revision':auth['revision'],'client_action_id':key+'-auth','license_basis':LICENSE}, headers=parent_headers(case))
            quote='判别式大于零，所以有两个不相等的实数根，我按系数代入计算。'
            case['command_state'].update(control='answer',quote=quote)
            reply=say(quote,key,prompt)
            return post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',dict(prompt_id=prompt['prompt_id'],
                item_version=prompt['item_version'],answer=quote,client_action_id=key+'-save',authorized_turn_id=reply['agent_turn_id']))
        old_answer=answer(old,'withdrawn-original-answer')
        with db.connect() as c:
            sibling=c.execute('SELECT child_id FROM child_profile WHERE child_id!=? AND status=\'active\' LIMIT 1',(child,)).fetchone()[0]
        sibling_session='withdrawal-sibling-session'
        with db.connect() as c:
            # One device permits one active child. Seed the sibling's previous
            # completed session rather than bypassing that production rule.
            c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,ended_at,
                last_checkpoint_at,client_action_id) SELECT ?,?,'completed','completed',started_at,
                started_at,started_at,'withdrawal-sibling-session' FROM daily_session WHERE session_id=?''',
                (sibling_session,sibling,sid))
            c.execute('''INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,
                version,created_at,client_action_id) SELECT 'withdrawal-sibling-plan',?,?,plan_date,'active',10,
                'synthetic-sibling',1,created_at,'withdrawal-sibling-plan' FROM daily_plan WHERE plan_id=?''',
                (sibling,sibling_session,c.execute('SELECT plan_id FROM plan_item WHERE plan_item_id=?',
                    (old_task['plan_item_id'],)).fetchone()[0]))
            c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position)
                VALUES ('withdrawal-sibling-task','withdrawal-sibling-plan','数学','另一孩子的任务',10,1,'pending',1)''')
            sibling_task=tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='withdrawal-sibling-task'").fetchone())
            c.commit()
        advance_clock(monkeypatch,agent_runtime.datetime.now(timezone.utc)+timedelta(minutes=2))
        with db.connect() as c:
            candidate=dict(c.execute('SELECT p.* FROM curriculum_supply_package p JOIN specialized_supply_candidate s USING(candidate_id) WHERE s.diagnostic_item_id=?',(old['diagnostic_item_id'],)).fetchone())
            original_answer=tuple(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(old_answer['attempt']['attempt_id'],)).fetchone())
        path=f"/api/parent/children/{child}/curriculum-supply/{candidate['candidate_id']}/withdraw"
        body=dict(action_id='daily-source-withdraw',expected_version=candidate['version'],note='合成来源撤回，保留孩子已做内容。')
        if fault=='write_failure':
            tables=('curriculum_supply_package','curriculum_supply_event','specialized_supply_candidate','diagnostic_item',
                'plan_item','plan_item_event','daily_plan','agent_plan','agent_step','agent_turn','agent_objective','diagnostic_prompt','plan_constraint_decision','specialized_supply_job')
            with db.connect() as c:
                before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}
                c.execute("""CREATE TRIGGER reject_withdrawal_gap BEFORE INSERT ON plan_constraint_decision
                    WHEN json_type(NEW.metadata_json,'$.oral_daily_withdrawal_event_id')='text'
                    BEGIN SELECT RAISE(ABORT,'synthetic withdrawal write failure'); END""")
                c.commit()
            failed=client.post(path,json=body, headers=parent_headers(case))
            assert failed.status_code>=400,failed.text
            with db.connect() as c:
                assert {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}==before
                c.execute('DROP TRIGGER reject_withdrawal_gap')
                c.commit()
        withdrawn=post(path,body, headers=parent_headers(case))
        assert withdrawn['state']=='withdrawn'
        post(path,body, headers=parent_headers(case))
        with db.connect() as c:
            stopped=dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?',(old_task['plan_item_id'],)).fetchone())
            assert stopped['status']=='cancelled' and stopped['active_seconds']>=120 and stopped['active_segment_started_at'] is None,stopped
            assert tuple(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(old_answer['attempt']['attempt_id'],)).fetchone())==original_answer
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='source_withdrawn'",(old_task['plan_item_id'],)).fetchone()[0]==1
            assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(root,)).fetchone()[0]=='pending'
            assert tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='withdrawal-sibling-task'").fetchone())==sibling_task
        old_view=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':old['prompt_id']}).json()
        assert old_view['status']=='invalidated' and old_view['reason']=='source_withdrawn',old_view
        pending_feedback=old_answer['local_learning_evaluation']['feedback_turn_id']
        assert client.post('/v1/agent-turns/'+pending_feedback+'/execute',json={}).status_code==409
        state=client.get(f'/v1/sessions/{sid}').json()
        gaps=[g for g in state['supply_obligations'] if g.get('root_obligation_id')==root]
        assert len(gaps)==1 and gaps[0]['status']=='missing_source',gaps
        if fault in ('damaged_receipt','missing_marker'):
            with db.connect() as c:
                if fault=='missing_marker':
                    c.execute("UPDATE plan_constraint_decision SET metadata_json='{}' WHERE plan_id=? AND obligation_type='oral_daily_check' AND decision='deferred'",(stopped['plan_id'],))
                else:
                    c.execute("UPDATE plan_item_event SET payload_json=json_set(payload_json,'$.scheduled_version','forged') WHERE plan_item_id=? AND event_type='source_withdrawn'",(old_task['plan_item_id'],))
                c.commit()
        newer=publish(case,'数学',excerpt_index=3,key='withdrawal-replacement')
        state=client.get(f'/v1/sessions/{sid}').json()
        gaps=[g for g in state['supply_obligations'] if g.get('root_obligation_id')==root]
        if fault in ('damaged_receipt','missing_marker'):
            assert len(gaps)==1 and gaps[0]['status']=='source_changed',gaps
            with db.connect() as c:
                from home_study_agent.supply_gap_resume import candidates
                c.execute('BEGIN')
                assert not any(t['source_ref']==root for t in candidates(c,sid,child))
                assert c.execute('SELECT COUNT(*) FROM plan_item WHERE source_type=\'oral_daily_check\' AND source_ref=?',(root,)).fetchone()[0]==1
            return
        assert len(gaps)==1 and gaps[0]['status']=='ready',gaps
        quote='继续今天还没完成的数学口述复查。'
        case['command_state'].update(control='task.resume',quote=quote,selector={'subject':'math','task_kind':'oral','relative_order':None})
        turn=say(quote,'replacement-resume')
        assert turn.get('runtime_capability')=='task.resume',turn
        result=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})['execution']['result']
        prompt=result['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id']==newer['diagnostic_item_id'] and prompt['prompt_id']!=old['prompt_id']
        assert post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})['execution']['result']['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
        if fault=='late_receipt_damage':
            with db.connect() as c:
                c.execute("UPDATE plan_item_event SET payload_json=json_set(payload_json,'$.scheduled_version','forged-after-start') WHERE plan_item_id=? AND event_type='source_withdrawn'",(old_task['plan_item_id'],))
                c.commit()
            invalid=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']}).json()
            assert invalid['status']=='invalidated',invalid
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0]==1
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(root,)).fetchone()[0]=='pending'
            return
        if fault=='repeat':
            with db.connect() as c:
                current=c.execute('SELECT version FROM curriculum_supply_package WHERE candidate_id=?',(newer['candidate_id'],)).fetchone()[0]
            path2=f"/api/parent/children/{child}/curriculum-supply/{newer['candidate_id']}/withdraw"
            post(path2,dict(action_id='daily-second-withdraw',expected_version=current,note='第二次合成来源撤回。'), headers=parent_headers(case))
            latest=publish(case,'数学',excerpt_index=4,key='withdrawal-second-replacement')
            second=say(quote,'second-replacement-resume')
            replacement=post('/v1/agent-turns/'+second['agent_turn_id']+'/execute',{})['execution']['result']['diagnostic_prompt']['prompt']
            assert replacement['prompt_id']!=prompt['prompt_id'] and replacement['diagnostic_item_id']==latest['diagnostic_item_id']
            prompt=replacement
        new_answer=answer(prompt,'replacement-answer')
        feedback=post('/v1/agent-turns/'+new_answer['local_learning_evaluation']['feedback_turn_id']+'/execute',{})
        assert feedback['execution']['result']['formative_only']
        quote='已经核对反馈了，结束这项复查。'
        case['command_state'].update(control='task.complete',quote=quote)
        finish=say(quote,'replacement-finish',prompt)
        post('/v1/agent-turns/'+finish['agent_turn_id']+'/execute',{})
        from home_study_agent.app import create_app
        from fastapi.testclient import TestClient
        with fresh_oral_case(case, TestClient, client=('127.0.0.1', 42015)) as fresh_case:
            case = fresh_case
            client = opened = case['client']
            restored=opened.get(f'/v1/sessions/{sid}').json()
            gaps=[g for g in restored['supply_obligations'] if g.get('root_obligation_id')==root]
            assert len(gaps)==1 and gaps[0]['status']=='practice_completed',gaps
            with db.connect() as c:
                assert tuple(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(old_answer['attempt']['attempt_id'],)).fetchone())==original_answer
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(root,)).fetchone()[0]=='completed'
                statuses=[r[0] for r in c.execute('SELECT status FROM plan_item WHERE source_type=\'oral_daily_check\' AND source_ref=? ORDER BY rowid',(root,))]
                assert statuses==(['cancelled','cancelled','completed'] if fault=='repeat' else ['cancelled','completed']),statuses
                assert tuple(c.execute("SELECT * FROM plan_item WHERE plan_item_id='withdrawal-sibling-task'").fetchone())==sibling_task
            assert root not in json.dumps(case['posts'],ensure_ascii=False)
    daily(tmp_path,monkeypatch,after_start=replace)


def test_withdrawal_and_gap_roll_back_together_then_same_request_recovers(tmp_path,monkeypatch):
    test_withdrawal_keeps_old_answer_and_replaces_original_daily_duty(tmp_path,monkeypatch,'write_failure')


@pytest.mark.parametrize('fault',['repeat','damaged_receipt','missing_marker','late_receipt_damage'])
def test_replacement_lineage_is_versioned_and_not_guessed(tmp_path,monkeypatch,fault):
    test_withdrawal_keeps_old_answer_and_replaces_original_daily_duty(tmp_path,monkeypatch,fault)


def test_withdrawal_cancels_exact_queued_start_before_a_prompt_exists(tmp_path,monkeypatch):
    def queued(case):
        client,db,child,sid=case['client'],case['db'],case['child'],case['daily_sid']
        source=case['daily_published']
        r=client.post(f"/api/parent/children/{child}/curriculum-supply/{source['candidate_id']}/withdraw",
            json=dict(action_id='withdraw-before-start',expected_version=source['version'],note='合成的未开始题源撤回。'), headers=parent_headers(case))
        assert r.status_code==200,r.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case['daily_task']['plan_item_id'],)).fetchone()[0]=='cancelled'
            state=c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?',(case['daily_turn']['agent_turn_id'],)).fetchone()[0]
            assert state=='cancelled',state
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==0
        assert client.post('/v1/agent-turns/'+case['daily_turn']['agent_turn_id']+'/execute',json={}).status_code==409
        gaps=[g for g in client.get(f'/v1/sessions/{sid}').json()['supply_obligations'] if g.get('root_obligation_id')==case['daily_task']['source_ref']]
        assert len(gaps)==1 and gaps[0]['status']=='missing_source',gaps
    daily(tmp_path,monkeypatch,after_authorized=queued)
