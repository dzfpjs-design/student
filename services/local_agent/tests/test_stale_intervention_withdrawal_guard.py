from test_behavior_goal_report_guard import managed, CHILD
from test_behavior_goal_confirmation import history
from test_behavior_goal_http import host
from home_study_agent.repository import StudyRepository,utc_now
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.reports import build_daily_report
from home_study_agent.behavior_model.service import BehaviorModelService
from concurrent.futures import ThreadPoolExecutor

def test_withdraw_under_send_guard(managed,tmp_path):
    db,_=history(managed); repo=StudyRepository(db)
    with host(db,tmp_path) as client:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id='session'",(utc_now(),))
            c.execute("UPDATE family_rule SET max_learning_minutes=30,task_segment_minutes=20,latest_end_time=NULL WHERE child_id=?",(CHILD,)); c.commit()
        sid=repo.start_session(CHILD,'withdraw-report-session')['session_id']
        repo.transition(sid,'check_in','classroom_recall','开始学习','withdraw-report-checkin')
        repo.add_recall(sid,'数学','复习合成题。','withdraw-report-recall')
        repo.transition(sid,'classroom_recall','homework_capture','记录完成','withdraw-report-homework-stage')
        repo.add_homework(sid,'数学','合成练习',20,'written',None,None,None,None,None,'withdraw-report-homework')
        workspace=repo.generate_plan(sid,'withdraw-report-plan')
        repo.transition(sid,'plan_review','study','确认计划','withdraw-report-study')
        item=workspace['plan_items'][0]['plan_item_id'];repo.start_plan_item(item,'withdraw-report-start')
        with db.connect() as c:
            c.execute("INSERT INTO intervention(intervention_id,child_id,session_id,decided_at,candidate_state,message,intervention_type,status,support_basis_json) VALUES('synthetic-reminder',?,?,?,'blocked_candidate','synthetic','contextual_habit_ask_first','active','{}')",(CHILD,sid,utc_now()));c.commit()
        repo.complete_plan_item(item,20,'withdraw-report-complete')
        repo.transition(sid,'study','reflection','学习完成','withdraw-report-reflection-stage')
        repo.complete_with_reflection(sid,'完成合成练习。','','可以','明天复习。',False,'withdraw-report-reflection')
        summary,_=build_daily_report(repo.report_evidence(sid))
        report=repo.get_daily_report(repo.save_daily_report(sid,summary,{'action':{'what':'复习合成题','duration_minutes':10},'script':['先看第一步。'],'avoid':[]})['report_id'])
        reports=ParentReportService(db);snap=reports.approve(reports.create_from_daily(report)['snapshot_id'])['snapshot_id']
        def current():
            with db.connect() as c:return reports.snapshot_source_current(c,c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snap,)).fetchone())
        assert current()
        child_session=repo.start_session(CHILD,'withdraw-next-session')['session_id']
        repo.transition(child_session,'check_in','classroom_recall','继续学习','withdraw-next-check')
        with ThreadPoolExecutor(max_workers=2) as pool:
            with db.external_effect_guard():
                response=pool.submit(client.get,f'/v1/sessions/{sid}/behavior/interventions/pending').result(timeout=3)
                assert response.status_code == 200 and response.json()['status']=='no_pending', response.text
                again=pool.submit(client.get,f'/v1/sessions/{sid}/behavior/interventions/pending').result(timeout=3)
                assert again.status_code==200 and again.json()['status']=='no_pending'
                with db.connect() as c:
                    row=c.execute("SELECT * FROM intervention WHERE intervention_id='synthetic-reminder'").fetchone()
                    assert row['status']=='active' and row['withdrawal_reason']=='habit_support_source_changed_pending'
                    from home_study_agent.agent_runtime import AgentRuntimeStore
                    from datetime import datetime,timezone
                    assert not AgentRuntimeStore._behavior_reminder_current(c,row,datetime.now(timezone.utc))
                assert not current()
                child=pool.submit(client.post,f'/v1/sessions/{child_session}/recalls',json={
                    'subject':'数学','summary':'继续记录。','client_action_id':'child-while-withdraw-pending'}).result(timeout=2)
                assert child.status_code==201,child.text
                assert pool.submit(BehaviorModelService(db).process_pending_intervention_withdrawals).result(timeout=4)['waiting']==1
        recovered=BehaviorModelService(db)
        assert recovered.process_pending_intervention_withdrawals()['completed']==1
        assert recovered.process_pending_intervention_withdrawals()['completed']==0
        with db.connect() as c:
            row=c.execute("SELECT status,withdrawal_reason FROM intervention WHERE intervention_id='synthetic-reminder'").fetchone()
            assert tuple(row)==('withdrawn','habit_support_source_changed')


def test_failed_final_withdrawal_preserves_pending_for_retry(managed,tmp_path):
    db,_=history(managed)
    with db.connect() as c:
        c.execute("INSERT INTO intervention(intervention_id,child_id,session_id,decided_at,candidate_state,message,intervention_type,status,support_basis_json,withdrawal_reason) VALUES('retry-reminder',?,'session',?,'blocked_candidate','synthetic','contextual_habit_ask_first','active','{}','habit_support_source_changed_pending')",(CHILD,utc_now()))
        c.execute("CREATE TRIGGER synthetic_intervention_failure BEFORE UPDATE OF status ON intervention WHEN NEW.status='withdrawn' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        c.commit()
    service=BehaviorModelService(db)
    assert service.process_pending_intervention_withdrawals()['failed']==1
    with db.connect() as c:
        assert tuple(c.execute("SELECT status,withdrawal_reason FROM intervention WHERE intervention_id='retry-reminder'").fetchone())==('active','habit_support_source_changed_pending')
        c.execute('DROP TRIGGER synthetic_intervention_failure');c.commit()
    assert BehaviorModelService(db).process_pending_intervention_withdrawals()['completed']==1


def test_decision_path_queues_stale_reminder_without_waiting_for_send(managed):
    from test_behavior_accepted_habit_support import active, offer, NOW
    db, service=active(managed)
    offered=offer(service)
    assert offered['strategy']=='contextual_habit_ask_first'
    with db.connect() as c:
        c.execute("UPDATE plan_item SET subject='english' WHERE plan_item_id='task'"); c.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard():
            result=pool.submit(service.decide_intervention,CHILD,'session',NOW).result(timeout=3)
            assert result['status'] in ('suppressed','no_action')
            with db.connect() as c:
                row=c.execute('SELECT status,withdrawal_reason FROM intervention WHERE intervention_id=?',(offered['intervention_id'],)).fetchone()
                assert tuple(row)==('active','habit_support_source_changed_pending')
            pool.submit(service.decide_intervention,CHILD,'session',NOW).result(timeout=3)
    assert service.process_pending_intervention_withdrawals()['completed']==1
    assert service.process_pending_intervention_withdrawals()['completed']==0


def test_pending_admission_failure_rolls_back_and_same_get_retries(managed,tmp_path):
    db,_=history(managed)
    with host(db,tmp_path) as client:
        with db.connect() as c:
            c.execute("INSERT INTO intervention(intervention_id,child_id,session_id,decided_at,candidate_state,message,intervention_type,status,support_basis_json) VALUES('queue-failure',?,'session',?,'blocked_candidate','synthetic','contextual_habit_ask_first','active','{}')",(CHILD,utc_now()))
            c.execute("CREATE TRIGGER synthetic_pending_failure BEFORE UPDATE OF withdrawal_reason ON intervention WHEN NEW.withdrawal_reason='habit_support_source_changed_pending' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
            c.commit()
        url='/v1/sessions/session/behavior/interventions/pending'
        assert client.get(url).status_code==500
        with db.connect() as c:
            assert tuple(c.execute("SELECT status,withdrawal_reason FROM intervention WHERE intervention_id='queue-failure'").fetchone())==('active',None)
            c.execute('DROP TRIGGER synthetic_pending_failure');c.commit()
        assert client.get(url).json()['status']=='no_pending'
        with db.connect() as c:
            assert tuple(c.execute("SELECT status,withdrawal_reason FROM intervention WHERE intervention_id='queue-failure'").fetchone())==('active','habit_support_source_changed_pending')
