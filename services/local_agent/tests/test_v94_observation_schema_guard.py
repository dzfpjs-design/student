"""Synthetic family DB upgrade, damage, rollback, and restart checks."""
import sqlite3

import pytest

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION, UnsupportedDatabaseVersionError
from home_study_agent.daily_report_observation import SOURCE_SCHEMA, OUTBOX_SCHEMA, schema_valid


def _make_v93(path):
    db = Database(path)
    db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DROP TABLE daily_observation_report_outbox')
        c.execute('DROP TABLE daily_observation_report_source')
        c.execute('DELETE FROM schema_migration WHERE version > 93')
        c.execute("INSERT INTO child_profile VALUES('a','child a','初三','江苏','active','2026-01-01','2026-01-01')")
        c.commit()
    return db


def test_v93_upgrade_preserves_data_creates_backup_and_durable_v94(tmp_path):
    db = _make_v93(tmp_path / 'v93.db')
    db.initialize()
    status = db.latest_upgrade_status()
    assert (status['source_schema_version'], status['target_schema_version'], status['status']) == (93, LATEST_SCHEMA_VERSION, 'completed')
    assert status['snapshot_available'] is True
    with db.connect() as c:
        assert schema_valid(c)
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='a'").fetchone()[0] == 'child a'
        c.execute("INSERT INTO daily_observation_report_source(source_id,child_id,day,fingerprint,source_ids_json) VALUES('s','a','2026-11-04','hash','[]')")
        c.execute("INSERT INTO daily_observation_report_outbox VALUES('a','2026-11-04','recipient','synthetic@example.test','s','prepared')")
        c.commit()
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute("SELECT fingerprint FROM daily_observation_report_source WHERE source_id='s'").fetchone()[0] == 'hash'
        assert c.execute("SELECT status FROM daily_observation_report_outbox WHERE source_id='s'").fetchone()[0] == 'prepared'
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION


def test_v93_failed_upgrade_restores_original_then_can_retry(tmp_path, monkeypatch):
    db = _make_v93(tmp_path / 'v93.db')
    real_initialize = db._initialize_schema
    def fail_after_commit():
        real_initialize()
        raise RuntimeError('synthetic failure after migration commit')
    monkeypatch.setattr(db, '_initialize_schema', fail_after_commit)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        db.initialize()
    with db.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 93
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='a'").fetchone()[0] == 'child a'
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'daily_observation_report_%'").fetchall()
    status = db.latest_upgrade_status()
    assert (status['source_schema_version'], status['target_schema_version'], status['status']) == (93,LATEST_SCHEMA_VERSION,'restored_after_failure')
    assert status['snapshot_available'] is True
    monkeypatch.undo()
    db.initialize()
    with db.connect() as c:
        assert schema_valid(c)


@pytest.mark.parametrize('damage', ['missing_source','missing_outbox','wrong_unique','wrong_child_foreign','wrong_outbox_foreign','wrong_check','wrong_check_case','wrong_primary'])
def test_declared_v94_corruption_is_backed_up_and_rejected(tmp_path, damage):
    db = Database(tmp_path / 'v94.db')
    db.initialize()
    with db.connect() as c:
        c.execute("INSERT INTO child_profile VALUES('a','child a','初三','江苏','active','2026-01-01','2026-01-01')")
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DELETE FROM schema_migration WHERE version > 94')
        c.execute('DROP TABLE daily_observation_report_outbox')
        if damage in ('missing_source','wrong_unique','wrong_child_foreign'):
            c.execute('DROP TABLE daily_observation_report_source')
            if damage != 'missing_source':
                ddl = SOURCE_SCHEMA
                if damage == 'wrong_unique':
                    ddl = ddl.replace('UNIQUE(source_id,child_id,day)', 'UNIQUE(source_id,child_id)')
                else:
                    ddl = ddl.replace('REFERENCES child_profile(child_id) ON DELETE CASCADE', 'REFERENCES child_profile(child_id) ON DELETE RESTRICT')
                c.execute(ddl)
        if damage not in ('missing_source','missing_outbox'):
            ddl = OUTBOX_SCHEMA
            if damage == 'wrong_outbox_foreign':
                ddl = ddl.replace('ON DELETE CASCADE', 'ON DELETE RESTRICT')
            elif damage == 'wrong_check':
                ddl = ddl.replace("CHECK(status='prepared')", "CHECK(status IN ('prepared','sent'))")
            elif damage == 'wrong_check_case':
                ddl = ddl.replace("CHECK(status='prepared')", "CHECK(status='PREPARED')")
            elif damage == 'wrong_primary':
                ddl = ddl.replace('PRIMARY KEY(child_id,day,address_identity)', 'PRIMARY KEY(child_id,day,recipient_id)')
            c.execute(ddl)
        c.commit()
    with pytest.raises(sqlite3.DatabaseError, match='DAILY_OBSERVATION_V94_SCHEMA_INVALID'):
        Database(db.path).initialize()
    status = db.latest_upgrade_status()
    assert (status['source_schema_version'], status['target_schema_version'], status['status']) == (94,LATEST_SCHEMA_VERSION,'restored_after_failure')
    assert status['snapshot_available'] is True
    with db.connect() as c:
        assert not schema_valid(c)
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 94
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='a'").fetchone()[0] == 'child a'


def test_newer_version_is_rejected_without_migration(tmp_path):
    db = Database(tmp_path / 'newer.db')
    db.initialize()
    with db.connect() as c:
        c.execute("INSERT INTO schema_migration VALUES(?,datetime('now'))",(LATEST_SCHEMA_VERSION+1,))
        c.commit()
    with pytest.raises(UnsupportedDatabaseVersionError):
        db.initialize()
    with db.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION+1
