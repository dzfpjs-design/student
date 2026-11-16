"""Synthetic v95 migration, restoration and child-scoped erasure tests."""
import hashlib
import json
import sqlite3

import pytest

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.daily_report_visual_schedule import SCHEDULE_SCHEMA, _valid_schema
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from local_restore_operation_fixture import restore_local
from test_daily_report_candidate import synthetic


def historical_v95(path):
    db=Database(path); db.initialize()
    with db.connect() as c:
        for name in ('smtp_owned_worker_member','smtp_owned_worker_binding','smtp_owned_worker_journal','combined_daily_delivery','combined_daily_snapshot','combined_diagnostic_member','combined_daily_mode'):
            c.execute(f'DROP TABLE IF EXISTS {name}')
        c.execute('DROP INDEX IF EXISTS idx_pilot_digest_combined_batch')
        pilot_columns={row[1] for row in c.execute('PRAGMA table_info(pilot_test_digest_outbox)')}
        if 'combined_batch_id' in pilot_columns:
            c.execute('ALTER TABLE pilot_test_digest_outbox DROP COLUMN combined_batch_id')
        consent_columns={row[1] for row in c.execute('PRAGMA table_info(pilot_diagnostic_recipient_consent)')}
        if 'authority_generation' in consent_columns:
            c.execute('ALTER TABLE pilot_diagnostic_recipient_consent DROP COLUMN authority_generation')
        c.execute('DROP TABLE IF EXISTS combined_diagnostic_batch')
        c.execute('DROP TABLE daily_visual_schedule')
        c.execute('DELETE FROM schema_migration WHERE version>95')
        c.execute("INSERT INTO child_profile VALUES('a','synthetic','初三','江苏','active','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO daily_observation_delivery_ledger VALUES('synthetic-key','a',NULL,'unknown','old','old')")
        c.commit()
    return db


def test_v95_upgrade_keeps_delivery_history_and_verifiable_backup(tmp_path):
    db=historical_v95(tmp_path/'family.db'); db.initialize()
    status=db.latest_upgrade_status()
    assert (status['source_schema_version'],status['target_schema_version'],status['status'])==(95,LATEST_SCHEMA_VERSION,'completed')
    backups=db.path.parent/'upgrade-backups'
    metadata=json.loads(max(backups.glob('pre-upgrade-*.json'),key=lambda p:p.stat().st_mtime).read_text())
    backup=backups/metadata['backup_file']
    assert hashlib.sha256(backup.read_bytes()).hexdigest()==metadata['sha256']
    with sqlite3.connect(backup) as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0]==95
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='daily_visual_schedule'").fetchone()
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='unknown'
    Database(db.path).initialize()
    with db.connect() as c:
        assert _valid_schema(c)
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='unknown'
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_v95_upgrade_failure_restores_and_retries(tmp_path,monkeypatch):
    db=historical_v95(tmp_path/'family.db'); original=db._initialize_schema
    def fail():
        original(); raise RuntimeError('synthetic post-upgrade failure')
    monkeypatch.setattr(db,'_initialize_schema',fail)
    with pytest.raises(RuntimeError,match='synthetic post-upgrade failure'): db.initialize()
    status=db.latest_upgrade_status()
    assert (status['source_schema_version'],status['target_schema_version'],status['status'])==(95,LATEST_SCHEMA_VERSION,'restored_after_failure')
    with db.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0]==95
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='daily_visual_schedule'").fetchone()
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger').fetchone()[0]=='unknown'
    monkeypatch.undo(); db.initialize()
    with db.connect() as c: assert _valid_schema(c)


@pytest.mark.parametrize('damage',['missing','check','foreign'])
def test_declared_v96_damaged_schema_fails_closed(tmp_path,damage):
    db=Database(tmp_path/'family.db');db.initialize()
    with db.connect() as c:
        c.execute('DROP TABLE daily_visual_schedule')
        if damage=='check': c.execute(SCHEDULE_SCHEMA.replace('IN (0,1)','IN (0,1,2)'))
        if damage=='foreign': c.execute(SCHEDULE_SCHEMA.replace('ON DELETE CASCADE','ON DELETE RESTRICT'))
        c.commit()
    with pytest.raises(sqlite3.DatabaseError): db.initialize()
    assert db.latest_upgrade_status()['status']=='restored_after_failure'
    with db.connect() as c: assert not _valid_schema(c)


@pytest.mark.parametrize('scope',['learning','media','reports','all_history'])
def test_delete_child_scope_removes_only_own_schedule(synthetic,tmp_path,scope):
    db,day=synthetic
    with db.connect() as c:
        for child in ('a','b'): c.execute('INSERT INTO daily_visual_schedule VALUES(?,?,?,1,?)',(child,day,day,'synthetic'))
        c.commit()
    service=PrivacyLifecycleService(db,tmp_path,b's'*32);service.initialize()
    result=service.delete_child_scope('a',scope,'确认删除','synthetic-delete-a')
    assert result['status']=='completed'
    with db.connect() as c:
        assert [tuple(x) for x in c.execute('SELECT child_id,enabled FROM daily_visual_schedule')]==[('b',1)]


def test_restore_disables_schedules_and_quarantines_delivery(synthetic,tmp_path):
    db,day=synthetic
    service=PrivacyLifecycleService(db,tmp_path,b's'*32);service.initialize()
    with db.connect() as c:
        for child in ('a','b'): c.execute('INSERT INTO daily_visual_schedule VALUES(?,?,?,1,?)',(child,day,day,'synthetic'))
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=last_checkpoint_at WHERE session_id='s'")
        c.commit()
    backup=service.create_backup(prune=False)
    assert restore_local(service,backup['backup_id'],'确认恢复')
    with db.connect() as c:
        assert [tuple(x) for x in c.execute('SELECT child_id,enabled FROM daily_visual_schedule ORDER BY child_id')]==[('a',0),('b',0)]
        assert tuple(c.execute('SELECT blocked,reason FROM daily_observation_delivery_barrier').fetchone())==(1,'RESTORE_HISTORY_UNRECONCILED')
