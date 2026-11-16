"""Real stopped/restarted app recovery, never manufacturing a task pause."""
import json

import pytest

from home_study_agent.repository import StudyRepository
from home_study_agent.model_gateway import ModelResult
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('before_start',[False,True],ids=['started-system-pause','prepared-timer-failure'])
def test_exact_school_review_voice_resumes_after_app_restart(school_case,monkeypatch,before_start):
    answer_and_review(school_case)
    db,client,_,_,_,_,_,_,settings=school_case
    if before_start:
        with monkeypatch.context() as fault:
            def fail(*a,**k):
                raise RuntimeError('isolated timer write failure')
            fault.setattr(StudyRepository,'start_plan_item',fail)
            with pytest.raises(RuntimeError,match='isolated timer write failure'):
                next_day(school_case,monkeypatch,key='recovery')
        with db.connect() as c:
            p=dict(c.execute("SELECT * FROM diagnostic_prompt WHERE child_id=? AND status='waiting_answer'",(CHILD,)).fetchone())
            sid=p['origin_session_id']
            slot=dict(c.execute('''SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.session_id=? AND source_type='due_review' AND source_ref=?''',(sid,p['review_task_id'])).fetchone())
    else:
        case,slot,p,_,_=next_day(school_case,monkeypatch,key='recovery')
        sid=case[3]
    gateway=client.school_gateway
    async def resume(request):
        gateway.requests.append(request)
        assert request.request_type=='voice_interaction'
        payload=json.loads(request.messages[-1]['content'])
        return ModelResult(content=json.dumps({'role':'control','control':'task.resume',
            'quote':payload['transcript'],'confidence':.99,
            'selector':{'subject':None,'task_kind':None,'relative_order':None}}),
            provider='synthetic',model='synthetic-control',latency_ms=1)
    monkeypatch.setattr(gateway,'complete',resume)
    client=client.school_reopen()
    read=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':p['prompt_id']})
    assert read.status_code==200 and read.json()['status']==('awaiting_task_start' if before_start else 'waiting_answer'),read.text
    if before_start:
        rejected=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={
            'prompt_id':p['prompt_id'],'item_version':p['item_version'],'answer':'before start',
            'client_action_id':'prestart-answer'})
        assert rejected.status_code==200 and rejected.json()['attempt'] is None,rejected.text
    quote='可以继续刚才的复习任务吗？'
    voice=capture(client,settings,monkeypatch,sid,quote,p,key='recover-voice',child=CHILD)
    turn=interpret(client,sid,quote,'recover-command',voice)
    assert turn['runtime_capability']=='task.resume',turn
    response=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert response.status_code==200,response.text
    result=response.json()['execution']['result']
    assert result['status']=='active' and result['plan_item_id']==slot['plan_item_id'],result
    assert result['diagnostic_prompt']['prompt']['prompt_id']==p['prompt_id']
    with db.connect() as c:
        events=[tuple(r) for r in c.execute('SELECT event_type,event_id FROM plan_item_event WHERE plan_item_id=? ORDER BY rowid',(slot['plan_item_id'],))]
        assert sum(kind=='started' for kind,_ in events)==1
        assert sum(kind=='resumed' for kind,_ in events)==(0 if before_start else 1)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?',(p['review_task_id'],)).fetchone()[0]==0
    same=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert same.status_code==200,same.text
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT event_type,event_id FROM plan_item_event WHERE plan_item_id=? ORDER BY rowid',(slot['plan_item_id'],))]==events
