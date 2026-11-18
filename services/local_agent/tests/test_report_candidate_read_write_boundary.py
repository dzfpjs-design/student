"""Historical evidence must stay scoped and report writes must recheck it."""
import pytest
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository, ReportNotReadyError
from home_study_agent.reports import build_daily_report
from test_report_delivery import _pending_report_job

SID='pending-report-session'
CHILD='child-primary-4'


@pytest.fixture
def report_source(tmp_path):
    db=Database(tmp_path/'boundary.db');db.initialize();_pending_report_job(db)
    with db.connect() as c:
        now=c.execute('SELECT started_at FROM daily_session WHERE session_id=?',(SID,)).fetchone()[0]
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES('plan',?,?,substr(?,1,10),'completed',15,'test',1,?,'plan')""",(CHILD,SID,now,now))
        c.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,status,details_json,created_at,client_action_id)
            VALUES('artifact',?,?,'document_ocr','completed','{}',?,'artifact')""",(CHILD,SID,now))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,completion_artifact_id)
            VALUES('task','plan','数学','合成任务',15,1,'completed',0,'artifact')""")
        c.commit()
    return db,StudyRepository(db)


def _save(repo):
    evidence=repo.report_evidence(SID); summary,coaching=build_daily_report(evidence)
    return repo.save_daily_report(SID,summary,coaching)


def test_snapshot_source_qualification_uses_owned_read_snapshot(report_source,monkeypatch):
    from home_study_agent.parent_reports import ParentReportService
    db,repo=report_source; report=_save(repo)
    service=ParentReportService(db);service.initialize()
    original=service.source_qualification_fingerprint
    seen=[]
    def checked(connection,*args):
        seen.append(connection.in_transaction)
        return original(connection,*args)
    monkeypatch.setattr(service,'source_qualification_fingerprint',checked)
    snapshot=service.create_from_daily(report)
    assert snapshot['snapshot_id'] and seen and all(seen)


def test_cloud_report_authority_opens_a_read_snapshot_for_source(report_source,monkeypatch):
    from home_study_agent.cloud_parent_reports import ParentReportGenerationAuthority
    _,repo=report_source
    evidence=repo.report_evidence(SID); summary,baseline=build_daily_report(evidence)
    version=ParentReportGenerationAuthority.capture_source(repo,SID)
    original=ParentReportGenerationAuthority.assert_source
    seen=[]
    def checked(self,connection):
        seen.append(connection.in_transaction)
        return original(self,connection)
    monkeypatch.setattr(ParentReportGenerationAuthority,'assert_source',checked)
    ParentReportGenerationAuthority(repo,evidence,summary,baseline,lambda:evidence,version)
    assert seen == [True]


def test_unconfirmed_source_after_report_does_not_reveal_or_rewrite_historical_report(report_source):
    db,repo=report_source; report=_save(repo)
    with db.connect() as c:
        before=tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='artifact'");c.commit()
    with pytest.raises(ReportNotReadyError,match='未确认'):
        repo.get_daily_report(report['report_id'])
    with db.connect() as c:
        assert tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())==before


def test_source_changes_inside_save_transaction_block_report_and_rollback(report_source):
    db,repo=report_source
    evidence=repo.report_evidence(SID);summary,coaching=build_daily_report(evidence)
    def mutate(c):
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='artifact'")
    with pytest.raises(ReportNotReadyError,match='未确认|已变化'):
        repo.save_daily_report(SID,summary,coaching,pre_persist=mutate)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_report WHERE session_id=?',(SID,)).fetchone()[0]==0
        assert c.execute("SELECT artifact_type FROM capture_artifact WHERE artifact_id='artifact'").fetchone()[0]=='document_ocr'


def test_foreign_child_plan_bound_to_session_blocks_read_and_generation(report_source):
    db,repo=report_source; report=_save(repo)
    with db.connect() as c:
        now=c.execute('SELECT started_at FROM daily_session WHERE session_id=?',(SID,)).fetchone()[0]
        before=tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES('foreign-plan','child-junior-3',?,substr(?,1,10),'completed',10,'test',1,?,'foreign-plan')""",(SID,now,now))
        c.execute("INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position) VALUES('foreign','foreign-plan','数学','另一孩子的任务',10,1,'completed',0)")
        c.commit()
    for call in (lambda:repo.report_evidence(SID),lambda:repo.get_daily_report(report['report_id'])):
        with pytest.raises(ReportNotReadyError,match='不匹配|不一致'):
            call()
    with db.connect() as c:
        assert tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())==before


def test_still_valid_source_change_at_commit_does_not_persist_stale_summary(report_source):
    db,repo=report_source
    evidence=repo.report_evidence(SID);summary,coaching=build_daily_report(evidence)
    def mutate(c):
        c.execute("UPDATE capture_artifact SET status='quality_warning' WHERE artifact_id='artifact'")
    with pytest.raises(ReportNotReadyError,match='已变化'):
        repo.save_daily_report(SID,summary,coaching,pre_persist=mutate)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_report WHERE session_id=?',(SID,)).fetchone()[0]==0
        assert c.execute("SELECT status FROM capture_artifact WHERE artifact_id='artifact'").fetchone()[0]=='completed'


def test_task_changes_after_save_preserve_original_report(report_source):
    db,repo=report_source;report=_save(repo)
    with db.connect() as c:
        before=tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())
        c.execute("UPDATE plan_item SET title='修订后的合成任务' WHERE plan_item_id='task'");c.commit()
    with pytest.raises(ReportNotReadyError,match='已变化'):
        repo.get_daily_report(report['report_id'])
    with db.connect() as c:
        assert tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())==before


def test_rejected_current_source_does_not_mutate_approved_historical_snapshot(report_source):
    from home_study_agent.parent_reports import ParentReportService
    db,repo=report_source;report=_save(repo)
    snapshots=ParentReportService(db);snapshots.initialize()
    original=snapshots.approve(snapshots.create_from_daily(report)['snapshot_id'])
    from home_study_agent.report_delivery import ReportDeliveryService
    delivery=ReportDeliveryService(db,b'1'*32);delivery.initialize()
    recipient=delivery.add_recipient(CHILD,'email','parent@example.test','家长',consented=True)
    outbox=delivery.enqueue(original['snapshot_id'],recipient['recipient_id'])
    with db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='sent',sent_at='2026-01-01' WHERE delivery_id=?",(outbox['delivery_id'],))
        before=tuple(c.execute('SELECT status,sent_at FROM report_delivery_outbox WHERE delivery_id=?',(outbox['delivery_id'],)).fetchone())
        snapshot_before=tuple(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(original['snapshot_id'],)).fetchone())
        c.commit()
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='artifact'");c.commit()
    with pytest.raises(ReportNotReadyError,match='未确认'):
        repo.get_daily_report(report['report_id'])
    assert snapshots.get(original['snapshot_id'])['status'] == 'approved'
    with db.connect() as c:
        assert tuple(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(original['snapshot_id'],)).fetchone())==snapshot_before
        assert tuple(c.execute('SELECT status,sent_at FROM report_delivery_outbox WHERE delivery_id=?',(outbox['delivery_id'],)).fetchone())==before


def test_report_child_must_match_session_owner(report_source):
    db,repo=report_source;report=_save(repo)
    with db.connect() as c:
        c.execute("UPDATE daily_report SET child_id='child-junior-3' WHERE report_id=?",(report['report_id'],));c.commit()
    with pytest.raises(ReportNotReadyError,match='不匹配'):
        repo.get_daily_report(report['report_id'])


def test_summary_built_before_source_changes_is_rebuilt_before_save(report_source):
    db,repo=report_source
    evidence=repo.report_evidence(SID);summary,coaching=build_daily_report(evidence)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET title='更新后的合成任务' WHERE plan_item_id='task'");c.commit()
    saved=repo.save_daily_report(SID,summary,coaching)
    assert saved['source']=='local_rules_revalidated_v3'
    assert saved['evidence']['report_task_source_fingerprint'] != evidence['report_task_source_fingerprint']
    assert '更新后的合成任务' in str(saved['summary'])


def test_legacy_report_without_task_binding_stays_unverified_without_rewriting_history(report_source):
    import json
    from home_study_agent.parent_reports import ParentReportService
    db,repo=report_source;report=_save(repo)
    snapshots=ParentReportService(db);snapshots.initialize()
    original=snapshots.approve(snapshots.create_from_daily(report)['snapshot_id'])
    with db.connect() as c:
        row=c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone()
        evidence=json.loads(row[0]);evidence.pop('report_task_source_fingerprint')
        c.execute('UPDATE daily_report SET evidence_json=? WHERE report_id=?',(json.dumps(evidence,ensure_ascii=False),report['report_id']))
        c.execute("UPDATE plan_item SET title='新明细与旧日报不相同' WHERE plan_item_id='task'");c.commit()
        before=tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())
    with pytest.raises(ReportNotReadyError,match='历史|旧版'):
        repo.get_daily_report(report['report_id'])
    issues=[]; assert report['report_id'] not in [r['report_id'] for r in repo.list_daily_reports(CHILD,source_issues=issues)]
    assert any('旧版' in issue['message'] and '无法核验' in issue['message'] for issue in issues)
    assert snapshots.get(original['snapshot_id'])['status']=='approved'
    assert not snapshots.validate_current_for_delivery(original['snapshot_id'])
    with db.connect() as c:
        assert tuple(c.execute('SELECT evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone())==before
        assert ParentReportService.source_qualification_fingerprint(c,CHILD,'daily_report',report['report_id']) is None
        assert not ParentReportService.snapshot_source_current(c,c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(original['snapshot_id'],)).fetchone())


def test_snapshot_source_qualification_rejects_unconfirmed_task_after_approval(report_source):
    from home_study_agent.parent_reports import ParentReportService
    db,repo=report_source;report=_save(repo); snapshots=ParentReportService(db);snapshots.initialize()
    original=snapshots.approve(snapshots.create_from_daily(report)['snapshot_id'])
    with db.connect() as c:
        assert ParentReportService.source_qualification_fingerprint(c,CHILD,'daily_report',report['report_id']) is not None
        c.execute("UPDATE capture_artifact SET artifact_type='document_ocr_candidate' WHERE artifact_id='artifact'");c.commit()
        assert ParentReportService.source_qualification_fingerprint(c,CHILD,'daily_report',report['report_id']) is None
        assert not ParentReportService.snapshot_source_current(c,c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',(original['snapshot_id'],)).fetchone())


def test_legacy_unverified_approved_snapshot_cannot_be_emailed(report_source):
    import json
    from home_study_agent.parent_reports import ParentReportService
    from home_study_agent.report_delivery import ReportDeliveryService
    from test_report_delivery import RecordingEmail
    db,repo=report_source;report=_save(repo)
    snapshots=ParentReportService(db);snapshots.initialize()
    original=snapshots.approve(snapshots.create_from_daily(report)['snapshot_id'])
    delivery=ReportDeliveryService(db,b'1'*32);delivery.initialize()
    recipient=delivery.add_recipient(CHILD,'email','parent@example.test','家长',consented=True)
    delivery.save_channel_config('email',{'host':'127.0.0.1','from_address':'report@example.test','allow_external_delivery':True})
    sink=RecordingEmail();delivery.adapters['email']=sink
    queued=delivery.enqueue(original['snapshot_id'],recipient['recipient_id'],'2000-01-01T00:00:00+00:00')
    with db.connect() as c:
        raw=c.execute('SELECT evidence_json FROM daily_report WHERE report_id=?',(report['report_id'],)).fetchone()[0]
        evidence=json.loads(raw);evidence.pop('report_task_source_fingerprint')
        c.execute('UPDATE daily_report SET evidence_json=? WHERE report_id=?',(json.dumps(evidence),report['report_id']));c.commit()
    delivery.process_due()
    assert sink.messages == []
    assert delivery.get_delivery(queued['delivery_id'])['status'] != 'sent'
    with db.connect() as c:
        assert c.execute('SELECT status FROM parent_report_snapshot WHERE snapshot_id=?',(original['snapshot_id'],)).fetchone()[0]=='approved'


def test_same_session_homework_change_invalidates_saved_task_source(report_source):
    db,repo=report_source
    with db.connect() as c:
        c.execute("""INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,
          expected_minutes,source_artifact_id,confirmation_status,status,created_at,client_action_id)
          VALUES('homework',?,?,'数学','written','合成作业',15,'artifact','child_confirmed','pending','2026-01-01','homework')""",(CHILD,SID))
        c.execute("UPDATE plan_item SET homework_id='homework',source_type='school_homework',source_ref='homework' WHERE plan_item_id='task'")
        c.commit()
    report=_save(repo)
    with db.connect() as c:
        c.execute("UPDATE homework_item SET title='已修订的合成作业' WHERE homework_id='homework'");c.commit()
    with pytest.raises(ReportNotReadyError,match='已变化|未确认'):
        repo.get_daily_report(report['report_id'])
