"""Real service decisions from existing qualified opportunity/confirmation records."""
from datetime import datetime, timedelta
import json
import sqlite3

import pytest

from home_study_agent.behavior_model import BehaviorModelService
from test_behavior_goal_confirmation import CHILD, NOW, history, confirm
from test_behavior_intervention_scope_http import signal
from test_behavior_observation_coverage import managed


def ready(managed, *, witnessed=True, assistance='none', stopped=False):
    db, service = history(managed)
    if stopped:
        service.stop_habit_experiment('goal', CHILD, '2026-08-29T00:00:00+00:00', 'stop-goal')
    if witnessed:
        for item in service.goal_behavior_opportunities(CHILD, 'goal', now=NOW)['opportunities']:
            confirm(service, item, success=item['phase'] == 'current', assistance=assistance)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET task_type='written',cognitive_load='medium',source_type='school_homework' WHERE plan_item_id='task'")
        c.execute("INSERT INTO plan_item_event VALUES('active-start','task','started',?,'{}','active-start')",
            ((datetime.fromisoformat(NOW)-timedelta(minutes=1)).isoformat(),))
        c.commit()
    return db, service


def decide(service, *, minutes=0, key='now', child=CHILD):
    stamp = (datetime.fromisoformat(NOW)+timedelta(minutes=minutes)).isoformat()
    signal(service, 'session', key, stamp=stamp, child=child)
    return service.decide_intervention(child, 'session', stamp)


def test_qualified_same_context_reduces_actual_reminder_and_persists_basis(managed):
    db, service = ready(managed)
    result = decide(service)
    assert result['strategy'] == 'autonomy_faded_ask_first'
    basis = result['support_basis']
    assert basis['experiment_id'] == 'goal' and basis['habit_success'] is False
    assert basis['burden_status'] == 'unknown'
    assert basis['cooldown_minutes'] == 20 and basis['daily_cap'] == 2
    assert basis['evidence_version'] and basis['confirmation_refs']
    receipt = service.record_feedback(result['intervention_id'], True, NOW, child_id=CHILD,
        session_id='session', client_action_id='accept-one')
    assert receipt
    assert decide(BehaviorModelService(db), minutes=11, key='during-fade')['reason'] == 'personalized_cooldown'
    second = decide(BehaviorModelService(db), minutes=21, key='later')
    assert second['strategy'] == 'autonomy_faded_ask_first'
    with db.connect() as c:
        frozen = json.loads(c.execute('SELECT support_basis_json FROM intervention WHERE intervention_id=?',
            (result['intervention_id'],)).fetchone()[0])
    assert frozen == basis


@pytest.mark.parametrize('case', ['unknown', 'assisted', 'different_context', 'withdrawn', 'stopped', 'expired', 'last_opportunity_expired', 'other_child'])
def test_unknown_or_changed_sources_never_supply_independent_success(managed, case):
    db, service = ready(managed, witnessed=case!='unknown', assistance='assisted' if case=='assisted' else 'none', stopped=case=='stopped')
    if case in {'different_context', 'withdrawn'}:
        with db.connect() as c:
            if case == 'different_context':
                c.execute("UPDATE plan_item SET subject='english' WHERE plan_item_id='task'")
            else:
                c.execute("UPDATE homework_item SET confirmation_status='pending' WHERE homework_id IN ('homework-6','homework-7','homework-8','homework-9','homework-10','homework-11')")
            c.commit()
    minutes = 30*24*60 if case=='expired' else 9*24*60+16*60 if case=='last_opportunity_expired' else 0
    result = decide(BehaviorModelService(db), minutes=minutes,
        child='child-junior-3' if case=='other_child' else CHILD)
    assert result.get('strategy') != 'autonomy_faded_ask_first'
    assert not result.get('support_basis')


def test_parent_retraction_recomputes_without_rewriting_original_decision(managed):
    db, service = ready(managed)
    first = decide(service)
    assert first['strategy'] == 'autonomy_faded_ask_first'
    service.record_feedback(first['intervention_id'], True, NOW, child_id=CHILD,
        session_id='session', client_action_id='first-feedback')
    from home_study_agent.behavior_model.goal_confirmation import BehaviorActorProvenance
    for item in service.goal_behavior_opportunities(CHILD, 'goal', now=NOW)['opportunities']:
        if item['phase'] == 'current':
            service.confirm_goal_behavior(CHILD, 'goal', opportunity_id=item['opportunity_id'],
                expected_version=item['binding_version'], operation='retract', opportunity_present=None,
                criteria={k:None for k in item['criteria']}, initiated_by='unknown', assistance_level='unknown',
                actor_provenance=BehaviorActorProvenance('parent','synthetic-witness',CHILD,'authenticated-host-witness'),
                client_action_id='retract-'+item['opportunity_id'], at=NOW)
    after = decide(BehaviorModelService(db), minutes=21, key='after-retraction')
    assert after['strategy'] == 'default_ask_first'
    with db.connect() as c:
        frozen = json.loads(c.execute('SELECT support_basis_json FROM intervention WHERE intervention_id=?',
            (first['intervention_id'],)).fetchone()[0])
    assert frozen == first['support_basis']


@pytest.mark.parametrize('case', ['future_confirmation', 'candidate_before_task_start', 'unknown_task_context'])
def test_support_is_not_borrowed_across_time_or_unknown_task_context(managed, case):
    db, service = ready(managed)
    if case == 'unknown_task_context':
        with db.connect() as c:
            c.execute("UPDATE plan_item SET source_type='unknown' WHERE plan_item_id='task'")
            c.commit()
    stamp = (datetime.fromisoformat(NOW)-timedelta(seconds=1)).isoformat() if case=='future_confirmation' else NOW
    event_stamp = (datetime.fromisoformat(NOW)-timedelta(minutes=2)).isoformat() if case=='candidate_before_task_start' else stamp
    signal(service, 'session', 'boundary', stamp=event_stamp)
    result = service.decide_intervention(CHILD, 'session', stamp)
    assert result.get('strategy') != 'autonomy_faded_ask_first'
    assert not result.get('support_basis')


def test_decision_write_failure_rolls_back_and_original_event_can_retry(managed):
    db, service = ready(managed)
    signal(service, 'session', 'retry-source', stamp=NOW)
    with db.connect() as c:
        before = c.execute('SELECT COUNT(*) FROM habit_experiment_event').fetchone()[0]
        c.execute("CREATE TRIGGER reject_decision BEFORE INSERT ON intervention BEGIN SELECT RAISE(ABORT,'injected'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        service.decide_intervention(CHILD, 'session', NOW)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM habit_experiment_event').fetchone()[0] == before
        c.execute('DROP TRIGGER reject_decision')
        c.commit()
    recovered = BehaviorModelService(db).decide_intervention(CHILD, 'session', NOW)
    assert recovered['strategy'] == 'autonomy_faded_ask_first'
    assert BehaviorModelService(db).decide_intervention(CHILD, 'session', NOW)['reason'] == 'awaiting_child_feedback'


def test_another_owned_session_with_same_context_does_not_inherit_child_goal(managed):
    db, service = ready(managed)
    other = 'child-junior-3'
    with db.connect() as c:
        # The host supports one live session, not one simultaneous session per child.
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id='session'")
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES('other-session',?,'active','study',?,?,'other-session')""", (other,NOW,NOW))
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES('other-plan',?,'other-session','2026-09-02','active',20,'test',1,?,'other-plan')""", (other,NOW))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,task_type,cognitive_load,source_type)
            VALUES('other-task','other-plan','math','Synthetic',20,1,'active',0,'written','medium','school_homework')""")
        c.execute("INSERT INTO plan_item_event VALUES('other-start','other-task','started',?,'{}','other-start')", (NOW,))
        c.commit()
    signal(service, 'other-session', 'other-signal', stamp=NOW, child=other)
    result = BehaviorModelService(db).decide_intervention(other, 'other-session', NOW)
    assert result['status'] == 'suggested' and result['strategy'] == 'default_ask_first'
    assert result['support_basis'] is None
