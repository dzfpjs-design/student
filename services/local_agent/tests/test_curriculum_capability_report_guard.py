"""Parent standard review serializes with disclosure of already frozen evidence."""
from concurrent.futures import ThreadPoolExecutor
import time

from home_study_agent.curriculum_internalization import CurriculumInternalizationService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository
from test_curriculum_catalog_reliability import catalog, run
from test_curriculum_supply import CHILD, prepared
from test_curriculum_supply_http import BASE, approved, post
from test_curriculum_first_source_http import app_client, auth


def test_parent_review_cannot_invalidate_frozen_report_while_sender_owns_guard(tmp_path,catalog):
    generated=run(catalog,'review-guard-fixture')[0]
    cap=generated['capability_id']
    study=tmp_path/'study';study.mkdir()
    db,_,payload=prepared(study)
    # Copy only synthetic catalogue fixtures; link the real review action to
    # the exact requirement used by the synthetic learning exercise.
    with db.connect() as c:
        c.execute('PRAGMA foreign_keys=OFF')
        c.execute('ATTACH DATABASE ? AS fixture_catalog',(str(catalog[0].path),))
        for table in ('curriculum_corpus','curriculum_topic','curriculum_internalization_job',
                      'curriculum_capability','curriculum_capability_source'):
            c.execute(f'INSERT OR IGNORE INTO main.{table} SELECT * FROM fixture_catalog.{table}')
        c.execute('INSERT INTO curriculum_capability_standard VALUES(?,?)',(cap,'req'))
        c.commit()
    review=CurriculumInternalizationService(db,None)
    assert review.review_capability(cap,'verify','合成复核')['maturity']=='verified'
    with app_client(study,db) as client:
        auth(client)
        source={k:v for k,v in payload.items() if k not in {'prompt','expected_answer'}}
        cid=approved(client,{'action_id':'http-generate','expected_version':0,'expected_scope_version':1,'source':source})
        item=post(client,f'/{cid}/publish',{'action_id':'p','expected_version':3})['diagnostic_item_id']
        sid=client.post('/v1/sessions',json={'child_id':CHILD,'client_action_id':'review-session'}).json()['session_id']
        base=f'/v1/sessions/{sid}/diagnostic-prompt'
        prompt=client.post(base+'/prepare',json={'track':'school','preferred_item_id':item,'client_action_id':'review-prompt'}).json()['prompt']
        answer=client.post(base+'/answer',json={'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
                           'answer':'5000','client_action_id':'review-answer'})
        assert answer.status_code==200 and answer.json()['attempt']['score_status']=='verified_local',answer.text
        repo=StudyRepository(db)
        with db.connect() as c:
            c.execute('UPDATE family_rule SET max_learning_minutes=30,task_segment_minutes=20,latest_end_time=NULL WHERE child_id=?',(CHILD,));c.commit()
        repo.transition(sid,'check_in','classroom_recall','开始学习','review-checkin')
        repo.add_recall(sid,'数学','复习了合成题。','review-recall')
        repo.transition(sid,'classroom_recall','homework_capture','记录完成','review-homework-stage')
        repo.add_homework(sid,'数学','合成练习',20,'written',None,None,None,None,None,'review-homework')
        workspace=repo.generate_plan(sid,'review-plan')
        repo.transition(sid,'plan_review','study','确认计划','review-study')
        plan_item=workspace['plan_items'][0]['plan_item_id']
        repo.start_plan_item(plan_item,'review-start');repo.complete_plan_item(plan_item,20,'review-complete')
        repo.transition(sid,'study','reflection','学习完成','review-reflection-stage')
        repo.complete_with_reflection(sid,'完成合成练习。','','可以','明天复习。',False,'review-reflection')
        summary,_=build_daily_report(repo.report_evidence(sid))
        report=repo.get_daily_report(repo.save_daily_report(sid,summary,
            {'action':{'what':'复习合成题','duration_minutes':10},'script':['先看第一步。'],'avoid':[]})['report_id'])
        reports=ParentReportService(db)
        snapshot=reports.approve(reports.create_from_daily(report)['snapshot_id'])['snapshot_id']
        def source_current():
            with db.connect() as c:
                return reports.snapshot_source_current(c,c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(snapshot,)).fetchone())
        assert source_current()
        second=repo.start_session(CHILD,'next-learning')['session_id']
        repo.transition(second,'check_in','classroom_recall','继续学习','next-checkin')
        url=f'/v1/parent/curriculum-capabilities/{cap}/review'
        command={'decision':'reject','note':'撤回合成课标依据'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            with db.external_effect_guard():
                replay=pool.submit(review.review_capability,cap,'verify','合成复核')
                assert replay.result(timeout=3)['maturity']=='verified'
                busy=client.post(url,json=command)
                assert busy.status_code==503 and busy.json()['detail']['code']=='CURRICULUM_BUSY',busy.text
                pending=pool.submit(client.post,url,json=command)
                time.sleep(.15)
                assert not pending.done() and source_current()
                normal=pool.submit(client.post,f'/v1/sessions/{second}/recalls',
                                   json={'subject':'数学','summary':'继续记录。','client_action_id':'next-recall'})
                assert normal.result(timeout=1.2).status_code==201
            response=pending.result(timeout=6)
        assert response.status_code==200,response.text
        assert not source_current()
        # A lost HTTP receipt can be replayed without waiting for another mail.
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.external_effect_guard():
                assert pool.submit(client.post,url,json=command).result(timeout=3).status_code==200


def test_same_review_is_not_replayed_after_its_source_changes(catalog):
    generated=run(catalog,'review-source-change')[0]
    db,_,_,_,service=catalog
    cap=generated['capability_id']
    service.review_capability(cap,'verify','相同审核说明')
    with db.connect() as c:
        before=c.execute("SELECT COUNT(*) FROM audit_event WHERE action='curriculum_capability_reviewed'").fetchone()[0]
        c.execute("UPDATE curriculum_topic SET learning_objective=learning_objective||'（更新）' WHERE topic_id IN (SELECT topic_id FROM curriculum_capability_source WHERE capability_id=?)",(cap,))
        c.commit()
    result=service.review_capability(cap,'verify','相同审核说明')
    assert result['maturity']=='verified' and result['review_provenance']=='current_sources_match'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='curriculum_capability_reviewed'").fetchone()[0]==before+1
