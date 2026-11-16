"""A finished oral practice cannot erase an unverified learning responsibility."""
from datetime import datetime,timedelta
import json
from synthetic_oral_app_lifecycle import fresh_oral_case, parent_headers
import pytest

from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent import oral_daily_check
from home_study_agent.learning_state import LearningStateService
from test_oral_delivery_resume_http import test_third_day_voice_continuation_delivers_same_question_once as continued
from test_oral_supply_cross_day_execute import test_new_oral_answer_gets_current_goal_feedback_and_persists as answered
from test_oral_supply_cross_day_source import publish
from test_voice_runtime_binding import capture,interpret
from test_writing_stage_review_consumers import advance_clock


def test_completed_continued_answer_survives_daily_closure(tmp_path,monkeypatch,after_closure=None):
    def close(case):
        client,db,sid=case['client'],case['db'],case['next_sid']
        def post(path,body, *, headers=None):
            r=client.post(path,json=body,headers=headers)
            assert r.status_code in (200,201),r.text
            return r.json()
        for item in client.get(f'/v1/sessions/{sid}').json()['plan_items']:
            if item['status'] in ('pending','paused'):
                post('/v1/plan-items/'+item['plan_item_id']+'/reschedule',dict(
                    reason='今天时间到了，其他任务留待下次。',client_action_id='answered-defer-'+item['plan_item_id']))
        post(f'/v1/sessions/{sid}/transition',dict(expected_state='study',target_state='reflection',
            reason='今天学习到这里。',client_action_id='answered-close-reflect'))
        post(f'/v1/sessions/{sid}/reflection/complete',dict(learned_text='',difficult_text='',client_action_id='answered-close'))
        summary=LearningStateService(db).close_session_learning(sid)
        evidence=case['new_answer']['attempt']['evidence_id']
        assert any(r['source']['source_id']==evidence for r in summary['needs_verification']),summary.keys()
        with db.connect() as c:
            goal=c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',
                (case['continuation_turn']['agent_turn_id'],)).fetchone()[0]
        followups=[r for r in summary['remediation'] if r.get('objective_id')==goal or r.get('source_evidence_id')==evidence]
        assert followups,summary['remediation']
        assert all(r.get('mastery_update_allowed') is False for r in followups)
        if after_closure:
            after_closure({**case,'followups':followups,'closed_goal':goal})
    def answer(case):
        answered(tmp_path,monkeypatch,delivered_case=case,after_completion=close)
    continued(tmp_path,monkeypatch,after_start=answer)


def test_next_day_check_uses_distinct_published_question_not_model_prompt(tmp_path,monkeypatch,fault=None,after_start=None,after_missing=None,after_authorized=None):
    importer=KnowledgeBaseService.import_source
    third={}
    def import_three(service,child,values):
        if values.get('source_ref')=='isolated-spoken-source':
            rows=[json.loads(line) for line in values['content_text'].splitlines()]
            third.update({**rows[-1],'prompt':'不求出具体根，口头说明 2x²+3x-2=0 的实数根情况与判断步骤。'})
            values={**values,'content_text':values['content_text']+'\n'+json.dumps(third,ensure_ascii=False)}
        return importer(service,child,values)
    monkeypatch.setattr(KnowledgeBaseService,'import_source',import_three)
    def next_day(case):
        client,db,child=case['client'],case['db'],case['child']
        def post(path,body, *, headers=None):
            r=client.post(path,json=body,headers=headers)
            assert r.status_code in (200,201),r.text
            return r.json()
        with db.connect() as c:
            ended=c.execute('SELECT ended_at FROM daily_session WHERE session_id=?',(case['next_sid'],)).fetchone()[0]
        advance_clock(monkeypatch,datetime.fromisoformat(ended)+timedelta(days=2))
        case['headers']['X-HSC-Parent']=post('/v1/parent/unlock',{'pin':'8264'})['parent_token']
        new=publish(case,'数学',excerpt_index=2,key='delayed-third-source') if fault!='missing_source' else None
        sid=post('/v1/sessions',dict(child_id=child,client_action_id='answered-next-day'))['session_id']
        with db.connect() as c:
            c.execute('BEGIN')
            proposed=oral_daily_check.planning(c,child,sid,case['followups'][0]['source_ref'])
            assert proposed and proposed['status']==('needs_supply' if fault=='missing_source' else 'ready'),proposed
        assert client.put(f'/v1/sessions/{sid}/planning-context',json=dict(energy_level='medium',fixed_commitment_minutes=0,
            client_action_id='answered-next-context')).status_code==200
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?",(sid,))
            c.commit()
        workspace=post(f'/v1/sessions/{sid}/plan/generate',dict(client_action_id='answered-next-plan'))
        refs={r['source_ref'] for r in case['followups']}
        tasks=[r for r in workspace['plan_items'] if r['source_ref'] in refs]
        if fault=='missing_source':
            assert not tasks
            assert any(r['obligation_type']=='oral_daily_check' and r['decision']=='deferred' and r['source_ref'] in refs
                for r in workspace['constraint_decisions'])
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==0
            if after_missing:
                after_missing({**case,'missing_sid':sid,'missing_workspace':workspace,'missing_refs':refs})
            return
        assert len(tasks)==1,[(r['source_type'],r['source_ref']) for r in workspace['plan_items']]
        task=tasks[0]
        for other in workspace['plan_items']:
            if other['plan_item_id']!=task['plan_item_id']:
                post('/v1/plan-items/'+other['plan_item_id']+'/reschedule',dict(reason='先完成约定的检查。',
                    client_action_id='check-defer-'+other['plan_item_id']))
        post(f'/v1/sessions/{sid}/transition',dict(expected_state='plan_review',target_state='study',
            reason='我来检查一下。',client_action_id='delayed-check-study'))
        quote='开始今天安排的数学复习'
        start_selector={'subject':'math','task_kind':'review','source_type':None,'target_quote':quote}
        case['command_state'].update(control='task.start',quote=quote,start_selector=start_selector)
        from test_cloud_first_agent import Planner
        original_complete=case['gateway'].complete
        async def complete(request):
            if request.request_type=='agent_plan':
                return await Planner('task.start',{'start_selector':start_selector},quote).complete(request)
            return await original_complete(request)
        monkeypatch.setattr(case['gateway'],'complete',complete)
        voice=capture(client,case['settings'],monkeypatch,sid,quote,key='delayed-check-voice',child=child)
        turn=interpret(client,sid,quote,'delayed-check-intent',voice)
        assert turn.get('runtime_capability')=='task.start',turn
        if after_authorized:
            after_authorized({**case,'daily_sid':sid,'daily_task':task,'daily_turn':turn,'daily_published':new})
            return
        if fault in ('receipt_write_failure','retry_after_new_voice','retry_after_denial'):
            tables=('plan_item','daily_plan','diagnostic_prompt','diagnostic_attempt','agent_objective',
                'agent_commitment','plan_constraint_decision')
            with db.connect() as c:
                before={name:[tuple(r) for r in c.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in tables}
                c.execute("""CREATE TRIGGER reject_daily_receipt BEFORE UPDATE ON agent_plan
                    WHEN json_type(NEW.context_manifest_json,'$.oral_daily_check_receipt')='object'
                    BEGIN SELECT RAISE(ABORT,'synthetic daily receipt failure'); END""")
                c.commit()
            rejected=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',json={})
            assert rejected.status_code>=400,rejected.text
            with db.connect() as c:
                assert {name:[tuple(r) for r in c.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in tables}==before
                c.execute('DROP TRIGGER reject_daily_receipt')
                c.commit()
            if fault in ('retry_after_new_voice','retry_after_denial'):
                if fault=='retry_after_new_voice':
                    newer='先别开始，我想休息一下。'
                    capture(client,case['settings'],monkeypatch,sid,newer,key='daily-retry-newer-voice',child=child)
                else:
                    with db.connect() as c:
                        c.execute("UPDATE agent_step SET error_code='InvalidAgentPlanError' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(turn['agent_turn_id'],))
                        c.execute("UPDATE agent_turn SET tool_error_code='InvalidAgentPlanError' WHERE agent_turn_id=?",(turn['agent_turn_id'],))
                        c.commit()
                rejected=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',json={})
                assert rejected.status_code==409,rejected.text
                with db.connect() as c:
                    assert {name:[tuple(r) for r in c.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in tables}==before
                return
            with db.connect() as c:
                c.execute('BEGIN')
                oral_daily_check.recover_request(c,turn['agent_turn_id'])
                c.rollback()
        if fault in ('withdraw_question','changed_original'):
            with db.connect() as c:
                if fault=='withdraw_question':
                    c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(new['diagnostic_item_id'],))
                else:
                    c.execute("UPDATE diagnostic_attempt SET submitted_answer='changed original' WHERE attempt_id=?",
                        (case['new_answer']['attempt']['attempt_id'],))
                c.commit()
            rejected=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',json={})
            assert rejected.status_code==409,rejected.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==0
                assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?',(task['plan_item_id'],)).fetchone()[0] is None
            return
        started=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})['execution']
        prompt=started['result'].get('diagnostic_prompt',{}).get('prompt')
        assert prompt and prompt['diagnostic_item_id']==new['diagnostic_item_id'],started
        assert prompt['origin_session_id']==sid and prompt['prompt_id']!=case['new_prompt']['prompt_id']
        if fault in ('missing_receipt','forged_receipt'):
            with db.connect() as c:
                if fault=='missing_receipt':
                    c.execute("UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.oral_daily_check_receipt') WHERE agent_turn_id=?",(turn['agent_turn_id'],))
                else:
                    c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.oral_daily_check_receipt.root_commitment_id','forged-root') WHERE agent_turn_id=?",(turn['agent_turn_id'],))
                c.commit()
            invalid=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
            assert invalid.status_code==200 and invalid.json()['status']=='invalidated',invalid.text
            return
        replay=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})['execution']['result']
        assert replay['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
        if after_start:
            after_start({**case,'daily_sid':sid,'daily_task':task,'daily_prompt':prompt,'daily_turn':turn})
            return
        from test_writing_question_authorization import LICENSE
        path=f"/v1/parent/children/{child}/writing-question-authorizations/{prompt['diagnostic_item_id']}"
        auth=client.get(path, headers=parent_headers(case)).json()
        post(path+'/grant',{**{k:auth[k] for k in ('item_version','source_version','curriculum_version')},
            'expected_revision':auth['revision'],'client_action_id':'delayed-check-source-auth','license_basis':LICENSE}, headers=parent_headers(case))
        response='a等于2，b等于3，c等于负2，判别式是9加16等于25，大于零，所以有两个不相等的实数根。'
        case['command_state'].update(control='answer',quote=response)
        voice=capture(client,case['settings'],monkeypatch,sid,response,prompt,key='delayed-answer-voice',child=child)
        reply=interpret(client,sid,response,'delayed-answer-intent',voice)
        assert reply['action']['type']=='submit_diagnostic_answer',reply
        saved=post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',dict(prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'],answer=response,client_action_id='delayed-answer',authorized_turn_id=reply['agent_turn_id']))
        assert saved['attempt']['correctness'] is None and saved['attempt']['submitted_answer']==response
        if fault in ('before_feedback','refusal_error','refusal_voice'):
            quote='这个复查结束吧。'
            case['command_state'].update(control='task.complete',quote=quote)
            voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,key='premature-complete-voice',child=child)
            command=interpret(client,sid,quote,'premature-complete',voice)
            rejected=client.post('/v1/agent-turns/'+command['agent_turn_id']+'/execute',json={})
            assert rejected.status_code==409,rejected.text
            detail=rejected.json()['detail']
            assert detail['code']=='ORAL_DAILY_CHECK_FEEDBACK_REQUIRED'
            assert '回答已保存' in detail['message'] and '不用重新回答' in detail['message']
            with db.connect() as c:
                assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(task['plan_item_id'],)).fetchone()[0]=='active'
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(task['source_ref'],)).fetchone()[0]!='completed'
                if fault=='refusal_error':
                    c.execute("UPDATE agent_turn SET tool_error_code='OTHER_FAILURE' WHERE agent_turn_id=?",(command['agent_turn_id'],))
                elif fault=='refusal_voice':
                    c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_id','missing') WHERE agent_turn_id=?",(command['agent_turn_id'],))
                c.commit()
        feedback=saved['local_learning_evaluation']
        assert feedback['status']=='feedback_required',feedback
        if fault in ('refusal_error','refusal_voice'):
            rejected=client.post('/v1/agent-turns/'+feedback['feedback_turn_id']+'/execute',json={})
            assert rejected.status_code==409,rejected.text
            assert rejected.json()['code']=='LEARNING_EVALUATION_SOURCE_CHANGED'
            with db.connect() as c:
                assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',(saved['attempt']['attempt_id'],)).fetchone()[0]==response
            return
        evaluated=post('/v1/agent-turns/'+feedback['feedback_turn_id']+'/execute',{})
        assert evaluated['execution']['result']['formative_only'] is True
        assert case['evaluations'][-1]['question']==third['prompt'] and case['evaluations'][-1]['response']==response
        transmitted=json.dumps(case['posts'],ensure_ascii=False)
        assert 'oral_daily_check_receipt' not in transmitted and 'oral_daily_binding' not in transmitted
        assert task['source_ref'] not in transmitted and case['closed_goal'] not in transmitted
        quote='这项复习我回答完，也核对过反馈了，结束吧。'
        case['command_state'].update(control='task.complete',quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,key='delayed-complete-voice',child=child)
        finishing=interpret(client,sid,quote,'delayed-complete-intent',voice)
        assert finishing.get('runtime_capability')=='task.complete',finishing
        finished=post('/v1/agent-turns/'+finishing['agent_turn_id']+'/execute',{})
        assert finished['execution']['result']['status']=='completed',finished
        with db.connect() as c:
            assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(task['source_ref'],)).fetchone()[0]=='completed'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(child,)).fetchone()[0]==3
        from home_study_agent.child_model import ChildModelService
        feedback=ChildModelService(db).snapshot(child)['payload']['learning']['reliable_feedback']
        assert not feedback['mastery_claim_allowed'],feedback
        assert all(n['retention_success_count']==0 and n['independent_correct']==0
            and not n['mastery_claim_allowed'] for n in feedback['nodes']),feedback
        from fastapi.testclient import TestClient
        from home_study_agent.app import create_app
        with fresh_oral_case(case, TestClient, client=('127.0.0.1', 41996)) as fresh_case:
            case = fresh_case
            client = reopened = case['client']
            result=reopened.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
            assert result.status_code==200 and result.json()['status']=='answered',result.text
            assert result.json()['attempt']['attempt_id']==saved['attempt']['attempt_id']
            current=reopened.get(f'/v1/sessions/{sid}').json()
            assert next(r for r in current['plan_items'] if r['plan_item_id']==task['plan_item_id'])['status']=='completed'
            from home_study_agent.database import Database
            restored=ChildModelService(Database(db.path)).snapshot(child)['payload']['learning']['reliable_feedback']
            assert restored==feedback
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM agent_plan WHERE child_id=? AND session_id=? AND json_extract(context_manifest_json,\'$.oral_daily_check_receipt.plan_item_id\')=?',
                    (child,sid,task['plan_item_id'])).fetchone()[0]==1
    test_completed_continued_answer_survives_daily_closure(tmp_path,monkeypatch,after_closure=next_day)


@pytest.mark.parametrize('fault',['missing_source','withdraw_question','changed_original','missing_receipt',
    'forged_receipt','before_feedback','receipt_write_failure','retry_after_new_voice','retry_after_denial',
    'refusal_error','refusal_voice'])
def test_daily_oral_check_rejects_incomplete_or_changed_evidence(tmp_path,monkeypatch,fault):
    test_next_day_check_uses_distinct_published_question_not_model_prompt(tmp_path,monkeypatch,fault=fault)
