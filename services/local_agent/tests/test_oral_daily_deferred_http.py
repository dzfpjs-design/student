"""A delivered daily recheck remains the same question after a deferred day."""
from datetime import datetime,timedelta
import json
from synthetic_oral_app_lifecycle import fresh_oral_case
import pytest

from home_study_agent.learning_state import LearningStateService
from test_oral_answer_daily_followup_http import test_next_day_check_uses_distinct_published_question_not_model_prompt as daily
from test_voice_runtime_binding import capture,interpret
from test_writing_stage_review_consumers import advance_clock


def test_daily_check_deferred_then_same_question_answer_feedback_and_completion(tmp_path,monkeypatch,fault=None):
    def defer(case):
        client,db,child=case['client'],case['db'],case['child']
        sid,task,prompt=case['daily_sid'],case['daily_task'],case['daily_prompt']
        root=task['source_ref']
        def post(path,body):
            r=client.post(path,json=body)
            assert r.status_code in (200,201),r.text
            return r.json()
        for control,quote in [('task.pause','先暂停这道题。'),('task.reschedule','这道复查题明天接着回答。')]:
            case['command_state'].update(control=control,quote=quote)
            voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,key='daily-defer-'+control,child=child)
            turn=interpret(client,sid,quote,'daily-defer-intent-'+control,voice)
            assert turn.get('runtime_capability')==control,turn
            post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})
        post(f'/v1/sessions/{sid}/transition',dict(expected_state='study',target_state='reflection',
            reason='今天时间到了。',client_action_id='daily-defer-reflect'))
        post(f'/v1/sessions/{sid}/reflection/complete',dict(learned_text='',difficult_text='',client_action_id='daily-defer-close'))
        summary=LearningStateService(db).close_session_learning(sid)
        kept=[r for r in summary['remediation'] if r['source_type']=='oral_delivered_continuation' and r['source_ref']==root]
        assert len(kept)==1,summary['remediation']
        with db.connect() as c:
            old=dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone())
            ended=c.execute('SELECT ended_at FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0]
        advance_clock(monkeypatch,datetime.fromisoformat(ended)+timedelta(days=1))
        client.headers['X-HSC-Parent']=post('/v1/parent/unlock',{'pin':'8264'})['parent_token']
        sid2=post('/v1/sessions',dict(child_id=child,client_action_id='daily-resume-day'))['session_id']
        assert client.put(f'/v1/sessions/{sid2}/planning-context',json=dict(energy_level='medium',fixed_commitment_minutes=0,
            client_action_id='daily-resume-context')).status_code==200
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?",(sid2,))
            c.commit()
        workspace=post(f'/v1/sessions/{sid2}/plan/generate',dict(client_action_id='daily-resume-plan'))
        kept=[r for r in workspace['supply_obligations'] if r.get('root_obligation_id')==root]
        assert len(kept)==1 and kept[0]['status']=='needs_plan',kept
        assert not any(r['source_ref']==root for r in workspace['plan_items'])
        post(f'/v1/sessions/{sid2}/transition',dict(expected_state='plan_review',target_state='study',
            reason='继续上次的题。',client_action_id='daily-resume-study'))
        quote='继续昨天没回答完的数学口述题。'
        case['command_state'].update(control='task.resume',quote=quote,
            selector={'subject':'math','task_kind':'oral','relative_order':None})
        voice=capture(client,case['settings'],monkeypatch,sid2,quote,key='daily-resume-voice',child=child)
        turn=interpret(client,sid2,quote,'daily-resume-intent',voice)
        assert turn.get('runtime_capability')=='task.resume',turn
        if fault in ('source_changed','previous_answer'):
            with db.connect() as c:
                if fault=='source_changed':
                    c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(prompt['diagnostic_item_id'],))
                else:
                    c.execute("UPDATE diagnostic_prompt SET status='answer_saved' WHERE prompt_id=?",(prompt['prompt_id'],))
                c.commit()
            rejected=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',json={})
            assert rejected.status_code==409,rejected.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid2,)).fetchone()[0]==0
                assert c.execute('SELECT COUNT(*) FROM plan_item WHERE source_ref=? AND plan_id=?',(root,workspace['plan']['plan_id'])).fetchone()[0]==0
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(root,)).fetchone()[0]!='completed'
            return
        result=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})['execution']['result']
        current=result['diagnostic_prompt']['prompt']
        assert current['prompt_id']!=prompt['prompt_id'] and current['origin_session_id']==sid2
        assert current['diagnostic_item_id']==prompt['diagnostic_item_id'] and current['item_version']==prompt['item_version']
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone())==old
            assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(root,)).fetchone()[0]!='completed'
        from test_writing_question_authorization import LICENSE
        path=f"/v1/parent/children/{child}/writing-question-authorizations/{current['diagnostic_item_id']}"
        auth=client.get(path).json()
        post(path+'/grant',{**{k:auth[k] for k in ('item_version','source_version','curriculum_version')},
            'expected_revision':auth['revision'],'client_action_id':'daily-resume-auth','license_basis':LICENSE})
        quote='判别式是9加16等于25，大于零，所以有两个不相等的实数根。'
        case['command_state'].update(control='answer',quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid2,quote,current,key='daily-resume-answer-voice',child=child)
        answer=interpret(client,sid2,quote,'daily-resume-answer-intent',voice)
        saved=post(f'/v1/sessions/{sid2}/diagnostic-prompt/answer',dict(prompt_id=current['prompt_id'],
            item_version=current['item_version'],answer=quote,client_action_id='daily-resume-answer',authorized_turn_id=answer['agent_turn_id']))
        assert saved['attempt']['correctness'] is None
        feedback=saved['local_learning_evaluation']
        if fault=='before_feedback':
            quote='我先结束这项复查。'
            case['command_state'].update(control='task.complete',quote=quote)
            voice=capture(client,case['settings'],monkeypatch,sid2,quote,current,key='continued-premature-voice',child=child)
            early=interpret(client,sid2,quote,'continued-premature-intent',voice)
            rejected=client.post('/v1/agent-turns/'+early['agent_turn_id']+'/execute',json={})
            assert rejected.status_code==409,rejected.text
            assert rejected.json()['detail']['code']=='ORAL_DAILY_CHECK_FEEDBACK_REQUIRED'
        evaluated=post('/v1/agent-turns/'+feedback['feedback_turn_id']+'/execute',{})
        assert evaluated['execution']['result']['formative_only'] is True
        quote='已经回答并核对反馈了，结束这项复查。'
        case['command_state'].update(control='task.complete',quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid2,quote,current,key='daily-resume-complete-voice',child=child)
        finish=interpret(client,sid2,quote,'daily-resume-complete-intent',voice)
        post('/v1/agent-turns/'+finish['agent_turn_id']+'/execute',{})
        from home_study_agent.child_model import ChildModelService
        knowledge=ChildModelService(db).snapshot(child)['payload']['learning']['reliable_feedback']
        assert not knowledge['mastery_claim_allowed']
        assert all(not n['mastery_claim_allowed'] and n['independent_correct']==0 and n['retention_success_count']==0
            for n in knowledge['nodes'])
        from fastapi.testclient import TestClient
        from home_study_agent.app import create_app
        with fresh_oral_case({**case,'client_options':{}},TestClient,client=('127.0.0.1',42003)) as reopened_case:
            reopened=reopened_case['client']
            restored=reopened.get(f'/v1/sessions/{sid2}/diagnostic-prompt',params={'prompt_id':current['prompt_id']})
            assert restored.status_code==200 and restored.json()['status']=='answered',restored.text
            assert restored.json()['attempt']['attempt_id']==saved['attempt']['attempt_id']
            obligations=reopened.get(f'/v1/sessions/{sid2}').json()['supply_obligations']
            retained=[r for r in obligations if r.get('root_obligation_id')==root]
            assert len(retained)==1 and retained[0]['status']=='practice_completed',retained
            from home_study_agent.database import Database
            assert ChildModelService(Database(db.path)).snapshot(child)['payload']['learning']['reliable_feedback']==knowledge
        with db.connect() as c:
            assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(root,)).fetchone()[0]=='completed'
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(result['plan_item_id'],)).fetchone()[0]=='completed'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(child,)).fetchone()[0]==3
        outgoing=json.dumps(case['posts'],ensure_ascii=False)
        assert root not in outgoing and prompt['prompt_id'] not in outgoing
    daily(tmp_path,monkeypatch,after_start=defer)


@pytest.mark.parametrize('fault',['source_changed','previous_answer','before_feedback'])
def test_deferred_daily_check_revalidates_before_execution(tmp_path,monkeypatch,fault):
    test_daily_check_deferred_then_same_question_answer_feedback_and_completion(tmp_path,monkeypatch,fault)
