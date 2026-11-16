"""Reusable single-DB, synthetic full-source fixture; never transmits email."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from zipfile import ZipFile

import pytest
from home_study_agent.database import Database
from home_study_agent.daily_diagnostic_bundle import build_daily_diagnostic_bundle
from home_study_agent.daily_report_frozen_snapshot import _decrypt
from home_study_agent.daily_report_observation import prepare
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.repository import StudyRepository, utc_now
from home_study_agent.reports import build_daily_report

SYNTHETIC_ADDRESS='parent@example.test'
SYNTHETIC_KEY=b's'*32
OCR_TEXT='合成数学页面：求 2+2'
CANARY='PRIVATE_SYNTHETIC_TRANSCRIPT_SHOULD_NOT_BE_IN_ZIP'


@pytest.fixture
def synthetic_combined_family(tmp_path):
    db=Database(tmp_path/'family.db'); db.initialize()
    repo=StudyRepository(db); repo.seed_family(); LearningStateService(db).seed_catalog()
    child='child-junior-3'
    with db.connect() as c:
        c.execute("UPDATE family_rule SET max_learning_minutes=30,task_segment_minutes=20,latest_end_time=NULL WHERE child_id=?",(child,)); c.commit()
    session=repo.start_session(child,'fixture-start')['session_id']
    repo.transition(session,'check_in','classroom_recall','开始学习','fixture-checkin')
    repo.add_recall(session,'数学','今天学习判别式。','fixture-recall')
    repo.transition(session,'classroom_recall','homework_capture','回忆已记录','fixture-homework-stage')
    repo.add_homework(session,'数学','判别式练习',20,'written',None,None,None,None,None,'fixture-homework')
    workspace=repo.generate_plan(session,'fixture-plan')
    item=workspace['plan_items'][0]
    repo.transition(session,'plan_review','study','确认计划','fixture-study')
    monitor=DocumentMonitoringService(db); monitor.start(session,'fixture-monitor')
    with db.connect() as c:
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,
            extracted_text,confidence,details_json,status,created_at,client_action_id)
            VALUES('fixture-art',?,?,'document_ocr',NULL,?,.9,'{}','completed',?,'fixture-capture')""",
            (child,session,OCR_TEXT,utc_now())); c.commit()
    revision=monitor.record_revision(session,'fixture-art','fixture-page','fixture-revision')
    repo.start_plan_item(item['plan_item_id'],'fixture-task-start')
    repo.complete_plan_item(item['plan_item_id'],20,'fixture-task-complete')
    repo.transition(session,'study','reflection','计划已执行','fixture-reflection-stage')
    repo.complete_with_reflection(session,'记录了判别式练习的步骤。','','还可以','明天复核第一步。',False,'fixture-reflection')
    evidence=repo.report_evidence(session)
    summary, _=build_daily_report(evidence)
    coaching={'action':{'what':'复习判别式第一步','duration_minutes':10},'script':['先说出已确定的一步。'],'avoid':[]}
    saved=repo.save_daily_report(session,summary,coaching)
    report=repo.get_daily_report(saved['report_id']); day=report['report_date']
    reports=ParentReportService(db)
    formal=reports.approve(reports.create_from_daily(report)['snapshot_id'])
    delivery=ReportDeliveryService(db,SYNTHETIC_KEY); delivery.initialize()
    recipient=delivery.add_recipient(child,'email',SYNTHETIC_ADDRESS,'合成家长',True)['recipient_id']
    with db.connect() as c: source=prepare(c,child,day,recipient)
    visual=delivery.freeze_daily_observation(child,day,recipient,source)['snapshot_id']
    telemetry=PilotTestTelemetryService(db,tmp_path); telemetry.initialize()
    telemetry.set_diagnostic_consent(SYNTHETIC_ADDRESS,consented=True)
    now=datetime.now(timezone.utc); logdir=tmp_path/'diagnostics'/'logs'; logdir.mkdir(parents=True)
    event={'privacy_policy_version':'diagnostic_metadata_v2','timestamp':now.isoformat(),
           'source':'local_service','level':'info','event':'local_api_request',
           'correlation_id':'f210d041-a277-42a1-b117-033b8511af48',
           'details':{'method':'POST','route_template':'/v1/agent-turns/{turn_id}/execute',
                      'statusCode':409,'durationMs':23.5,'transcript':CANARY}}
    (logdir/'runtime.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
    start,end=now-timedelta(hours=1),now+timedelta(hours=1)
    bundle=build_daily_diagnostic_bundle(tmp_path,start_utc=start,end_utc=end)
    return {'db':db,'repo':repo,'reports':reports,'delivery':delivery,'telemetry':telemetry,
            'child':child,'session':session,'day':day,'formal':formal,'recipient':recipient,
            'visual':visual,'revision':revision,'bundle':bundle}


def test_combined_fixture_has_one_real_reflection_approved_formal_visual_and_safe_logs(synthetic_combined_family):
    f=synthetic_combined_family; db=f['db']; formal=f['formal']
    assert formal['status']=='approved' and formal['child_id']==f['child']
    assert f['reports'].validate_current_for_delivery(formal['snapshot_id'])
    assert f['delivery'].inspect_frozen_daily_observation(f['child'],f['day'],f['recipient'],f['visual'])['current']
    with db.connect() as c:
        reflection=c.execute('SELECT * FROM daily_reflection WHERE session_id=?',(f['session'],)).fetchone()
        assert reflection and reflection['child_id']==f['child'] and reflection['skipped']==0
        assert c.execute("SELECT COUNT(*) FROM document_page_revision WHERE child_id=? AND session_id=? AND artifact_id='fixture-art'",(f['child'],f['session'])).fetchone()[0]==1
        row=c.execute('SELECT * FROM daily_observation_report_snapshot WHERE snapshot_id=?',(f['visual'],)).fetchone()
        assert OCR_TEXT in _decrypt(row,SYNTHETIC_KEY)['details']['pages'][0]['text']
        assert c.execute('SELECT status FROM pilot_diagnostic_recipient_consent WHERE recipient=?',(SYNTHETIC_ADDRESS,)).fetchone()[0]=='consented'
        assert c.execute('SELECT COUNT(*) FROM pilot_test_digest_outbox').fetchone()[0]==0
    with ZipFile(BytesIO(f['bundle'].content)) as archive:
        assert set(archive.namelist())=={'manifest.json','events.jsonl'}
        events=archive.read('events.jsonl').decode()
        assert 'local_api_request' in events and CANARY not in events
