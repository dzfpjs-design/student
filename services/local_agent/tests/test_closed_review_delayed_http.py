"""Advance the test clock, never rewrite due dates or manufacture review results."""
from datetime import datetime, timedelta
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from test_closed_review_voice_http import flow, review_clock, settings_for, CHILD
from test_natural_task_navigation_http import Offline, hear, execute
from test_voice_runtime_binding import submit
from test_cloud_first_agent import Planner


class CompletionGateway(Offline):
    configured=False
    quote='做完了并且检查过了'

    async def complete(self,request):
        if request.request_type=='agent_plan':
            return await Planner('task.complete',{},self.quote).complete(request)
        assert request.request_type=='voice_interaction'
        quote=json.loads(request.messages[-1]['content'])['transcript']
        assert quote in {'做完了并且检查过了','刚才问的每一项我都完成并检查了'}
        return ModelResult(content=json.dumps({'role':'control','control':'task.complete',
            'quote':quote,'confidence':.99}),provider='synthetic',model='synthetic-control',latency_ms=1)


def test_real_scheduled_48h_and_7d_reviews_are_executed_not_merely_created(flow,monkeypatch):
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    original_day=datetime.fromisoformat(flow['clock'][0])-timedelta(days=1)
    completed={}
    for day in (1,2,3,7):
        if day!=1:
            flow['clock'][0]=(original_day+timedelta(days=day,hours=1)).isoformat()
            sid=flow['repo'].start_session(CHILD,str(uuid4()))['session_id']
            with flow['db'].connect() as c:
                c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?",(sid,))
                c.commit()
            flow['repo'].generate_plan(sid,str(uuid4()))
            with flow['db'].connect() as c:
                c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
                c.commit()
        else:
            sid=flow['session']
        gateway=CompletionGateway()
        with TestClient(create_app(settings,model_gateway=gateway)) as client:
            start,_=hear(client,settings,monkeypatch,sid,'开始下一项',f'day-{day}-start')
            prompt=execute(client,start)['execution']['result']['diagnostic_prompt']['prompt']
            with flow['db'].connect() as c:
                assert c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(start['agent_turn_id'],)).fetchone()[0] is None
                source=dict(c.execute('SELECT * FROM review_task WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone())
                assert datetime.fromisoformat(source['due_at'])<=datetime.fromisoformat(flow['clock'][0])
            assert prompt['diagnostic_item_id']=='diag-p4-arithmetic-closed-2'
            answer,_=hear(client,settings,monkeypatch,sid,'我的答案是42',f'day-{day}-answer',prompt=prompt)
            response=submit(client,sid,prompt,answer,'42',f'day-{day}-submission')
            assert response.status_code==200,response.text
            receipt=response.json()
            assert receipt['status']=='answered' and receipt['attempt']['correctness']==1,receipt
            gateway.configured=True
            done,_=hear(client,settings,monkeypatch,sid,'做完了并且检查过了',f'day-{day}-finish',cloud=True)
            result=execute(client,done)
            if result['execution']['result']['status']=='awaiting_completion_check':
                gateway.quote='刚才问的每一项我都完成并检查了'
                done,_=hear(client,settings,monkeypatch,sid,'刚才问的每一项我都完成并检查了',f'day-{day}-confirm',cloud=True)
                result=execute(client,done)
            assert result['execution']['result']['status']=='completed',result
            completed[source['interval_label']]=receipt['attempt']['evidence_id']
            transition=client.post(f'/v1/sessions/{sid}/transition',json={'expected_state':'study',
                'target_state':'reflection','reason':'Synthetic day closure','client_action_id':f'day-{day}-reflection'})
            assert transition.status_code==200,transition.text
            closed=client.post(f'/v1/sessions/{sid}/reflection/complete',json={
                'learned_text':'','difficult_text':'','client_action_id':f'day-{day}-close'})
            assert closed.status_code==200,closed.text
    assert {'48h','7d'}<=completed.keys(),completed
    with flow['db'].connect() as c:
        for label in ('48h','7d'):
            row=c.execute('SELECT status,result_evidence_id FROM review_task WHERE source_evidence_id=? AND interval_label=?',
                (flow['original']['evidence_id'],label)).fetchone()
            assert tuple(row)==('completed',completed[label])
            evidence=c.execute('SELECT independent,confidence,correctness FROM learning_evidence WHERE evidence_id=?',(completed[label],)).fetchone()
            assert tuple(evidence)==(1,1.0,1.0)
