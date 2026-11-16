"""Actual HTTP current-obligation projection and actionable rejection wording."""
import json
from synthetic_unclean_app_process import unclean_app

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_supply_gap_resume_http import supplied, facts, start, execute, CHILD


def read(case, client=None):
    result=(client or case['client']).get(f"/v1/sessions/{case['sid']}")
    assert result.status_code==200,result.text
    return result.json()


def test_workspace_current_obligation_does_not_rewrite_missing_history(supplied):
    case=supplied
    before=facts(case)
    ready=read(case)['supply_obligations']
    assert any(row['status']=='ready' for row in ready)
    assert facts(case)==before
    from home_study_agent.supply_gap_presentation import project_supply_obligations
    with case['db'].connect() as c:
        c.execute('BEGIN')
        changes=c.total_changes
        assert project_supply_obligations(c,case['sid'],'child-junior-3',before['daily_plan'][0]['plan_id'])==[]
        assert c.in_transaction and c.total_changes==changes
        c.rollback()
    turn,task,prompt=start(case)
    placed=facts(case)
    receipt=next(json.loads(row['metadata_json']) for row in placed['plan_constraint_decision']
        if json.loads(row['metadata_json']).get('plan_item_id')==task['plan_item_id'])
    original_id=receipt['supply_resume']['obligation_id']
    result=read(case)
    current=next(row for row in result['supply_obligations'] if row['obligation_id']==original_id)
    assert current['status']=='active' and current['plan_item_id']==task['plan_item_id']
    assert current['message']=='已补题并安排，正在练习。'
    assert current['mastery_claim_allowed'] is False and current['local_only'] is True
    history=next(row for row in result['constraint_decisions'] if row['decision_id']==original_id)
    assert history['decision']=='deferred'
    assert facts(case)==placed
    with unclean_app(case, lane='supply_gap', headers=dict(case['client'].headers)) as client:
        paused=facts(case)
        recovered=next(row for row in read(case,client)['supply_obligations'] if row['obligation_id']==original_id)
        assert recovered['status']=='paused' and recovered['plan_item_id']==task['plan_item_id']
        assert facts(case)==paused
        replay=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert replay.status_code==200,replay.text
        assert facts(case)==paused
        unlocked=client.post('/v1/parent/unlock',json={'pin':'7391'})
        assert unlocked.status_code==200,unlocked.text
        client.headers['X-HSC-Parent']=unlocked.json()['parent_token']
        case['post'](client,case['base']+f"/{case['draft']['candidate_id']}/withdraw",
            dict(expected_version=4,action_id='presentation-withdraw',note='No longer valid source'))
        withdrawn=facts(case)
        current=next(row for row in read(case,client)['supply_obligations'] if row['obligation_id']==original_id)
        assert current['status']=='source_changed'
        assert current['plan_item_id']==task['plan_item_id'] and current['mastery_claim_allowed'] is False
        assert facts(case)==withdrawn


@pytest.mark.parametrize('fault,phrase', [('budget','剩余时间'),('withdraw','题目来源')])
def test_real_execute_conflict_explains_what_is_blocked_without_erasing_progress(supplied,fault,phrase):
    case=supplied
    turn=case['say']('presentation-'+fault)
    if fault=='budget':
        rule=case['client'].get(f'/v1/parent/children/{CHILD}/family-rule').json()
        response=case['client'].put(f'/v1/parent/children/{CHILD}/family-rule',json={
            **{k:rule[k] for k in ('latest_end_time','max_learning_minutes','task_segment_minutes','break_minutes',
                'pet_min_minutes','competition_minutes','observation_mode','reminder_budget_per_10m')},
            'task_segment_minutes':10})
        assert response.status_code==200,response.text
    else:
        case['post'](case['client'],case['base']+f"/{case['draft']['candidate_id']}/withdraw",
            dict(expected_version=4,action_id='message-withdraw',note='Withdraw exact source'))
    before=facts(case)
    result=execute(case,turn)
    assert result.status_code==409,result.text
    assert phrase in result.json()['detail']['message'],result.text
    assert '保留' in result.json()['detail']['message']
    assert facts(case)==before
