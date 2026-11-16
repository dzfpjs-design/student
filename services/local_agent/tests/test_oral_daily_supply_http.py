"""Published material turns an existing daily-check gap into actual voice practice."""
import json
import pytest
from synthetic_oral_app_lifecycle import fresh_oral_case, parent_headers

from test_oral_answer_daily_followup_http import test_next_day_check_uses_distinct_published_question_not_model_prompt as daily
from test_oral_supply_cross_day_source import publish
from test_voice_runtime_binding import capture,interpret


def test_daily_supply_publication_voice_answer_feedback_and_restart(tmp_path,monkeypatch,fault=None,before_publication=None,before_resume=None):
    def supply(case):
        client,db,child=case['client'],case['db'],case['child']
        sid,workspace=case['missing_sid'],case['missing_workspace']
        root=next(iter(case['missing_refs']))
        def post(path,body, *, headers=None):
            r=client.post(path,json=body,headers=headers)
            assert r.status_code in (200,201),r.text
            return r.json()
        gaps=[r for r in workspace['supply_obligations'] if r.get('root_obligation_id')==root]
        assert len(gaps)==1 and gaps[0]['status']=='missing_source',workspace['supply_obligations']
        for task in workspace['plan_items']:
            if task['status'] in ('pending','paused'):
                post('/v1/plan-items/'+task['plan_item_id']+'/reschedule',dict(reason='先完成刚补齐的复查题。',
                    client_action_id='daily-supplied-defer-'+task['plan_item_id']))
        post(f'/v1/sessions/{sid}/transition',dict(expected_state='plan_review',target_state='study',
            reason='继续安排中的复查。',client_action_id='daily-supply-study'))
        current={**case,'sid':sid,'root':root}
        if before_publication:
            before_publication(current)
        source=publish(case,'数学',excerpt_index=2,key='daily-later-publish')
        refreshed=client.get(f'/v1/sessions/{sid}').json()
        ready=[r for r in refreshed['supply_obligations'] if r.get('root_obligation_id')==root]
        assert len(ready)==1 and ready[0]['status']=='ready',ready
        assert not any(r['source_ref']==root for r in refreshed['plan_items'])
        quote=before_resume({**current,'source':source}) if before_resume else '继续今天还没安排好的数学口述复查。'
        case['command_state'].update(control='task.resume',quote=quote,
            selector={'subject':None if before_resume else 'math','task_kind':None if before_resume else 'oral','relative_order':None})
        voice=capture(client,case['settings'],monkeypatch,sid,quote,key='daily-supply-voice',child=child)
        turn=interpret(client,sid,quote,'daily-supply-intent',voice)
        assert turn.get('runtime_capability')=='task.resume',turn
        if fault in ('source_changed','cancelled'):
            with db.connect() as c:
                if fault=='source_changed':
                    c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(source['diagnostic_item_id'],))
                else:
                    c.execute("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=?",(root,))
                c.commit()
            rejected=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',json={})
            assert rejected.status_code==409,rejected.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==0
                assert c.execute('SELECT COUNT(*) FROM plan_item WHERE source_ref=?',(root,)).fetchone()[0]==0
            if fault=='cancelled':
                states=[r for r in client.get(f'/v1/sessions/{sid}').json()['supply_obligations'] if r.get('root_obligation_id')==root]
                assert len(states)==1 and states[0]['status']=='cancelled',states
            return
        if fault=='write_failure':
            names=('daily_plan','plan_item','plan_constraint_decision','diagnostic_prompt','diagnostic_attempt','agent_objective','agent_commitment')
            with db.connect() as c:
                before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in names}
                c.execute("""CREATE TRIGGER reject_supply_daily_receipt BEFORE UPDATE ON agent_plan
                    WHEN json_type(NEW.context_manifest_json,'$.oral_daily_check_receipt')='object'
                    BEGIN SELECT RAISE(ABORT,'synthetic daily supply write failure'); END""")
                c.commit()
            rejected=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',json={})
            assert rejected.status_code>=400,rejected.text
            with db.connect() as c:
                assert {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in names}==before
                c.execute('DROP TRIGGER reject_supply_daily_receipt')
                c.commit()
        result=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})['execution']['result']
        prompt=result['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id']==source['diagnostic_item_id'] and prompt['origin_session_id']==sid
        replay=post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})['execution']['result']
        assert replay['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
        from test_writing_question_authorization import LICENSE
        path=f"/v1/parent/children/{child}/writing-question-authorizations/{prompt['diagnostic_item_id']}"
        auth=client.get(path,headers=parent_headers(case)).json()
        post(path+'/grant',{**{k:auth[k] for k in ('item_version','source_version','curriculum_version')},
            'expected_revision':auth['revision'],'client_action_id':'daily-supply-auth','license_basis':LICENSE},
            headers=parent_headers(case))
        quote='判别式是9加16等于25，大于零，所以有两个不相等的实数根。'
        case['command_state'].update(control='answer',quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,key='daily-supply-answer-voice',child=child)
        answer=interpret(client,sid,quote,'daily-supply-answer-intent',voice)
        saved=post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',dict(prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'],answer=quote,client_action_id='daily-supply-answer',authorized_turn_id=answer['agent_turn_id']))
        assert saved['attempt']['correctness'] is None
        evaluation=post('/v1/agent-turns/'+saved['local_learning_evaluation']['feedback_turn_id']+'/execute',{})
        assert evaluation['execution']['result']['formative_only'] is True
        quote='已经核对反馈了，结束这项复查。'
        case['command_state'].update(control='task.complete',quote=quote)
        voice=capture(client,case['settings'],monkeypatch,sid,quote,prompt,key='daily-supply-complete-voice',child=child)
        finish=interpret(client,sid,quote,'daily-supply-complete-intent',voice)
        post('/v1/agent-turns/'+finish['agent_turn_id']+'/execute',{})
        from fastapi.testclient import TestClient
        with fresh_oral_case(case,TestClient,client=('127.0.0.1',42004)) as reopened_case:
            reopened=reopened_case['client']
            restored=reopened.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
            assert restored.status_code==200 and restored.json()['status']=='answered',restored.text
            assert restored.json()['attempt']['attempt_id']==saved['attempt']['attempt_id']
            states=[r for r in reopened.get(f'/v1/sessions/{sid}').json()['supply_obligations'] if r.get('root_obligation_id')==root]
            assert len(states)==1 and states[0]['status']=='practice_completed',states
        with db.connect() as c:
            assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(root,)).fetchone()[0]=='completed'
            assert c.execute('SELECT COUNT(*) FROM plan_item WHERE source_ref=?',(root,)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(sid,)).fetchone()[0]==1
        outgoing=json.dumps(case['posts'],ensure_ascii=False)
        assert root not in outgoing and 'oral_daily_binding' not in outgoing
    daily(tmp_path,monkeypatch,fault='missing_source',after_missing=supply)


@pytest.mark.parametrize('fault',['source_changed','write_failure','cancelled'])
def test_daily_supply_revalidates_and_rolls_back(tmp_path,monkeypatch,fault):
    test_daily_supply_publication_voice_answer_feedback_and_restart(tmp_path,monkeypatch,fault)
