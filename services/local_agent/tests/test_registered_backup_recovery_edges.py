"""Finite real backup edge cases; every file and registry is synthetic."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from home_study_agent import registered_asset_backup as registered
from test_registered_asset_backup import fixture


def test_capsule_manifest_is_not_counted_as_a_payload_asset(tmp_path, monkeypatch):
    _, service, _, _, oracle = fixture(tmp_path)
    backup = service.create_backup(prune=False)
    snapshot = Path(backup['path'])
    # Scale the existing payload limit to the real fixture count. This executes
    # the original producer, authenticated decryption and complete extraction;
    # the independent source audit also covers the actual 100000+1 arithmetic.
    monkeypatch.setattr(registered, 'MAX_FILES', len(oracle['assets']))
    parent = tmp_path / 'verified-private'
    parent.mkdir(mode=0o700)
    stage = registered.decrypt_capsule(
        registered.capsule_path(snapshot), service.data_encryption_key,
        service._decrypt_file, parent, registered.record(snapshot)['sha256'],
    )
    manifest = registered.unique_json((stage/'manifest.json').read_bytes())
    result = registered.verify_registered_asset_snapshot(stage, registered.Policy(**manifest['policy']))
    assert result['files'] == len(oracle['assets'])
    assert (stage / 'manifest.json').is_file()


def test_prune_later_io_failure_keeps_completed_manifest_deletion(tmp_path, monkeypatch):
    database, service, _, _, _ = fixture(tmp_path)
    first = service.create_backup(prune=False)
    second = service.create_backup(prune=False)
    with database.connect() as connection:
        for backup, minute in [(first, 10), (second, 9)]:
            connection.execute('UPDATE backup_manifest SET created_at=? WHERE backup_id=?',
                               (f'2026-01-01T12:{minute:02d}:00+00:00', backup['backup_id']))
        # Seven retention-only rows on the same local day/week preserve the
        # original daily/weekly policy and put both actual bundles beyond it.
        for i in range(7):
            connection.execute('''INSERT INTO backup_manifest
                (backup_id,path,database_version,size_bytes,sha256,integrity_status,reason,created_at)
                VALUES (?,?,?,?,?,?,?,?)''',
                (f'synthetic-keep-{i}', str(service.data_dir/'backups'/f'home-study-keep-{i}.db'),
                 1, 0, '0'*64, 'ok', 'manual', f'2026-01-01T12:{20+i:02d}:00+00:00'))
        connection.commit()
    remove = service._remove_backup_bundle

    def remove_one(path):
        if path == Path(second['path']):
            raise PermissionError('synthetic second bundle sharing violation')
        return remove(path)

    monkeypatch.setattr(service, '_remove_backup_bundle', remove_one)
    with pytest.raises(PermissionError):
        service._prune_backups()
    assert not Path(first['path']).exists()
    assert Path(second['path']).is_file()
    with database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM backup_manifest WHERE backup_id=?',
                                  (first['backup_id'],)).fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM backup_manifest WHERE backup_id=?',
                                  (second['backup_id'],)).fetchone()[0] == 1


def test_capsule_manifest_write_keeps_actual_phase_disk_headroom(tmp_path, monkeypatch):
    _, service, _, _, _ = fixture(tmp_path)
    backup = service.create_backup(prune=False)
    snapshot = Path(backup['path'])
    capsule = registered.capsule_path(snapshot)
    metadata = registered.read_json(service._backup_metadata_path(snapshot))
    header, _ = registered.header(capsule)
    parent = tmp_path / 'low-space-private'
    parent.mkdir(mode=0o700)
    # Enough room for the decrypted archive and payload, then one byte beyond
    # the original 8MiB headroom. The separately written manifest cannot fit.
    initial_free = 8*1024*1024 + header['ciphertext_size'] + metadata['registered']['total_bytes'] + 1

    def disk_usage(_):
        written = sum(p.stat().st_size for p in parent.rglob('*') if p.is_file())
        return SimpleNamespace(total=initial_free, used=written, free=initial_free-written)

    monkeypatch.setattr(registered.shutil, 'disk_usage', disk_usage)
    with pytest.raises(ValueError, match='^REGISTERED_BACKUP_DISK_REQUIRED$'):
        registered.decrypt_capsule(capsule, service.data_encryption_key, service._decrypt_file,
                                   parent, registered.record(snapshot)['sha256'])
    assert list(parent.iterdir()) == []
