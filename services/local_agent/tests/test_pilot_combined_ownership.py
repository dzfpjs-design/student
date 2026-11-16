"""Synthetic joint-log ownership: old daily sender must never take a batch."""
from datetime import datetime, timezone
import sqlite3
import pytest
from home_study_agent.database import Database
from home_study_agent.pilot_test_telemetry import PilotDigestClaim
from test_pilot_test_telemetry import service_for

DAY='2026-11-04'
ADDRESS='parent@example.test'
NOW=datetime(2026,11,4,16,0,tzinfo=timezone.utc)


def schema(db):
    from home_study_agent.daily_report_combined_schema import BATCH_SCHEMA, INDEX_SCHEMA
    with db.connect() as c:
        c.execute(BATCH_SCHEMA)
        columns={r['name'] for r in c.execute('PRAGMA table_info(pilot_test_digest_outbox)')}
        if 'combined_batch_id' not in columns:
            c.execute('ALTER TABLE pilot_test_digest_outbox ADD COLUMN combined_batch_id TEXT REFERENCES combined_diagnostic_batch(batch_id)')
        c.execute(INDEX_SCHEMA)
        c.commit()


def linked(db):
    with db.connect() as c:
        c.execute("INSERT INTO combined_diagnostic_batch VALUES('batch',?,'hmac',?,'digest','ziphash','members',2,1,'active','2026-01-01','2026-01-01')", (DAY,DAY))
        c.execute('''INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,
          digest_sha256,attachment_content,attachment_sha256,created_at,updated_at,claim_token,combined_batch_id)
          VALUES(?,?,'processing',1,'digest',X'01','ziphash','2026-01-01','2026-01-01','old-token','batch')''', (DAY,ADDRESS))
        c.commit()


def test_bound_daily_never_reclaimed_or_finished_by_old_sender(tmp_path):
    db,service=service_for(tmp_path); schema(db)
    service.set_diagnostic_consent(ADDRESS,consented=True); linked(db)
    with pytest.raises(ValueError,match='PILOT_COMBINED_DELIVERY_OWNED'):
        service.claim_due(ADDRESS,now=NOW,send_hour=22)
    for action in (
        lambda: service.processing_claim(DAY,ADDRESS,'digest'),
        lambda: service.reserve_submission(DAY,ADDRESS,'digest','old-token'),
        lambda: service.record_result(DAY,ADDRESS,status='failed',claim_token='old-token'),
        lambda: service.finish_submission(DAY,ADDRESS,'digest',submission_token='old-token',status='sent'),
    ):
        with pytest.raises(ValueError,match='PILOT_COMBINED_DELIVERY_OWNED'): action()
    claim=PilotDigestClaim(DAY,ADDRESS,'synthetic','synthetic','digest',submission_token='old-token')
    assert service.submission_check(claim) == 'PILOT_SUBMISSION_OWNERSHIP_CHANGED'
    service.initialize()
    with db.connect() as c:
        assert tuple(c.execute('SELECT status,combined_batch_id FROM pilot_test_digest_outbox').fetchone()) == ('processing','batch')


def test_missing_schema_fails_closed_without_losing_existing_processing(tmp_path):
    db,service=service_for(tmp_path)
    service.set_diagnostic_consent(ADDRESS,consented=True)
    with db.connect() as c:
        c.execute('DROP INDEX IF EXISTS idx_pilot_digest_combined_batch')
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,created_at,updated_at) VALUES(?,?,'processing','old','old')", (DAY,ADDRESS)); c.commit()
    with pytest.raises(ValueError,match='PILOT_COMBINED_SCHEMA_UNAVAILABLE'):
        service.claim_due(ADDRESS,now=NOW,send_hour=22)
    service.initialize()
    with db.connect() as c:
        assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()[0] == 'processing'


def test_v96_rebuilt_pilot_without_joint_foreign_key_cannot_recover_or_send(tmp_path):
    from home_study_agent.daily_report_combined_schema import PILOT_BASE_SCHEMA, INDEX_SCHEMA
    db,service=service_for(tmp_path)
    service.set_diagnostic_consent(ADDRESS,consented=True)
    with db.connect() as c:
        c.execute('DROP TABLE pilot_test_digest_outbox')
        c.execute(PILOT_BASE_SCHEMA)
        c.execute('ALTER TABLE pilot_test_digest_outbox ADD COLUMN combined_batch_id TEXT')
        c.execute(INDEX_SCHEMA)
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,created_at,updated_at) VALUES(?,?,'processing','old','old')", (DAY,ADDRESS))
        c.commit()
    service.initialize()
    with db.connect() as c:
        assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()[0] == 'processing'
    with pytest.raises(ValueError,match='PILOT_COMBINED_SCHEMA_UNAVAILABLE'):
        service.claim_due(ADDRESS,now=NOW,send_hour=22)


@pytest.mark.parametrize('tamper', ['orphan','digest','index'])
def test_broken_link_or_index_fails_closed(tmp_path,tamper):
    db,service=service_for(tmp_path); schema(db)
    service.set_diagnostic_consent(ADDRESS,consented=True); linked(db)
    with db.connect() as c:
        if tamper == 'orphan':
            c.execute('PRAGMA foreign_keys=OFF'); c.execute("DELETE FROM combined_diagnostic_batch WHERE batch_id='batch'")
        elif tamper == 'digest': c.execute("UPDATE pilot_test_digest_outbox SET digest_sha256='changed'")
        else: c.execute('DROP INDEX idx_pilot_digest_combined_batch')
        c.commit()
    with pytest.raises(ValueError,match='PILOT_COMBINED_(SCHEMA_UNAVAILABLE|LINK_UNCONFIRMED)'):
        service.claim_due(ADDRESS,now=NOW,send_hour=22)
    service.initialize()
    with db.connect() as c:
        assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()[0] == 'processing'


@pytest.mark.parametrize('pilot_table', ['absent', 'valid', 'malformed_column'])
def test_real_v95_upgrade_pilot_table_presence_and_bad_column(tmp_path,pilot_table):
    from home_study_agent.daily_report_combined_schema import PILOT_BASE_SCHEMA, schema_valid
    path=tmp_path/'legacy.db'; db=Database(path); db.initialize()
    # Construct an actual historical schema: v96 objects and pilot table absent,
    # then restore only the specific v95 pilot table under test.
    with sqlite3.connect(path) as c:
        c.execute('DROP TABLE combined_daily_delivery')
        c.execute('DROP TABLE combined_daily_snapshot')
        c.execute('DROP TABLE combined_diagnostic_member')
        c.execute('DROP TABLE pilot_test_digest_outbox')
        c.execute('DROP TABLE combined_diagnostic_batch')
        c.execute('DROP TABLE daily_visual_schedule')
        c.execute('DELETE FROM schema_migration WHERE version>95')
        if pilot_table != 'absent':
            c.execute(PILOT_BASE_SCHEMA)
            c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,created_at,updated_at) VALUES(?,?,'unknown',2,'old','old')", (DAY,ADDRESS))
        if pilot_table == 'malformed_column':
            c.execute('ALTER TABLE pilot_test_digest_outbox ADD COLUMN combined_batch_id TEXT')
        c.commit()
    if pilot_table == 'malformed_column':
        with pytest.raises(sqlite3.DatabaseError,match='COMBINED_REPORT_V96_SCHEMA_INVALID'):
            db.initialize()
        with sqlite3.connect(path) as c:
            assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 95
            assert c.execute('SELECT status,attempt_count FROM pilot_test_digest_outbox').fetchone() == ('unknown',2)
            assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='combined_diagnostic_batch'").fetchone()
        return
    db.initialize()
    with db.connect() as c:
        assert schema_valid(c)
        if pilot_table == 'valid':
            assert tuple(c.execute('SELECT status,attempt_count,combined_batch_id FROM pilot_test_digest_outbox').fetchone()) == ('unknown',2,None)
        else:
            assert c.execute('SELECT COUNT(*) FROM pilot_test_digest_outbox').fetchone()[0] == 0
