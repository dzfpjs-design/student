"""Accepted optional support is not a completion condition or success label."""
from datetime import datetime, timedelta
import json

import pytest

from home_study_agent.behavior_model import BehaviorModelService
from test_behavior_goal_confirmation import CHILD, history
from test_behavior_intervention_scope_http import signal
from test_behavior_observation_coverage import managed

NOW = '2026-08-29T10:00:00+00:00'


def active(managed):
    db, service = history(managed)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET started_at='2026-08-01T00:00:00+00:00' WHERE session_id='session'")
        c.execute("UPDATE plan_item SET task_type='written',cognitive_load='medium',source_type='school_homework' WHERE plan_item_id='task'")
        c.execute("INSERT INTO plan_item_event VALUES('live-start','task','started',?,'{}','live-start')", (NOW,))
        c.commit()
    return db, service


def offer(service, now=NOW, key='offer'):
    signal(service,'session',key,stamp=now)
    return service.decide_intervention(CHILD,'session',now)


def test_actual_decide_offers_accepted_action_without_claiming_success(managed):
    db, service = active(managed)
    result = offer(service)
    assert result['strategy'] == 'contextual_habit_ask_first'
    assert '具体行动' in result['message'] and '不想做也可以' in result['message']
    basis = result['support_basis']
    assert basis['acceptance_ref'] and basis['goal_version']
    assert basis['completion_condition'] is False and basis['bounded_goal_success'] is False
    assert basis['habit_success'] is False and basis['burden_status'] == 'unknown'
    with db.connect() as c:
        assert c.execute("SELECT completion_standard FROM plan_item WHERE plan_item_id='task'").fetchone()[0].find('具体行动') == -1
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type='goal_behavior_confirmed'").fetchone()[0] == 0
    assert BehaviorModelService(db).pending_feedback(CHILD,'session',NOW)['intervention']['message'] == result['message']
    service.record_feedback(result['intervention_id'],False,NOW,child_id=CHILD,session_id='session',client_action_id='decline-prompt')
    service.stop_habit_experiment('goal',CHILD,NOW,'stop-experiment')
    later = (datetime.fromisoformat(NOW)+timedelta(minutes=21)).isoformat()
    after = offer(BehaviorModelService(db),later,'after-stop')
    assert after['strategy'] == 'default_ask_first' and '具体行动' not in after['message']


@pytest.mark.parametrize('case',['different_context','sources_withdrawn','changed_definition','legacy_acceptance','malformed_acceptance','expired'])
def test_no_inferred_applicability_or_inherited_consent(managed,case):
    db, service = active(managed)
    with db.connect() as c:
        if case=='different_context':
            c.execute("UPDATE plan_item SET subject='english' WHERE plan_item_id='task'")
        elif case=='sources_withdrawn':
            c.execute("UPDATE homework_item SET confirmation_status='pending'")
        elif case=='changed_definition':
            c.execute("UPDATE habit_experiment SET child_action='Changed instruction' WHERE experiment_id='goal'")
        elif case=='legacy_acceptance':
            c.execute("UPDATE habit_experiment_event SET payload_json=? WHERE event_type='accepted'",(json.dumps({'accepted':True}),))
        elif case=='malformed_acceptance':
            c.execute("UPDATE habit_experiment_event SET payload_json='[]' WHERE event_type='accepted'")
        c.commit()
    result = offer(service,'2026-08-31T00:00:00+00:00' if case=='expired' else NOW)
    assert result.get('strategy') != 'contextual_habit_ask_first'
    assert '具体行动' not in result.get('message','') and 'Changed instruction' not in result.get('message','')


def test_acceptance_original_key_remains_replayable_with_version_snapshot(managed):
    db, service = active(managed)
    with db.connect() as c:
        first = c.execute("SELECT event_id,payload_json FROM habit_experiment_event WHERE client_action_id='accept-goal'").fetchone()
    assert json.loads(first['payload_json'])['goal_version']
    saved = BehaviorModelService(db).respond_habit_experiment('goal',CHILD,True,'2026-08-23T00:00:00+00:00','accept-goal')
    assert saved['status']=='active'
    with pytest.raises(ValueError):
        service.respond_habit_experiment('goal',CHILD,False,NOW,'accept-goal')
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type='accepted'").fetchone()[0]==1


@pytest.mark.parametrize('change',['stop','source','context','definition','expired'])
def test_pending_optional_instruction_is_not_replayed_after_scope_changes(managed,change):
    db, service = active(managed)
    result = offer(service)
    assert result['strategy']=='contextual_habit_ask_first'
    stamp = NOW
    if change=='stop':
        service.stop_habit_experiment('goal',CHILD,NOW,'stop-with-pending')
    else:
        with db.connect() as c:
            if change=='source':
                c.execute("UPDATE homework_item SET confirmation_status='pending'")
            elif change=='context':
                c.execute("UPDATE plan_item SET subject='english' WHERE plan_item_id='task'")
            elif change=='definition':
                c.execute("UPDATE habit_experiment SET child_action='Changed instruction' WHERE experiment_id='goal'")
            else:
                stamp='2026-08-31T00:00:00+00:00'
            c.commit()
    pending = BehaviorModelService(db).pending_feedback(CHILD,'session',stamp)
    assert pending['status']=='no_pending', pending
    if change!='stop':
        with db.connect() as c:
            row=c.execute('SELECT status,withdrawal_reason FROM intervention WHERE intervention_id=?',(result['intervention_id'],)).fetchone()
            assert tuple(row)==('active','habit_support_source_changed_pending')
        assert BehaviorModelService(db).process_pending_intervention_withdrawals()['completed']==1
    with db.connect() as c:
        row=c.execute('SELECT status,message FROM intervention WHERE intervention_id=?',(result['intervention_id'],)).fetchone()
        assert row['status']=='withdrawn' and row['message']==result['message']


def test_child_rejection_policy_stops_optional_goal_and_keeps_low_frequency_prompt(managed):
    db, service = active(managed)
    # Historical feedback remains authoritative even when a newly accepted
    # experiment is still in its non-intervening baseline window.
    with db.connect() as c:
        for index in range(4):
            stamp=(datetime.fromisoformat(NOW)-timedelta(days=2-index//2)+timedelta(minutes=40*(index%2))).isoformat()
            c.execute(
                """INSERT INTO intervention(
                     intervention_id,child_id,session_id,decided_at,candidate_state,message,
                     feedback,feedback_at,intervention_type,status
                   ) VALUES(?,?,?,?,?,?,?,?,?,'active')""",
                (f'past-declined-{index}',CHILD,'session',stamp,'blocked_candidate','历史提醒',
                 'declined',stamp,'default_ask_first'),
            )
        c.commit()
    policy=service.personalization(CHILD)['items'][0]
    assert policy['strategy']=='low_frequency_ask_first'
    assert policy['declined']==4
    current=offer(BehaviorModelService(db))
    assert current['status']=='suppressed'
    assert current['reason']=='habit_auto_stop_pending'
    assert service.process_pending_habit_stops()['completed']==1
    current=offer(BehaviorModelService(db))
    assert current['status']=='suggested'
    assert current['strategy']=='low_frequency_ask_first'
    assert '具体行动' not in current['message']
    with db.connect() as c:
        assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]=='stopped'


@pytest.mark.parametrize('mode',['optional','faded'])
@pytest.mark.parametrize('change',['replanned','superseded','completed_session','wrong_state','future_session'])
def test_support_requires_current_plan_and_valid_session(managed,mode,change):
    from test_behavior_goal_intervention_consumer import ready, NOW as REVIEW_NOW
    db, service = active(managed) if mode=='optional' else ready(managed)
    stamp = NOW if mode=='optional' else REVIEW_NOW
    with db.connect() as c:
        if change in {'replanned','superseded'}:
            c.execute('UPDATE daily_plan SET status=? WHERE plan_id=\'plan\'',(change,))
        elif change=='completed_session':
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id='session'")
        elif change=='wrong_state':
            c.execute("UPDATE daily_session SET state='break' WHERE session_id='session'")
        else:
            c.execute("UPDATE daily_session SET started_at=? WHERE session_id='session'",
                ((datetime.fromisoformat(stamp)+timedelta(minutes=1)).isoformat(),))
        c.commit()
    result=offer(service,stamp)
    assert result.get('strategy') not in {'contextual_habit_ask_first','autonomy_faded_ask_first'}, result
    assert result.get('support_basis') is None
