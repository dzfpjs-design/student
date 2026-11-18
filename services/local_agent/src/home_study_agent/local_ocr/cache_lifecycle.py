"""Owned OCR bodies and deletion fences. Callers own transactions and authorization."""
from __future__ import annotations

from hashlib import sha256
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class OcrActionConflict(ValueError):
    pass


class OcrSourceInvalid(ValueError):
    pass


def _exists(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def ensure_schema(c):
    """No executescript, implicit commit, connection, or worker side effects."""
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_job(
        job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, image_hash TEXT NOT NULL,
        preferred_route TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
        started_at TEXT,completed_at TEXT,result_json TEXT,error_code TEXT,
        child_id TEXT,session_id TEXT,parent_artifact_id TEXT,artifact_id TEXT,
        owner_key TEXT NOT NULL DEFAULT 'isolated',owner_epoch TEXT,
        monitor_generation INTEGER,pipeline_version INTEGER,run_token TEXT,
        publication_started INTEGER NOT NULL DEFAULT 0)""")
    columns = {r[1] for r in c.execute('PRAGMA table_info(ocr_job)')}
    old = 'owner_key' not in columns
    for name, declaration in (
        ('child_id', 'TEXT'), ('session_id', 'TEXT'), ('parent_artifact_id', 'TEXT'),
        ('artifact_id', 'TEXT'), ('owner_key', "TEXT NOT NULL DEFAULT 'isolated'"),
        ('owner_epoch', 'TEXT'), ('monitor_generation', 'INTEGER'),
        ('pipeline_version', 'INTEGER'), ('run_token', 'TEXT'),
        ('publication_started', 'INTEGER NOT NULL DEFAULT 0'),
    ):
        if name not in columns:
            c.execute(f'ALTER TABLE ocr_job ADD COLUMN {name} {declaration}')
    if old:
        c.execute("UPDATE ocr_job SET status='quarantined',error_code='OCR_LEGACY_UNOWNED',run_token=NULL")
    c.execute('CREATE INDEX IF NOT EXISTS idx_ocr_owned_child ON ocr_job(child_id,artifact_id)')
    if _exists(c, 'ocr_cache') and 'owner_key' not in {r[1] for r in c.execute('PRAGMA table_info(ocr_cache)')}:
        # Preserve legacy bodies, but never expose them as a usable cache entry.
        c.execute('ALTER TABLE ocr_cache RENAME TO ocr_cache_legacy_quarantine')
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_cache(
        owner_key TEXT NOT NULL,image_hash TEXT NOT NULL,result_json TEXT NOT NULL,
        created_at TEXT NOT NULL,PRIMARY KEY(owner_key,image_hash))""")
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_request_tombstone(
        request_digest TEXT PRIMARY KEY,reason TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_cache_epoch(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch TEXT NOT NULL)""")
    c.execute('INSERT OR IGNORE INTO ocr_cache_epoch VALUES(1,?)', (str(uuid4()),))
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_restore_fence(
        restore_id TEXT PRIMARY KEY)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_legacy_reference(
        store_id TEXT NOT NULL,job_id TEXT NOT NULL,child_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,source_kind TEXT NOT NULL,image_hash TEXT NOT NULL,
        PRIMARY KEY(store_id,job_id,child_id,artifact_id,source_kind))""")


def request_digest(request_id):
    return sha256(request_id.encode()).hexdigest()


def reject_deleted(c, request_id):
    row = c.execute('SELECT reason FROM ocr_request_tombstone WHERE request_digest=?',
                    (request_digest(request_id),)).fetchone()
    if row:
        codes = {'deleted': 'OCR_REQUEST_DELETED', 'restore': 'OCR_RESTORE_QUARANTINED', 'legacy': 'OCR_LEGACY_QUARANTINED'}
        raise OcrSourceInvalid(codes.get(row[0], 'OCR_QUARANTINED'))


def resolve_owner(c, request_id, *, session_id=None, monitor_generation=None):
    reject_deleted(c, request_id)
    epoch = c.execute('SELECT epoch FROM ocr_cache_epoch WHERE singleton=1').fetchone()[0]
    if session_id is not None:
        session = c.execute('SELECT child_id,status FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not session or session['status'] != 'active':
            raise OcrSourceInvalid('OCR_SESSION_NOT_ACTIVE')
        owner = dict(child_id=session['child_id'], session_id=session_id, parent_artifact_id=None,
                     owner_epoch=epoch, monitor_generation=monitor_generation)
    elif request_id.startswith('parent-textbook:') and _exists(c, 'kg_parent_textbook_ocr_artifact'):
        action_id = request_id.removeprefix('parent-textbook:')
        row = c.execute('SELECT artifact_id,child_id FROM kg_parent_textbook_ocr_artifact WHERE client_action_id=?',
                        (action_id,)).fetchone()
        if not row:
            row = c.execute('''SELECT a.artifact_id,a.child_id FROM kg_parent_textbook_ocr_artifact a
                JOIN kg_parent_textbook_ocr_review_event e ON e.artifact_id=a.artifact_id
                WHERE e.client_action_id=? AND e.event_type='retry_requested' AND e.payload_sha256=?''',
                (action_id, sha256(b'{}').hexdigest())).fetchone()
        if not row:
            raise OcrSourceInvalid('OCR_PARENT_SOURCE_MISSING')
        owner = dict(child_id=row['child_id'], session_id=None, parent_artifact_id=row['artifact_id'],
                     owner_epoch=epoch, monitor_generation=None)
    else:
        raise OcrSourceInvalid('OCR_OWNER_REQUIRED')
    owner['owner_key'] = sha256(json.dumps(owner, sort_keys=True).encode()).hexdigest()
    return owner


def assert_current(c, job):
    if job['owner_key'] == 'isolated':
        if _exists(c, 'child_profile'):
            raise OcrSourceInvalid('OCR_OWNER_REQUIRED')
        return
    epoch = c.execute('SELECT epoch FROM ocr_cache_epoch WHERE singleton=1').fetchone()[0]
    if job['owner_epoch'] != epoch:
        raise OcrSourceInvalid('OCR_RESTORE_QUARANTINED')
    child = c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (job['child_id'],)).fetchone()
    if not child:
        raise OcrSourceInvalid('OCR_CHILD_NOT_ACTIVE')
    if job['session_id']:
        row = c.execute("SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND status='active'",
                        (job['session_id'], job['child_id'])).fetchone()
        if not row:
            raise OcrSourceInvalid('OCR_SESSION_NOT_ACTIVE')
        if job['monitor_generation'] is not None:
            row = c.execute("""SELECT 1 FROM document_monitor_session m JOIN daily_session s ON s.session_id=m.session_id
                WHERE m.session_id=? AND m.child_id=? AND m.status='active' AND m.generation=?
                AND s.status='active' AND s.ended_at IS NULL
                AND (s.state='study' OR (s.state IN ('homework_capture','plan_review') AND s.observation_status='enabled'
                    AND EXISTS (SELECT 1 FROM sensor_lease l WHERE l.session_id=s.session_id
                        AND l.device_type='document_camera' AND l.status='active' AND julianday(l.expires_at)>julianday('now'))))""",
                (job['session_id'], job['child_id'], job['monitor_generation'])).fetchone()
            if not row:
                raise OcrSourceInvalid('OCR_MONITOR_INVALIDATED')
    else:
        row = c.execute("""SELECT a.* FROM kg_parent_textbook_ocr_artifact a
            WHERE a.artifact_id=? AND a.child_id=? AND a.status='processing' AND a.raw_image_path IS NOT NULL""",
            (job['parent_artifact_id'], job['child_id'])).fetchone()
        if not row:
            raise OcrSourceInvalid('OCR_PARENT_SOURCE_INVALIDATED')
        from ..knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
        try:
            request = ParentTextbookCaptureService._validate_retained_source(c, row)
        except (ValueError, KeyError, TypeError):
            raise OcrSourceInvalid('OCR_PARENT_SOURCE_INVALIDATED')
        if request.get('ocr_image_sha256', row['image_sha256']) != job['image_hash']:
            raise OcrSourceInvalid('OCR_PARENT_SOURCE_INVALIDATED')


def validate_publication(c, job_id, session_id, image_bytes):
    """LocalMediaAI calls this in its source INSERT transaction, and before replay."""
    job = c.execute('SELECT * FROM ocr_job WHERE job_id=?', (job_id,)).fetchone()
    if not job or job['status'] != 'completed' or not job['publication_started']:
        raise OcrSourceInvalid('OCR_PUBLICATION_NOT_AUTHORIZED')
    if job['session_id'] != session_id or job['image_hash'] != sha256(image_bytes).hexdigest():
        raise OcrActionConflict('OCR_PUBLICATION_CONFLICT')
    reject_deleted(c, job['request_id'])
    assert_current(c, job)


def delete_scope(c, child_id, artifact_id=None, *, parent_artifact_id=None):
    """Run BEFORE source deletion. Single-page and child scopes never touch other owners.

    Hold database.external_effect_guard around the outer privacy operation, also
    used by workflow publication. No schema initialization or commit occurs here.
    """
    if not _exists(c, 'ocr_job'):
        return {'ocr_jobs': 0, 'ocr_cache': 0}
    if 'child_id' not in {r[1] for r in c.execute('PRAGMA table_info(ocr_job)')}:
        raise OcrSourceInvalid('OCR_LEGACY_MIGRATION_REQUIRED')
    if artifact_id is not None and parent_artifact_id is not None:
        raise ValueError('OCR_DELETE_SCOPE_INVALID')
    where, params = 'child_id=?', [child_id]
    if artifact_id is not None:
        # A crash can occur after LocalMediaAI's commit but before the job's
        # artifact pointer is attached. Its frozen publication intent still owns it.
        where += """ AND (artifact_id=? OR (publication_started=1 AND request_id IN
            (SELECT 'pipeline:' || client_action_id FROM capture_artifact WHERE artifact_id=? AND child_id=?)))"""
        params.extend((artifact_id, artifact_id, child_id))
    if parent_artifact_id is not None:
        where += ' AND parent_artifact_id=?'
        params.append(parent_artifact_id)
    rows = c.execute('SELECT job_id,request_id,owner_key,image_hash FROM ocr_job WHERE ' + where, params).fetchall()
    cache = 0
    for row in rows:
        c.execute('INSERT OR IGNORE INTO ocr_request_tombstone VALUES(?,?)', (request_digest(row['request_id']), 'deleted'))
        cache += c.execute('DELETE FROM ocr_cache WHERE owner_key=? AND image_hash=?',
                           (row['owner_key'], row['image_hash'])).rowcount
    count = c.execute('DELETE FROM ocr_job WHERE ' + where, params).rowcount
    return {'ocr_jobs': count, 'ocr_cache': cache}


def quarantine_after_restore(c, restore_id):
    """Borrow the initialized staging transaction; never execute or requeue work."""
    if not _exists(c, 'ocr_job'):
        return {'ocr_jobs': 0, 'ocr_cache': 0}
    if not _exists(c, 'ocr_restore_fence'):
        raise OcrSourceInvalid('OCR_SCHEMA_INITIALIZATION_REQUIRED')
    if not c.execute('INSERT OR IGNORE INTO ocr_restore_fence VALUES(?)', (restore_id,)).rowcount:
        return {'ocr_jobs': 0, 'ocr_cache': 0}
    for row in c.execute('SELECT request_id FROM ocr_job'):
        c.execute('INSERT OR IGNORE INTO ocr_request_tombstone VALUES(?,?)', (request_digest(row[0]), 'restore'))
    c.execute('UPDATE ocr_cache_epoch SET epoch=? WHERE singleton=1', (str(uuid4()),))
    count = c.execute("UPDATE ocr_job SET status='quarantined',run_token=NULL,error_code='OCR_RESTORE_QUARANTINED'").rowcount
    cache = c.execute('DELETE FROM ocr_cache').rowcount
    return {'ocr_jobs': count, 'ocr_cache': cache}


def _legacy_path(database, path):
    path = Path(path).resolve()
    if path == database.path.resolve() or (path.exists() and database.path.exists() and path.samefile(database.path)):
        raise ValueError('OCR_LEGACY_MUST_BE_SEPARATE')
    return path, sha256(str(path).encode()).hexdigest()


def _legacy_inventory(source):
    jobs = []
    if _exists(source, 'ocr_job'):
        columns = {r[1] for r in source.execute('PRAGMA table_info(ocr_job)')}
        if not {'job_id', 'request_id', 'image_hash'}.issubset(columns):
            raise OcrSourceInvalid('OCR_LEGACY_SCHEMA_UNSUPPORTED')
        jobs = source.execute('SELECT job_id,request_id,image_hash FROM ocr_job').fetchall()
    caches = []
    for table in ('ocr_cache', 'ocr_cache_legacy_quarantine'):
        if _exists(source, table):
            caches.extend((table, r[0]) for r in source.execute(f'SELECT image_hash FROM {table}'))
    return jobs, caches


def register_legacy_store(database, legacy_path):
    """Index exact historical references, never import a legacy body as trusted work.

    Call before source deletion, and before exposing production workers. Missing
    ownership stays explicit; a key/hash alone never identifies a child.
    """
    path, store_id = _legacy_path(database, legacy_path)
    if not path.exists():
        return {'legacy_ocr_unattributed_jobs': 0, 'legacy_ocr_unattributed_cache': 0}
    with database.external_effect_guard(), closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        jobs, caches = _legacy_inventory(source)
        by_id = {row['job_id']: row for row in jobs}
        by_request = {row['request_id']: row for row in jobs}
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            ensure_schema(c)
            for row in jobs:
                owned_current = c.execute("SELECT 1 FROM ocr_job WHERE request_id=? AND child_id IS NOT NULL AND owner_key!='isolated'",
                                          (row['request_id'],)).fetchone()
                if not owned_current:
                    c.execute('INSERT OR IGNORE INTO ocr_request_tombstone VALUES(?,?)', (request_digest(row['request_id']), 'legacy'))
            if _exists(c, 'capture_artifact'):
                for artifact in c.execute("SELECT artifact_id,child_id,client_action_id,details_json FROM capture_artifact WHERE artifact_type IN ('document_ocr','document_ocr_candidate')"):
                    try:
                        details = json.loads(artifact['details_json'] or '{}')
                        task = details.get('local_pipeline', {}).get('queue', {}).get('task_id', '')
                    except (TypeError, ValueError, AttributeError):
                        continue  # Unparseable ownership is counted as unattributed, not silently deleted.
                    job = by_id.get(task.removeprefix('ocr:')) if isinstance(task, str) and task.startswith('ocr:') else None
                    if job and job['request_id'] == 'pipeline:' + artifact['client_action_id']:
                        c.execute('INSERT OR IGNORE INTO ocr_legacy_reference VALUES(?,?,?,?,?,?)',
                            (store_id, job['job_id'], artifact['child_id'], artifact['artifact_id'], 'capture_artifact', job['image_hash']))
            if _exists(c, 'kg_parent_textbook_ocr_artifact'):
                for artifact in c.execute('SELECT artifact_id,child_id,client_action_id,image_sha256 FROM kg_parent_textbook_ocr_artifact'):
                    job = by_request.get('parent-textbook:' + artifact['client_action_id'])
                    if job and job['image_hash'] == artifact['image_sha256']:
                        c.execute('INSERT OR IGNORE INTO ocr_legacy_reference VALUES(?,?,?,?,?,?)',
                            (store_id, job['job_id'], artifact['child_id'], artifact['artifact_id'], 'parent_textbook', job['image_hash']))
            owned = {r[0] for r in c.execute('SELECT job_id FROM ocr_legacy_reference WHERE store_id=?', (store_id,))}
            hashes = {row['image_hash'] for row in jobs if row['job_id'] in owned}
            c.commit()
        return {'legacy_ocr_unattributed_jobs': sum(row['job_id'] not in owned for row in jobs),
                'legacy_ocr_unattributed_cache': sum(digest not in hashes for _, digest in caches)}


def purge_legacy_scope(database, legacy_path, child_id, artifact_id=None, *, parent_artifact_id=None):
    """Idempotent external cleanup, AFTER the main source deletion commits.

    Metadata references survive a partial failure so PrivacyLifecycle can retry.
    Nonzero unattributed counts require explicit family-wide cache disposition;
    they must not be reported as proof that all derived text has been removed.
    """
    path, store_id = _legacy_path(database, legacy_path)
    if artifact_id is not None and parent_artifact_id is not None:
        raise ValueError('OCR_DELETE_SCOPE_INVALID')
    if not path.exists():
        return {'legacy_ocr_jobs_deleted': 0, 'legacy_ocr_cache_deleted': 0,
                'legacy_ocr_unattributed_jobs': 0, 'legacy_ocr_unattributed_cache': 0}
    where, args = 'store_id=? AND child_id=?', [store_id, child_id]
    if artifact_id is not None or parent_artifact_id is not None:
        where += ' AND artifact_id=? AND source_kind=?'
        args += [artifact_id or parent_artifact_id, 'capture_artifact' if artifact_id else 'parent_textbook']
    with database.external_effect_guard(), database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        targets = c.execute('SELECT * FROM ocr_legacy_reference WHERE ' + where, args).fetchall()
        remaining = c.execute('SELECT job_id FROM ocr_legacy_reference WHERE store_id=? AND NOT (' + where + ')', [store_id, *args]).fetchall()
        shared = {row[0] for row in remaining}
        deleted, removed_cache = 0, 0
        with closing(sqlite3.connect(path)) as source:
            source.row_factory = sqlite3.Row
            source.execute('PRAGMA secure_delete=ON')
            source.execute('BEGIN IMMEDIATE')
            for row in targets:
                if row['job_id'] in shared:
                    continue
                deleted += source.execute('DELETE FROM ocr_job WHERE job_id=?', (row['job_id'],)).rowcount
                if not source.execute('SELECT 1 FROM ocr_job WHERE image_hash=?', (row['image_hash'],)).fetchone():
                    for table in ('ocr_cache', 'ocr_cache_legacy_quarantine'):
                        if _exists(source, table):
                            removed_cache += source.execute(f'DELETE FROM {table} WHERE image_hash=?', (row['image_hash'],)).rowcount
            source.commit()
            if source.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise OcrSourceInvalid('OCR_LEGACY_FILE_BUSY')
            jobs, caches = _legacy_inventory(source)
        c.execute('DELETE FROM ocr_legacy_reference WHERE ' + where, args)
        known = {r[0] for r in c.execute('SELECT job_id FROM ocr_legacy_reference WHERE store_id=?', (store_id,))}
        hashes = {row['image_hash'] for row in jobs if row['job_id'] in known}
        c.commit()
    return {'legacy_ocr_jobs_deleted': deleted, 'legacy_ocr_cache_deleted': removed_cache,
            'legacy_ocr_unattributed_jobs': sum(row['job_id'] not in known for row in jobs),
            'legacy_ocr_unattributed_cache': sum(digest not in hashes for _, digest in caches)}
