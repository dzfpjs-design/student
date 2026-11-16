"""Explicit household disposition of an exact, unattributed legacy OCR snapshot."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

from .cache_lifecycle import ensure_schema, register_legacy_store, _legacy_path, _exists


class LegacyDispositionConflict(ValueError):
    pass


class LegacyDispositionInput(ValueError):
    pass


class LegacyDispositionIncomplete(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


class LegacyOcrDispositionService:
    confirmation_text = '确认清理无法归属的旧OCR缓存'

    def __init__(self, database, legacy_path):
        self.database = database
        self.path, self.store_id = _legacy_path(database, legacy_path)
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            ensure_schema(c)
            c.execute("""CREATE TABLE IF NOT EXISTS ocr_legacy_disposition_preview(
                preview_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,owner_epoch TEXT NOT NULL,
                fingerprint TEXT NOT NULL,selection_json TEXT NOT NULL,counts_json TEXT NOT NULL,
                created_at TEXT NOT NULL,expires_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS ocr_legacy_disposition_receipt(
                receipt_id TEXT PRIMARY KEY,preview_id TEXT NOT NULL,store_id TEXT NOT NULL,
                action_digest TEXT NOT NULL UNIQUE,request_json TEXT NOT NULL,owner_epoch TEXT NOT NULL,
                selection_json TEXT NOT NULL,selected_counts_json TEXT NOT NULL,
                status TEXT NOT NULL,error_code TEXT,result_json TEXT,created_at TEXT NOT NULL)""")
            c.commit()

    def _snapshot(self, c, side):
        references = [dict(r) for r in c.execute('SELECT * FROM ocr_legacy_reference WHERE store_id=? ORDER BY job_id,child_id,artifact_id,source_kind', (self.store_id,))]
        protected = {r['job_id'] for r in references}
        jobs = [dict(r) for r in side.execute('SELECT * FROM ocr_job ORDER BY job_id')] if _exists(side, 'ocr_job') else []
        # Missing main references do not erase ownership already stored in an upgraded legacy DB.
        protected.update(r['job_id'] for r in jobs if any(r.get(k) for k in
            ('child_id', 'session_id', 'parent_artifact_id', 'artifact_id')) or r.get('owner_key') not in (None, '', 'isolated'))
        protected_hashes = {r['image_hash'] for r in jobs if r['job_id'] in protected}
        selected, entries = [], []
        counts = dict(unattributed_jobs=0, unattributed_cache=0, protected_jobs=0, protected_cache=0)
        for row in jobs:
            entry = dict(table='ocr_job', key={'job_id': row['job_id']}, digest=_digest(row))
            entries.append(entry)
            owned = row['job_id'] in protected
            counts['protected_jobs' if owned else 'unattributed_jobs'] += 1
            if not owned:
                selected.append(entry)
        for table in ('ocr_cache', 'ocr_cache_legacy_quarantine'):
            if not _exists(side, table):
                continue
            columns = {r[1] for r in side.execute(f'PRAGMA table_info({table})')}
            ordering = 'image_hash,owner_key' if 'owner_key' in columns else 'image_hash'
            for item in side.execute(f'SELECT * FROM {table} ORDER BY {ordering}'):
                row = dict(item)
                key = {'image_hash': row['image_hash']}
                if 'owner_key' in columns:
                    key['owner_key'] = row['owner_key']
                entry = dict(table=table, key=key, digest=_digest(row))
                entries.append(entry)
                owned = row['image_hash'] in protected_hashes or row.get('owner_key') not in (None, '', 'isolated')
                counts['protected_cache' if owned else 'unattributed_cache'] += 1
                if not owned:
                    selected.append(entry)
        epoch = c.execute('SELECT epoch FROM ocr_cache_epoch WHERE singleton=1').fetchone()[0]
        fingerprint = _digest(dict(entries=entries, references=references, store_id=self.store_id, epoch=epoch))
        return dict(selection=selected, counts=counts, fingerprint=fingerprint, epoch=epoch)

    def preview_unknown(self):
        with self.database.external_effect_guard():
            register_legacy_store(self.database, self.path)
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if self.path.exists():
                    with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as side:
                        side.row_factory = sqlite3.Row
                        side.execute('BEGIN')
                        snapshot = self._snapshot(c, side)
                else:
                    with closing(sqlite3.connect(':memory:')) as side:
                        side.row_factory = sqlite3.Row
                        snapshot = self._snapshot(c, side)
                now = datetime.now(timezone.utc)
                preview_id, expires = str(uuid4()), (now + timedelta(minutes=10)).isoformat()
                c.execute('INSERT INTO ocr_legacy_disposition_preview VALUES(?,?,?,?,?,?,?,?)',
                    (preview_id, self.store_id, snapshot['epoch'], snapshot['fingerprint'],
                     _json(snapshot['selection']), _json(snapshot['counts']), now.isoformat(), expires))
                c.commit()
        return dict(preview_id=preview_id, snapshot_fingerprint=snapshot['fingerprint'], expires_at=expires,
            counts=snapshot['counts'], scope='legacy_unattributed_ocr_cache', current_database_included=False,
            confirmation_text=self.confirmation_text, local_only=True, cloud_transmission_allowed=False)

    @staticmethod
    def _predicate(entry):
        table, key = entry['table'], entry['key']
        if table == 'ocr_job' and set(key) == {'job_id'}:
            pass
        elif table in {'ocr_cache', 'ocr_cache_legacy_quarantine'} and set(key) in ({'image_hash'}, {'image_hash', 'owner_key'}):
            pass
        else:
            raise LegacyDispositionConflict('LEGACY_DISPOSITION_JOURNAL_INVALID')
        return table, ' AND '.join(name + '=?' for name in key), list(key.values())

    def dispose_unknown(self, *, preview_id, expected_fingerprint, client_action_id, reviewer_id, confirmation):
        if (confirmation != self.confirmation_text or any(not isinstance(v, str) or not v.strip() or len(v) > 200
                for v in (preview_id, expected_fingerprint, client_action_id, reviewer_id))):
            raise LegacyDispositionInput('LEGACY_DISPOSITION_CONFIRMATION_REQUIRED')
        request = _json(dict(preview_id=preview_id, fingerprint=expected_fingerprint, reviewer_id=reviewer_id))
        action_digest, receipt_id = _digest(client_action_id), None
        with self.database.external_effect_guard():
            # Ownership can change after preview; refresh before accepting consent.
            register_legacy_store(self.database, self.path)
            with self.database.connect() as c:
                existing = c.execute('SELECT * FROM ocr_legacy_disposition_receipt WHERE action_digest=?', (action_digest,)).fetchone()
                if existing and (existing['request_json'] != request or existing['store_id'] != self.store_id):
                    raise LegacyDispositionConflict('LEGACY_DISPOSITION_ACTION_CONFLICT')
                if existing and existing['status'] == 'completed':
                    return self._public(c, existing)
                preview = c.execute('SELECT * FROM ocr_legacy_disposition_preview WHERE preview_id=? AND store_id=?',
                                    (preview_id, self.store_id)).fetchone()
                epoch = c.execute('SELECT epoch FROM ocr_cache_epoch WHERE singleton=1').fetchone()[0]
                if (not preview or preview['fingerprint'] != expected_fingerprint or preview['owner_epoch'] != epoch
                        or not existing and datetime.fromisoformat(preview['expires_at']) < datetime.now(timezone.utc)):
                    raise LegacyDispositionConflict('LEGACY_DISPOSITION_PREVIEW_STALE')
                if not self.path.exists():
                    raise LegacyDispositionConflict('LEGACY_DISPOSITION_SOURCE_MISSING')
                selection = json.loads(existing['selection_json'] if existing else preview['selection_json'])
                if not selection:
                    raise LegacyDispositionInput('LEGACY_DISPOSITION_NO_UNKNOWN_RECORDS')
                try:
                    with closing(sqlite3.connect(self.path)) as side:
                        side.row_factory = sqlite3.Row
                        side.execute('PRAGMA secure_delete=ON')
                        side.execute('BEGIN IMMEDIATE')
                        current = self._snapshot(c, side)
                        if not existing and current['fingerprint'] != expected_fingerprint:
                            raise LegacyDispositionConflict('LEGACY_DISPOSITION_PREVIEW_STALE')
                        eligible = {_json(entry['key']) + entry['table']: entry for entry in current['selection']}
                        for entry in selection:
                            table, where, args = self._predicate(entry)
                            row = side.execute(f'SELECT * FROM {table} WHERE {where}', args).fetchone()
                            if row and (_digest(dict(row)) != entry['digest'] or _json(entry['key']) + table not in eligible):
                                raise LegacyDispositionConflict('LEGACY_DISPOSITION_RECORD_CHANGED_OR_OWNED')
                        receipt_id = existing['receipt_id'] if existing else str(uuid4())
                        if not existing:
                            c.execute('INSERT INTO ocr_legacy_disposition_receipt VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                (receipt_id, preview_id, self.store_id, action_digest, request, epoch,
                                 _json(selection), _json({k: v for k, v in json.loads(preview['counts_json']).items() if k.startswith('unattributed_')}),
                                 'prepared', None, None, datetime.now(timezone.utc).isoformat()))
                            c.commit()
                        for entry in selection:
                            table, where, args = self._predicate(entry)
                            side.execute(f'DELETE FROM {table} WHERE {where}', args)
                        side.commit()
                        if side.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                            raise OSError('legacy cache checkpoint busy')
                        remaining = self._snapshot(c, side)['counts']
                    self._complete_receipt(c, receipt_id, remaining)
                    return self._public(c, c.execute('SELECT * FROM ocr_legacy_disposition_receipt WHERE receipt_id=?', (receipt_id,)).fetchone())
                except (sqlite3.Error, OSError) as error:
                    if receipt_id:
                        c.execute("UPDATE ocr_legacy_disposition_receipt SET status='failed',error_code='LEGACY_DISPOSITION_INCOMPLETE' WHERE receipt_id=?", (receipt_id,))
                        c.commit()
                    raise LegacyDispositionIncomplete('LEGACY_DISPOSITION_INCOMPLETE') from error

    def _complete_receipt(self, c, receipt_id, remaining):
        c.execute("UPDATE ocr_legacy_disposition_receipt SET status='completed',error_code=NULL,result_json=? WHERE receipt_id=?",
                  (_json({'remaining_counts': remaining}), receipt_id))
        c.commit()

    def _public(self, c, row):
        epoch = c.execute('SELECT epoch FROM ocr_cache_epoch WHERE singleton=1').fetchone()[0]
        return dict(receipt_id=row['receipt_id'], preview_id=row['preview_id'], status=row['status'],
            selected_counts=json.loads(row['selected_counts_json']),
            remaining_counts=json.loads(row['result_json'])['remaining_counts'] if row['result_json'] else None,
            error_code=row['error_code'], restored_invalidated=row['owner_epoch'] != epoch,
            current_database_included=False, local_only=True, cloud_transmission_allowed=False)

    def get_receipt(self, receipt_id):
        with self.database.connect() as c:
            row = c.execute('SELECT * FROM ocr_legacy_disposition_receipt WHERE receipt_id=? AND store_id=?', (receipt_id, self.store_id)).fetchone()
            if not row:
                raise KeyError('LEGACY_DISPOSITION_NOT_FOUND')
            return self._public(c, row)
