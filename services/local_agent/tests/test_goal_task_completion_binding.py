"""A planned skill check cannot be completed by a different answer receipt."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.learning_state import LearningStateService
from home_study_agent.database import Database
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item


@pytest.fixture
def started(execution):
    db,sid,pid,node,exact = execution
    settings=Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        with db.connect() as c:
            others=c.execute('SELECT plan_item_id FROM plan_item WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?) AND plan_item_id!=?',(pid,pid)).fetchall()
        for item in others:
            response=client.post(f'/v1/plan-items/{item[0]}/reschedule',json={
                'reason':'本次先检查计划目标，其他任务另行安排','client_action_id':'defer-'+item[0]})
            assert response.status_code==200,response.text
        response=client.post(f'/v1/sessions/{sid}/transition',json=dict(expected_state='plan_review',target_state='study',reason='start',client_action_id='completion-study'))
        assert response.status_code==200,response.text
        turn=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':'开始任务','client_action_id':'start-goal'}).json()
        response=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code==200,response.text
        prompt=response.json()['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id']==exact[0]
        yield db,client,sid,pid,exact,prompt


def complete(client,pid):
    return client.post(f'/v1/plan-items/{pid}/complete',json={
        'actual_minutes':1,'completion_evidence_type':'voice_confirmed',
        'completion_note':'本次检查已完成，不代表掌握','client_action_id':'complete-goal'})


def test_unbound_same_item_answer_cannot_complete_planned_prompt(started):
    db,client,sid,pid,exact,prompt=started
    LearningStateService(db).submit_diagnostic(sid,exact[0],exact[1],'other-attempt')
    result=complete(client,pid)
    assert result.status_code==422,result.text
    assert result.json()['code']=='PLAN_INPUT_REQUIRED'
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0]=='active'
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()[0]=='waiting_answer'


@pytest.mark.parametrize('fault',[None,'withdrawn','changed_prompt','cancelled_command'])
def test_bound_wrong_answer_completes_execution_only_while_source_current(started,fault):
    db,client,sid,pid,exact,prompt=started
    response=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={
        'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
        'answer':'-9999','client_action_id':'actual-bound-answer'})
    assert response.status_code==200,response.text
    assert response.json()['attempt']['correctness']==0
    attempt=response.json()['attempt']['attempt_id']
    if fault:
        with db.connect() as c:
            if fault=='withdrawn':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(exact[0],))
            elif fault=='changed_prompt':
                c.execute("UPDATE diagnostic_prompt SET item_version='changed' WHERE prompt_id=?",(prompt['prompt_id'],))
            else:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE session_id=?",(sid,))
            c.commit()
    result=complete(client,pid)
    assert result.status_code==(422 if fault else 200),result.text
    if fault:
        assert result.json()['code']=='PLAN_INPUT_REQUIRED'
    with db.connect() as c:
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',(attempt,)).fetchone()[0]==0
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0]==('active' if fault else 'completed')
    if not fault:
        assert complete(client,pid).status_code==200


def test_voice_completion_and_wrong_answer_survive_app_restart(started):
    db,client,sid,pid,exact,prompt=started
    answer=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json={
        'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
        'answer':'-9999','client_action_id':'voice-finish-original'})
    assert answer.status_code==200,answer.text
    spoken=client.post(f'/v1/sessions/{sid}/agent-turn',json={
        'transcript':'这项做完了','client_action_id':'voice-finish-request'})
    assert spoken.status_code==200,spoken.text
    turn=spoken.json()
    assert turn['action']['type']=='complete_active_task',spoken.text
    completed=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert completed.status_code==200,completed.text
    assert completed.json()['execution']['result']['status']=='completed'
    settings=Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    client.__exit__(None, None, None)
    with TestClient(create_app(settings)) as restarted:
        replay=restarted.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert replay.status_code==200,replay.text
    with Database(db.path).connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0]=='completed'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(pid,)).fetchone()[0]==1
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',
            (answer.json()['attempt']['attempt_id'],)).fetchone()[0]==0
        assert c.execute('SELECT 1 FROM review_task WHERE source_evidence_id=?',
            (answer.json()['attempt']['evidence_id'],)).fetchone()
