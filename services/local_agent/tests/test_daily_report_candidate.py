"""Synthetic only; validates the candidate design, not a shippable mail flow."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from home_study_agent.database import Database
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.repository import utc_now

from home_study_agent import daily_report_observation as candidate
from home_study_agent.parent_reports import ParentReportService

@pytest.fixture
def synthetic(tmp_path):
    db = Database(tmp_path / 'synthetic.db')
    db.initialize()
    ReportDeliveryService(db, None).initialize()
    now = utc_now()
    with db.connect() as c:
        for child in ('a','b'):
            c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES(?,?,'初三','江苏','active',?,?)", (child,child,now,now))
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('s','a','active','study',?,?,'session')", (now,now))
        c.execute("INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,confidence,details_json,status,created_at,client_action_id) VALUES('art','a','s','document_ocr',NULL,'1. 求 2+2',.9,'{}','completed',?,'capture')", (now,))
        for child in ('a','b'):
            c.execute("INSERT INTO report_delivery_recipient(recipient_id,child_id,channel,address,label,consent_status,consented_at,created_at) VALUES(?,?,'email','parent@example.test','p','consented',?,?)", ('recipient-'+child,child,now,now))
        c.commit()
    m = DocumentMonitoringService(db)
    m.start('s','start')
    m.record_revision('s','art','page','revision')
    return db, datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def test_atomic_duplicate_and_independent_child(synthetic):
    db, day = synthetic
    def run(_):
        with db.connect() as c: return candidate.prepare(c,'a',day,'recipient-a')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0] == results[1]
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_observation_report_source').fetchone()[0] == 1
        assert '求 2+2' not in c.execute('SELECT source_ids_json FROM daily_observation_report_source').fetchone()[0]
        assert candidate.inspect_current(c,'a',day,'recipient-a') == {'current':True,'external_delivery_permitted':False}
        with pytest.raises(ValueError, match='RECIPIENT_NOT_AUTHORIZED'):
            candidate.prepare(c,'b',day,'recipient-a')
        assert candidate.prepare(c,'b',day,'recipient-b') != results[0]


def test_mutation_deletion_and_consent_fail_closed(synthetic):
    db, day = synthetic
    with db.connect() as c:
        candidate.prepare(c,'a',day,'recipient-a')
        c.execute("UPDATE capture_artifact SET extracted_text='different' WHERE artifact_id='art'")
        c.commit()
        assert not candidate.inspect_current(c,'a',day,'recipient-a')['current']
        with pytest.raises(ValueError, match='OBSERVATION_STALE'):
            candidate.prepare(c,'a',day,'recipient-a')
        c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,target_type,target_id,status,created_at,updated_at) VALUES('d','a','capture_artifact','capture_artifact','art','deleting',?,?)", (utc_now(),utc_now()))
        c.commit()
        with pytest.raises(ValueError, match='DELETION_PENDING'):
            candidate.inspect_current(c,'a',day,'recipient-a')
        with pytest.raises(ValueError, match='DELETION_PENDING'):
            candidate.prepare(c,'a',day,'recipient-a')
        c.execute("UPDATE deletion_job SET status='completed' WHERE deletion_id='d'")
        c.execute("UPDATE report_delivery_recipient SET consent_status='revoked',revoked_at=? WHERE recipient_id='recipient-a'", (utc_now(),))
        c.commit()
        with pytest.raises(ValueError, match='RECIPIENT_NOT_AUTHORIZED'):
            candidate.inspect_current(c,'a',day,'recipient-a')


def test_formal_unknown_blocks_candidate(synthetic):
    db, day = synthetic
    now = utc_now()
    with db.connect() as c:
        candidate.prepare(c,'a',day,'recipient-a')
        c.execute("INSERT INTO daily_report(report_id,session_id,child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at) VALUES('r','s','a',?,'fixture','{}','{}','{}',?)", (day,now))
        c.execute("""INSERT INTO parent_report_snapshot(snapshot_id,source_kind,source_id,report_id,child_id,report_type,source_version,template_version,evidence_version,facts_json,inferences_json,content_json,confidence_json,status,created_at) VALUES('snap','daily_report','r','r','a','daily','1','1','1','{}','{}','{}','{}','approved',?)""", (now,))
        c.execute("""INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at) VALUES('o','snap','recipient-a','email','old','unknown',?,?,?)""", (now,now,now))
        c.commit()
        with pytest.raises(ValueError, match='FORMAL_DELIVERY_EXISTS'):
            candidate.inspect_current(c,'a',day,'recipient-a')
        with pytest.raises(ValueError, match='FORMAL_DELIVERY_EXISTS'):
            candidate.prepare(c,'a',day,'recipient-a')
        assert c.execute('SELECT count(*) FROM daily_observation_report_outbox').fetchone()[0] == 1


def test_parent_service_persists_across_database_reopen_without_reflection(synthetic):
    db, day = synthetic
    service = ParentReportService(db)
    saved = service.prepare_daily_observation_source('a', day, 'recipient-a')
    assert saved['status'] == 'prepared_local_only'
    assert saved['external_delivery_permitted'] is False
    reopened = Database(db.path)
    reopened.initialize()
    assert ParentReportService(reopened).prepare_daily_observation_source('a', day, 'recipient-a') == saved
    assert ParentReportService(reopened).daily_observation_source_status('a', day, 'recipient-a')['current']
    with reopened.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_reflection').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM daily_report').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM report_delivery_outbox').fetchone()[0] == 0


def test_failed_outbox_write_rolls_back_source(synthetic):
    db, day = synthetic
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_daily_outbox BEFORE INSERT ON daily_observation_report_outbox BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        c.commit()
        with pytest.raises(Exception, match='synthetic failure'):
            candidate.prepare(c, 'a', day, 'recipient-a')
        assert c.execute('SELECT count(*) FROM daily_observation_report_source').fetchone()[0] == 0
