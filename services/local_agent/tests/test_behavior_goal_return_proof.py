"""Real C05 resume/answer receipts feed C04; coverage is synthetic, not a camera trial."""
from datetime import datetime, timedelta
import json

from home_study_agent.behavior_model import BehaviorModelService
from home_study_agent.behavior_model.outcome_evidence import task_context_snapshot
from test_companion_verified_return import returned, saved_answer
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item, CHILD


def test_c04_reuses_real_return_after_sampling_started_without_inventing_child_initiation(returned):
    db,sid,pid,_,_,_,receipt=returned
    svc=BehaviorModelService(db)
    svc.initialize()
    start=datetime.fromisoformat(receipt['occurred_at'])+timedelta(microseconds=1)
    attempt=saved_answer(returned)
    end=datetime.now(start.tzinfo)+timedelta(seconds=8)
    with db.connect() as c:
        c.execute('''INSERT INTO habit_experiment(experiment_id,child_id,target_state,title,child_action,success_signal,
            duration_days,baseline_json,status,proposed_at,started_at,ends_at)
            VALUES('return-goal',?,'fatigue_candidate','短休息后返回','主动短休息然后回来','真实复合证据',7,
            '{"feature":"fatigue_candidate_count"}','active',?,?,?)''',
            (CHILD,(start-timedelta(hours=2)).isoformat(),(start-timedelta(hours=1)).isoformat(),(end+timedelta(days=7)).isoformat()))
        task=dict(c.execute('SELECT pi.*,dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE plan_item_id=?',(pid,)).fetchone())
        context=task_context_snapshot(task)
        svc._insert_coverage(c,'return-open',CHILD,sid,'observation.stream_started',start.isoformat(),
            {'schema_version':'observation_coverage_v1','stream_epoch':'return-epoch'},[])
        previous=None
        for n in range(int((end-start).total_seconds()/4)+1):
            at=(start+timedelta(seconds=4*n)).isoformat()
            key='return-coverage-'+str(n)
            sample=dict(schema_version='observation_coverage_v1',child_id=CHILD,session_id=sid,stream_epoch='return-epoch',
                sequence=n,captured_at=at,capture_monotonic_ms=n*4000,received_at=at,
                quality=dict(observation_ready=True,scene_quality='ready',person_baseline='established',shadow=False,analysis_skipped=False),
                task_context=context,state='writing_like',confidence=.95,model_version='synthetic-qualified',analysis_id=key,
                producer_epoch='return-epoch',state_event_id=None)
            svc._insert_coverage(c,key,CHILD,sid,'observation.sample',at,sample,['return-open']+([previous] if previous else []))
            previous=key
        svc._insert_coverage(c,'return-stop',CHILD,sid,'observation.stream_stopped',at,
            {'schema_version':'observation_coverage_v1','stream_epoch':'return-epoch'},['return-open'])
        c.commit()
    offered=svc.goal_behavior_opportunities(CHILD,'return-goal',now=end.isoformat())
    item=next(i for i in offered['opportunities'] if i['context']['plan_item_id']==pid and i['observed_seconds']>0)
    assert item['automatic']['criteria']['returned_original_task'] is True
    assert item['automatic']['criteria']['child_initiated_short_break'] is None
    assert item['automatic']['assistance_level']=='unknown'
    assert item['automatic']['independent_response'] is True
    assert attempt['evidence_id'] in item['automatic']['evidence_refs']
    assert 'submitted_answer' not in json.dumps(item)
    assert BehaviorModelService(db).goal_behavior_opportunities(CHILD,'return-goal',now=end.isoformat())==offered
    with db.connect() as c:
        c.execute('UPDATE learning_evidence SET independent=0 WHERE evidence_id=?',(attempt['evidence_id'],))
        c.commit()
    current=svc.goal_behavior_opportunities(CHILD,'return-goal',now=end.isoformat())
    revised=next(i for i in current['opportunities'] if i['opportunity_id']==item['opportunity_id'])
    assert revised['automatic']['criteria']['returned_original_task'] is None
    assert revised['source_version']!=item['source_version']
    with db.connect() as c:
        assert c.execute('SELECT 1 FROM plan_item_event WHERE event_id=?',(receipt['event_id'],)).fetchone()
