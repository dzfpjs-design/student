"""Real backup replacement must roll back migrations/audit and reject orphan data."""

from local_restore_operation_fixture import restore_local
from portable_restore_operation_fixture import restore_portable
from pathlib import Path
import hashlib
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService, InvalidBackupError
from test_portable_backup import _seed_source


@pytest.mark.parametrize('failure', ['migration', 'audit'])
def test_local_restore_postcopy_failure_rolls_back_current_data(tmp_path, monkeypatch, failure):
    db, _ = _seed_source(tmp_path, b'o' * 32)
    from home_study_agent.database import LocalMediaRuntime
    db.local_media_runtime = LocalMediaRuntime(tmp_path, b'o' * 32)
    service = PrivacyLifecycleService(db, tmp_path, b'o' * 32)
    saved = service.create_backup()
    with db.connect() as c:
        c.execute("UPDATE child_profile SET nickname='Newer saved identity' WHERE child_id='child-primary-4'")
        c.commit()
    if failure == 'migration':
        from home_study_agent.parent_restore_ownership import staged_capability
        initialize = Database.initialize
        calls = []
        def fail_once(candidate):
            if candidate.path != db.path:
                original_work = staged_capability(candidate)
                assert original_work is not None and original_work.owner.path == db.path
                assert candidate.path == original_work.work / 'database.db'
                calls.append(candidate.path)
                if len(calls) == 1:
                    raise RuntimeError('synthetic migration failure')
            return initialize(candidate)
        monkeypatch.setattr(Database, 'initialize', fail_once)
    else:
        audit = service._audit
        def fail_audit(connection, actor, action, *args):
            if action == 'backup_restored':
                raise RuntimeError('synthetic audit failure')
            return audit(connection, actor, action, *args)
        monkeypatch.setattr(service, '_audit', fail_audit)
    original_operations = []
    with pytest.raises(RuntimeError, match='^synthetic ' + failure + ' failure$'):
        restore_local(service,saved['backup_id'], '确认恢复', original_operations=original_operations)
    assert len(original_operations) == 1
    from home_study_agent.parent_restore_workflow import recover_original
    assert recover_original(db, original_operations[0])['status'] == 'failed'
    if failure == 'migration':
        assert len(calls) == 1
    with db.connect() as c:
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0] == 'Newer saved identity'
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_local_restore_rejects_fk_orphans_even_when_file_checksum_matches(tmp_path):
    db, _ = _seed_source(tmp_path, b'o' * 32)
    service = PrivacyLifecycleService(db, tmp_path, b'o' * 32)
    # Build an authentic product backup from an actually damaged source; never
    # tamper the already authenticated package/metadata or edit its ledger SHA.
    with sqlite3.connect(db.path) as c:
        c.row_factory = sqlite3.Row
        original_child = dict(c.execute("SELECT * FROM child_profile WHERE child_id='child-primary-4'").fetchone())
        c.execute("DELETE FROM child_profile WHERE child_id='child-primary-4'")
        c.commit()
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert c.execute('PRAGMA foreign_key_check').fetchone()
    saved = service.create_backup()
    with db.connect() as c:
        columns = list(original_child)
        c.execute('INSERT INTO child_profile (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                  [original_child[column] for column in columns])
        c.commit()
    checksum, _ = service._backup_checksum(Path(saved['path']))
    assert checksum == saved['sha256']
    source_digest = hashlib.sha256(Path(saved['path']).read_bytes()).hexdigest()
    original_operations = []
    with pytest.raises(InvalidBackupError, match='关联'):
        restore_local(service, saved['backup_id'], '确认恢复', original_operations=original_operations)
    assert len(original_operations) == 1
    from home_study_agent.parent_restore_workflow import recover_original
    assert recover_original(db, original_operations[0])['status'] == 'failed'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM child_profile').fetchone()[0] == 1
        assert dict(c.execute("SELECT * FROM child_profile WHERE child_id='child-primary-4'").fetchone()) == original_child
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert service._backup_checksum(Path(saved['path']))[0] == saved['sha256']
    assert hashlib.sha256(Path(saved['path']).read_bytes()).hexdigest() == source_digest


def test_portable_restore_rejects_fk_orphans_before_replacing_destination(tmp_path):
    source_dir, target_dir = tmp_path / 'source', tmp_path / 'destination'
    source_dir.mkdir(); target_dir.mkdir()
    source_db, _ = _seed_source(source_dir, b'o' * 32)
    with sqlite3.connect(source_db.path) as c:
        c.execute("DELETE FROM child_profile WHERE child_id='child-primary-4'")
        c.commit()
    source = PrivacyLifecycleService(source_db, source_dir, b'o' * 32)
    portable = source.create_portable_backup('synthetic safe backup passphrase')
    target_db, _ = _seed_source(target_dir, b'n' * 32)
    target = PrivacyLifecycleService(target_db, target_dir, b'n' * 32)
    with target_db.connect() as c:
        original_child = dict(c.execute("SELECT * FROM child_profile WHERE child_id='child-primary-4'").fetchone())
    source_digest = hashlib.sha256(Path(portable['path']).read_bytes()).hexdigest()
    original_operations = []
    with pytest.raises(InvalidBackupError, match='关联'):
        restore_portable(target, portable['path'], 'synthetic safe backup passphrase', '确认迁移恢复', original_operations=original_operations)
    assert len(original_operations) == 1
    from home_study_agent.parent_restore_workflow import recover_original
    assert recover_original(target_db, original_operations[0])['status'] == 'failed'
    with target_db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM child_profile').fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()

    with target_db.connect() as c:
        assert dict(c.execute("SELECT * FROM child_profile WHERE child_id='child-primary-4'").fetchone()) == original_child
    assert hashlib.sha256(Path(portable['path']).read_bytes()).hexdigest() == source_digest


@pytest.mark.parametrize('kind', ['local', 'portable'])
def test_authenticated_restore_accepts_fk_complete_source(tmp_path, kind):
    """The same original typed service-unit wrappers still publish valid data."""
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    source_db, _ = _seed_source(source_dir, b'o' * 32)
    from home_study_agent.database import LocalMediaRuntime
    source_db.local_media_runtime = LocalMediaRuntime(source_dir, b'o' * 32)
    source = PrivacyLifecycleService(source_db, source_dir, b'o' * 32)
    passphrase = 'synthetic valid restore passphrase'
    saved = source.create_backup() if kind == 'local' else source.create_portable_backup(passphrase)
    digest = hashlib.sha256(Path(saved['path']).read_bytes()).hexdigest()
    if kind == 'local':
        target_db, target = source_db, source
    else:
        target_dir = tmp_path / 'destination'
        target_dir.mkdir()
        target_db, _ = _seed_source(target_dir, b'n' * 32)
        target_db.local_media_runtime = LocalMediaRuntime(target_dir, b'n' * 32)
        target = PrivacyLifecycleService(target_db, target_dir, b'n' * 32)
    with target_db.connect() as c:
        c.execute("UPDATE child_profile SET nickname='newer target identity' WHERE child_id='child-primary-4'")
        c.commit()
    result = (restore_local(target, saved['backup_id'], '确认恢复') if kind == 'local'
              else restore_portable(target, saved['path'], passphrase, '确认迁移恢复'))
    assert result['status'] == 'restored' and result['integrity_status'] == 'ok'
    with target_db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM child_profile').fetchone()[0] == 1
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0] == '小学孩子'
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert hashlib.sha256(Path(saved['path']).read_bytes()).hexdigest() == digest
