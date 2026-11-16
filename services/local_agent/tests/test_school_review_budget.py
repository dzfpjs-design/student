"""Real queued school task start retains the family's current time constraints."""
import pytest

from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD


@pytest.mark.parametrize('constraint',['latest_end','segment_cap','total_cap'])
def test_real_school_voice_start_rechecks_current_family_budget(school_case,monkeypatch,constraint):
    answer_and_review(school_case)
    case,slot,_,turn,_=next_day(school_case,monkeypatch,key='budget',execute=False)
    db,client=case[:2]
    with db.connect() as c:
        if constraint=='latest_end':
            c.execute("UPDATE family_rule SET latest_end_time='15:59' WHERE child_id=?",(CHILD,))
        elif constraint=='segment_cap':
            c.execute('UPDATE family_rule SET task_segment_minutes=5 WHERE child_id=?',(CHILD,))
        else:
            c.execute('UPDATE family_rule SET max_learning_minutes=5 WHERE child_id=?',(CHILD,))
        c.commit()
    response=client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert response.status_code in {200,400,409,422},response.text
    if response.status_code==200:
        assert response.json()['execution']['result']['status']!='active',response.text
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(slot['plan_item_id'],)).fetchone()[0]=='pending'
        assert not c.execute("SELECT 1 FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(slot['plan_item_id'],)).fetchone()
        assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE review_task_id=?',(slot['source_ref'],)).fetchone()
