"""Synthetic legacy receipts; never contacts a mail server."""
import pytest
from home_study_agent.daily_report_observation import _authority
from home_study_agent.daily_report_delivery_state import identity_key
from home_study_agent.report_delivery import ReportDeliveryService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import utc_now
from test_daily_report_candidate import synthetic


def legacy(db, day, status):
    now=utc_now()
    with db.connect() as c:
        c.execute("INSERT INTO daily_report(report_id,session_id,child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at) VALUES('r','s','a',?,'fixture','{}','{}','{}',?)", (day,now))
        c.execute("INSERT INTO parent_report_snapshot(snapshot_id,source_kind,source_id,report_id,child_id,report_type,source_version,template_version,evidence_version,facts_json,inferences_json,content_json,confidence_json,status,created_at) VALUES('snap','daily_report','r','r','a','daily','1','1','1','{}','{}','{}','{}','approved',?)", (now,))
        c.execute("INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at) VALUES('old','snap','recipient-a','email','old',?,?,?,'2020-01-01 00:00:00')", (status,now,now)); c.commit()


@pytest.mark.parametrize('status',['sent','unknown','unknown_acknowledged','processing'])
def test_report_deletion_blocks_when_legacy_receipt_erased(synthetic,tmp_path,status):
    db,day=synthetic
    legacy(db,day,status)
    privacy=PrivacyLifecycleService(db,tmp_path,b's'*32); privacy.initialize()
    assert privacy.delete_child_scope('a','reports','确认删除','delete-report')['status']=='completed'
    service=ReportDeliveryService(db,b's'*32)
    recipient=service.add_recipient('a','email','parent@example.test','parent',True)['recipient_id']
    with db.connect() as c:
        assert tuple(c.execute('SELECT blocked,reason FROM daily_observation_delivery_barrier').fetchone()) == (1,'DELIVERY_HISTORY_ERASED')
        with pytest.raises(ValueError,match='RECONCILIATION_REQUIRED'):
            _authority(c,'a',day,recipient)


def test_retention_keeps_opaque_same_day_identity(synthetic):
    db,day=synthetic
    legacy(db,day,'sent')
    service=ReportDeliveryService(db,b's'*32)
    service.update_preferences('a','21:00','07:00',7)
    assert service.purge_expired() == 1
    token=identity_key(b's'*32,'a',day,'parent@example.test')
    with db.connect() as c:
        assert tuple(c.execute('SELECT child_id,status FROM daily_observation_delivery_ledger WHERE identity=?',(token,)).fetchone()) == ('a','sent')
        assert c.execute('SELECT blocked FROM daily_observation_delivery_barrier').fetchone()[0] == 0
        with pytest.raises(ValueError,match='PRIOR_DELIVERY_UNRESOLVED_OR_SENT'):
            _authority(c,'a',day,'recipient-a')


def test_retention_without_key_sets_barrier(synthetic):
    db,day=synthetic
    legacy(db,day,'sent')
    service=ReportDeliveryService(db,None)
    service.update_preferences('a','21:00','07:00',7)
    assert service.purge_expired() == 1
    with db.connect() as c:
        assert c.execute('SELECT blocked FROM daily_observation_delivery_barrier').fetchone()[0] == 1


@pytest.mark.parametrize('status',['failed','cancelled'])
def test_retention_preserves_uncertain_attempted_history(synthetic,status):
    db,day=synthetic
    legacy(db,day,status)
    with db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET attempt_count=1 WHERE delivery_id='old'")
        c.commit()
    service=ReportDeliveryService(db,b's'*32)
    service.update_preferences('a','21:00','07:00',7)
    assert service.purge_expired() == 1
    token=identity_key(b's'*32,'a',day,'parent@example.test')
    with db.connect() as c:
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger WHERE identity=?',(token,)).fetchone()[0] == 'unknown'
        with pytest.raises(ValueError,match='PRIOR_DELIVERY_UNRESOLVED_OR_SENT'):
            _authority(c,'a',day,'recipient-a')
