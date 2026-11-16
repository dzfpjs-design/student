import sqlite3

import pytest

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_observation_segments import SEGMENT_SCHEMA_SQL


def test_v93_database_alone_has_all_fk_parents_and_supports_child_delete(tmp_path):
    db = Database(tmp_path / 'synthetic.db')
    db.initialize()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('document_monitor_session','document_page_revision')").fetchone()[0] == 2
        c.execute("INSERT INTO child_profile VALUES('synthetic-child','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.execute("DELETE FROM child_profile WHERE child_id='no-child'")
        c.execute("DELETE FROM child_profile WHERE child_id='synthetic-child'")
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('damage', ['missing_table', 'wrong_table', 'missing_unique', 'wrong_foreign', 'missing_monitor_column', 'missing_generation', 'missing_study_signal', 'missing_revision_field'])
def test_declared_v93_damaged_observation_schema_is_backed_up_and_rejected(tmp_path, damage):
    db = Database(tmp_path / 'synthetic.db')
    db.initialize()
    DocumentMonitoringService(db)
    with db.connect() as c:
        # Recreate an authentic v93 layout before testing its corruption.
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DROP TABLE daily_observation_report_outbox')
        c.execute('DROP TABLE daily_observation_report_source')
        c.execute('DELETE FROM schema_migration WHERE version > 93')
        c.execute("INSERT INTO child_profile VALUES('synthetic-child','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        if damage == 'missing_table':
            c.execute('DROP TABLE document_observation_segment')
        elif damage == 'wrong_table':
            c.execute('DROP TABLE document_observation_segment')
            c.execute('CREATE TABLE document_observation_segment(segment_id TEXT PRIMARY KEY)')
        elif damage in {'missing_unique', 'wrong_foreign'}:
            c.execute('DROP TABLE document_observation_segment')
            sql = SEGMENT_SCHEMA_SQL
            if damage == 'missing_unique':
                sql = sql.replace(',\n    UNIQUE(monitor_id, epoch, generation, seq)', '')
            else:
                sql = sql.replace('REFERENCES child_profile(child_id) ON DELETE CASCADE',
                                  'REFERENCES child_profile(child_id) ON DELETE RESTRICT')
            c.execute(sql)
        elif damage == 'missing_monitor_column':
            c.execute('ALTER TABLE document_monitor_session DROP COLUMN generation_started_at')
        elif damage == 'missing_generation':
            c.execute('ALTER TABLE document_monitor_session DROP COLUMN generation')
        elif damage == 'missing_study_signal':
            c.execute('ALTER TABLE document_page_revision DROP COLUMN study_signal_json')
        else:
            c.execute('ALTER TABLE document_page_revision DROP COLUMN content_signature')
        c.commit()
    with pytest.raises(sqlite3.DatabaseError, match='OBSERVATION_V93_SCHEMA_INVALID'):
        Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM child_profile WHERE child_id='synthetic-child'").fetchone()
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 93
        if damage == 'missing_table':
            assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='document_observation_segment'").fetchone()
        elif damage == 'wrong_table':
            assert [r[1] for r in c.execute('PRAGMA table_info(document_observation_segment)')] == ['segment_id']
        elif damage == 'missing_monitor_column':
            assert 'generation_started_at' not in {r[1] for r in c.execute('PRAGMA table_info(document_monitor_session)')}
    status = db.latest_upgrade_status()
    assert status['status'] == 'restored_after_failure'
    assert status['source_schema_version'] == 93
    assert status['target_schema_version'] == LATEST_SCHEMA_VERSION
    assert status['snapshot_available'] is True


@pytest.mark.parametrize('monitor_present', [False, True])
def test_old_v92_monitor_schema_migrates_under_recovery_gate_and_reopens(tmp_path, monitor_present):
    db = Database(tmp_path / 'old.db'); db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE daily_report_email_verification')
        c.execute('DROP TABLE daily_observation_delivery_ledger')
        c.execute('DROP TABLE daily_observation_delivery_barrier')
        c.execute('DROP TABLE daily_observation_report_snapshot')
        c.execute('DROP TABLE daily_observation_report_outbox')
        c.execute('DROP TABLE daily_observation_report_source')
        c.execute('DROP TABLE document_observation_segment')
        if monitor_present:
            c.execute('ALTER TABLE document_monitor_session DROP COLUMN generation_started_at')
            c.execute('ALTER TABLE document_monitor_session DROP COLUMN generation')
            c.execute('ALTER TABLE document_page_revision DROP COLUMN study_signal_json')
        else:
            c.execute('DROP TABLE document_page_revision')
            c.execute('DROP TABLE document_monitor_session')
        c.execute('DELETE FROM schema_migration WHERE version > 92')
        c.execute("INSERT INTO child_profile VALUES('synthetic-child','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.commit()
    Database(db.path).initialize()
    assert db.latest_upgrade_status()['snapshot_available'] is True
    assert db.latest_upgrade_status()['source_schema_version'] == 92
    with db.connect() as c:
        assert {'generation', 'generation_started_at'} <= {r[1] for r in c.execute('PRAGMA table_info(document_monitor_session)')}
        assert 'study_signal_json' in {r[1] for r in c.execute('PRAGMA table_info(document_page_revision)')}
        assert c.execute("SELECT 1 FROM child_profile WHERE child_id='synthetic-child'").fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    DocumentMonitoringService(db)
    Database(db.path).initialize()
