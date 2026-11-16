"""Synthetic observation history; real service records and consumer projections."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from home_study_agent.behavior_model import BehaviorModelService
from home_study_agent.behavior_model.goal_confirmation import BehaviorActorProvenance
from home_study_agent.behavior_model.outcome_evidence import task_context_snapshot, validate_coverage_sample
from test_behavior_observation_coverage import managed

CHILD = 'child-primary-4'
NOW = '2026-09-02T00:00:00+00:00'


def history(managed, target='blocked_candidate'):
    db, svc = managed[:2]
    with db.connect() as c:
        c.execute("UPDATE daily_plan SET created_at='2026-08-01T00:00:00+00:00'")
        c.execute('''INSERT INTO habit_experiment(experiment_id,child_id,target_state,title,child_action,
            success_signal,duration_days,baseline_json,status,proposed_at)
            VALUES('goal',?,?, '具体目标','具体行动','逐项行为证据',7,?,'proposed','2026-08-22T00:00:00+00:00')''',
            (CHILD, target, json.dumps({'feature': {'blocked_candidate':'stall_count','distraction_candidate':'distraction_candidate_count',
                'frequent_switching':'switch_count','fatigue_candidate':'fatigue_candidate_count'}.get(target,'stall_count')})))
        for n, day in enumerate((19,19,20,20,21,21,24,24,26,26,28,28)):
            task = 'source-task-' + str(n)
            c.execute('''INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,expected_minutes,
                confirmation_status,status,created_at,client_action_id) VALUES(?,?,'session','math','written','Synthetic original task',
                10,'child_confirmed','completed','2026-08-01T00:00:00+00:00',?)''', ('homework-'+str(n),CHILD,'homework-'+str(n)))
            c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
                position,task_type,cognitive_load,source_type,homework_id) VALUES(?,'plan','math','Synthetic original task',10,1,'completed',?,
                'written','medium','school_homework',?)''', (task,n+1,'homework-'+str(n)))
            ctx = task_context_snapshot(dict(plan_item_id=task,subject='math',task_type='written',cognitive_load='medium',source_type='school_homework',plan_version=1))
            start = datetime(2026,8,day,10+n%2,tzinfo=timezone.utc)
            epoch, opened = 'stream-'+str(n), 'open-'+str(n)
            svc._insert_coverage(c,opened,CHILD,'session','observation.stream_started',start.isoformat(),
                {'schema_version':'observation_coverage_v1','stream_epoch':epoch},[])
            prev = None
            for index in range(81):
                at = (start+timedelta(seconds=index*4)).isoformat()
                eid = f'sample-{n}-{index}'
                sample = dict(schema_version='observation_coverage_v1',child_id=CHILD,session_id='session',stream_epoch=epoch,
                    sequence=index,captured_at=at,capture_monotonic_ms=index*4000,received_at=at,
                    quality=dict(observation_ready=True,scene_quality='ready',person_baseline='established',shadow=False,analysis_skipped=False),
                    task_context=ctx,state='writing_like',confidence=.95,model_version='synthetic-qualified',analysis_id=eid,producer_epoch=epoch,state_event_id=None)
                validate_coverage_sample(sample)
                svc._insert_coverage(c,eid,CHILD,'session','observation.sample',at,sample,[opened]+([prev] if prev else []))
                prev = eid
            svc._insert_coverage(c,'stop-'+str(n),CHILD,'session','observation.stream_stopped',at,
                {'schema_version':'observation_coverage_v1','stream_epoch':epoch},[opened])
        c.commit()
    svc.respond_habit_experiment('goal',CHILD,True,'2026-08-23T00:00:00+00:00','accept-goal')
    return db, svc


def confirm(svc, opportunity, *, success=True, observer='parent', assistance='none', key=None):
    return svc.confirm_goal_behavior(CHILD,'goal',opportunity_id=opportunity['opportunity_id'],expected_version=opportunity['binding_version'],
        opportunity_present=True,criteria={k:success for k in opportunity['criteria']},initiated_by='child',assistance_level=assistance,
        actor_provenance=BehaviorActorProvenance(observer,'synthetic-witness',CHILD,'authenticated-host-witness'),
        client_action_id=key or 'review-'+opportunity['opportunity_id'],at=NOW)


def automatic_records(db, svc, *, indices=range(12), observations=True):
    """Synthetic classifier outputs linked to original rows, not physical-camera evidence."""
    with db.connect() as c:
        for n in indices:
            sample_rows = c.execute("SELECT * FROM observable_event WHERE event_id LIKE ? ORDER BY occurred_at",
                                    ('sample-'+str(n)+'-%',)).fetchall()
            first, last = sample_rows[0],sample_rows[-1]
            for kind,stamp in [('started',first['occurred_at']),('completed',last['occurred_at'])]:
                eid = kind+'-'+str(n)
                c.execute('INSERT INTO plan_item_event VALUES(?,?,?,?,?,?)',
                          (eid,'source-task-'+str(n),kind,stamp,'{}',eid))
            if observations:
                for row in sample_rows:
                    payload = json.loads(row['payload_json'])
                    state_id = 'original-'+row['event_id']
                    payload['state_event_id'] = state_id
                    c.execute('UPDATE observable_event SET payload_json=? WHERE event_id=?', (json.dumps(payload),row['event_id']))
                    c.execute('''INSERT INTO observable_event(event_id,child_id,session_id,occurred_at,event_type,source,
                        confidence,source_refs_json,missing_sources_json,payload_json,alternatives_json)
                        VALUES(?,?,?,?,'work.writing','local_vision',.95,'[]','[]',?,'[]')''',
                        ('observation:'+state_id,CHILD,'session',row['occurred_at'],json.dumps({'shadow':False})))
        c.commit()


def goal_outcome(svc):
    return svc.habit_experiments(CHILD,NOW)['items'][0]['outcome']['goal_behavior']


def test_automatic_segments_do_not_require_parent_checkboxes_or_certify_independence(managed):
    db,svc = history(managed,'distraction_candidate')
    automatic_records(db,svc)
    result = goal_outcome(svc)
    assert result['status']=='repeated_observed_behavior'
    assert result['direction']=='not_yet_clear'
    assert 'goal_behavior_confirmation' not in result['missing_evidence']
    assert 'independent_goal_behavior' in result['missing_evidence']
    assert result['habit_success'] is False
    assert result['current']['independent_successes']==0
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type='goal_behavior_confirmed'").fetchone()[0]==0
    assert goal_outcome(BehaviorModelService(db))==result


def test_timer_alone_and_child_self_report_remain_unknown(managed):
    db,svc = history(managed,'distraction_candidate')
    automatic_records(db,svc,observations=False)
    for item in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities']:
        confirm(svc,item,observer='child')
    result = goal_outcome(svc)
    assert result['status']=='insufficient_evidence'
    assert result['current']['unknown']==6
    assert all(s['failures']==0 for s in result['current']['strata'].values())


def test_one_unconfirmed_opportunity_is_not_failure_or_permanent_block(managed):
    db,svc = history(managed)
    items = svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities']
    for phase in ('baseline','current'):
        for item in [i for i in items if i['phase']==phase][:5]:
            confirm(svc,item,success=phase=='current')
    result=goal_outcome(svc)
    assert result['direction']=='improving'
    assert result['current']['unknown']==1
    current=next(iter(result['current']['strata'].values()))
    assert current['failures']==0
    assert current['success_lower_bound']==pytest.approx(5/6)


def test_source_and_parent_correction_retraction_recompute_without_erasing_receipts(managed):
    from home_study_agent.behavior_model.goal_confirmation import GoalBehaviorConflict
    db,svc=history(managed,'distraction_candidate')
    automatic_records(db,svc)
    item=svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities'][-1]
    old=confirm(svc,item,success=False,key='sample-correction')
    assert confirm(svc,item,success=False,key='sample-correction')['replayed']
    fresh=next(i for i in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities'] if i['opportunity_id']==item['opportunity_id'])
    assert fresh['confirmation']['event_id']==old['event_id']
    svc.confirm_goal_behavior(CHILD,'goal',opportunity_id=item['opportunity_id'],expected_version=fresh['binding_version'],
        opportunity_present=None,criteria={k:None for k in item['criteria']},initiated_by='unknown',assistance_level='unknown',
        actor_provenance=BehaviorActorProvenance('parent','synthetic-witness',CHILD,'host-auth'),
        client_action_id='withdraw-correction',operation='retract',at=NOW)
    assert confirm(svc,item,success=False,key='sample-correction')['historical_receipt']
    restored=next(i for i in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities'] if i['opportunity_id']==item['opportunity_id'])
    assert restored['confirmation'] is None
    with pytest.raises(GoalBehaviorConflict,match='VERSION_CHANGED'):
        confirm(svc,item,key='stale-positive')
    svc.retract_event('observation:original-sample-11-40',CHILD,'withdraw-raw','Original state withdrawn')
    invalid=next(i for i in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities'] if i['opportunity_id']==item['opportunity_id'])
    assert invalid['source_version']!=restored['source_version']


def test_unobserved_task_is_in_denominator_and_does_not_become_failure(managed):
    db,svc=history(managed,'distraction_candidate')
    automatic_records(db,svc)
    with db.connect() as c:
        c.execute("INSERT INTO plan_item_event VALUES('unobserved-start','task','started','2026-08-29T00:00:00+00:00','{}','unobserved-start')")
        c.commit()
    result=goal_outcome(svc)
    assert result['current']['opportunities']==7
    assert result['current']['unknown']==1
    assert sum(s['failures'] for s in result['current']['strata'].values())==0


@pytest.mark.parametrize('target',['blocked_candidate','fatigue_candidate','unsupported'])
def test_automatic_subgoal_limits_are_not_hidden_as_habit_success(managed,target):
    db,svc=history(managed,target)
    automatic_records(db,svc)
    result=goal_outcome(svc)
    assert result['habit_success'] is False
    assert result['direction']=='not_yet_clear'
    assert result['automatic_limitations']
    assert result['status']==('unsupported' if target=='unsupported' else 'insufficient_evidence')


def test_switch_proof_requires_real_sequence_after_the_segment(managed):
    db,svc=history(managed,'frequent_switching')
    automatic_records(db,svc)
    assert goal_outcome(svc)['status']=='insufficient_evidence'
    with db.connect() as c:
        c.execute("UPDATE plan_item_event SET event_type='paused' WHERE event_type='completed'")
        for n in range(12):
            at=c.execute("SELECT occurred_at FROM plan_item_event WHERE event_id=?",('completed-'+str(n),)).fetchone()[0]
            at=(datetime.fromisoformat(at)+timedelta(seconds=1)).isoformat()
            key='after-segment-'+str(n)
            c.execute('INSERT INTO plan_item_event VALUES(?,?,?,?,?,?)',
                      (key,'source-task-'+str(n ^ 1),'resumed',at,'{}',key))
        c.commit()
    assert goal_outcome(svc)['status']=='repeated_observed_behavior'
    with db.connect() as c:
        c.execute("DELETE FROM plan_item_event WHERE client_action_id LIKE 'after-segment-%'")
        c.commit()
    assert goal_outcome(svc)['status']=='insufficient_evidence'


def test_actor_is_host_derived_and_borrowed_transaction_rolls_back(managed):
    from home_study_agent.behavior_model.goal_confirmation import GoalBehaviorConflict
    db,svc=history(managed)
    item=svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities'][0]
    args=dict(opportunity_id=item['opportunity_id'],expected_version=item['binding_version'],opportunity_present=True,
        criteria={k:True for k in item['criteria']},initiated_by='child',assistance_level='none',client_action_id='borrowed',at=NOW)
    with pytest.raises(GoalBehaviorConflict,match='ACTOR_NOT_AUTHORIZED'):
        svc.confirm_goal_behavior(CHILD,'goal',actor_provenance=dict(actor_type='parent',actor_id='forged'),**args)
    actor=BehaviorActorProvenance('parent','parent',CHILD,'host-authenticated')
    with db.connect() as c:
        with pytest.raises(GoalBehaviorConflict,match='WRITE_TRANSACTION_REQUIRED'):
            svc.confirm_goal_behavior(CHILD,'goal',connection=c,actor_provenance=actor,**args)
        c.execute('BEGIN IMMEDIATE')
        svc.confirm_goal_behavior(CHILD,'goal',connection=c,actor_provenance=actor,**args)
        assert c.in_transaction
        c.rollback()
        assert not c.execute("SELECT 1 FROM habit_experiment_event WHERE client_action_id='borrowed'").fetchone()
    first=svc.confirm_goal_behavior(CHILD,'goal',actor_provenance=actor,**args)
    with db.connect() as c:
        assert c.execute('SELECT event_id FROM habit_experiment_event WHERE client_action_id=?',('borrowed',)).fetchone()[0]==first['event_id']
    with pytest.raises(GoalBehaviorConflict,match='ACTOR_NOT_AUTHORIZED'):
        svc.confirm_goal_behavior('child-junior-3','goal',actor_provenance=actor,**args)


def test_source_disappearance_retains_unknown_denominator_and_allows_retraction(managed):
    db,svc=history(managed)
    item=svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities'][-1]
    confirm(svc,item)
    with db.connect() as c:
        c.execute("DELETE FROM observable_event WHERE event_id LIKE 'sample-11-%' OR event_id IN ('open-11','stop-11')")
        c.commit()
    offered=svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities']
    assert len(offered)==12
    missing=next(i for i in offered if i['opportunity_id']==item['opportunity_id'])
    assert not missing['source_current'] and missing['confirmation'] is None
    svc.confirm_goal_behavior(CHILD,'goal',opportunity_id=missing['opportunity_id'],expected_version=missing['binding_version'],
        opportunity_present=None,criteria={k:None for k in missing['criteria']},initiated_by='unknown',assistance_level='unknown',
        actor_provenance=BehaviorActorProvenance('parent','parent',CHILD,'host'),operation='retract',client_action_id='withdraw-missing',at=NOW)
    assert goal_outcome(svc)['current']['unknown']==6


def test_not_applicable_is_distinct_from_failure_and_insufficient_windows_remain_visible(managed):
    db,svc=history(managed)
    item=svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities'][-1]
    svc.confirm_goal_behavior(CHILD,'goal',opportunity_id=item['opportunity_id'],expected_version=item['binding_version'],
        opportunity_present=False,criteria={k:None for k in item['criteria']},initiated_by='unknown',assistance_level='unknown',
        actor_provenance=BehaviorActorProvenance('parent','parent',CHILD,'host'),client_action_id='not-applicable',at=NOW)
    current=next(iter(goal_outcome(svc)['current']['strata'].values()))
    assert current['not_applicable']==1 and current['failures']==0
    assert current['unknown']==5 and current['eligible_denominator']==5


def test_child_model_rechecks_original_source_instead_of_cached_improvement(managed):
    from home_study_agent.child_model import ChildModelService
    db,svc=history(managed)
    for item in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities']:
        confirm(svc,item,success=item['phase']=='current')
    consumer=ChildModelService(db)
    with db.connect() as c:
        assert consumer._habit_experiments(c,CHILD)['improving_count']==1
        c.execute("UPDATE homework_item SET title='Changed original source' WHERE homework_id IN ('homework-10','homework-11')")
        c.commit()
        # No behavior refresh is called: the stale persisted direction is deliberately still present.
        assert json.loads(c.execute("SELECT outcome_json FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0])['direction']=='improving'
        assert consumer._habit_experiments(c,CHILD)['improving_count']==0
        assert not c.in_transaction


def test_current_bounded_goal_can_be_met_without_claiming_improvement_over_unknown_baseline(managed):
    db,svc=history(managed)
    for item in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities']:
        if item['phase']=='current':
            confirm(svc,item)
    result=goal_outcome(svc)
    assert result['status']=='bounded_confirmed_behavior'
    assert result['bounded_goal_success'] is True
    assert result['direction']=='not_yet_clear'
    assert result['baseline']['unknown']==6
    assert result['habit_success'] is False


def test_different_subject_windows_do_not_get_averaged_into_improvement(managed):
    db,svc=history(managed)
    with db.connect() as c:
        for n in range(6,12):
            c.execute("UPDATE plan_item SET subject='english' WHERE plan_item_id=?",('source-task-'+str(n),))
            c.execute("UPDATE homework_item SET subject='english' WHERE homework_id=?",('homework-'+str(n),))
            for row in c.execute('SELECT event_id,payload_json FROM observable_event WHERE event_id LIKE ?',('sample-'+str(n)+'-%',)).fetchall():
                payload=json.loads(row['payload_json'])
                ctx=payload['task_context']
                ctx['subject']='english'
                payload['task_context']=task_context_snapshot(ctx)
                c.execute('UPDATE observable_event SET payload_json=? WHERE event_id=?',(json.dumps(payload),row['event_id']))
        c.commit()
    for item in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities']:
        confirm(svc,item,success=item['phase']=='current')
    result=goal_outcome(svc)
    assert result['bounded_goal_success'] is True
    assert result['direction']=='not_yet_clear'
    assert 'comparable_task_coverage' in result['missing_evidence']


def test_confirmed_repeated_behavior_is_not_permanently_missing(managed):
    db, svc = history(managed)
    offered = svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)
    assert len(offered['opportunities']) == 12
    for item in offered['opportunities']:
        confirm(svc,item,success=item['phase']=='current')
    result = svc.habit_experiments(CHILD,NOW)['items'][0]['outcome']
    assert 'goal_behavior_confirmation' not in result['missing_evidence']
    assert result['direction'] == 'improving'
    assert result['goal_behavior']['current']['opportunities'] == 6
    assert result['goal_behavior']['current']['independent_successes'] == 6
    assert result['goal_behavior']['habit_success'] is False
    restarted = BehaviorModelService(db)
    assert restarted.habit_experiments(CHILD,NOW)['items'][0]['outcome'] == result


def test_source_retraction_removes_current_confirmation_not_history(managed):
    db, svc = history(managed)
    for item in svc.goal_behavior_opportunities(CHILD,'goal',now=NOW)['opportunities']:
        confirm(svc,item,success=item['phase']=='current')
    svc.retract_event('sample-6-40',CHILD,'retract-original-source','original observation withdrawn')
    svc.retract_event('sample-7-40',CHILD,'retract-second-source','original observation withdrawn')
    result = svc.habit_experiments(CHILD,NOW)['items'][0]['outcome']
    assert result['direction'] != 'improving'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type='goal_behavior_confirmed'").fetchone()[0] == 12
