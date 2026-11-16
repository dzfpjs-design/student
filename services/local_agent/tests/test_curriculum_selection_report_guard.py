"""Changing the selected textbook cannot race an in-flight report."""
from concurrent.futures import ThreadPoolExecutor
import time

from home_study_agent.learning_feedback import session_report_feedback
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository
from test_curriculum_supply import CHILD, SCOPE, prepared
from test_curriculum_supply_http import BASE, approved, post
from test_curriculum_first_source_http import app_client, auth


def test_selected_textbook_change_waits_but_replay_does_not(tmp_path):
    db,service,payload=prepared(tmp_path)
    with db.connect() as c:
        scope=dict(SCOPE,textbook_source_id=c.execute('SELECT source_id FROM knowledge_source WHERE child_id=?',(CHILD,)).fetchone()[0])
    with app_client(tmp_path,db) as client:
        auth(client)
        source={k:v for k,v in payload.items() if k not in {'prompt','expected_answer'}}
        cid=approved(client,{'action_id':'http-generate','expected_version':0,'expected_scope_version':1,'source':source})
        item=post(client,f'/{cid}/publish',{'action_id':'p','expected_version':3})['diagnostic_item_id']
        sid=client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'selection-session'}).json()['session_id']
        base=f'/v1/sessions/{sid}/diagnostic-prompt'
        response=client.post(base+'/prepare',json={'track':'school','preferred_item_id':item,'client_action_id':'selection-prompt'})
        assert response.status_code==200,response.text
        prompt=response.json()['prompt']
        response=client.post(base+'/answer',json={'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
                             'answer':'5000','client_action_id':'selection-answer'})
        assert response.status_code==200 and response.json()['attempt']['score_status']=='verified_local',response.text
        with db.connect() as c:
            before=session_report_feedback(c,sid)
        assert before['items']
        repo=StudyRepository(db)
        with db.connect() as c:
            c.execute('UPDATE family_rule SET max_learning_minutes=30,task_segment_minutes=20,latest_end_time=NULL WHERE child_id=?',(CHILD,));c.commit()
        repo.transition(sid,'check_in','classroom_recall','开始学习','selection-checkin')
        repo.add_recall(sid,'数学','复习了合成题。','selection-recall')
        repo.transition(sid,'classroom_recall','homework_capture','记录完成','selection-homework-stage')
        repo.add_homework(sid,'数学','合成练习',20,'written',None,None,None,None,None,'selection-homework')
        workspace=repo.generate_plan(sid,'selection-plan')
        repo.transition(sid,'plan_review','study','确认计划','selection-study')
        plan_item=workspace['plan_items'][0]['plan_item_id']
        repo.start_plan_item(plan_item,'selection-start')
        repo.complete_plan_item(plan_item,20,'selection-complete')
        repo.transition(sid,'study','reflection','学习完成','selection-reflection-stage')
        repo.complete_with_reflection(sid,'完成合成练习。','','可以','明天复习。',False,'selection-reflection')
        summary,_=build_daily_report(repo.report_evidence(sid))
        report=repo.get_daily_report(repo.save_daily_report(sid,summary,
            {'action':{'what':'复习合成题','duration_minutes':10},'script':['先看第一步。'],'avoid':[]})['report_id'])
        reports=ParentReportService(db)
        snapshot=reports.approve(reports.create_from_daily(report)['snapshot_id'])['snapshot_id']
        def source_current():
            with db.connect() as c:
                row=c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot,)).fetchone()
                return reports.snapshot_source_current(c,row)
        assert source_current()
        command={'scope':{**scope,'semester':'下学期'},'action_id':'selection-next-term','expected_version':1}
        with ThreadPoolExecutor(max_workers=2) as pool:
            with db.external_effect_guard():
                # A successful prior action is a read-only replay and must not
                # wait for another thread's ongoing disclosure.
                replay=pool.submit(service.select_curriculum,CHILD,scope,actor='parent',action_id='scope',expected_version=0)
                assert replay.result(timeout=3)['version']==1
                busy=client.post(BASE+'/select',json=command)
                assert busy.status_code==503 and busy.json()['detail']['code']=='CURRICULUM_BUSY',busy.text
                pending=pool.submit(client.post,BASE+'/select',json=command)
                time.sleep(.15)
                assert not pending.done()
                assert source_current()
                with db.connect() as c:
                    assert session_report_feedback(c,sid)['evidence_fingerprint']==before['evidence_fingerprint']
                    assert c.execute('SELECT MAX(version) FROM curriculum_supply_selection WHERE child_id=?',(CHILD,)).fetchone()[0]==1
            response=pending.result(timeout=6)
        assert response.status_code==200,response.text
        with db.connect() as c:
            after=session_report_feedback(c,sid)
            assert c.execute('SELECT MAX(version) FROM curriculum_supply_selection WHERE child_id=?',(CHILD,)).fetchone()[0]==2
        assert after['evidence_fingerprint']!=before['evidence_fingerprint']
        assert any(item['reason']=='source_unqualified_or_reference_only' for item in after['excluded'])
        assert not source_current()
