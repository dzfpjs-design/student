
from local_restore_operation_fixture import restore_local
import sqlite3
import pytest

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.runtime_data_scope import RuntimeDataScopeError, read_scope
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService, InvalidBackupError


def seeded(root, mode):
    root.mkdir()
    database = Database(root / 'study.db', runtime_mode=mode)
    database.initialize()
    return database, PrivacyLifecycleService(database, root, b's' * 32)


def test_storage_scope_survives_restart_and_cannot_be_changed(tmp_path):
    database, _ = seeded(tmp_path / 'trial', 'trial')
    Database(database.path, runtime_mode='trial').initialize()
    with database.connect() as c:
        assert read_scope(c) == 'trial'
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            c.execute("UPDATE runtime_data_scope SET mode='formal'")
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            c.execute('DELETE FROM runtime_data_scope')
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            c.execute("INSERT OR REPLACE INTO runtime_data_scope VALUES(1,'formal',datetime('now'))")
    with pytest.raises(RuntimeDataScopeError, match='CONFLICT'):
        Database(database.path).initialize()
    with pytest.raises(RuntimeDataScopeError, match='CONFLICT'):
        with Database(database.path).connect():
            pytest.fail('cross-scope read was allowed')


@pytest.mark.parametrize('source_mode,target_mode', [('trial','formal'), ('formal','trial')])
def test_portable_restore_rejects_cross_scope_before_replacing_target(tmp_path, source_mode, target_mode):
    source_db, source = seeded(tmp_path / 'source', source_mode)
    target_db, target = seeded(tmp_path / 'target', target_mode)
    with target_db.connect() as c:
        c.execute('CREATE TABLE restore_sentinel(value TEXT)')
        c.execute("INSERT INTO restore_sentinel VALUES('keep target')")
        c.commit()
    backup = source.create_portable_backup('synthetic strong passphrase')
    with pytest.raises(InvalidBackupError, match='作用域'):
        target.restore_portable_backup(backup['path'], 'synthetic strong passphrase', '确认迁移恢复')
    with target_db.connect() as c:
        assert read_scope(c) == target_mode
        assert c.execute('SELECT value FROM restore_sentinel').fetchone()[0] == 'keep target'
    with source_db.connect() as c:
        assert read_scope(c) == source_mode


def test_trial_portable_restore_keeps_trial_identity(tmp_path):
    _, source = seeded(tmp_path / 'source', 'trial')
    target_db, target = seeded(tmp_path / 'target', 'trial')
    backup = source.create_portable_backup('synthetic strong passphrase')
    assert target.restore_portable_backup(backup['path'], 'synthetic strong passphrase', '确认迁移恢复')['integrity_status'] == 'ok'
    Database(target_db.path, runtime_mode='trial').initialize()
    with target_db.connect() as c:
        assert read_scope(c) == 'trial'


def test_local_snapshot_restore_rejects_trial_database_in_formal_profile(tmp_path):
    _, source = seeded(tmp_path / 'source', 'trial')
    target_db, target = seeded(tmp_path / 'target', 'formal')
    backup = source.create_backup()
    with target_db.connect() as c:
        c.execute('''INSERT INTO backup_manifest(backup_id,path,database_version,size_bytes,sha256,
            integrity_status,reason,created_at) VALUES(?,?,?,?,?,'ok','manual',datetime('now'))''',
            (backup['backup_id'], backup['path'], LATEST_SCHEMA_VERSION, backup['size_bytes'], backup['sha256']))
        c.commit()
    original_operations = []
    with pytest.raises(InvalidBackupError, match='作用域'):
        restore_local(target,backup['backup_id'], '确认恢复', original_operations=original_operations)
    assert len(original_operations) == 1
    from home_study_agent.parent_restore_workflow import recover_original
    assert recover_original(target_db, original_operations[0])['status'] == 'failed'
    with target_db.connect() as c:
        assert read_scope(c) == 'formal'


def test_legacy_unmarked_database_defaults_to_formal_and_upgrade_keeps_data(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE schema_migration(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        c.execute("INSERT INTO schema_migration VALUES(78,datetime('now'))")
        c.execute('CREATE TABLE legacy_sentinel(value TEXT)')
        c.execute("INSERT INTO legacy_sentinel VALUES('kept')")
    with pytest.raises(RuntimeDataScopeError, match='CONFLICT'):
        Database(path, runtime_mode='trial').initialize()
    database = Database(path)
    database.initialize()
    with database.connect() as c:
        assert read_scope(c) == 'formal'
        assert c.execute('SELECT value FROM legacy_sentinel').fetchone()[0] == 'kept'
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
    assert database.latest_upgrade_status()['source_schema_version'] == 78
