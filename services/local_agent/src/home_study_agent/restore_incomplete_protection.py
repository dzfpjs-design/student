"""Original-owner protection for repair of a precisely missing standard PDF.

This is neither a FULL backup nor permission to ignore an unreadable asset.
Only a sealed, same-record PDF may stand in for a true ENOENT in the private
inventory probe. Live files, registry values and the ordinary collector stay
unchanged. SQLite's original publication transaction still owns publication.
"""
from contextlib import contextmanager
from functools import wraps
import errno
import os
import sqlite3
from pathlib import Path

from . import restore_publication as pub
from . import registered_asset_snapshot as snap
from . import registered_asset_backup as reg
from .backup_local_io import io_path

CODE = 'RESTORE_CURRENT_PROTECTION_UNCONFIRMED'
FIELDS = ('source_id', 'source_key', 'local_root_path', 'pdf_filename', 'pdf_sha256', 'text_sha256')
STATUS = 'internal_database_only'


def _finite(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, sqlite3.Error, snap.AssetSnapshotError) as error:
            if isinstance(error, (OSError, sqlite3.Error)) or (
                    type(error) is snap.AssetSnapshotError and error.args == ('HANDLE_CLOSE_UNCONFIRMED',)):
                # Preserve the same original exception/traceback before its
                # finite public projection. This includes original output
                # files and popped ExitStack/native callbacks, never a count
                # or reopened handle. Poison precedes any possible unwinding.
                protection = args[0]
                protection.owner.gate.poisoned = True
                protection.owner.gate.retained.append(('incomplete_current_io', protection, error))
            raise pub.RestorePublicationError(CODE) from None
    return run


def public_result(recovery):
    result = {'recovery_point_backup_id': recovery['backup_id']}
    if recovery['backup_id'] is None:
        pub._need(recovery.get('restore_protection') == STATUS, CODE)
        result['restore_protection'] = STATUS
    return result


def _absent(parent, name):
    """True ENOENT only; a dangling link is an existing entry and must fail."""
    parent.check()
    try:
        if os.name == 'nt':
            os.stat(io_path(parent.path / name), follow_symlinks=False)
        else:
            os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError as error:
        pub._need(error.errno == errno.ENOENT, CODE)
        parent.check()
        return
    pub._need(False, CODE)


def _standard_rows(connection, policy):
    result = {}
    for count, row in enumerate(connection.execute('SELECT ' + ','.join(FIELDS) + ' FROM curriculum_standard_source'), 1):
        pub._need(count <= policy.max_files and str(row[0]) not in result, CODE)
        result[str(row[0])] = tuple(row)
    return result


class _RepairProbe(snap._Collector):
    """Independent inventory; no mutation/monkeypatch of the normal collector."""
    def __init__(self, stage, scratch, policy, protection, source_rows, current_rows, sealed):
        super().__init__(stage, scratch, policy)
        self.protection = protection
        self.source_rows = source_rows
        self.current_rows = current_rows
        self.sealed = sealed

    def ordinary(self, *args, **kwargs):
        try:
            return super().add(*args, **kwargs)
        except (OSError, sqlite3.Error) as error:
            # Retain the original traceback/collector too: a failing original
            # collector close must not lose its original file/SQL object.
            self.protection.owner.gate.poisoned = True
            self.protection.owner.gate.retained.append(('incomplete_probe_io', self, error))
            raise

    def add(self, kind, row, role, root, relative, *, sqlite=False, version=None, expected_sha=None):
        # Every other role, including unknown/current-only missing references,
        # uses the unmodified collector and must be fully readable.
        if kind != 'standard' or role != 'pdf' or sqlite:
            return self.ordinary(kind, row, role, root, relative, sqlite=sqlite, version=version, expected_sha=expected_sha)
        key = str(row)
        source = self.source_rows.get(key)
        current = self.current_rows.get(key)
        pub._need(source is not None and current == source and current[2:4] == (str(root), relative), CODE)
        parts = snap.safe_relative(relative, self.policy)
        pub._need(len(parts) == 1 and expected_sha == current[4], CODE)
        root = self.root(root)
        parent = self.protection.stack.enter_context(snap._held_directory(root, self.policy))
        try:
            if os.name == 'nt':
                os.stat(io_path(root / relative), follow_symlinks=False)
            else:
                os.stat(relative, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError as error:
            pub._need(error.errno == errno.ENOENT, CODE)
        else:
            return self.ordinary(kind, row, role, root, relative, version=version, expected_sha=expected_sha)
        _absent(parent, relative)
        name = 'registered/standard/' + snap.record_key(kind, row) + '/pdf/' + relative
        snap.safe_relative(name, self.policy)
        authority = self.sealed.authority
        pub._need(name in authority.registered_payloads and name in authority.held_registered, CODE)
        target = authority.registered_payloads[name]
        item = authority.plan[target]
        held = authority.held_registered[name]
        pub._need(item['sha256'] == expected_sha and held.hash() == (item['sha256'], item['bytes']), CODE)
        pub._need(len(self.protection.missing) < 16 and name not in self.assets
                  and name.casefold() not in self.folded
                  and str(root / relative).casefold() not in self.managed_media, CODE)
        out = self.stage / name
        io_path(out.parent).mkdir(parents=True, exist_ok=True)
        pub._need(item['bytes'] <= self.policy.max_total_bytes - self.total, CODE)
        digest, size = held.copy(out)
        pub._need((digest, size) == (expected_sha, item['bytes']), CODE)
        held.check()
        self.total += size
        pub._need(self.total <= self.policy.max_total_bytes and len(self.assets) < self.policy.max_files, CODE)
        self.folded[name.casefold()] = name
        self.assets[name] = ({'logical_name': name, 'kind': kind,
            'record_sha256': snap.record_key(kind, row), 'role': role,
            'relative_path': relative, 'bytes': size, 'sha256': digest,
            'storage': 'regular', 'version_sha256s': [snap.digest(snap.canonical(version))]},
            (str(root), relative, False))
        self.protection.missing.append((parent, relative))
        return out


class Protection:
    def __init__(self, service, owner, work, verify, seal):
        from .parent_restore_workflow import original_source_bundle
        from . import parent_restore_assets as assets
        pub._need(seal is not None, CODE)
        self.bundle = original_source_bundle(service, work, verify)
        pub._need(self.bundle.capsule_held is not None, CODE)
        self.sealed = assets._seal(seal, owner)
        pub._need(self.sealed.authority.work is work and self.sealed.service is service, CODE)
        self.service = service; self.owner = owner; self.work = work; self.verify = verify
        self.stack = self.bundle.stack; self.missing = []; self.witness = None; self.cm = None; self.close_attempted = False
        self.path = work.work / 'rollback-current.db'
        self.meta = work.work / 'rollback-current.json'
        # Original durable namespace/identities precede any bytes or SQL. These
        # own-anchor changes deliberately precede data_version observation.
        work.add_file(self.path, family=True)
        work.add_file(self.meta)

    @contextmanager
    def probe_work(self):
        self.probe_cm = reg.owned_work(self.service.data_dir / 'backups')
        try:
            stage = self.probe_cm.__enter__()
        except BaseException as error:
            self.owner.gate.poisoned = True
            self.owner.gate.retained.append(('incomplete_probe_enter', self, error))
            raise
        try:
            yield stage
        except BaseException:
            import sys
            error = sys.exc_info()
            try:
                self.probe_cm.__exit__(*error)
            except BaseException as cleanup_error:
                if cleanup_error is not error[1]:
                    self.owner.gate.poisoned = True
                    self.owner.gate.retained.append(('incomplete_probe_cleanup', self, cleanup_error))
                raise
            raise
        else:
            try:
                self.probe_cm.__exit__(None, None, None)
            except BaseException as cleanup_error:
                self.owner.gate.poisoned = True
                self.owner.gate.retained.append(('incomplete_probe_cleanup', self, cleanup_error))
                raise

    @_finite
    def capture(self):
        from . import parent_restore_assets as assets
        binding = self.work._value()['binding']
        self.cm = pub._sql(self.owner.path, owner=self.owner, readonly=True, timeout=0)
        self.witness = self.cm.__enter__()  # Original registered SQL/close owner.
        self.version = self.witness.execute('PRAGMA data_version').fetchone()[0]
        pub._binding_facts(self.witness, binding)
        pages = self.witness.execute('PRAGMA page_count').fetchone()[0]
        page_size = self.witness.execute('PRAGMA page_size').fetchone()[0]
        pub._need(type(pages) is int and type(page_size) is int and 0 < pages * page_size <= reg.SAFE - reg.M, CODE)
        reg.space(self.work.work, pages * page_size + reg.M)
        with pub._sql(self.path, owner=self.owner, timeout=0) as destination:
            destination.execute('PRAGMA synchronous=FULL')
            def progress(status, remaining, total):
                pub._need(status not in (5, 6), 'RESTORE_PUBLICATION_BUSY')
            self.witness.backup(destination, pages=256, progress=progress)
            pub._need(destination.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete', CODE)
            pub._need(destination.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                      and destination.execute('PRAGMA foreign_key_check').fetchone() is None, CODE)
            pub._binding_facts(destination, binding)
        policy = reg.stage_policy(self.service.data_dir / 'backups')
        with pub._cold(self.work.work / 'authenticated-cold.db', owner=self.owner) as source:
            source_rows = _standard_rows(source, policy)
        # This private stage is never a backup, manifest, authority or live root.
        # Its original context is retained in RAM on an uncertain cleanup.
        with self.probe_work() as stage:
            scratch = stage / '.working'; io_path(scratch).mkdir(mode=0o700)
            with pub._cold(self.path, owner=self.owner) as current:
                current.row_factory = sqlite3.Row
                current_rows = _standard_rows(current, policy)
                with assets.held_registered_payloads(self.sealed.cap):
                    probe = _RepairProbe(stage, scratch, policy, self, source_rows, current_rows, self.sealed)
                    probe.inventory(current)
                    for path, identity in probe.roots.items():
                        pub._need(snap._check_directory(Path(path), policy) == identity, CODE)
                    pub._need(self.missing, CODE)
                    self.check()
        db_policy = snap.Policy(reg.MAX_FILES, 64, 1024, reg.M, reg.SAFE, reg.SAFE)
        self.held = self.stack.enter_context(snap.HeldFile(self.path.parent, self.path.name, db_policy))
        digest, size = self.held.hash(); self.held.check()
        self.record = {'schema': 1, 'kind': STATUS, 'binding': binding,
            'database': {'sha256': digest, 'bytes': size}, 'missing_pdf_count': len(self.missing)}
        self.work.metadata(self.meta, self.record)
        self.owner.parent.flush()
        pub._fault('after_incomplete_current_protection')

    def check(self):
        self.verify()
        pub._need(self.witness is not None and not self.witness.in_transaction
                  and self.witness.execute('PRAGMA data_version').fetchone()[0] == self.version, CODE)
        pub._binding_facts(self.witness, self.work._value()['binding'])
        for parent, name in self.missing:
            _absent(parent, name)

    @_finite
    def before_publication(self):
        self.check()
        pub._need(self.held.hash() == (self.record['database']['sha256'], self.record['database']['bytes']), CODE)
        # No live witness crosses the original prepare/reconcile cold scopes.
        self.close()
        pub._need(self.witness is None, CODE)
        for parent, name in self.missing:
            _absent(parent, name)

    def close(self):
        if self.witness is not None:
            # _sql performs the sole original close, retention and poisoning.
            pub._need(not self.close_attempted, 'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')
            self.close_attempted = True
            try:
                self.cm.__exit__(None, None, None)
            except BaseException as error:
                self.owner.gate.retained.append(('incomplete_protection_sql_close', self, error))
                raise
            self.witness = None

    def failed(self):
        # A failure during cleanup may leave unknown original handles or stage.
        # Never discard RAM responsibility or manufacture successful closure.
        if self.close_attempted and self.witness is not None:
            return  # Original close already failed; no retry or lost identity.
        try:
            self.close()
        except BaseException:
            self.owner.gate.poisoned = True
            self.owner.gate.retained.append(('incomplete_current_protection', self))
            raise

