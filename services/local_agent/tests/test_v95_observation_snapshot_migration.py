"""Synthetic v94-to-v95 migration and recovery; no household data."""
import sqlite3
import hashlib
import json

import pytest

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.daily_report_frozen_snapshot import SNAPSHOT_SCHEMA, schema_valid
from home_study_agent.daily_report_email_verification import schema_valid as email_schema_valid
from home_study_agent.daily_report_delivery_state import schema_valid as delivery_schema_valid, LEDGER_SCHEMA, BARRIER_SCHEMA

V95_TABLES = ('daily_observation_report_snapshot', 'daily_report_email_verification',
              'daily_observation_delivery_ledger', 'daily_observation_delivery_barrier')


def _remove_v96_schema(c):
    # A fresh v95 installation need not have initialized the optional pilot
    # service. Remove the later schema, including its pilot link, faithfully.
    c.execute('DROP TABLE combined_daily_delivery')
    c.execute('DROP TABLE combined_daily_snapshot')
    c.execute('DROP TABLE combined_diagnostic_member')
    c.execute('DROP TABLE pilot_test_digest_outbox')
    c.execute('DROP TABLE combined_diagnostic_batch')
    c.execute('DROP TABLE pilot_diagnostic_recipient_consent')
    c.execute('DROP TABLE daily_visual_schedule')
    c.execute('DELETE FROM schema_migration WHERE version > 95')


def _v94(path):
    db = Database(path)
    db.initialize()
    with db.connect() as c:
        _remove_v96_schema(c)
        # Remove every v95 table to reproduce an actual v94 database.
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DELETE FROM schema_migration WHERE version > 94')
        c.execute("INSERT INTO child_profile VALUES('a','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO daily_observation_report_source(source_id,child_id,day,fingerprint,source_ids_json) VALUES('s','a','2026-11-04','synthetic-hash','[]')")
        c.execute("INSERT INTO daily_observation_report_outbox VALUES('a','2026-11-04','recipient','synthetic@example.test','s','prepared')")
        c.commit()
    return db


def test_v94_upgrade_retains_source_outbox_and_backup_across_restart(tmp_path):
    db = _v94(tmp_path / 'family.db')
    db.initialize()
    status = db.latest_upgrade_status()
    assert (status['source_schema_version'], status['target_schema_version'], status['status']) == (94, LATEST_SCHEMA_VERSION, 'completed')
    assert status['snapshot_available'] is True
    backup_dir = db.path.parent / 'upgrade-backups'
    metadata_path = max(backup_dir.glob('pre-upgrade-*.json'), key=lambda path: path.stat().st_mtime)
    metadata = json.loads(metadata_path.read_text())
    backup_path = backup_dir / metadata['backup_file']
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == metadata['sha256']
    with sqlite3.connect(backup_path) as old:
        assert old.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 94
        assert old.execute("SELECT fingerprint FROM daily_observation_report_source WHERE source_id='s'").fetchone()[0] == 'synthetic-hash'
        assert old.execute("SELECT status FROM daily_observation_report_outbox WHERE source_id='s'").fetchone()[0] == 'prepared'
        assert all(not old.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() for name in V95_TABLES)
    Database(db.path).initialize()
    with db.connect() as c:
        assert schema_valid(c)
        assert email_schema_valid(c) and delivery_schema_valid(c)
        assert c.execute("SELECT fingerprint FROM daily_observation_report_source WHERE source_id='s'").fetchone()[0] == 'synthetic-hash'
        assert c.execute("SELECT status FROM daily_observation_report_outbox WHERE source_id='s'").fetchone()[0] == 'prepared'
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_v94_failed_upgrade_restores_original_with_retry(tmp_path, monkeypatch):
    db = _v94(tmp_path / 'family.db')
    original = db._initialize_schema
    def fail_after_migration():
        original()
        raise RuntimeError('synthetic post-migration failure')
    monkeypatch.setattr(db, '_initialize_schema', fail_after_migration)
    with pytest.raises(RuntimeError, match='synthetic post-migration failure'):
        db.initialize()
    status = db.latest_upgrade_status()
    assert (status['source_schema_version'], status['target_schema_version'], status['status']) == (94, LATEST_SCHEMA_VERSION, 'restored_after_failure')
    assert status['snapshot_available'] is True
    with db.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 94
        assert all(not c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() for name in V95_TABLES)
        assert c.execute("SELECT status FROM daily_observation_report_outbox WHERE source_id='s'").fetchone()[0] == 'prepared'
    monkeypatch.undo()
    db.initialize()
    with db.connect() as c:
        assert schema_valid(c)


def test_v94_history_of_restore_sets_persistent_delivery_barrier(tmp_path):
    db = _v94(tmp_path / 'prior-restore.db')
    with db.connect() as c:
        c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES('restore','system','report_delivery_restore_quarantined',NULL,'{}','2026-01-01')")
        c.commit()
    db.initialize()
    with db.connect() as c:
        assert tuple(c.execute('SELECT blocked,reason FROM daily_observation_delivery_barrier').fetchone()) == (1, 'RESTORE_HISTORY_UNRECONCILED')
    Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute('SELECT blocked FROM daily_observation_delivery_barrier').fetchone()[0] == 1


@pytest.mark.parametrize('damage', ['missing', 'wrong_check', 'wrong_foreign'])
def test_declared_v95_snapshot_damage_fails_closed_with_backup(tmp_path, damage):
    db = Database(tmp_path / 'damaged.db')
    db.initialize()
    with db.connect() as c:
        _remove_v96_schema(c)
        c.execute("INSERT INTO child_profile VALUES('a','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.execute('DROP TABLE daily_observation_report_snapshot')
        if damage != 'missing':
            ddl = SNAPSHOT_SCHEMA.replace("CHECK(status='frozen_local')", "CHECK(status IN ('frozen_local','sent'))") if damage == 'wrong_check' else SNAPSHOT_SCHEMA.replace('ON DELETE CASCADE', 'ON DELETE RESTRICT')
            c.execute(ddl)
        c.commit()
    with pytest.raises(sqlite3.DatabaseError, match='DAILY_OBSERVATION_V95_SCHEMA_INVALID'):
        db.initialize()
    status = db.latest_upgrade_status()
    assert (status['source_schema_version'], status['target_schema_version'], status['status']) == (95, LATEST_SCHEMA_VERSION, 'restored_after_failure')
    assert status['snapshot_available'] is True
    with db.connect() as c:
        assert not schema_valid(c)
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='a'").fetchone()[0] == 'synthetic'


@pytest.mark.parametrize('damage,expected', [
    ('missing_email', 'DAILY_EMAIL_VERIFICATION_V95_SCHEMA_INVALID'),
    ('missing_ledger', 'DAILY_DELIVERY_STATE_V95_SCHEMA_INVALID'),
    ('missing_barrier', 'DAILY_DELIVERY_STATE_V95_SCHEMA_INVALID'),
    ('missing_barrier_row', 'DAILY_DELIVERY_STATE_V95_SCHEMA_INVALID'),
    ('wrong_ledger_status', 'DAILY_DELIVERY_STATE_V95_SCHEMA_INVALID'),
    ('wrong_barrier_check', 'DAILY_DELIVERY_STATE_V95_SCHEMA_INVALID'),
])
def test_declared_v95_delivery_damage_fails_closed(tmp_path, damage, expected):
    db = Database(tmp_path / 'damaged-delivery.db')
    db.initialize()
    with db.connect() as c:
        _remove_v96_schema(c)
        if damage == 'missing_email':
            c.execute('DROP TABLE daily_report_email_verification')
        elif damage in ('missing_ledger', 'wrong_ledger_status'):
            c.execute('DROP TABLE daily_observation_delivery_ledger')
            if damage == 'wrong_ledger_status':
                c.execute(LEDGER_SCHEMA.replace("'unknown','sent'", "'unknown','sent','unsafe'"))
        elif damage in ('missing_barrier', 'wrong_barrier_check'):
            c.execute('DROP TABLE daily_observation_delivery_barrier')
            if damage == 'wrong_barrier_check':
                c.execute(BARRIER_SCHEMA.replace('CHECK(blocked IN (0,1))', 'CHECK(blocked IN (0,1,2))'))
        else:
            c.execute('DELETE FROM daily_observation_delivery_barrier')
        c.commit()
    with pytest.raises(sqlite3.DatabaseError, match=expected):
        db.initialize()
    status = db.latest_upgrade_status()
    assert (status['source_schema_version'], status['target_schema_version'], status['status']) == (95,LATEST_SCHEMA_VERSION,'restored_after_failure')
    assert status['snapshot_available'] is True
