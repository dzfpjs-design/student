"""Synthetic parent corrections cannot race a frozen daily report."""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from home_study_agent.behavior_model.goal_confirmation import BehaviorActorProvenance,GoalBehaviorConflict
from home_study_agent.daily_behavior_source import daily_behavior_fingerprint
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository,utc_now
from test_behavior_goal_http import host,ROOT
from test_behavior_goal_confirmation import CHILD,history
from test_behavior_observation_coverage import managed


def _body(item,action):
    return dict(opportunity_id=item['opportunity_id'],expected_version=item['binding_version'],
        opportunity_present=True,criteria={key:True for key in item['criteria']},
        initiated_by='child',assistance_level='none',client_action_id=action)


def _report(db):
    repo=StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id='session'",(utc_now(),))
        c.execute('UPDATE family_rule SET max_learning_minutes=30,task_segment_minutes=20,latest_end_time=NULL WHERE child_id=?',(CHILD,));c.commit()
    sid=repo.start_session(CHILD,'goal-report-session')['session_id']
    repo.transition(sid,'check_in','classroom_recall','开始学习','goal-report-checkin')
    repo.add_recall(sid,'数学','复习合成题。','goal-report-recall')
    repo.transition(sid,'classroom_recall','homework_capture','记录完成','goal-report-homework-stage')
    repo.add_homework(sid,'数学','合成练习',20,'written',None,None,None,None,None,'goal-report-homework')
    workspace=repo.generate_plan(sid,'goal-report-plan')
    repo.transition(sid,'plan_review','study','确认计划','goal-report-study')
    item=workspace['plan_items'][0]['plan_item_id']
    repo.start_plan_item(item,'goal-report-start');repo.complete_plan_item(item,20,'goal-report-complete')
    repo.transition(sid,'study','reflection','学习完成','goal-report-reflection-stage')
    repo.complete_with_reflection(sid,'完成合成练习。','','可以','明天复习。',False,'goal-report-reflection')
    summary,_=build_daily_report(repo.report_evidence(sid))
    report=repo.get_daily_report(repo.save_daily_report(sid,summary,
        {'action':{'what':'复习合成题','duration_minutes':10},'script':['先看第一步。'],'avoid':[]})['report_id'])
    reports=ParentReportService(db)
    snapshot=reports.approve(reports.create_from_daily(report)['snapshot_id'])['snapshot_id']
    def source_current():
        with db.connect() as c:
            return reports.snapshot_source_current(c,c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot,)).fetchone())
    return repo,source_current


@pytest.mark.parametrize('operation',['retract','confirm'])
def test_http_correction_waits_and_preserves_frozen_report(managed,tmp_path,operation):
    db,_=history(managed)
    with host(db,tmp_path) as client:
        parent=client.post('/v1/parent/setup',json={'pin':'8362'});assert parent.status_code==200
        client.headers['X-HSC-Parent']=parent.json()['parent_token']
        opportunities=client.get(ROOT+'/opportunities').json()['opportunities']
        first=_body(opportunities[-1],'goal-original')
        assert client.post(ROOT+'/confirmation',json=first).status_code==200
        repo,source_current=_report(db)
        assert source_current()
        child_session=repo.start_session(CHILD,'goal-next-session')['session_id']
        repo.transition(child_session,'check_in','classroom_recall','继续学习','goal-next-checkin')
        current=next(i for i in client.get(ROOT+'/opportunities').json()['opportunities'] if i['opportunity_id']==first['opportunity_id'])
        correction={**first,'expected_version':current['binding_version'],'operation':operation,
                    'client_action_id':'goal-correction','reason':'合成来源修正',
                    'opportunity_present':None,'criteria':{key:None for key in current['criteria']},
                    'initiated_by':'unknown','assistance_level':'unknown'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            with db.external_effect_guard():
                replay=pool.submit(client.post,ROOT+'/confirmation',json=first).result(timeout=3)
                assert replay.status_code==200 and replay.json()['replayed']
                busy=client.post(ROOT+'/confirmation',json=correction)
                assert busy.status_code==503 and busy.json()['detail']['code']=='BEHAVIOR_GOAL_BUSY',busy.text
                assert source_current()
                pending=pool.submit(client.post,ROOT+'/confirmation',json=correction)
                time.sleep(.15)
                assert not pending.done() and source_current()
                child=pool.submit(client.post,f'/v1/sessions/{child_session}/recalls',json={
                    'subject':'数学','summary':'继续记录。','client_action_id':'child-while-correction-waits'}).result(timeout=1.2)
                assert child.status_code==201,child.text
                with db.connect() as c:
                    assert not c.execute("SELECT 1 FROM habit_experiment_event WHERE client_action_id='goal-correction'").fetchone()
            response=pending.result(timeout=8)
        assert response.status_code==200,response.text
        assert not source_current()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.external_effect_guard():
                replay=pool.submit(client.post,ROOT+'/confirmation',json=correction).result(timeout=3)
                assert replay.status_code==200 and replay.json()['replayed']


def test_first_confirmation_and_normal_child_event_do_not_wait(managed,tmp_path):
    db,_=history(managed)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='classroom_recall' WHERE session_id='session'");c.commit()
    with host(db,tmp_path) as client:
        parent=client.post('/v1/parent/setup',json={'pin':'8362'});client.headers['X-HSC-Parent']=parent.json()['parent_token']
        first=_body(client.get(ROOT+'/opportunities').json()['opportunities'][-1],'goal-first')
        with ThreadPoolExecutor(max_workers=2) as pool:
            with db.external_effect_guard():
                result=pool.submit(client.post,ROOT+'/confirmation',json=first).result(timeout=3)
                assert result.status_code==200 and not result.json()['replayed']
                # A real child event is independent of the mail effect lock.
                normal=pool.submit(client.post,'/v1/sessions/session/recalls',json={
                    'subject':'数学','summary':'继续学习。','client_action_id':'child-during-send'}).result(timeout=3)
                assert normal.status_code == 201,normal.text


def test_borrowed_transaction_requires_original_guard_for_correction(managed):
    db,service=history(managed)
    actor=BehaviorActorProvenance('parent','synthetic',CHILD,'fixture')
    item=service.goal_behavior_opportunities(CHILD,'goal')['opportunities'][-1]
    first=_body(item,'borrowed-first');first.pop('client_action_id')
    service.confirm_goal_behavior(CHILD,'goal',actor_provenance=actor,client_action_id='borrowed-first',**first)
    item=next(i for i in service.goal_behavior_opportunities(CHILD,'goal')['opportunities'] if i['opportunity_id']==item['opportunity_id'])
    update={**_body(item,'borrowed-correction'),'operation':'retract','reason':'合成修正'}
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(GoalBehaviorConflict,match='BEHAVIOR_EFFECT_GUARD_REQUIRED'):
            service.confirm_goal_behavior(CHILD,'goal',connection=c,actor_provenance=actor,**update)
        c.rollback()
    with db.external_effect_guard(),db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert service.confirm_goal_behavior(CHILD,'goal',connection=c,actor_provenance=actor,**update)['status']=='recorded'
        c.rollback()
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM habit_experiment_event WHERE client_action_id='borrowed-correction'").fetchone()


def test_failed_correction_rolls_back_and_exact_retry_survives_restart(managed,tmp_path):
    db,_=history(managed)
    with host(db,tmp_path) as client:
        parent=client.post('/v1/parent/setup',json={'pin':'8362'});client.headers['X-HSC-Parent']=parent.json()['parent_token']
        item=client.get(ROOT+'/opportunities').json()['opportunities'][-1]
        first=_body(item,'failure-original')
        assert client.post(ROOT+'/confirmation',json=first).status_code==200
        current=next(i for i in client.get(ROOT+'/opportunities').json()['opportunities'] if i['opportunity_id']==item['opportunity_id'])
        correction={**first,'expected_version':current['binding_version'],'operation':'retract',
                    'client_action_id':'failure-retry','reason':'合成修正'}
        with db.connect() as c:
            before=daily_behavior_fingerprint(c,CHILD,'session')
            c.execute("CREATE TRIGGER synthetic_goal_failure BEFORE INSERT ON habit_experiment_event WHEN NEW.client_action_id='failure-retry' BEGIN SELECT RAISE(ABORT,'synthetic'); END");c.commit()
        assert client.post(ROOT+'/confirmation',json=correction).status_code==500
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM habit_experiment_event WHERE client_action_id='failure-retry'").fetchone()
            assert daily_behavior_fingerprint(c,CHILD,'session')==before
            c.execute('DROP TRIGGER synthetic_goal_failure');c.commit()
        accepted=client.post(ROOT+'/confirmation',json=correction)
        assert accepted.status_code==200 and not accepted.json()['replayed'],accepted.text
        event=accepted.json()['event_id']
    with host(db,tmp_path) as client:
        parent=client.post('/v1/parent/unlock',json={'pin':'8362'});client.headers['X-HSC-Parent']=parent.json()['parent_token']
        replay=client.post(ROOT+'/confirmation',json=correction)
        assert replay.status_code==200 and replay.json()['event_id']==event and replay.json()['replayed']
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE client_action_id='failure-retry'").fetchone()[0]==1
