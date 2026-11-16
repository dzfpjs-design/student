"""Real read/withdraw contracts; reference scores never close an obligation."""
import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD


@pytest.mark.parametrize('school_case',[None,'physics'],indirect=True)
def test_real_review_get_waits_for_rubric_then_returns_exact_nullable_attempt(school_case,monkeypatch):
    answer_and_review(school_case)
    case,slot,p,_,_=next_day(school_case,monkeypatch,key='read-contract')
    db,client,headers,sid=case[:4]
    prompt,scored,_,path,body=answer_and_review(case,prompt=p,key='read-original',score=0,submit=False)
    url=f"/v1/plan-items/{slot['plan_item_id']}/review-question"
    waiting=client.get(url)
    assert waiting.status_code==200,waiting.text
    assert waiting.json()['review_status']=='pending' and waiting.json()['attempt'] is None
    current=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':p['prompt_id']})
    assert current.status_code==200 and current.json()['status']=='answered',current.text
    assert current.json()['attempt']['correctness'] is None
    assert client.post(path,headers=headers,json=body).status_code==200
    reviewed=client.get(url)
    assert reviewed.status_code==200,reviewed.text
    assert reviewed.json()['review_status']=='completed'
    assert reviewed.json()['attempt']['attempt_id']==scored['attempt']['attempt_id']
    assert reviewed.json()['attempt']['child_id']==CHILD and reviewed.json()['attempt']['session_id']==sid
    assert reviewed.json()['attempt']['correctness'] is None
    assert reviewed.json()['attempt']['review_task_id']==slot['source_ref']
    assert client.post(f"/v1/plan-items/{slot['plan_item_id']}/complete",json={
        'actual_minutes':1,'client_action_id':'read-completed'}).status_code==200
    retracted=client.post(path,headers=headers,json={'operation':'retract','expected_revision':1,
        'client_action_id':'read-retracted','reason':'Original assessment needs correction'})
    assert retracted.status_code==200,retracted.text
    assert client.get(url).status_code==422
    with db.connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(slot['source_ref'],)).fetchone()[0]=='completed'


def test_previously_shown_cancelled_question_is_not_independent_transfer(school_case):
    db,client,_,sid,_,near=school_case[:6]
    p=DiagnosticPromptService(db).prepare(sid,track='school',preferred_item_id=near['diagnostic_item_id'],
        client_action_id='seen-near')['prompt']
    cancelled=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/cancel',json={
        'prompt_id':p['prompt_id'],'item_version':p['item_version']})
    assert cancelled.status_code==200,cancelled.text
    answer_and_review(school_case)
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM review_task WHERE status='pending'").fetchone(), 'Previously shown cancelled question was scheduled as new transfer'


def test_real_parent_source_withdrawal_does_not_revive_or_cross_child(school_case,monkeypatch):
    answer_and_review(school_case)
    case,slot,p,_,_=next_day(school_case,monkeypatch,key='withdraw-source')
    db,client,headers,sid,_,near=case[:6]
    url=f"/api/parent/children/{CHILD}/curriculum-supply/{near['candidate_id']}/withdraw"
    body={'action_id':'source-withdraw','expected_version':near['version'],'note':'Withdraw only this reviewed transfer source'}
    assert client.post(url.replace(CHILD,'child-primary-4'),headers=headers,json=body).status_code==404
    withdrawn=client.post(url,headers=headers,json=body)
    assert withdrawn.status_code==200,withdrawn.text
    current=client.get(f'/v1/sessions/{sid}/diagnostic-prompt',params={'prompt_id':p['prompt_id']})
    assert current.status_code==200 and current.json()['status']=='invalidated',current.text
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE review_task_id=?',(slot['source_ref'],)).fetchone()
    reopened=client.school_reopen()
    assert reopened.get(f"/v1/plan-items/{slot['plan_item_id']}/review-question").status_code==422
