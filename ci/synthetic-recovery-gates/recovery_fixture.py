"""Synthetic-only repair experiment. Not an installer or a production repair tool.

All paths are created inside one fresh TemporaryDirectory; no caller paths or
production product identifiers are accepted. Windows durability remains unproved.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


class Rejected(RuntimeError):
    pass


V1 = {'app.bin': b'synthetic app version 1\n', 'resources/model.dat': b'synthetic model 1\n'}
V2 = {'app.bin': b'synthetic app version 2\n', 'resources/model.dat': b'synthetic model 2\n'}


def expected(entries):
    return {name: hashlib.sha256(value).hexdigest() for name, value in entries.items()}


def manifest(root):
    if root.is_symlink() or not root.is_dir():
        raise Rejected('INVALID_ROOT')
    records = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        folder = Path(parent)
        for name in dirs:
            path = folder / name
            if path.is_symlink() or not path.is_dir():
                raise Rejected('UNSUPPORTED_DIRECTORY')
        for name in files:
            path = folder / name
            before = path.lstat()
            if path.is_symlink() or not path.is_file() or before.st_nlink != 1:
                raise Rejected('UNSUPPORTED_FILE')
            content = path.read_bytes()
            after = path.lstat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise Rejected('CHANGED_DURING_READ')
            records[path.relative_to(root).as_posix()] = hashlib.sha256(content).hexdigest()
    return records


def write_files(root, entries):
    root.mkdir()
    for name, content in entries.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def sqlite_ok(path):
    # Synthetic test database is closed, has no WAL and contains no sensitive data.
    if path.with_name(path.name + '-wal').exists() or path.with_name(path.name + '-shm').exists():
        raise Rejected('LIVE_SQLITE_NOT_SUPPORTED')
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as db:
        if db.execute('pragma quick_check').fetchone()[0] != 'ok':
            raise Rejected('SQLITE_INVALID')
        if db.execute('select value from marker').fetchone() != ('synthetic',):
            raise Rejected('SQLITE_MARKER_INVALID')


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hsc-synthetic-recovery-')
        self.root = Path(self.tmp.name).resolve(strict=True)
        self.live = self.root / 'program'
        self.old = self.root / 'program-previous'
        self.source = self.root / 'synthetic-v2'
        self.stage = self.root / 'program-stage'
        self.data = self.root / 'synthetic-data'
        self.registry = self.root / 'synthetic-registry'
        self.backup = self.root / 'synthetic-backup'
        self.restored = self.root / 'synthetic-restored'
        self.journal = self.root / 'transaction.json'
        write_files(self.live, V1)
        write_files(self.source, V2)
        self.data.mkdir()
        with sqlite3.connect(self.data / 'sample.db') as db:
            db.execute('create table marker (value text not null)')
            db.execute("insert into marker values ('synthetic')")
        self.registry.mkdir()
        (self.registry / 'registry.json').write_text('{"synthetic_version":"1"}\n', encoding='utf-8')
        self.launched = False

    def close(self):
        self.tmp.cleanup()

    def verify_backup(self):
        if self.backup.exists() or self.restored.exists():
            raise Rejected('BACKUP_ALREADY_EXISTS')
        if manifest(self.live) != expected(V1):
            raise Rejected('OLD_VERSION_OR_MANIFEST_MISMATCH')
        sqlite_ok(self.data / 'sample.db')
        self.backup.mkdir()
        for name, path in [('program', self.live), ('data', self.data), ('registry', self.registry)]:
            before = manifest(path)
            shutil.copytree(path, self.backup / name, symlinks=True)
            if manifest(path) != before or manifest(self.backup / name) != before:
                raise Rejected('BACKUP_MISMATCH')
        shutil.copytree(self.backup, self.restored, symlinks=True)
        for name in ('program', 'data', 'registry'):
            if manifest(self.backup / name) != manifest(self.restored / name):
                raise Rejected('RESTORE_MISMATCH')
        sqlite_ok(self.restored / 'data' / 'sample.db')

    def write_journal(self, phase):
        value = json.dumps({'schema': 1, 'phase': phase}, sort_keys=True).encode()
        temp = self.root / 'transaction.pending'
        with temp.open('xb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.journal)
        # Directory flush is deliberately not claimed portable to Windows here.

    def upgrade(self, fail_at=None):
        if self.journal.exists() or self.old.exists() or self.stage.exists():
            raise Rejected('TRANSACTION_ALREADY_EXISTS')
        self.verify_backup()
        if manifest(self.source) != expected(V2):
            raise Rejected('NEW_VERSION_OR_MANIFEST_MISMATCH')
        shutil.copytree(self.source, self.stage, symlinks=True)
        if manifest(self.stage) != expected(V2):
            raise Rejected('STAGING_MISMATCH')
        if len({self.root.stat().st_dev, self.stage.stat().st_dev, self.live.stat().st_dev}) != 1:
            raise Rejected('NOT_SAME_VOLUME')
        if manifest(self.live) != expected(V1):
            raise Rejected('SOURCE_CHANGED')
        for name, path in [('program', self.live), ('data', self.data), ('registry', self.registry)]:
            if manifest(path) != manifest(self.backup / name) or manifest(path) != manifest(self.restored / name):
                raise Rejected('BACKUP_CHANGED')
        sqlite_ok(self.restored / 'data' / 'sample.db')
        for phase, action in [
            ('MOVE_OLD_INTENT', lambda: os.rename(self.live, self.old)),
            ('MOVE_NEW_INTENT', lambda: os.rename(self.stage, self.live)),
        ]:
            self.write_journal(phase)
            if fail_at == phase + '_BEFORE':
                raise Rejected('INJECTED_STOP')
            action()
            if fail_at == phase + '_AFTER':
                raise Rejected('INJECTED_STOP')
        self.write_journal('FILES_ACTIVE_NOT_STARTED')

    def mark_started(self):
        if json.loads(self.journal.read_text()).get('phase') != 'FILES_ACTIVE_NOT_STARTED':
            raise Rejected('UNKNOWN_JOURNAL_PHASE')
        if manifest(self.live) != expected(V2):
            raise Rejected('ACTIVE_MISMATCH')
        self.write_journal('STARTED_DATA_ROLLBACK_FORBIDDEN')
        self.launched = True

    def rollback_before_start(self):
        if self.launched:
            raise Rejected('DATABASE_MAY_HAVE_CHANGED')
        phase = json.loads(self.journal.read_text())['phase']
        if phase == 'STARTED_DATA_ROLLBACK_FORBIDDEN':
            raise Rejected('DATABASE_MAY_HAVE_CHANGED')
        if phase not in ('MOVE_OLD_INTENT', 'MOVE_NEW_INTENT', 'FILES_ACTIVE_NOT_STARTED'):
            raise Rejected('UNKNOWN_JOURNAL_PHASE')
        for name, path in [('data', self.data), ('registry', self.registry)]:
            if manifest(path) != manifest(self.backup / name) or manifest(path) != manifest(self.restored / name):
                raise Rejected('DATA_CHANGED_SINCE_BACKUP')
        # Require the recorded phase and the three on-disk paths to agree.
        # A broken symlink is present too and must never be treated as absent.
        paths = (self.live, self.old, self.stage)
        if any(path.is_symlink() for path in paths):
            raise Rejected('AMBIGUOUS_STATE')
        present = tuple(os.path.lexists(path) for path in paths)
        allowed = {
            'MOVE_OLD_INTENT': {(True, False, True), (False, True, True)},
            'MOVE_NEW_INTENT': {(False, True, True), (True, True, False)},
            'FILES_ACTIVE_NOT_STARTED': {(True, True, False)},
        }
        if present not in allowed[phase]:
            raise Rejected('AMBIGUOUS_STATE')
        if present[1] and manifest(self.old) != expected(V1):
            raise Rejected('OLD_COPY_MISMATCH')
        if present[2] and manifest(self.stage) != expected(V2):
            raise Rejected('STAGING_MISMATCH')
        if present[0]:
            expected_live = expected(V2) if present[1] else expected(V1)
            if manifest(self.live) != expected_live:
                raise Rejected('AMBIGUOUS_STATE')
        if present[1]:
            if present[0]:
                os.rename(self.live, self.stage)
            os.rename(self.old, self.live)
        self.write_journal('ROLLED_BACK')
