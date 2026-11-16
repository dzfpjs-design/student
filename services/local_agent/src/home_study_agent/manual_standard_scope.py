"""One parent-reviewed local official-page requirement; no model or extraction job.

The server renders the registered PDF. Request image hashes/paths are not accepted.
Downloaded does not become whole-source ready. Only the exact approved child,
school node, quote and two page proofs can enter the explicit manual branch.
"""
from __future__ import annotations

from contextlib import nullcontext
from collections import OrderedDict
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Optional
from urllib.parse import urlparse
from uuid import uuid4

import cv2
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .parent_auth import ParentSessionRequiredError
from .repository import utc_now
from .knowledge_ingestion.textbook_pdf import TextbookPdfOcrPipeline
from .curriculum_standards import ALLOWED_DOMAINS
from .pdf_renderer import pdf_renderer_available

ORIGIN = 'parent_reviewed_single_official_scope_v1'

# Pure parsing metadata only. Every source/byte/renderer check still runs below.
_PDF_PAGE_COUNTS = OrderedDict()
_PDF_PAGE_COUNTS_LOCK = Lock()
_PDF_PAGE_COUNTS_LIMIT = 32


class ManualScopeError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def require(ok, code, status=409):
    if not ok:
        raise ManualScopeError(code, status)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def file_bytes(path):
    path = Path(path)
    require(path.is_absolute(), 'MANUAL_SOURCE_PATH_REQUIRED')
    for p in (path, *path.parents):
        require(not p.is_symlink(), 'MANUAL_SOURCE_ALIAS_REJECTED')
    require(path.is_file() and path.stat().st_size <= 80_000_000, 'MANUAL_SOURCE_FILE_REQUIRED')
    return path.read_bytes()


def ensure_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS curriculum_manual_pdf_blob(
      pdf_sha256 TEXT PRIMARY KEY,page_count INTEGER NOT NULL,content BLOB NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS curriculum_manual_requirement(
      candidate_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),
      version INTEGER NOT NULL,state TEXT NOT NULL CHECK(state IN ('draft','approved','rejected','withdrawn')),
      payload_json TEXT NOT NULL,source_json TEXT NOT NULL,scope_json TEXT NOT NULL,
      pages_json TEXT NOT NULL,requirement_id TEXT UNIQUE REFERENCES curriculum_standard_requirement(requirement_id),
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS curriculum_manual_requirement_page(
      candidate_id TEXT NOT NULL REFERENCES curriculum_manual_requirement(candidate_id),
      pdf_page INTEGER NOT NULL,image BLOB NOT NULL,PRIMARY KEY(candidate_id,pdf_page))''')
    c.execute('''CREATE TABLE IF NOT EXISTS curriculum_manual_requirement_event(
      event_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES curriculum_manual_requirement(candidate_id),
      child_id TEXT NOT NULL,kind TEXT NOT NULL,action_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
      version INTEGER NOT NULL,note TEXT,occurred_at TEXT NOT NULL,UNIQUE(child_id,action_id))''')


def school_scope(c, child, node_id=None, *, allow_missing=False):
    row = c.execute('SELECT grade,status FROM child_profile WHERE child_id=?', (child,)).fetchone()
    require(row is not None and row['status'] == 'active', 'MANUAL_SCOPE_NOT_FOUND', 404)
    require(row['grade'] in {'三年级', '四年级'}, 'MANUAL_CURRENT_SCHOOL_SCOPE_REQUIRED')
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (node_id,)).fetchone() if node_id else None
    if node is None:
        require(allow_missing, 'MANUAL_CURRENT_SCHOOL_NODE_REQUIRED')
    else:
        require(node['track'] == 'school' and node['subject'] == '数学' and node['grade_band'] == row['grade']
                and node['node_type'] == 'skill' and node['status'] == 'active', 'MANUAL_CURRENT_SCHOOL_NODE_REQUIRED')
    return {'child_id': child, 'grade': row['grade'], 'subject': '数学', 'stage_scope': '第二学段',
            'node': dict(node) if node else None}


# Used only by Root's just-opened original query-only business snapshot.
# It saves SQLite BLOB materialization, never original-file validation or rights.
from contextlib import contextmanager as _pdf_contextmanager
from contextvars import ContextVar as _PdfContextVar
from itertools import count as _pdf_count

_SAVED_PDF_MEMO = _PdfContextVar('root_saved_pdf_snapshot_blob', default=None)
_PDF_BLOB_PROBES = _pdf_count(1)
_PDF_BLOB_MEMO_LIMIT = 80_000_000
_PDF_BLOB_SQL = 'SELECT page_count,content FROM curriculum_manual_pdf_blob WHERE pdf_sha256=?'
_PDF_C_CURSOR = sqlite3.Connection.cursor
_PDF_C_AUTHORIZER = sqlite3.Connection.set_authorizer


def _pdf_observer_retired(*args):
    # Python 3.9 does not support set_authorizer(None). The Root-only
    # connection closes next; this callback has no state or permission effect.
    return sqlite3.SQLITE_OK


class _SavedPdfBlobMemo:
    def __init__(self, connection):
        from .database import StudyConnection
        from .original_connection_read import _state
        from .standard_relocation_provenance import _BUSINESS_SCOPES
        require(type(connection) is StudyConnection, 'MANUAL_PDF_BLOB_ORIGINAL_SNAPSHOT_REQUIRED')
        self.connection = connection
        self.proof = _state(connection)[0]
        markers = [entry for entry in _BUSINESS_SCOPES.get() if entry[0]() is connection]
        require(len(markers) == 1, 'MANUAL_PDF_BLOB_ORIGINAL_SNAPSHOT_REQUIRED')
        self.marker = markers[0]
        self.changes = connection.total_changes
        self.valid, self.selects, self.size, self.rows = True, 0, 0, {}

    def invalidate(self):
        self.valid = False
        self.rows.clear()
        self.size = 0

    def observe(self, action, first, second, database, trigger):
        # Observe only closed action numbers. Never log SQL, values or identifiers.
        if action == sqlite3.SQLITE_SELECT:
            self.selects += 1
        if (action in {
            sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_ANALYZE,
        } or (action == sqlite3.SQLITE_PRAGMA and first == 'query_only'
              and second is not None and str(second).lower() not in {'1', 'on', 'yes', 'true'})):
            self.invalidate()
        return sqlite3.SQLITE_OK  # No permission is added, removed or reinterpreted.

    def check(self, connection):
        from .original_connection_read import _core_state, _same_active_read_sql
        from .standard_relocation_provenance import _BUSINESS_SCOPES
        try:
            original = self.connection
            require(self.valid and (connection is original or _same_active_read_sql(connection, original))
                    and original.in_transaction and original.total_changes == self.changes
                    and any(entry is self.marker for entry in _BUSINESS_SCOPES.get())
                    and _core_state(original)[0] is self.proof,
                    'MANUAL_PDF_BLOB_SNAPSHOT_CHANGED')
            cursor = _PDF_C_CURSOR(original, factory=sqlite3.Cursor)
            try:
                cursor.row_factory = None
                require(cursor.execute('PRAGMA query_only').fetchone() == (1,),
                        'MANUAL_PDF_BLOB_SNAPSHOT_CHANGED')
                from . import restore_publication as _pdf_pub
                databases = cursor.execute('PRAGMA database_list').fetchall()
                require(len(databases) in (1, 2) and databases[0][:2] == (0, 'main')
                        and _pdf_pub._path(databases[0][2]) == self.proof[3]
                        and (len(databases) == 1 or databases[1] == (1, 'temp', '')),
                        'MANUAL_PDF_BLOB_SNAPSHOT_CHANGED')
                seen = self.selects
                # Local ordinal forces a new prepare; no payload enters this SQL.
                cursor.execute('SELECT 1 /*hsc_saved_pdf_probe_' + str(next(_PDF_BLOB_PROBES)) + '*/').fetchone()
                require(self.valid and self.selects > seen, 'MANUAL_PDF_BLOB_OBSERVER_CHANGED')
            finally:
                cursor.close()
        except BaseException:
            self.invalidate()
            raise


@_pdf_contextmanager
def _saved_pdf_blob_snapshot(connection):
    """Root-only callsite: the fresh Database.connect owner has no prior hook.

    The caller installs this immediately inside its original business snapshot.
    Normal product reads do not enter it. Unknown/preexisting hook ownership must
    never be adopted; there is no public endpoint or serialized activation flag.
    """
    require(_SAVED_PDF_MEMO.get() is None, 'MANUAL_PDF_BLOB_NESTED_SCOPE_REJECTED')
    memo = _SavedPdfBlobMemo(connection)
    token, attempted, body_error = None, False, None
    try:
        attempted = True
        _PDF_C_AUTHORIZER(connection, memo.observe)
        memo.check(connection)
        token = _SAVED_PDF_MEMO.set(memo)
        yield
    except BaseException as error:
        body_error = error
        raise
    finally:
        memo.invalidate()
        try:
            if attempted:
                try:
                    _PDF_C_AUTHORIZER(connection, _pdf_observer_retired)
                except BaseException as error:
                    from .original_connection_read import poison_original
                    poison_original(connection, ('saved_pdf_observer_close', connection, error))
                    if body_error is None:
                        raise ManualScopeError('MANUAL_PDF_BLOB_OBSERVER_CLOSE_UNCONFIRMED') from None
        finally:
            if token is not None:
                _SAVED_PDF_MEMO.reset(token)


def _saved_pdf_blob(connection, pdf_sha256):
    memo = _SAVED_PDF_MEMO.get()
    if memo is None:
        return connection.execute(_PDF_BLOB_SQL, (pdf_sha256,)).fetchone()
    memo.check(connection)
    require(type(pdf_sha256) is str and len(pdf_sha256) == 64
            and all(c in '0123456789abcdef' for c in pdf_sha256),
            'MANUAL_PDF_BLOB_KEY_REQUIRED')
    if pdf_sha256 in memo.rows:
        return memo.rows[pdf_sha256]
    saved = connection.execute(_PDF_BLOB_SQL, (pdf_sha256,)).fetchone()
    memo.check(connection)
    if saved is not None and type(saved['content']) is bytes:
        size = len(saved['content'])
        if memo.size + size <= _PDF_BLOB_MEMO_LIMIT:
            memo.rows[pdf_sha256] = saved
            memo.size += size
    return saved


def registered_pdf(c, source_id):
    row = c.execute('SELECT * FROM curriculum_standard_source WHERE source_id=?', (source_id,)).fetchone()
    require(row is not None, 'MANUAL_SOURCE_NOT_FOUND', 404)
    require(row['status'] == 'downloaded' and row['source_type'] == 'national_standard'
            and row['jurisdiction'] == 'CN' and row['subject_title'] == '数学'
            and urlparse(row['official_url']).hostname in ALLOWED_DOMAINS, 'MANUAL_OFFICIAL_SOURCE_REQUIRED')
    name = row['pdf_filename']
    require(isinstance(name, str) and Path(name).name == name and name.lower().endswith('.pdf'),
            'MANUAL_REGISTERED_PDF_REQUIRED')
    path = Path(row['local_root_path']) / name
    # A present original must still match; an unavailable historical location
    # may use only the exact complete server-saved bytes carried by backup.
    saved = _saved_pdf_blob(c, row['pdf_sha256'])
    if path.exists():
        data = file_bytes(path)
    else:
        require(saved is not None, 'MANUAL_SOURCE_FILE_REQUIRED')
        data = saved['content']
    require(isinstance(data, bytes) and len(data) <= 80_000_000
            and hashlib.sha256(data).hexdigest() == row['pdf_sha256'], 'MANUAL_PDF_CHANGED')
    if saved is not None:
        require(saved['page_count'] == row['page_count'] and saved['content'] == data, 'MANUAL_SAVED_PDF_CHANGED')
    require(pdf_renderer_available(), 'MANUAL_PDF_RENDERER_UNAVAILABLE', 503)
    with _PDF_PAGE_COUNTS_LOCK:
        count = _PDF_PAGE_COUNTS.get(row['pdf_sha256'])
        if count is not None:
            _PDF_PAGE_COUNTS.move_to_end(row['pdf_sha256'])
    if count is None:
        try:
            with TemporaryDirectory(prefix='manual-pdf-metadata-') as tmp:
                snapshot = Path(tmp) / 'source.pdf'
                snapshot.write_bytes(data)
                count = TextbookPdfOcrPipeline._pdf_page_count(snapshot)
        except Exception:
            raise ManualScopeError('MANUAL_PDF_METADATA_UNAVAILABLE', 503) from None
    require(count == row['page_count'] and count > 0, 'MANUAL_PDF_PAGE_COUNT_CHANGED')
    if type(count) is int:
        with _PDF_PAGE_COUNTS_LOCK:
            _PDF_PAGE_COUNTS[row['pdf_sha256']] = count
            _PDF_PAGE_COUNTS.move_to_end(row['pdf_sha256'])
            while len(_PDF_PAGE_COUNTS) > _PDF_PAGE_COUNTS_LIMIT:
                _PDF_PAGE_COUNTS.popitem(last=False)
    fields = ('source_id', 'source_key', 'source_type', 'jurisdiction', 'subject_title', 'title',
              'version_label', 'official_url', 'pdf_filename', 'pdf_sha256', 'page_count', 'status')
    return {k: row[k] for k in fields}, data


def saved_page(c, cid, number):
    row = c.execute('SELECT image FROM curriculum_manual_requirement_page WHERE candidate_id=? AND pdf_page=?',
                    (cid, number)).fetchone()
    require(row is not None, 'MANUAL_GENERATED_PAGE_REQUIRED')
    require(isinstance(row[0], bytes) and len(row[0]) <= 4_000_000, 'MANUAL_PAGE_IMAGE_TOO_LARGE')
    return row[0]


def purge_manual_requirements(c, child):
    """Parent privacy deletion retains no transcript, review note or image for this child."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_manual_requirement'").fetchone():
        return {}
    sources = [r[0] for r in c.execute("SELECT DISTINCT json_extract(source_json,'$.source_id') FROM curriculum_manual_requirement WHERE child_id=?", (child,))]
    c.execute("UPDATE curriculum_standard_requirement SET maturity='retired',title='来源已删除',requirement_text='',observable_outcome='',updated_at=? WHERE requirement_id IN (SELECT requirement_id FROM curriculum_manual_requirement WHERE child_id=?)", (utc_now(), child))
    counts = {}
    for table in ('curriculum_manual_requirement_page', 'curriculum_manual_requirement_event'):
        counts[table] = c.execute(f'DELETE FROM {table} WHERE candidate_id IN (SELECT candidate_id FROM curriculum_manual_requirement WHERE child_id=?)', (child,)).rowcount
    counts['curriculum_manual_requirement'] = c.execute('DELETE FROM curriculum_manual_requirement WHERE child_id=?', (child,)).rowcount
    for source in sources:
        current = c.execute('SELECT status,pdf_sha256 FROM curriculum_standard_source WHERE source_id=?', (source,)).fetchone()
        if current and current['status'] == 'downloaded':
            c.execute("DELETE FROM curriculum_standard_page WHERE source_id=? AND NOT EXISTS (SELECT 1 FROM curriculum_standard_requirement r WHERE r.source_id=curriculum_standard_page.source_id AND r.maturity='verified' AND curriculum_standard_page.page_number BETWEEN r.page_start AND r.page_end)", (source,))
            c.execute('UPDATE curriculum_standard_source SET extracted_page_count=(SELECT COUNT(*) FROM curriculum_standard_page WHERE source_id=?) WHERE source_id=?', (source, source))
        if current and not c.execute("SELECT 1 FROM curriculum_manual_requirement WHERE json_extract(source_json,'$.pdf_sha256')=?", (current['pdf_sha256'],)).fetchone():
            c.execute('DELETE FROM curriculum_manual_pdf_blob WHERE pdf_sha256=?', (current['pdf_sha256'],))
    return counts


def checked(c, row, *, node_required=True):
    payload, source, scope, pages = (json.loads(row[k]) for k in ('payload_json', 'source_json', 'scope_json', 'pages_json'))
    require(isinstance(pages, list) and len(pages) == 2
            and {p['pdf_page'] for p in pages} == {payload['page_number'], payload['stage_page_number']}
            and payload['page_number'] != payload['stage_page_number'], 'MANUAL_TWO_REAL_PAGES_REQUIRED')
    actual, _ = registered_pdf(c, source['source_id'])
    require(actual == source, 'MANUAL_SOURCE_VERSION_CHANGED')
    now_scope = school_scope(c, row['child_id'], payload['knowledge_node_id'], allow_missing=not node_required)
    require(now_scope == scope, 'MANUAL_CURRENT_SCHOOL_SCOPE_CHANGED')
    require(payload['stage_scope'] == '第二学段' and '第二学段（3～4年级）' in payload['stage_page_text'],
            'MANUAL_STAGE_QUOTE_REQUIRED')
    require(payload['quote'] in payload['page_text'] and len(payload['quote']) >= 8, 'MANUAL_EXACT_PAGE_QUOTE_REQUIRED')
    for page in pages:
        require(Path(page['filename']).name == page['filename'] and page['filename'] ==
                f"{row['candidate_id']}-{page['pdf_page']}.png", 'MANUAL_PAGE_PATH_CHANGED')
        require(hashlib.sha256(saved_page(c, row['candidate_id'], page['pdf_page'])).hexdigest() == page['image_sha256'], 'MANUAL_GENERATED_PAGE_CHANGED')
        text = payload['page_text'] if page['pdf_page'] == payload['page_number'] else payload['stage_page_text']
        require(page['text_sha256'] == hashlib.sha256(text.encode()).hexdigest(), 'MANUAL_PAGE_TEXT_CHANGED')
    return payload, source, scope, pages


def manual_requirement(c, child, requirement_id, page_number, quote, *, knowledge_node_id=None):
    """None is exclusively the unchanged legacy branch. Invalid manual stays denied."""
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='curriculum_manual_requirement'").fetchone()
    if not exists:
        return None
    row = c.execute('SELECT * FROM curriculum_manual_requirement WHERE requirement_id=?', (requirement_id,)).fetchone()
    if row is None:
        return None
    require(row['child_id'] == child and row['state'] == 'approved', 'MANUAL_REQUIREMENT_NOT_CURRENT')
    payload, source, scope, pages = checked(c, row)
    require(page_number == payload['page_number'] and quote == payload['quote'], 'MANUAL_APPROVED_PAGE_QUOTE_REQUIRED')
    require(knowledge_node_id == payload['knowledge_node_id'], 'MANUAL_APPROVED_SCHOOL_NODE_REQUIRED')
    req = c.execute('SELECT * FROM curriculum_standard_requirement WHERE requirement_id=?', (requirement_id,)).fetchone()
    require(req is not None and req['maturity'] == 'verified' and req['source_id'] == source['source_id']
            and req['requirement_text'] == quote and req['stage_scope'] == scope['stage_scope']
            and req['subject_title'] == '数学' and req['title'] == payload['title']
            and req['observable_outcome'] == payload['observable_outcome']
            and req['page_start'] == page_number == req['page_end']
            and req['requirement_type'] == 'content_requirement'
            and req['model_provider'] is None and req['model_name'] is None
            and req['prompt_version'] is None, 'MANUAL_REQUIREMENT_CHANGED')
    page = c.execute('SELECT * FROM curriculum_standard_page WHERE source_id=? AND page_number=?',
                     (source['source_id'], page_number)).fetchone()
    require(page is not None and page['content_text'] == payload['page_text'] and page['content_hash'] ==
            hashlib.sha256(payload['page_text'].encode()).hexdigest(), 'MANUAL_REGISTERED_PAGE_CHANGED')
    event = c.execute("SELECT * FROM curriculum_manual_requirement_event WHERE candidate_id=? AND kind='approve'",
                      (row['candidate_id'],)).fetchone()
    require(event is not None and event['version'] == row['version'], 'MANUAL_REVIEW_EVENT_REQUIRED')
    return {'origin': ORIGIN, 'candidate_id': row['candidate_id'], 'candidate_version': row['version'],
            'child_id': child, 'source_sha256': digest(source), 'scope_sha256': digest(scope),
            'pages': pages, 'review_event_id': event['event_id']}


class ManualStandardService:
    def __init__(self, database):
        self.database = database
        with database.connect() as c:
            ensure_schema(c)
            c.commit()

    def context(self, child):
        """Real registered sources and nodes only; never creates a school scope."""
        with self.database.connect() as c:
            scope = school_scope(c, child, allow_missing=True)
            sources = [dict(r) for r in c.execute("""SELECT source_id,title,version_label,
                official_url,pdf_sha256,page_count,status FROM curriculum_standard_source
                WHERE source_type='national_standard' AND jurisdiction='CN'
                  AND subject_title='数学' AND status='downloaded' AND pdf_sha256 IS NOT NULL
                ORDER BY title,source_id""")]
            nodes = [dict(r) for r in c.execute("""SELECT knowledge_node_id,title,description,
                source_ref,knowledge_version FROM knowledge_node WHERE track='school'
                  AND node_type='skill' AND subject='数学' AND grade_band=? AND status='active'
                ORDER BY title,knowledge_node_id""", (scope['grade'],))]
            return {'child_id': child, 'grade': scope['grade'], 'stage_scope': scope['stage_scope'],
                    'sources': sources, 'nodes': nodes, 'local_only': True,
                    'cloud_transmission_allowed': False}

    def list(self, child):
        with self.database.connect() as c:
            require(c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child,)).fetchone(), 'MANUAL_SCOPE_NOT_FOUND', 404)
            rows = c.execute('SELECT candidate_id,payload_json,state,version FROM curriculum_manual_requirement WHERE child_id=? ORDER BY created_at DESC,candidate_id', (child,)).fetchall()
            return [{'candidate_id': r['candidate_id'], 'state': r['state'], 'version': r['version'],
                     'title': json.loads(r['payload_json'])['title'],
                     'page_number': json.loads(r['payload_json'])['page_number']} for r in rows]

    def action(self, child, action_id):
        with self.database.connect() as c:
            row = c.execute('SELECT candidate_id FROM curriculum_manual_requirement_event WHERE child_id=? AND action_id=?',
                            (child, action_id)).fetchone()
            require(row is not None, 'MANUAL_ACTION_NOT_SAVED', 404)
            cid = row[0]
        return self.get(child, cid, images=True)

    def get(self, child, cid, *, images=False):
        with self.database.connect() as c:
            row = c.execute('SELECT * FROM curriculum_manual_requirement WHERE candidate_id=? AND child_id=?', (cid, child)).fetchone()
            require(row is not None, 'MANUAL_SCOPE_NOT_FOUND', 404)
            result = {'local_only': True, 'cloud_transmission_allowed': False, 'origin': ORIGIN,
                      **{k: row[k] for k in ('candidate_id', 'child_id', 'version', 'state', 'requirement_id')},
                      'payload': json.loads(row['payload_json']), 'source': json.loads(row['source_json']),
                      'scope': json.loads(row['scope_json']), 'pages': json.loads(row['pages_json'])}
            result['events'] = [dict(e) for e in c.execute('SELECT kind,action_id,version,note,occurred_at FROM curriculum_manual_requirement_event WHERE candidate_id=? ORDER BY version', (cid,))]
            try:
                checked(c, row, node_required=row['state'] == 'approved')
                if row['state'] == 'approved':
                    manual_requirement(c, child, row['requirement_id'], result['payload']['page_number'], result['payload']['quote'],
                                       knowledge_node_id=result['payload']['knowledge_node_id'])
                result['blockers'] = []
            except ManualScopeError as e:
                result['blockers'] = [e.code]
            result['eligible'] = row['state'] == 'approved' and not result['blockers']
            if images and not result['blockers']:
                for page in result['pages']:
                    raw = saved_page(c, cid, page['pdf_page'])
                    require(len(raw) <= 4_000_000, 'MANUAL_PAGE_IMAGE_TOO_LARGE')
                    page['image_data_url'] = 'data:image/png;base64,' + base64.b64encode(raw).decode()
            return result

    def draft(self, child, body):
        payload = {k: v for k, v in body.items() if k not in {'action_id', 'expected_version'}}
        ph = digest(body)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM curriculum_manual_requirement_event WHERE child_id=? AND action_id=?', (child, body['action_id'])).fetchone()
            if old:
                require(old['kind'] == 'draft' and old['payload_hash'] == ph, 'MANUAL_ACTION_CONFLICT')
                cid = old['candidate_id']
            else:
                require(body['expected_version'] == 0, 'MANUAL_VERSION_CONFLICT')
                scope = school_scope(c, child, body['knowledge_node_id'], allow_missing=True)
                source, pdf = registered_pdf(c, body['source_id'])
                require(source['pdf_sha256'] == body['source_pdf_sha256'] and source['version_label'] == body['source_version'], 'MANUAL_SOURCE_VERSION_CHANGED')
                require(body['stage_scope'] == scope['stage_scope'] and '第二学段（3～4年级）' in body['stage_page_text'], 'MANUAL_STAGE_QUOTE_REQUIRED')
                require(body['quote'] in body['page_text'], 'MANUAL_EXACT_PAGE_QUOTE_REQUIRED')
                require(body['page_number'] != body['stage_page_number'] and all(1 <= p <= source['page_count'] for p in (body['page_number'], body['stage_page_number'])), 'MANUAL_TWO_REAL_PAGES_REQUIRED')
                cid, pages, images = str(uuid4()), [], {}
                try:
                    # Render an owned immutable snapshot, never a path that can be
                    # swapped between the source hash check and a subprocess read.
                    with TemporaryDirectory(prefix='manual-official-page-') as tmp:
                        snapshot = Path(tmp) / 'source.pdf'
                        raw_pdf = pdf
                        require(hashlib.sha256(raw_pdf).hexdigest() == source['pdf_sha256'], 'MANUAL_PDF_CHANGED')
                        snapshot.write_bytes(raw_pdf)
                        for number, text in ((body['page_number'], body['page_text']), (body['stage_page_number'], body['stage_page_text'])):
                            try:
                                image = TextbookPdfOcrPipeline._render_page(snapshot, number)
                            except Exception:
                                raise ManualScopeError('MANUAL_PDF_RENDERER_UNAVAILABLE', 503) from None
                            ok, encoded = cv2.imencode('.png', image)
                            require(ok and len(encoded) <= 4_000_000, 'MANUAL_PDF_PAGE_RENDER_FAILED', 503)
                            filename = f'{cid}-{number}.png'
                            raw = encoded.tobytes()
                            images[number] = raw
                            pages.append({'pdf_page': number, 'filename': filename,
                                          'image_sha256': hashlib.sha256(raw).hexdigest(),
                                          'text_sha256': hashlib.sha256(text.encode()).hexdigest()})
                    actual, _ = registered_pdf(c, body['source_id'])
                    require(actual == source and school_scope(c, child, body['knowledge_node_id'], allow_missing=True) == scope, 'MANUAL_INPUT_CHANGED_DURING_RENDER')
                    now = utc_now()
                    c.execute('INSERT INTO curriculum_manual_requirement VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                              (cid, child, 1, 'draft', json.dumps(payload, ensure_ascii=False), json.dumps(source, ensure_ascii=False), json.dumps(scope, ensure_ascii=False), json.dumps(pages), None, now, now))
                    c.execute('INSERT OR IGNORE INTO curriculum_manual_pdf_blob VALUES(?,?,?)',
                              (source['pdf_sha256'], source['page_count'], raw_pdf))
                    for number, raw in images.items():
                        c.execute('INSERT INTO curriculum_manual_requirement_page VALUES(?,?,?)', (cid, number, raw))
                    self.event(c, child, cid, 'draft', body['action_id'], ph, 1, None)
                    c.commit()
                except Exception:
                    # Images are in the same SQLite transaction as the immutable
                    # draft and audit event; rollback never leaves orphan artifacts.
                    raise
        return self.get(child, cid)

    @staticmethod
    def event(c, child, cid, kind, action, ph, version, note):
        event = str(uuid4())
        now = utc_now()
        c.execute('INSERT INTO curriculum_manual_requirement_event VALUES(?,?,?,?,?,?,?,?,?)', (event, cid, child, kind, action, ph, version, note, now))
        c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,'parent','manual_official_scope',?,?,?)",
                  (str(uuid4()), child, json.dumps({'candidate_id': cid, 'kind': kind, 'version': version, 'origin': ORIGIN, 'event_id': event}), now))

    def decide(self, child, cid, body, kind):
        ph = digest({'kind': kind, **body})
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM curriculum_manual_requirement WHERE candidate_id=? AND child_id=?', (cid, child)).fetchone()
            require(row is not None, 'MANUAL_SCOPE_NOT_FOUND', 404)
            old = c.execute('SELECT * FROM curriculum_manual_requirement_event WHERE child_id=? AND action_id=?', (child, body['action_id'])).fetchone()
            if old:
                require(old['candidate_id'] == cid and old['payload_hash'] == ph and old['kind'] == kind, 'MANUAL_ACTION_CONFLICT')
            else:
                require(row['version'] == body['expected_version'], 'MANUAL_VERSION_CONFLICT')
                require(bool(body['note'].strip()), 'MANUAL_EXPLICIT_PARENT_REVIEW_REQUIRED')
                require((kind in {'approve', 'reject'} and row['state'] == 'draft') or (kind == 'withdraw' and row['state'] == 'approved'), 'MANUAL_STATE_CONFLICT')
                rid = row['requirement_id']
                if kind == 'approve':
                    payload, source, scope, pages = checked(c, row)
                    rid, now = str(uuid4()), utc_now()
                    for page in pages:
                        text = payload['page_text'] if page['pdf_page'] == payload['page_number'] else payload['stage_page_text']
                        existing = c.execute('SELECT content_text,content_hash FROM curriculum_standard_page WHERE source_id=? AND page_number=?', (source['source_id'], page['pdf_page'])).fetchone()
                        require(existing is None or (existing['content_text'], existing['content_hash']) == (text, page['text_sha256']), 'MANUAL_REGISTERED_PAGE_CONFLICT')
                        if existing is None:
                            c.execute('INSERT INTO curriculum_standard_page VALUES(?,?,?,?,?)', (source['source_id'], page['pdf_page'], text, page['text_sha256'], len(text)))
                    c.execute('UPDATE curriculum_standard_source SET extracted_page_count=(SELECT COUNT(*) FROM curriculum_standard_page WHERE source_id=?) WHERE source_id=?', (source['source_id'], source['source_id']))
                    c.execute('''INSERT INTO curriculum_standard_requirement(requirement_id,source_id,subject_title,stage_scope,requirement_type,title,requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
                                 VALUES(?,?,'数学','第二学段','content_requirement',?,?,?,?,?,1,'verified',?,?)''',
                              (rid, source['source_id'], payload['title'], payload['quote'], payload['observable_outcome'], payload['page_number'], payload['page_number'], now, now))
                elif kind == 'withdraw':
                    # Deauthorizing is allowed even when the current source is unavailable.
                    # get() still returns exact current blockers; never calls it ready.
                    c.execute("UPDATE curriculum_standard_requirement SET maturity='retired',updated_at=? WHERE requirement_id=?", (utc_now(), rid))
                version = row['version'] + 1
                c.execute('UPDATE curriculum_manual_requirement SET state=?,version=?,requirement_id=?,updated_at=? WHERE candidate_id=?', ({'approve': 'approved', 'reject': 'rejected', 'withdraw': 'withdrawn'}[kind], version, rid, utc_now(), cid))
                self.event(c, child, cid, kind, body['action_id'], ph, version, body['note'])
                c.commit()
        return self.get(child, cid)


class DraftBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action_id: str = Field(min_length=8, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    expected_version: int = Field(ge=0)
    source_id: str = Field(min_length=1, max_length=120)
    source_pdf_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_version: str = Field(min_length=1, max_length=120)
    knowledge_node_id: Optional[str] = Field(default=None, max_length=120)
    stage_scope: str = Field(min_length=1, max_length=100)
    page_number: int = Field(ge=1)
    stage_page_number: int = Field(ge=1)
    page_text: str = Field(min_length=8, max_length=6000)
    stage_page_text: str = Field(min_length=8, max_length=6000)
    quote: str = Field(min_length=8, max_length=1200)
    title: str = Field(min_length=2, max_length=200)
    observable_outcome: str = Field(min_length=8, max_length=1200)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action_id: str = Field(min_length=8, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    expected_version: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(pattern=r'^(approve|reject)$')


def create_manual_standard_router(service, parent_auth):
    router = APIRouter(prefix='/v1/parent/children/{child}/manual-standard-requirements')

    def run(request, operation, *, mutating=False):
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
            with service.database.external_effect_guard(timeout=0) if mutating else nullcontext():
                return operation()
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={'code': 'PARENT_SESSION_REQUIRED'}) from None
        except ManualScopeError as e:
            raise HTTPException(e.status, detail={'code': e.code}) from None

    @router.post('')
    def draft(child: str, body: DraftBody, request: Request):
        return run(request, lambda: service.draft(child, body.model_dump()), mutating=True)

    @router.get('')
    def listing(child: str, request: Request):
        return run(request, lambda: service.list(child))

    @router.get('/context')
    def context(child: str, request: Request):
        return run(request, lambda: service.context(child))

    @router.get('/actions/{action_id}')
    def action(child: str, action_id: str, request: Request):
        return run(request, lambda: service.action(child, action_id))

    @router.get('/{cid}')
    def get(child: str, cid: str, request: Request, images: bool = False):
        return run(request, lambda: service.get(child, cid, images=images))

    @router.post('/{cid}/review')
    def review(child: str, cid: str, body: DecisionBody, request: Request):
        return run(request, lambda: service.decide(child, cid, body.model_dump(), body.decision), mutating=True)

    @router.post('/{cid}/withdraw')
    def withdraw(child: str, cid: str, body: DecisionBody, request: Request):
        return run(request, lambda: service.decide(child, cid, body.model_dump(), 'withdraw'), mutating=True)

    return router
