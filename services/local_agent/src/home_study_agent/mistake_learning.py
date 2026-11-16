"""Persisted, cloud-formative learning on existing mistake sources.

This is a practice journal. It never writes learning_evidence, diagnostic_answer,
mastery, or the formal mistake gate. Source text is the selected case only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from time import monotonic

from .cloud_privacy import sanitize_cloud_text
from .config import validate_tutor_explanation_url, validate_tutor_explanation_model
from .mistake_graph_study import (cloud_targets, practice_graph_is_stale, practice_graph_target,
                                 prerequisite_checks, review_targets, study_targets)
from .mistake_graph_semantic import SEMANTIC_INSTRUCTION, parse_semantic_assessments
from .model_gateway import ModelRequest
from .privacy_operation_receipt import generation

VERSION = 1
_ACTIVE_ACTIONS = set()


def graph_version_from_reader(reader, connection):
    if reader is None:
        return None
    try:
        return reader(connection=connection) or ''
    except (OSError, sqlite3.Error, ValueError):
        return ''
_PROCESS_RUN = str(uuid4())  # Only distinguishes abandoned previous-process work; never an execution grant.
INTERVALS = (1, 3, 7, 14, 30)
STAGES = ('near_transfer', 'representation_transfer', 'discrimination')
OPERATIONS = frozenset({'analyze', 'practice', 'coach', 'answer', 'focus', 'review'})
GRAPH_REFRESH_BATCH = 8


class MistakeLearningError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def _fail(code, status=409):
    raise MistakeLearningError(code, status)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _diagnostic(event, request_type, status, *, result=None, error_code=None, provider_error_code=None, elapsed_ms=None):
    """Only bounded operational metadata; learning material never enters logs."""
    try:
        from .daily_diagnostic_schema import ENUMS, ERROR_CODES
        if request_type not in ENUMS['request_type'] or status not in ENUMS['mistake_status']:
            return
        details = {'request_type': request_type, 'mistake_status': status}
        if result is not None:
            if result.provider in ENUMS['model_provider']:
                details['model_provider'] = result.provider
            if result.model in ENUMS['model_name']:
                details['model_name'] = result.model
            if type(result.latency_ms) is int and result.latency_ms >= 0:
                details['latency_ms'] = result.latency_ms
            for key, attribute in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')):
                value = getattr(result, attribute, None)
                if type(value) is int and value >= 0:
                    details[key] = value
        if error_code in ERROR_CODES:
            details['error_code'] = error_code
        if provider_error_code in ERROR_CODES:
            details['error'] = {'code': provider_error_code}
        elif isinstance(provider_error_code, str) and re.fullmatch(r'MODEL_HTTP_[1-5]\d{2}', provider_error_code):
            details['statusCode'] = int(provider_error_code[-3:])
        if elapsed_ms is not None:
            details['elapsed_ms'] = max(0, int(elapsed_ms))
        print('HSC_DIAGNOSTIC ' + json.dumps({'event': event, 'level': 'info' if status == 'completed' else 'warn',
                                            'details': details}, separators=(',', ':')), flush=True)
    except Exception:
        pass


def _json(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(v):
    return hashlib.sha256(_json(v).encode()).hexdigest()


def analysis_context_key(graph, dedicated):
    if isinstance(dedicated, dict):
        # Route changes invalidate cached analysis without persisting credentials.
        return _hash(['graph_analysis_v2', graph, dedicated])
    return _hash(['graph_analysis_v1', graph, bool(dedicated)])


def _tutor_route_identity(gateway):
    settings = getattr(gateway, 'settings', None)
    key = getattr(settings, 'tutor_explanation_api_key', None)
    url = validate_tutor_explanation_url(getattr(settings, 'tutor_explanation_base_url', None))
    model = validate_tutor_explanation_model(getattr(settings, 'tutor_explanation_model', None))
    if isinstance(key, str) and re.fullmatch(r'[!-~]{16,512}', key) and url and model:
        return {'provider': 'deepseek', 'endpoint': url, 'model': model}
    return None


def analysis_for_graph(row, graph):
    if not row or not row['analysis_json']:
        return None
    context = row['analysis_context_key']
    if graph and not context:
        return None
    allowed = (analysis_context_key(graph, False), analysis_context_key(graph, True),
               analysis_context_key(None, False), analysis_context_key(None, True))
    if context and context not in allowed:
        return None
    return json.loads(row['analysis_json'])


def _text(v, maximum=4000, *, optional=False):
    if optional and v is None:
        return ''
    if not isinstance(v, str) or not v.strip() or len(v) > maximum:
        _fail('MISTAKE_LEARNING_TEXT_INVALID', 422)
    return v.strip()


def _action_status(row, current_generation):
    if row['status']!='processing': return row['status']
    try: owner=json.loads(row['provider_json'] or '{}').get('process_run')
    except (ValueError,TypeError,AttributeError): owner=None
    return 'processing' if row['storage_generation']==current_generation and owner==_PROCESS_RUN and (row['storage_generation'],row['client_action_id']) in _ACTIVE_ACTIONS else 'unknown'


def _exists(c, table):
    return bool(c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone())


def _source(c, child_id, mid):
    row = c.execute('SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?', (mid, child_id)).fetchone()
    if row:
        row = dict(row)
        if row['status'] == 'excluded':
            _fail('MISTAKE_LEARNING_SOURCE_NOT_FOUND', 404)
        question = row['prompt_summary']
        version = None
        if _exists(c, 'mistake_source_revision'):
            revision = c.execute('SELECT revision_id,version,question,after_hash FROM mistake_source_revision WHERE mistake_id=? AND child_id=? ORDER BY version DESC LIMIT 1', (mid, child_id)).fetchone()
            if revision:
                # This learning use does not grant the revision formal scoring authority.
                question, version = revision['question'], dict(revision)
    else:
        from .mistake_learning_intake import read_intake_card
        row = read_intake_card(c, child_id, mid)
        if not row or row['status'] != 'active':
            _fail('MISTAKE_LEARNING_SOURCE_NOT_FOUND', 404)
        row = dict(row)
        question, version = row['prompt_summary'], row.get('source_revision')
    value = {'subject': row['subject'], 'title': row['title'], 'question': question,
             'original_answer': row.get('submitted_answer') or ''}
    return value, _hash([value, version])


def _display_source(c, child_id, mid, source):
    row = c.execute('SELECT created_at FROM mistake_case WHERE mistake_id=? AND child_id=?', (mid, child_id)).fetchone()
    if row is None:
        from .mistake_learning_intake import read_intake_card
        row = read_intake_card(c, child_id, mid)
    crop = _exists(c, 'mistake_question_crop') and c.execute(
        'SELECT 1 FROM mistake_question_crop WHERE mistake_id=? AND child_id=?', (mid, child_id),
    ).fetchone() is not None
    return source | {'created_at': row['created_at'] if row else None, 'has_question_crop': crop}


def _original_review(c, child_id, mid, source_revision):
    result = c.execute('''SELECT result_json,next_review_at FROM mistake_learning_original_review
        WHERE child_id=? AND mistake_id=? AND source_revision=? ORDER BY sequence DESC LIMIT 1''',
        (child_id, mid, source_revision)).fetchone()
    last_result = json.loads(result['result_json']) if result else None
    wrong = c.execute('''SELECT count(*) FROM mistake_learning_original_review
        WHERE child_id=? AND mistake_id=? AND source_revision=? AND judgement='incorrect' ''',
        (child_id, mid, source_revision)).fetchone()[0] if result else 0
    return {'status': ('retest_pending' if last_result['judgement'] == 'correct' else 'not_mastered') if result else 'not_started',
            'wrong_count': wrong, 'last_result': last_result, 'next_review_at': result['next_review_at'] if result else None}


def _review_due_at(original, practice_due):
    times = [due for due in (original['next_review_at'], practice_due) if due]
    return min(times) if times else None


def _coach_variant_batch(mid, source_revision):
    return 'coach-variant:' + _hash([mid, source_revision])


def _coach_variant(c, mid, child_id, source_revision):
    return c.execute('''SELECT * FROM mistake_learning_item WHERE mistake_id=? AND child_id=?
        AND source_revision=? AND batch_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1''',
        (mid, child_id, source_revision, _coach_variant_batch(mid, source_revision))).fetchone()


def _public_learning_item(row, graph_sha):
    return {k: row[k] for k in ('practice_item_id', 'position', 'question', 'stage', 'difficulty', 'attempts', 'help_count')} | {
        'status': 'stale' if practice_graph_is_stale(row, graph_sha) else 'answered' if row['answered_at'] else 'ready',
        'feedback': json.loads(row['feedback_json']) if row['feedback_json'] else None,
        'graph_target': practice_graph_target(row, graph_sha),
    }


def _latest_coach_turn(c, mid, child_id, source_revision, item_id=None):
    turn = c.execute('''SELECT t.turn_id,t.created_at,a.provider_json FROM mistake_learning_turn t
        JOIN mistake_learning_action a USING(client_action_id) WHERE t.mistake_id=? AND t.child_id=?
        AND t.source_revision=? AND t.practice_item_id IS ? AND t.role='assistant' AND t.kind='coach'
        AND a.operation='coach' AND a.status='completed' ORDER BY t.created_at DESC,t.rowid DESC LIMIT 1''',
        (mid, child_id, source_revision, item_id)).fetchone()
    if not turn:
        return None
    return {'turn_id': turn['turn_id'], 'created_at': turn['created_at'],
            'phase': json.loads(turn['provider_json'] or '{}').get('coaching_phase')}


def _is_due(view):
    original_due = (view.get('original_review') or {}).get('next_review_at')
    return view['review_due'] and (view['today']['status'] != 'done' or bool(original_due and original_due <= _now()))


def _owner(c, child_id, session_id=None):
    child = c.execute('SELECT status FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
    if not child or child['status'] != 'active':
        _fail('MISTAKE_LEARNING_CHILD_NOT_FOUND', 404)
    if session_id:
        session = c.execute('SELECT child_id,status FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not session or session['child_id'] != child_id or session['status'] != 'active':
            _fail('MISTAKE_LEARNING_SESSION_CHANGED')


def _permission(c):
    row = c.execute('SELECT cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    if not row or type(row[0]) is not int or row[0] != 1:
        _fail('MISTAKE_LEARNING_CLOUD_CONSENT_REQUIRED', 403)


def _guard(c, guard):
    if guard is not None and guard(c) is not True:
        _fail('MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED', 403)


def _ids(c, child_id):
    rows = [dict(r) for r in c.execute('SELECT mistake_id,title FROM mistake_case WHERE child_id=? AND status!="excluded" ORDER BY created_at,mistake_id', (child_id,))]
    if _exists(c, 'mistake_learning_card'):
        from .mistake_learning_intake import list_intake_cards
        seen = {r['mistake_id'] for r in rows}
        rows += [dict(r) for r in list_intake_cards(c, child_id) if r['mistake_id'] not in seen and r['status'] == 'active']
    return rows


def native_focus(c, session_id, *, prefer_coach_variant=True, current_graph_version=None):
    if not _exists(c, 'mistake_learning_focus'):
        return None
    row = c.execute('SELECT * FROM mistake_learning_focus WHERE session_id=?', (session_id,)).fetchone()
    if not row or not row['mistake_id']:
        return None
    _owner(c, row['child_id'], session_id)
    _, source = _source(c, row['child_id'], row['mistake_id'])
    if source != row['source_revision']:
        return None
    state = c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (row['mistake_id'],)).fetchone()
    if not state or state['source_revision'] != source:
        return None
    try:
        graph = json.loads(state['graph_json']) if state['graph_json'] else None
    except (TypeError, ValueError):
        graph = None
    graph_sha = _hash(graph) if (isinstance(graph, dict) and isinstance(graph.get('graph_version'), str)
                                and (current_graph_version is None or graph['graph_version'] == current_graph_version)) else None
    candidates = c.execute('SELECT * FROM mistake_learning_item WHERE mistake_id=? AND source_revision=? AND batch_id=? AND answered_at IS NULL ORDER BY position', (row['mistake_id'], source, state['batch_id'])) if state['practice_date']==datetime.now().astimezone().date().isoformat() else ()
    item = next((candidate for candidate in candidates if not practice_graph_is_stale(candidate, graph_sha)), None)
    variant = _coach_variant(c, row['mistake_id'], row['child_id'], source)
    if prefer_coach_variant and variant and not variant['answered_at'] and not practice_graph_is_stale(variant, graph_sha):
        item = variant
    return {'mistake_id': row['mistake_id'], 'child_id': row['child_id'], 'session_id': session_id,
            'source_revision': source, 'focus_generation': row['focus_generation'], 'focus_created_at': row['created_at'],
            'learning_session_id': state['learning_session_id'], 'revision': state['revision'],
            'practice_item_id': item['practice_item_id'] if item else None,
            'practice_created_at': item['created_at'] if item else None}


lookup_native_focus = native_focus


def native_target(c, session_id, selector=None, *, operation='select'):
    session = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
    if not session:
        _fail('MISTAKE_LEARNING_SESSION_CHANGED')
    child_id = session[0]
    _owner(c, child_id, session_id)
    rows = _ids(c, child_id)
    if selector is None:
        focus = native_focus(c, session_id)
        if focus and operation != 'review_today':
            return focus
        matches = []
        for row in rows:
            state = MistakeLearningService._view(c, row['mistake_id'], child_id)
            if _is_due(state):
                matches.append(row)
        if not matches:
            _fail('MISTAKE_LEARNING_NOTHING_DUE', 404)
        chosen = matches[0]
    elif type(selector) is int or isinstance(selector, str) and selector.isdecimal():
        n = int(selector)
        if not 1 <= n <= len(rows):
            _fail('MISTAKE_LEARNING_SELECTION_REQUIRED', 422)
        chosen = rows[n-1]
    else:
        matches = [r for r in rows if selector in (r['mistake_id'], r['title'])]
        if len(matches) != 1:
            _fail('MISTAKE_LEARNING_SELECTION_REQUIRED', 422)
        chosen = matches[0]
    source, revision = _source(c, child_id, chosen['mistake_id'])
    previous = c.execute('SELECT focus_generation FROM mistake_learning_focus WHERE session_id=?', (session_id,)).fetchone()
    return {'mistake_id': chosen['mistake_id'], 'child_id': child_id, 'session_id': session_id,
            'source_revision': revision, 'focus_generation': (native_focus(c, session_id)['focus_generation'] if native_focus(c, session_id) and native_focus(c, session_id)['mistake_id'] == chosen['mistake_id'] else (previous[0] if previous else 0)+1),
            'focus_created_at': None, 'learning_session_id': None, 'revision': 0,
            'practice_item_id': None, 'practice_created_at': None, 'selection': True,
            'selection_order_sha256': _hash([[r['mistake_id'], _source(c, child_id, r['mistake_id'])[1]] for r in rows])}


def validate_source(c, session_id, snapshot, *, operation):
    if not isinstance(snapshot, dict) or snapshot.get('session_id') != session_id:
        _fail('MISTAKE_LEARNING_FOCUS_CHANGED')
    _owner(c, snapshot['child_id'], session_id)
    _, current = _source(c, snapshot['child_id'], snapshot['mistake_id'])
    if current != snapshot.get('source_revision'):
        _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
    focus = native_focus(c, session_id)
    if snapshot.get('selection'):
        if focus and focus['mistake_id'] == snapshot['mistake_id'] and focus['focus_generation'] == snapshot['focus_generation']:
            return True
        order = _hash([[r['mistake_id'], _source(c, snapshot['child_id'], r['mistake_id'])[1]] for r in _ids(c, snapshot['child_id'])])
        if order != snapshot.get('selection_order_sha256'):
            _fail('MISTAKE_LEARNING_FOCUS_CHANGED')
        previous = c.execute('SELECT focus_generation FROM mistake_learning_focus WHERE session_id=?', (session_id,)).fetchone()
        if (previous[0] if previous else 0)+1 != snapshot['focus_generation']:
            _fail('MISTAKE_LEARNING_FOCUS_CHANGED')
    elif not focus or any(focus[k] != snapshot.get(k) for k in ('mistake_id', 'child_id', 'source_revision', 'focus_generation')):
        _fail('MISTAKE_LEARNING_FOCUS_CHANGED')
    item_id = snapshot.get('practice_item_id')
    if item_id and not c.execute('SELECT 1 FROM mistake_learning_item WHERE practice_item_id=? AND mistake_id=? AND source_revision=?', (item_id, snapshot['mistake_id'], current)).fetchone():
        _fail('MISTAKE_LEARNING_PRACTICE_CHANGED')
    return True


def native_receipt(c, session_id, client_action_id):
    row = c.execute('SELECT * FROM mistake_learning_action WHERE client_action_id=? AND session_id=?', (client_action_id, session_id)).fetchone()
    if not row:
        return None
    return {k: row[k] for k in ('client_action_id', 'operation', 'mistake_id', 'child_id', 'session_id', 'source_revision', 'created_at', 'updated_at')} | {'status':_action_status(row,generation(c))} | {'revision': json.loads(row['result_json']).get('revision') if row['result_json'] else None, 'learning_session_id': json.loads(row['result_json']).get('learning_session_id') if row['result_json'] else None}


def native_context(c, session_id, *, current_graph_version=None):
    focus = native_focus(c, session_id, current_graph_version=current_graph_version or '')
    if not focus:
        return None
    _permission(c)
    source, _ = _source(c, focus['child_id'], focus['mistake_id'])
    state = c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (focus['mistake_id'],)).fetchone()
    raw_graph = state['graph_json']
    try:
        graph = json.loads(raw_graph) if raw_graph else None
    except (TypeError, ValueError):
        graph = None
    graph_stale = bool(raw_graph and (
        not isinstance(graph, dict) or not current_graph_version or graph.get('graph_version') != current_graph_version
    ))
    if graph_stale:
        unbound_keys = (analysis_context_key(None, False), analysis_context_key(None, True))
        analysis = analysis_for_graph(state, None) if state['analysis_context_key'] in unbound_keys else None
    elif graph and not state['analysis_context_key']:
        analysis = None
    else:
        analysis = analysis_for_graph(state, graph)
    item = c.execute('SELECT question,stage,validation_json FROM mistake_learning_item WHERE practice_item_id=?', (focus['practice_item_id'],)).fetchone()
    graph_sha = _hash(graph) if graph and not graph_stale else None
    if item and practice_graph_is_stale(item, graph_sha):
        item = None
    practice = {'practice_item_id': focus['practice_item_id'], 'question': item['question'], 'stage': item['stage']} if item else None
    return {'focus': focus, 'source': source, 'analysis': analysis, 'practice': practice}


SCHEMA = '''
CREATE TABLE IF NOT EXISTS mistake_learning_state(
 mistake_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
 learning_session_id TEXT NOT NULL,source_revision TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
 analysis_json TEXT,graph_json TEXT,analysis_context_key TEXT,batch_id TEXT,practice_date TEXT,target_count INTEGER NOT NULL DEFAULT 3,
 fail_streak INTEGER NOT NULL DEFAULT 0,interval_index INTEGER NOT NULL DEFAULT 0,next_review_at TEXT,
 needs_explanation INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'ready',error_code TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mistake_learning_item(
 practice_item_id TEXT PRIMARY KEY,mistake_id TEXT NOT NULL REFERENCES mistake_learning_state(mistake_id) ON DELETE CASCADE,
 child_id TEXT NOT NULL,source_revision TEXT NOT NULL,batch_id TEXT NOT NULL,position INTEGER NOT NULL,
 question TEXT NOT NULL,stage TEXT NOT NULL,difficulty INTEGER NOT NULL,expected_json TEXT NOT NULL,
 explanation TEXT NOT NULL,validation_json TEXT NOT NULL,feedback_json TEXT,attempts INTEGER NOT NULL DEFAULT 0,
 help_count INTEGER NOT NULL DEFAULT 0,answered_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mistake_learning_turn(
 turn_id TEXT PRIMARY KEY,mistake_id TEXT NOT NULL REFERENCES mistake_learning_state(mistake_id) ON DELETE CASCADE,
 child_id TEXT NOT NULL,source_revision TEXT NOT NULL,session_id TEXT,client_action_id TEXT NOT NULL,
 role TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,practice_item_id TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mistake_learning_action(
 client_action_id TEXT PRIMARY KEY,mistake_id TEXT NOT NULL,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,session_id TEXT,
 operation TEXT NOT NULL,request_sha256 TEXT NOT NULL,source_revision TEXT NOT NULL,storage_generation TEXT NOT NULL,
 status TEXT NOT NULL,result_json TEXT,error_code TEXT,provider_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mistake_learning_focus(
 session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id) ON DELETE CASCADE,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,mistake_id TEXT,
 source_revision TEXT NOT NULL,focus_generation INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mistake_learning_original_review(
 sequence INTEGER PRIMARY KEY,mistake_id TEXT NOT NULL REFERENCES mistake_learning_state(mistake_id) ON DELETE CASCADE,
 child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,source_revision TEXT NOT NULL,
 session_id TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,judgement TEXT NOT NULL CHECK(judgement IN ('correct','incorrect')),
 artifact_id TEXT UNIQUE,voice_input_id TEXT UNIQUE,result_json TEXT NOT NULL,next_review_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_mistake_learning_original_review_lookup
 ON mistake_learning_original_review(child_id,mistake_id,source_revision,sequence DESC);
'''


class MistakeLearningService:
    def __init__(self, database, gateway, *, graph_matcher=None):
        self.database, self.gateway, self.graph_matcher = database, gateway, graph_matcher
        with database.connect() as c:
            c.executescript(SCHEMA)
            columns = {row[1] for row in c.execute('PRAGMA table_info(mistake_learning_state)')}
            for name in ('graph_json', 'analysis_context_key'):
                if name not in columns:
                    c.execute(f'ALTER TABLE mistake_learning_state ADD COLUMN {name} TEXT')
            c.commit()

    def _state_row(self, c, mid, child_id, revision):
        row = c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone()
        if not row:
            now = _now()
            c.execute('INSERT INTO mistake_learning_state(mistake_id,child_id,learning_session_id,source_revision,created_at,updated_at) VALUES(?,?,?,?,?,?)', (mid, child_id, str(uuid4()), revision, now, now))
        elif row['source_revision'] != revision:
            # Preserve historical items, user turns, and actions; no old answers attach to the new text.
            c.execute('UPDATE mistake_learning_state SET source_revision=?,learning_session_id=?,revision=revision+1,analysis_json=NULL,analysis_context_key=NULL,graph_json=NULL,batch_id=NULL,practice_date=NULL,fail_streak=0,needs_explanation=0,interval_index=0,next_review_at=NULL,status="ready",error_code=NULL,updated_at=? WHERE mistake_id=?', (revision, str(uuid4()), _now(), mid))
        return c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone()

    @staticmethod
    def _graph(row):
        if not row or not row['graph_json']:
            return None
        try:
            graph = json.loads(row['graph_json'])
        except (TypeError, ValueError):
            return None
        return graph if isinstance(graph, dict) and isinstance(graph.get('graph_version'), str) else None

    def _analysis_context_key(self, graph):
        return analysis_context_key(graph, _tutor_route_identity(self.gateway))

    def _authorized_cloud_targets(self, targets):
        """Keep local graph labels local unless each label has explicit cloud rights."""
        if not self.graph_matcher or not hasattr(self.graph_matcher, 'semantic_options'):
            return []
        allowed = []
        for target in targets:
            options, _ = self.graph_matcher.semantic_options({
                'status': 'ambiguous', 'candidates_truncated': False,
                'alternatives': [{'key': target['key'], 'title': target['title']}],
            })
            if options:
                allowed.append(target)
        return allowed if any(target['kind'] == 'primary' for target in allowed) else []

    def _refresh_graph(self, mid, child_id, session_id=None, *, version=None):
        if self.graph_matcher is None:
            return False
        version = version if version is not None else self.graph_matcher.cache_version()
        with self.database.connect() as c:
            _owner(c, child_id, session_id)
            source, revision = _source(c, child_id, mid)
            current_generation = generation(c)
            row = c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone()
            current = self._graph(row) if row and row['source_revision'] == revision else None
            if current and current['graph_version'] == version:
                return False
        result = self.graph_matcher.match(child_id, source)
        if result['graph_version'] != version:
            return False
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _owner(c, child_id, session_id)
            if (generation(c) != current_generation or _source(c, child_id, mid)[1] != revision
                    or self.graph_matcher.cache_version(connection=c) != version):
                return False
            row = self._state_row(c, mid, child_id, revision)
            current = self._graph(row)
            if not current or current['graph_version'] != version:
                c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=? AND child_id=?',
                          (_json(result), mid, child_id))
            c.commit()
        return True

    @staticmethod
    def _view(c, mid, child_id, session_id=None, *, graph_version=None):
        source, revision = _source(c, child_id, mid)
        row = c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone()
        current = row and row['source_revision'] == revision
        graph = MistakeLearningService._graph(row) if current else None
        graph_untrusted = bool(current and row['graph_json'] and (
            not graph or graph_version is not None and graph.get('graph_version') != graph_version
        ))
        if graph_untrusted:
            graph = None
        graph_sha = _hash(graph) if graph else None
        display_source = _display_source(c, child_id, mid, source)
        if graph and graph.get('subject'):
            display_source['subject'] = graph['subject']
        if graph_untrusted:
            unbound_keys = (analysis_context_key(None, False), analysis_context_key(None, True))
            analysis = analysis_for_graph(row, None) if row['analysis_context_key'] in unbound_keys else None
        elif graph and not row['analysis_context_key']:
            analysis = None
        else:
            analysis = analysis_for_graph(row, graph) if current else None
        items = [dict(r) for r in c.execute('SELECT * FROM mistake_learning_item WHERE mistake_id=? AND source_revision=? AND batch_id=? ORDER BY position', (mid, revision, row['batch_id']))] if current else []
        variant = _coach_variant(c, mid, child_id, revision) if current else None
        coach_turn = _latest_coach_turn(c, mid, child_id, revision) if current else None
        stale_practice = any(practice_graph_is_stale(item, graph_sha) for item in items)
        current_items = [item for item in items if not practice_graph_is_stale(item, graph_sha)]
        turns = [dict(r) for r in c.execute('SELECT * FROM mistake_learning_turn WHERE mistake_id=? AND source_revision=? ORDER BY created_at,turn_id', (mid, revision))] if current else []
        date = datetime.now().astimezone().date().isoformat()
        same_day = bool(current and row['practice_date'] == date)
        completed = sum(r['answered_at'] is not None for r in current_items) if same_day else 0
        target = row['target_count'] if current else 3
        needs = bool(same_day and current_items and row['needs_explanation'])
        quota = 'needs_explanation' if needs else 'done' if completed >= target else 'in_progress' if same_day and current_items else 'not_started'
        original = _original_review(c, child_id, mid, revision)
        next_review_at = _review_due_at(original, row['next_review_at'] if current else None)
        review_focus = None
        if graph:
            targets, checked = review_targets(c, row, graph, graph_sha, analysis)
            if targets:
                focus_target = targets[0]
                label = {k: focus_target[k] for k in ('title', 'kind')}
                scheduled = any(not item['answered_at'] and practice_graph_target(item, graph_sha) == label for item in items)
                review_focus = label | {'state': 'scheduled' if scheduled else 'checked' if focus_target['key'] in checked else 'due'}
        focus = None
        if session_id:
            try:
                focus = native_focus(c, session_id)
            except MistakeLearningError:
                pass
            if focus and focus['mistake_id'] != mid:
                focus = None
        active=c.execute('SELECT * FROM mistake_learning_action WHERE mistake_id=? AND source_revision=? AND storage_generation=? AND status IN ("processing","unknown") ORDER BY created_at DESC LIMIT 1',(mid,revision,generation(c))).fetchone()
        return {'version': VERSION, 'mistake_id': mid, 'child_id': child_id,'active_action':({k:active[k] for k in ('client_action_id','operation','error_code','updated_at')}|{'status':_action_status(active,generation(c))}) if active else None,
            'learning_session_id': row['learning_session_id'] if current else None,
            'revision': row['revision'] if row else 0, 'source_revision': revision, 'source': display_source,
            'analysis': analysis,
            'graph_context': graph, 'graph_review_focus': review_focus, 'graph_practice_stale': stale_practice,
            'practice_items': [_public_learning_item(r, graph_sha) for r in items],
            'coach_variant': _public_learning_item(variant, graph_sha) if variant else None,
            'coach_turn_id': coach_turn['turn_id'] if coach_turn and coach_turn['phase'] in ('question','explain_step') else None,
            'turns': [{k: r[k] for k in ('turn_id', 'role', 'kind', 'text', 'practice_item_id', 'created_at')} for r in turns[-60:]],
            'today': {'date': date, 'target_count': target, 'completed_count': completed, 'remaining_count': max(0, target-completed), 'status': quota, 'stop_reason': 'two_failures' if needs else None},
            'next_review_at': next_review_at, 'original_review': original,
            'review_due': stale_practice or quota in ('in_progress','needs_explanation') or not next_review_at or next_review_at <= _now(),
            'status': (_action_status(active,generation(c)) if row['status']=='processing' and active else 'unknown' if row['status']=='processing' else row['status']) if current else 'ready', 'error_code': ('MISTAKE_LEARNING_OUTCOME_UNKNOWN' if row['status']=='processing' and not active else row['error_code']) if current else None, 'focus': focus}

    def state(self, mistake_id, *, child_id, session_id=None):
        version = self.graph_matcher.cache_version() if self.graph_matcher is not None else None
        self._refresh_graph(mistake_id, child_id, session_id, version=version)
        with self.database.connect() as c:
            _owner(c, child_id, session_id)
            return self._view(c, mistake_id, child_id, session_id, graph_version=version)

    def read_action(self, client_action_id, *, child_id, mistake_id):
        with self.database.connect() as c:
            _owner(c,child_id)
            source,revision=_source(c,child_id,mistake_id)
            row=c.execute('SELECT * FROM mistake_learning_action WHERE client_action_id=? AND child_id=? AND mistake_id=?',(client_action_id,child_id,mistake_id)).fetchone()
            if not row: _fail('MISTAKE_LEARNING_ACTION_NOT_FOUND',404)
            if row['source_revision']!=revision: _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
            if row['result_json']: return json.loads(row['result_json'])
            return self._view(c,mistake_id,child_id,row['session_id'])|{'operation':row['operation'],'action_status':_action_status(row,generation(c))}

    def invalidate_focus(self, session_id, child_id, expected_generation=None):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _owner(c, child_id, session_id)
            row=c.execute('SELECT * FROM mistake_learning_focus WHERE session_id=? AND child_id=?',(session_id,child_id)).fetchone()
            if row and expected_generation is not None and row['focus_generation'] != expected_generation:
                _fail('MISTAKE_LEARNING_FOCUS_CHANGED')
            if row and row['mistake_id']:
                c.execute('UPDATE mistake_learning_focus SET mistake_id=NULL,focus_generation=focus_generation+1,created_at=? WHERE session_id=?',(_now(),session_id))
            c.commit()
            return {'version':1,'session_id':session_id,'child_id':child_id,'focus':None}

    def library(self, child_id):
        version = self.graph_matcher.cache_version() if self.graph_matcher is not None else None
        if version is not None:
            with self.database.connect() as c:
                _owner(c, child_id)
                ids = [r['mistake_id'] for r in _ids(c, child_id)]
            changed = 0
            for mid in reversed(ids):
                if changed >= GRAPH_REFRESH_BATCH:
                    break
                try:
                    changed += int(self._refresh_graph(mid, child_id, version=version))
                except MistakeLearningError as error:
                    if error.code != 'MISTAKE_LEARNING_SOURCE_NOT_FOUND':
                        raise
        with self.database.connect() as c:
            _owner(c, child_id)
            items = [self._view(c, r['mistake_id'], child_id, graph_version=version) for r in _ids(c, child_id)]
        due = [r for r in items if _is_due(r)]
        result = {'version': VERSION, 'child_id': child_id, 'items': items, 'today': {'date': datetime.now().astimezone().date().isoformat(), 'due_count': len(due), 'remaining_questions': sum(r['today']['remaining_count'] for r in due)}}
        if version is not None:
            result['graph_pending_count'] = sum(item['graph_context'] is None for item in items)
        return result

    def focus(self, mistake_id, *, child_id, session_id, client_action_id, expected_revision=None, guard=None):
        if not session_id:
            _fail('MISTAKE_LEARNING_SESSION_REQUIRED', 422)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _owner(c, child_id, session_id)
            _guard(c, guard)
            source, revision = _source(c, child_id, mistake_id)
            req = _hash([mistake_id, child_id, session_id, 'focus'])
            existing = c.execute('SELECT * FROM mistake_learning_action WHERE client_action_id=?', (client_action_id,)).fetchone()
            if existing:
                if existing['request_sha256'] != req or existing['source_revision'] != revision:
                    _fail('MISTAKE_LEARNING_ACTION_CONFLICT')
                return json.loads(existing['result_json'])
            row = self._state_row(c, mistake_id, child_id, revision)
            self._version(row, expected_revision)
            prev = c.execute('SELECT * FROM mistake_learning_focus WHERE session_id=?', (session_id,)).fetchone()
            gen = prev['focus_generation'] if prev and prev['mistake_id'] == mistake_id and prev['source_revision'] == revision else (prev['focus_generation'] if prev else 0)+1
            c.execute('INSERT INTO mistake_learning_focus VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET child_id=excluded.child_id,mistake_id=excluded.mistake_id,source_revision=excluded.source_revision,focus_generation=excluded.focus_generation,created_at=excluded.created_at', (session_id, child_id, mistake_id, revision, gen, prev['created_at'] if prev and prev['focus_generation'] == gen else _now()))
            result = self._view(c, mistake_id, child_id, session_id) | {'operation': 'focus', 'action_status': 'completed'}
            now = _now()
            c.execute('INSERT INTO mistake_learning_action VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (client_action_id, mistake_id, child_id, session_id, 'focus', req, revision, generation(c), 'completed', _json(result), None, None, now, now))
            _guard(c, guard)
            c.commit()
            return result

    @staticmethod
    def _version(row, expected):
        if expected is not None and (type(expected) is not int or expected < 0):
            _fail('MISTAKE_LEARNING_REVISION_INVALID', 422)
        if expected is not None and row['revision'] != expected:
            _fail('MISTAKE_LEARNING_REVISION_CHANGED')

    async def analyze(self, mid, **kw): return await self._operate('analyze', mid, **kw)
    async def practice(self, mid, **kw): return await self._operate('practice', mid, **kw)
    async def coach(self, mid, **kw): return await self._operate('coach', mid, **kw)
    async def answer(self, mid, **kw): return await self._operate('answer', mid, **kw)

    @staticmethod
    def _document_review_scope(c, child_id, session_id, lease_id):
        _owner(c, child_id, session_id)
        row = c.execute('''SELECT s.observation_revision FROM daily_session s JOIN sensor_lease l USING(session_id)
            WHERE s.session_id=? AND l.lease_id=? AND l.device_type='document_camera' AND l.status='active'
              AND julianday(l.expires_at)>julianday('now') AND s.observation_status='enabled'
              AND (s.snooze_until IS NULL OR julianday(s.snooze_until)<=julianday('now'))''',
            (session_id, lease_id)).fetchone()
        if not row:
            _fail('MISTAKE_REVIEW_CAMERA_LEASE_REQUIRED', 403)
        return row['observation_revision']

    async def review_ocr(self, workflow, *, child_id, session_id, lease_id, client_action_id, image_bytes):
        action_id = 'mistake-review-ocr:' + client_action_id
        digest = hashlib.sha256(image_bytes).hexdigest()
        with self.database.connect() as c:
            snapshot = {'lease_id': lease_id, 'image_sha256': digest,
                        'observation_revision': self._document_review_scope(c, child_id, session_id, lease_id),
                        'storage_generation': generation(c)}
            existing = c.execute('SELECT * FROM capture_artifact WHERE client_action_id=?', (action_id,)).fetchone()
            if existing:
                details = json.loads(existing['details_json'])
                if (existing['child_id'] != child_id or existing['session_id'] != session_id
                        or details.get('local_pipeline', {}).get('image_sha256') != digest
                        or (details.get('mistake_review_capture') and details['mistake_review_capture'] != snapshot)):
                    _fail('MISTAKE_REVIEW_CAPTURE_CONFLICT')
                if details.get('mistake_review_capture') == snapshot:
                    return {'artifact_id': existing['artifact_id']}
        artifact = await asyncio.to_thread(workflow.process, session_id, image_bytes, action_id, False)
        with self.database.external_effect_guard():
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if (self._document_review_scope(c, child_id, session_id, lease_id) != snapshot['observation_revision']
                        or generation(c) != snapshot['storage_generation']):
                    _fail('MISTAKE_REVIEW_CAPTURE_CHANGED')
                row = c.execute('''SELECT details_json FROM capture_artifact WHERE artifact_id=? AND child_id=?
                    AND session_id=? AND artifact_type='document_ocr' AND local_path IS NULL AND client_action_id=?''',
                    (artifact['artifact_id'], child_id, session_id, action_id)).fetchone()
                if not row:
                    _fail('MISTAKE_REVIEW_CAPTURE_CHANGED')
                details = json.loads(row['details_json'])
                if (details.get('local_pipeline', {}).get('image_sha256') != digest
                        or (details.get('mistake_review_capture') and details['mistake_review_capture'] != snapshot)):
                    _fail('MISTAKE_REVIEW_CAPTURE_CONFLICT')
                details['mistake_review_capture'] = snapshot
                c.execute("UPDATE capture_artifact SET artifact_type='mistake_review_ocr',details_json=? WHERE artifact_id=?",
                          (_json(details), artifact['artifact_id']))
                c.commit()
        return {'artifact_id': artifact['artifact_id']}

    def _paper_review_text(self, c, child_id, session_id, artifact_id):
        from .mistake_original_review import has_explicit_fraction
        row = c.execute('''SELECT extracted_text, confidence, details_json, status FROM capture_artifact
            WHERE artifact_id=? AND child_id=? AND session_id=? AND artifact_type='mistake_review_ocr' AND local_path IS NULL''',
            (artifact_id, child_id, session_id)).fetchone()
        details = json.loads(row['details_json']) if row else {}
        capture = details.get('mistake_review_capture')
        if (not isinstance(capture, dict) or capture.get('storage_generation') != generation(c)
                or capture.get('image_sha256') != details.get('local_pipeline', {}).get('image_sha256')
                or self._document_review_scope(c, child_id, session_id, capture.get('lease_id')) != capture.get('observation_revision')):
            _fail('MISTAKE_REVIEW_CAPTURE_CHANGED')
        answer = (row['extracted_text'] or '').strip()
        if not answer or len(answer) > 4000:
            return ''
        if row['status'] == 'completed' and (row['confidence'] or 0) >= .80:
            return answer
        # The general document pipeline routes symbolic regions for manual
        # review. An explicit, strongly recognized fraction can still be
        # checked locally as a single scalar, without inferring other formulae.
        issues = set((details.get('quality') or {}).get('issues', ()))
        if (row['status'] == 'quality_warning' and issues == {'formula_review_required'}
                and (details.get('ocr_confidence') or 0) >= .90 and has_explicit_fraction(answer)):
            return answer
        return ''

    def review(self, mid, **kwargs):
        """Keep deterministic review available to the local scoring callers."""
        result, _ = self._begin_original_review(mid, use_cloud=False, **kwargs)
        return result

    def _begin_original_review(self, mid, *, child_id, client_action_id, session_id, expected_revision=None,
                               source_revision=None, modality=None, utterance=None, artifact_id=None,
                               voice_input_id=None, guard=None, use_cloud):
        from .mistake_original_review import prepare_grade, review_context
        _text(client_action_id, 180)
        if not session_id:
            _fail('MISTAKE_LEARNING_SESSION_REQUIRED', 422)
        if (modality not in ('paper', 'voice') or (modality == 'paper' and (not artifact_id or voice_input_id))
                or (modality == 'voice' and (artifact_id or utterance is None))):
            _fail('MISTAKE_LEARNING_ARGUMENT_INVALID', 422)
        request_hash = _hash(['review', mid, child_id, session_id, source_revision, modality, artifact_id, voice_input_id,
                              utterance.strip() if utterance else None])
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _owner(c, child_id, session_id)
            _guard(c, guard)
            source, current_revision = _source(c, child_id, mid)
            if source_revision is not None and source_revision != current_revision:
                _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
            prior = c.execute('SELECT * FROM mistake_learning_action WHERE client_action_id=?', (client_action_id,)).fetchone()
            if prior:
                if prior['request_sha256'] != request_hash or prior['source_revision'] != current_revision:
                    _fail('MISTAKE_LEARNING_ACTION_CONFLICT')
                if prior['result_json']:
                    return json.loads(prior['result_json']), None
                return self._view(c, mid, child_id, session_id) | {
                    'operation': 'review', 'action_status': _action_status(prior, generation(c)),
                }, None
            row = self._state_row(c, mid, child_id, current_revision)
            self._version(row, expected_revision)
            if row['status'] == 'processing' and self._view(c, mid, child_id, session_id)['status'] == 'processing':
                _fail('MISTAKE_LEARNING_ACTION_BUSY')
            work = {'mid': mid, 'child_id': child_id, 'session_id': session_id, 'source_revision': current_revision,
                    'revision': row['revision'], 'generation': generation(c), 'client_action_id': client_action_id,
                    'request_hash': request_hash, 'modality': modality, 'artifact_id': artifact_id,
                    'voice_input_id': voice_input_id, 'utterance': utterance, 'guard': guard}
            answer = self._original_review_input(c, work)
            if answer:
                result, basis = prepare_grade(c, child_id, session_id, mid, source['question'], answer, modality)
            else:
                result, basis = ('uncertain', '这张纸还没拍清，请对准高拍仪再拍一次，或用语音回答。', ''), None
            if use_cloud and basis is not None:
                if self.gateway is None or getattr(self.gateway, 'configured', True) is not True:
                    result = ('uncertain', '云端判题暂未配置，请让家长检查模型设置。', '')
                else:
                    try:
                        _permission(c)
                    except MistakeLearningError:
                        result = ('uncertain', '这类回答需要云端判题。请让家长核对是否允许发送学习文本。', '')
                    else:
                        names = [row[0] for row in c.execute('SELECT nickname FROM child_profile')]
                        context, response = review_context(source['question'], answer, basis, names)
                        work.update(answer_sha256=_hash(answer), basis=basis, context=context, response=response, names=names)
                        now = _now()
                        c.execute('INSERT INTO mistake_learning_action VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (client_action_id,
                            mid, child_id, session_id, 'review', request_hash, current_revision, work['generation'], 'processing',
                            None, None, _json({'process_run': _PROCESS_RUN}), now, now))
                        c.execute('UPDATE mistake_learning_state SET status="processing",error_code=NULL WHERE mistake_id=?', (mid,))
                        owner = (work['generation'], client_action_id)
                        _ACTIVE_ACTIONS.add(owner)
                        try:
                            c.commit()
                        except BaseException:
                            _ACTIVE_ACTIONS.discard(owner)
                            raise
                        return None, work
            return self._record_original_review(c, work, result), None

    def _original_review_input(self, c, work):
        child_id, session_id = work['child_id'], work['session_id']
        artifact_id, voice_input_id = work['artifact_id'], work['voice_input_id']
        if c.execute('SELECT 1 FROM mistake_learning_original_review WHERE artifact_id=? OR voice_input_id=?',
                     (artifact_id, voice_input_id)).fetchone():
            _fail('MISTAKE_REVIEW_INPUT_USED')
        if work['modality'] == 'paper':
            return self._paper_review_text(c, child_id, session_id, artifact_id)
        answer = _text(work['utterance'], 4000)
        if voice_input_id:
            claimed = c.execute('''SELECT a.summary_json,v.transcript FROM voice_input v
                JOIN audit_event a ON a.audit_id='mistake-workspace-voice-'||v.voice_input_id
                    AND a.child_id=v.child_id AND a.actor='mistake_workspace_voice_v1'
                WHERE v.voice_input_id=? AND v.child_id=? AND v.session_id=? AND v.status='completed' ''',
                (voice_input_id, child_id, session_id)).fetchone()
            scope = {'session_id': session_id, 'kind': 'answer',
                     'mistake_id': work['mid'], 'source_revision': work['source_revision']}
            if (not claimed or claimed['transcript'].strip() != answer or json.loads(claimed['summary_json']) != scope):
                _fail('MISTAKE_REVIEW_VOICE_CHANGED')
        return answer

    def _record_original_review(self, c, work, grade, *, metadata=None):
        judgement, feedback, hint = grade
        mid, child_id, session_id = work['mid'], work['child_id'], work['session_id']
        revision, action = work['source_revision'], work['client_action_id']
        now = _now()
        result = {'judgement': judgement, 'feedback': feedback, 'next_hint': hint, 'modality': work['modality'], 'created_at': now}
        if metadata is not None:
            if judgement != 'uncertain':
                result['grading_basis'] = 'cloud_formative'
            c.execute('UPDATE mistake_learning_action SET status="completed",provider_json=?,updated_at=? WHERE client_action_id=?',
                      (_json(metadata), now, action))
            c.execute('UPDATE mistake_learning_state SET status="ready",error_code=NULL WHERE mistake_id=?', (mid,))
        if judgement != 'uncertain':
            row = c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone()
            graph = self._graph(row)
            analysis = analysis_for_graph(row, graph)
            targets, _ = review_targets(c, row, graph, _hash(graph) if graph else None, analysis)
            days = 1 if targets and targets[0]['kind'] == 'prerequisite' else 2
            next_review_at = (datetime.fromisoformat(now) + timedelta(days=days)).isoformat() if judgement == 'correct' else now
            c.execute('''INSERT INTO mistake_learning_original_review(mistake_id,child_id,source_revision,session_id,
                client_action_id,judgement,artifact_id,voice_input_id,result_json,next_review_at,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (mid, child_id, revision, session_id, action, judgement,
                work['artifact_id'], work['voice_input_id'], _json(result), next_review_at, now))
            c.execute('UPDATE mistake_learning_state SET revision=revision+1,updated_at=? WHERE mistake_id=?', (now, mid))
            if hint:
                c.execute('INSERT INTO mistake_learning_turn VALUES(?,?,?,?,?,?,?,?,?,?,?)', (str(uuid4()), mid,
                    child_id, revision, session_id, action, 'assistant', 'coach', hint, None, now))
        response = self._view(c, mid, child_id, session_id) | {'operation': 'review', 'action_status': 'completed', 'review_result': result}
        if metadata is None:
            c.execute('INSERT INTO mistake_learning_action VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (action, mid, child_id,
                session_id, 'review', work['request_hash'], revision, work['generation'], 'completed', _json(response),
                None, None, now, now))
        else:
            c.execute('UPDATE mistake_learning_action SET result_json=? WHERE client_action_id=?', (_json(response), action))
        _guard(c, work['guard'])
        c.commit()
        return response

    def _fresh_original_review(self, c, work):
        from .mistake_original_review import prepare_grade
        mid, child_id, session_id = work['mid'], work['child_id'], work['session_id']
        self._fresh(c, mid, child_id, session_id, work, work['guard'])
        row = c.execute('''SELECT state.revision FROM mistake_learning_state state
            JOIN mistake_learning_action action ON action.mistake_id=state.mistake_id AND action.child_id=state.child_id
            WHERE state.mistake_id=? AND state.child_id=? AND state.status='processing' AND action.client_action_id=?
                AND action.source_revision=state.source_revision AND action.status='processing' AND action.storage_generation=?
                AND action.request_sha256=? ''',
            (mid, child_id, work['client_action_id'], work['generation'], work['request_hash'])).fetchone()
        if row is None or row['revision'] != work['revision']:
            _fail('MISTAKE_LEARNING_REVISION_CHANGED')
        answer = self._original_review_input(c, work)
        source, _ = _source(c, child_id, mid)
        _, basis = prepare_grade(c, child_id, session_id, mid, source['question'], answer, work['modality'])
        if _hash(answer) != work['answer_sha256'] or basis != work['basis']:
            _fail('MISTAKE_LEARNING_SOURCE_CHANGED')

    async def review_with_cloud(self, mid, **kwargs):
        from .mistake_original_review import (REVIEW_PROMPT, RUBRIC_PROMPT, VERIFY_PROMPT, UncertainReview,
                                               read_assessment, read_rubric, reconcile_assessments)
        if self.graph_matcher is not None:
            await asyncio.to_thread(self._refresh_graph, mid, kwargs['child_id'], kwargs.get('session_id'))
        local, work = self._begin_original_review(mid, use_cloud=True, **kwargs)
        if work is None:
            return local
        action = work['client_action_id']
        owner = (work['generation'], action)
        deadline = monotonic() + 55
        metadata = {'kind': 'cloud_formative_original_review_v1', 'calls': {}}

        async def ask(stage, request_type, prompt, context):
            def pre_dispatch():
                with self.database.connect() as c:
                    self._fresh_original_review(c, work)
            pre_dispatch()
            result = await asyncio.wait_for(self.gateway.complete(ModelRequest(
                request_id=f'{action}:{stage}', child_id=work['child_id'], session_id=work['session_id'],
                request_type=request_type, risk_level='medium', provider_preference='primary',
                context_manifest={'scope': 'selected_original_review_v1', 'stage': stage, 'raw_media': False, 'formal_mastery': False},
                policy_version='selected_original_review_v1', prompt_version=f'original_review_{stage}_v1',
                knowledge_version=None, output_type='diagnosis', evidence_refs=(),
                messages=({'role': 'system', 'content': prompt}, {'role': 'user', 'content': _json(context)}),
                pre_dispatch=pre_dispatch)), timeout=max(.001, deadline-monotonic()))
            metadata['calls'][stage] = {key: getattr(result, key, None) for key in
                ('provider', 'model', 'latency_ms', 'prompt_tokens', 'completion_tokens')}
            return result.content

        try:
            try:
                rubric = read_rubric(await ask('rubric', 'paper_scoring_authority_draft', RUBRIC_PROMPT, work['context']))
                context = work['context'] | {'response': work['response'], 'rubric': {'task_points': rubric, 'rubric_dimensions': []}}
                first = read_assessment(context, await ask('assessment', 'learning_outcome_evaluation', REVIEW_PROMPT, context))
                second = read_assessment(context, await ask('verification', 'learning_outcome_evaluation', VERIFY_PROMPT, context))
                accepted = reconcile_assessments(first, second, work['names'])
                grade = (accepted.judgement, accepted.feedback, accepted.hint)
                metadata.update(criteria=rubric, evidence=accepted.checks,
                                basis='registered_references' if work['basis'] else 'question_derived')
            except UncertainReview:
                grade = ('uncertain', '这次的判题依据还不足。可以补充题目要求或再说明你的推理。', '')
            except MistakeLearningError:
                raise
            except Exception as error:
                # Provider/network failures are visible as uncertainty. The
                # immutable result still lets a reload recover this attempt.
                code = getattr(error, 'code', None)
                metadata['provider_error'] = code if isinstance(code, str) and re.fullmatch(r'[A-Z0-9_]{1,90}', code) else 'MODEL_UNAVAILABLE'
                grade = ('uncertain', '云端判题暂时没有返回可用结果，可以稍后再试。', '')
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                self._fresh_original_review(c, work)
                return self._record_original_review(c, work, grade, metadata=metadata)
        except BaseException as error:
            code = error.code if isinstance(error, MistakeLearningError) else 'MISTAKE_LEARNING_OUTCOME_UNKNOWN'
            try:
                self._failed(mid, action, 'unknown' if isinstance(error, asyncio.CancelledError) else 'failed', code, work['generation'])
            except Exception:
                pass
            raise
        finally:
            _ACTIVE_ACTIONS.discard(owner)

    async def _operate(self, operation, mid, *, child_id, client_action_id, session_id=None, expected_revision=None, practice_item_id=None, utterance=None, style='socratic', count=3, original_review=False, guard=None, voice_input_id=None, coach_turn_id=None, source_revision=None):
        _text(client_action_id, 180)
        if style not in ('direct', 'socratic') or type(count) is not int or not 1 <= count <= 3:
            _fail('MISTAKE_LEARNING_ARGUMENT_INVALID', 422)
        if voice_input_id:
            if guard or utterance is not None or not session_id or client_action_id != 'mistake-voice:'+voice_input_id or not source_revision:
                _fail('MISTAKE_LEARNING_ARGUMENT_INVALID',422)
            if not (operation=='coach' and original_review and coach_turn_id and not practice_item_id
                    or operation=='answer' and practice_item_id and not coach_turn_id and not original_review):
                _fail('MISTAKE_LEARNING_ARGUMENT_INVALID',422)
        elif operation in ('answer', 'coach'):
            utterance = _text(utterance, 4000)
        elif utterance is not None:
            utterance = _text(utterance, 4000)
        if original_review and (operation != 'coach' or practice_item_id):
            _fail('MISTAKE_LEARNING_ARGUMENT_INVALID', 422)
        expected_source = source_revision
        version = self.graph_matcher.cache_version() if self.graph_matcher is not None else None
        if version is not None and operation in ('analyze', 'practice', 'coach', 'answer'):
            await asyncio.to_thread(self._refresh_graph, mid, child_id, session_id, version=version)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _owner(c, child_id, session_id)
            _guard(c, guard)
            source, source_revision = _source(c, child_id, mid)
            if voice_input_id:
                if expected_source != source_revision:
                    _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
                voice = c.execute('''SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?
                    AND purpose='native_cloud_dialogue' AND status='completed' AND local_path IS NULL''',
                    (voice_input_id, child_id, session_id)).fetchone()
                if not voice:
                    _fail('MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED',403)
                utterance = _text(voice['transcript'], 4000)
            request_fields = [operation, mid, child_id, session_id, practice_item_id, utterance, style, count]
            if original_review:
                request_fields.append('original_review')
            if voice_input_id:
                request_fields.extend([voice_input_id, coach_turn_id, expected_source])
            request_hash = _hash(request_fields)
            old = c.execute('SELECT * FROM mistake_learning_action WHERE client_action_id=?', (client_action_id,)).fetchone()
            if old:
                if old['request_sha256'] != request_hash:
                    _fail('MISTAKE_LEARNING_ACTION_CONFLICT')
                if old['source_revision'] != source_revision:
                    _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
                if old['result_json']:
                    return json.loads(old['result_json'])
                return self._view(c, mid, child_id, session_id) | {'operation': operation, 'action_status': 'unknown' if old['status'] == 'processing' else old['status']}
            if voice_input_id:
                guard = self._coaching_voice_guard(c, voice, mid, source_revision, client_action_id, coach_turn_id, practice_item_id)
                _guard(c, guard)
            row = self._state_row(c, mid, child_id, source_revision)
            self._version(row, expected_revision)
            _permission(c)
            graph = self._graph(row)
            if graph and version is not None and graph['graph_version'] != version:
                graph = None
            graph_sha = _hash(graph) if graph else None
            analysis_key = self._analysis_context_key(graph)
            saved_analysis = json.loads(row['analysis_json']) if row['analysis_context_key'] == analysis_key and row['analysis_json'] else None
            # Same-source analysis is reusable; no duplicate paid request on a UI/voice race.
            pending = next((r for r in c.execute('SELECT * FROM mistake_learning_action WHERE mistake_id=? AND source_revision=? AND storage_generation=? AND status IN ("processing","unknown") AND operation!="focus" ORDER BY created_at', (mid,source_revision,generation(c))) if _action_status(r,generation(c))=='processing' or r['request_sha256']==request_hash),None)
            if pending:
                return self._view(c, mid, child_id, session_id) | {'operation': operation, 'action_status': _action_status(pending,generation(c)), 'pending_client_action_id': pending['client_action_id']}
            item = None
            variant = _coach_variant(c, mid, child_id, source_revision)
            variant_id = variant['practice_item_id'] if variant else None
            if practice_item_id:
                item = variant if practice_item_id == variant_id else c.execute('SELECT * FROM mistake_learning_item WHERE practice_item_id=? AND mistake_id=? AND child_id=? AND source_revision=? AND batch_id=?', (practice_item_id, mid, child_id, source_revision, row['batch_id'])).fetchone()
                if not item:
                    _fail('MISTAKE_LEARNING_PRACTICE_CHANGED')
            if operation == 'coach' and not original_review and not item and row['batch_id']:
                candidates = c.execute('SELECT * FROM mistake_learning_item WHERE mistake_id=? AND source_revision=? AND batch_id=? AND answered_at IS NULL ORDER BY position',(mid,source_revision,row['batch_id']))
                item = next((candidate for candidate in candidates if not practice_graph_is_stale(candidate, graph_sha)), None)
            if item and operation in ('answer', 'coach') and practice_graph_is_stale(item, graph_sha):
                _fail('MISTAKE_LEARNING_PRACTICE_CHANGED')
            separate_check = bool(item and item['practice_item_id'] == variant_id)
            if operation == 'answer' and not separate_check and row['practice_date'] != datetime.now().astimezone().date().isoformat():
                _fail('MISTAKE_LEARNING_PRACTICE_CHANGED')
            if operation == 'answer' and not separate_check and row['needs_explanation']:
                _fail('MISTAKE_LEARNING_EXPLANATION_FIRST')
            if operation == 'answer' and (not item or item['answered_at']):
                _fail('MISTAKE_LEARNING_UNANSWERED_ITEM_REQUIRED')
            view = self._view(c, mid, child_id, session_id, graph_version=version)
            if operation == 'practice' and row['needs_explanation']:
                only_stale_items = view['graph_practice_stale'] and all(
                    item['status'] == 'stale' for item in view['practice_items']
                )
                if not only_stale_items:
                    _fail('MISTAKE_LEARNING_EXPLANATION_FIRST')
            if operation == 'analyze' and saved_analysis or operation == 'practice' and row['practice_date']==view['today']['date'] and (view['today']['status']=='done' or any(r['status'] == 'ready' for r in view['practice_items'])):
                result=view | {'operation':operation,'action_status':'completed'}
                now=_now()
                c.execute('INSERT INTO mistake_learning_action VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(client_action_id,mid,child_id,session_id,operation,request_hash,source_revision,generation(c),'completed',_json(result),None,_json({'reused':True}),now,now))
                _guard(c,guard)
                c.commit()
                return result
            now = _now()
            c.execute('INSERT INTO mistake_learning_action VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (client_action_id, mid, child_id, session_id, operation, request_hash, source_revision, generation(c), 'processing', None, None, _json({'process_run':_PROCESS_RUN}), now, now))
            if utterance and operation in ('coach','answer'):
                c.execute('INSERT INTO mistake_learning_turn VALUES(?,?,?,?,?,?,?,?,?,?,?)', (str(uuid4()), mid, child_id, source_revision, session_id, client_action_id, 'user', 'answer' if operation == 'answer' else 'coach', utterance, item['practice_item_id'] if item else None, now))
            c.execute('UPDATE mistake_learning_state SET status="processing",error_code=NULL WHERE mistake_id=?', (mid,))
            work_key=(generation(c),client_action_id)
            _ACTIVE_ACTIONS.add(work_key)
            try:c.commit()
            except BaseException:
                _ACTIVE_ACTIONS.discard(work_key)
                raise
            snapshot = {'source_revision': source_revision, 'generation': work_key[0], 'graph_sha': graph_sha,
                        'graph_version': graph['graph_version'] if graph else None}
            history = [t for t in view['turns'] if t['practice_item_id']==(item['practice_item_id'] if item else None)][-8:]
            previous_phase = None
            prior_variants = []
            if operation == 'coach':
                previous = _latest_coach_turn(c, mid, child_id, source_revision, item['practice_item_id'] if item else None)
                previous_phase = previous['phase'] if previous else None
                prior_variants = [v[0] for v in c.execute('''SELECT question FROM mistake_learning_item
                    WHERE mistake_id=? AND child_id=? AND source_revision=? ORDER BY created_at DESC LIMIT 30''',
                    (mid, child_id, source_revision))]
            coaching_step = 'follow_up' if previous_phase else 'opening'
            names = [r[0] for r in c.execute('SELECT nickname FROM child_profile')]
            targets, _ = review_targets(c, row, graph, graph_sha, saved_analysis)
        targets = self._authorized_cloud_targets(targets)
        # No raw family media, profile identity, other cases, or formal bank rows enter this context.
        practice_plan = []
        if operation == 'practice' and targets:
            practice_plan = [targets[0]] + [targets[-1]] * (count - 1)
        scoped_history=[{'role':t['role'],'text':t['text']} for t in history]
        if operation=='coach':
            current_question=item['question'] if item else source['question']
            context={'question':current_question,'utterance':utterance,'style':style,
                     'history':scoped_history,'coaching_step':coaching_step,
                     'previous_phase':previous_phase,
                     'independent_practice_allowed':not item and previous_phase in ('question', 'explain_step')
                     and not (variant and not variant['answered_at'])}
            if not item:
                context['original_answer'] = source['original_answer']
                if targets:
                    context['graph_targets'] = cloud_targets(targets, names)
            elif item and (target := practice_graph_target(item, graph_sha)):
                binding = json.loads(item['validation_json']).get('graph_target', {})
                allowed = self._authorized_cloud_targets(study_targets(graph))
                if any(entry['key'] == binding.get('key') and entry['title'] == target['title']
                       and entry['kind'] == target['kind'] for entry in allowed):
                    context['practice_focus'] = target['title']
        elif operation=='answer' and item:
            context={'practice':{'question':item['question'],'explanation':item['explanation'],
                        'expected_answer':json.loads(item['expected_json'])},
                     'utterance':utterance,'history':scoped_history}
        else:
            model_source = source | {'subject': graph['subject']} if graph and graph.get('subject') else source
            context = {'source': model_source, 'analysis': saved_analysis, 'utterance': utterance, 'style': style, 'count': count,
                       'history': scoped_history, 'previous_questions':[v['question'] for v in view['practice_items']] if row['practice_date']==view['today']['date'] else []}
            if operation == 'analyze' and graph:
                context['graph_targets'] = cloud_targets(self._authorized_cloud_targets(study_targets(graph)), names)
            if practice_plan:
                context['graph_practice_plan'] = cloud_targets(practice_plan, names)
        semantic_options, semantic_keys = [], {}
        if (operation in ('analyze', 'coach') and not item and graph and graph.get('status') == 'ambiguous'
                and self.graph_matcher is not None and hasattr(self.graph_matcher, 'semantic_options')):
            semantic_options, semantic_keys = self.graph_matcher.semantic_options(graph)
            semantic_options = [{'ref': item['ref'], 'label': sanitize_cloud_text(item['label'], names).text}
                                for item in semantic_options]
            if semantic_options:
                context['graph_options'] = semantic_options
        sent = [False]
        main_sent = [False]
        started=monotonic()
        deadline=started+(115 if _tutor_route_identity(self.gateway) else 65)
        def pre_dispatch():
            with self.database.connect() as c:
                self._fresh(c, mid, child_id, session_id, snapshot, guard)
            if main_sent[0]:
                _fail('MISTAKE_LEARNING_OUTCOME_UNKNOWN')
            main_sent[0] = True
            sent[0] = True  # Includes retries/fallbacks: one physical attempt per durable action.
        try:
            coach_check_trace=None
            semantic_ref = None
            semantic_graph = None
            semantic_repaired = False
            if operation=='coach':
                check_sent=[False]
                def check_dispatch():
                    with self.database.connect() as c:
                        self._fresh(c,mid,child_id,session_id,snapshot,guard)
                    if check_sent[0]:_fail('MISTAKE_LEARNING_OUTCOME_UNKNOWN')
                    check_sent[0]=True;sent[0]=True
                checked=await asyncio.wait_for(self.gateway.complete(ModelRequest(
                    request_id=client_action_id+':coach-check',child_id=child_id,session_id=session_id or 'mistake-learning',
                    request_type='mistake_learning_verify',risk_level='medium',context_manifest={'scope':'selected_mistake_formative_v1','source_sha256':source_revision,'raw_media':False,'formal_mastery':False},
                    provider_preference='primary',policy_version='selected_mistake_formative_v1',prompt_version='mistake_coach_neutral_check_v1',knowledge_version=None,output_type='diagnosis',evidence_refs=(),
                    messages=({'role':'system','content':self._coach_check_instruction()},
                              {'role':'user','content':sanitize_cloud_text(_json({'question':current_question,'utterance':utterance}),names).text}),
                    pre_dispatch=check_dispatch)),timeout=max(.001,deadline-monotonic()))
                _diagnostic('mistake.learning.model_result','mistake_learning_verify','completed',result=checked)
                model_check=self._parse_coach_check(checked.content)
                coach_check_trace={'result':model_check,**{k:getattr(checked,k,None) for k in ('provider','model','latency_ms','prompt_tokens','completion_tokens')}}
                # Keep the fact-check private even if later coaching is interrupted.
                # process_run remains present so an active action is never mistaken
                # for an abandoned previous-process request.
                with self.database.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    self._fresh(c,mid,child_id,session_id,snapshot,guard)
                    c.execute('UPDATE mistake_learning_action SET provider_json=?,updated_at=? WHERE client_action_id=? AND storage_generation=? AND source_revision=? AND status="processing"',
                              (_json({'process_run':_PROCESS_RUN,'coach_fact_check':coach_check_trace}),_now(),client_action_id,snapshot['generation'],source_revision))
                    self._fresh(c,mid,child_id,session_id,snapshot,guard)
                    c.commit()
                context['model_check']=model_check
            if operation == 'analyze' and saved_analysis:
                parsed, metadata = saved_analysis, {'reused': True}
            else:
                clean = sanitize_cloud_text(_json(context), names).text
                result = await asyncio.wait_for(self.gateway.complete(ModelRequest(
                    request_id=client_action_id, child_id=child_id, session_id=session_id or 'mistake-learning',
                    request_type='mistake_learning_'+operation, risk_level='medium', context_manifest={'scope': 'selected_mistake_formative_v1', 'source_sha256': source_revision, 'raw_media': False, 'formal_mastery': False},
                    provider_preference='primary', policy_version='selected_mistake_formative_v1',
                    prompt_version='mistake_learning_coach_socratic_v2' if operation=='coach' else 'mistake_learning_v1',
                    knowledge_version=None, output_type='diagnosis' if operation != 'coach' else 'tutoring', evidence_refs=(),
                    messages=({'role': 'system', 'content': self._instruction(operation) + (SEMANTIC_INSTRUCTION if semantic_options else '')}, {'role': 'user', 'content': clean}), pre_dispatch=pre_dispatch)), timeout=max(.001,deadline-monotonic()))
                _diagnostic('mistake.learning.model_result', 'mistake_learning_'+operation, 'completed', result=result)
                metadata = {k: getattr(result, k, None) for k in ('provider', 'model', 'latency_ms', 'prompt_tokens', 'completion_tokens')}
                if coach_check_trace is not None:metadata['coach_fact_check']=coach_check_trace
                def extra_dispatch():
                    sent = False
                    def dispatch():
                        nonlocal sent
                        with self.database.connect() as c:
                            self._fresh(c, mid, child_id, session_id, snapshot, guard)
                        if sent: _fail('MISTAKE_LEARNING_OUTCOME_UNKNOWN')
                        sent = True
                    return dispatch
                if operation == 'coach':
                    parsed, format_repaired = await self._parse_coaching_reply(client_action_id,child_id,session_id,
                        result.content,context,names,extra_dispatch,deadline,source)
                    metadata['coaching_format_repaired'] = format_repaired
                    semantic_repaired = format_repaired
                    metadata['coaching_phase'] = parsed['phase']
                    if parsed['phase']=='explain_step':
                        parsed, repaired = await self._verify_coaching_step(client_action_id, child_id, session_id,
                            parsed, context, names, extra_dispatch, deadline, source)
                        metadata['step_scope_checked'] = True
                        metadata['step_scope_repaired'] = repaired
                        semantic_repaired = semantic_repaired or repaired
                    if parsed['phase']=='independent_practice':
                        variant_context = {'source': {'question': current_question, 'subject': source['subject']},
                            'count': 1, 'previous_questions': prior_variants, 'coach_focus': parsed['focus_step']}
                        parsed['variant'], metadata['answer_check'], metadata['variant_repaired'] = await self._generate_coach_variant(
                            client_action_id,child_id,session_id,variant_context,source,names,extra_dispatch,deadline)
                        parsed['text'] = '现在请独立做下面这一道变式题。'
                else:
                    parsed = self._parse(operation, result.content, source, count, previous_questions=context.get('previous_questions',()),
                                         graph=graph, graph_sha=graph_sha, practice_plan=practice_plan)
                if operation == 'practice':
                    metadata['answer_check'] = await self._verify_variants(client_action_id, child_id, session_id,
                        parsed, [t['title'] for t in practice_plan], names, extra_dispatch(), deadline)

                if semantic_options and not semantic_repaired:
                    safe_question = sanitize_cloud_text(source['question'], names).text
                    semantic_ref = parse_semantic_assessments(result.content, safe_question, semantic_options)
                    if semantic_ref in semantic_keys:
                        try:
                            candidate = await asyncio.to_thread(self.graph_matcher.match, child_id, source,
                                                                _verified_key=semantic_keys[semantic_ref])
                        except (OSError, sqlite3.Error, ValueError, TypeError):
                            candidate = None
                        if (isinstance(candidate, dict) and candidate.get('status') == 'matched'
                                and candidate.get('graph_version') == snapshot['graph_version']
                                and isinstance(candidate.get('primary'), dict)
                                and candidate['primary'].get('key') == semantic_keys[semantic_ref]):
                            semantic_graph = candidate | {'verification': {'method': 'model_semantic_v1',
                                                                              'review_status_unchanged': True}}

            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                self._fresh(c, mid, child_id, session_id, snapshot, guard)
                if semantic_graph:
                    c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=? AND child_id=?',
                              (_json(semantic_graph), mid, child_id))
                    snapshot = snapshot | {'graph_sha': _hash(semantic_graph)}
                    if operation == 'analyze':
                        analysis_key = self._analysis_context_key(semantic_graph)
                    metadata['graph_semantic_verified'] = True
                self._persist(c, operation, mid, child_id, session_id, client_action_id, parsed, item, count, style, original_review)
                if operation == 'analyze':
                    c.execute('UPDATE mistake_learning_state SET analysis_context_key=? WHERE mistake_id=?', (analysis_key, mid))
                c.execute('UPDATE mistake_learning_state SET status="ready",error_code=NULL,revision=revision+1,updated_at=? WHERE mistake_id=?', (_now(), mid))
                c.execute('UPDATE mistake_learning_action SET status="completed",provider_json=?,updated_at=? WHERE client_action_id=?', (_json(metadata), _now(), client_action_id))
                response = self._view(c, mid, child_id, session_id) | {'operation': operation, 'action_status': 'completed'}
                if operation == 'coach':
                    response['coach_phase'] = parsed['phase']
                c.execute('UPDATE mistake_learning_action SET result_json=? WHERE client_action_id=?', (_json(response), client_action_id))
                self._fresh(c, mid, child_id, session_id, snapshot, guard)
                c.commit()
                _diagnostic('mistake.learning.action_result', 'mistake_learning_'+operation, 'completed', elapsed_ms=(monotonic()-started)*1000)
                return response
        except asyncio.CancelledError:
            try:
                self._failed(mid,client_action_id,'unknown' if sent[0] else 'failed',
                    'MISTAKE_LEARNING_OUTCOME_UNKNOWN' if sent[0] else 'MISTAKE_LEARNING_CANCELLED',snapshot['generation'])
            except Exception:
                # Revoked write authority may forbid even cleanup. The process
                # liveness marker is still retired in finally; no old call retries.
                pass
            _diagnostic('mistake.learning.action_result', 'mistake_learning_'+operation, 'unknown' if sent[0] else 'cancelled', error_code='MISTAKE_LEARNING_CANCELLED', elapsed_ms=(monotonic()-started)*1000)
            raise
        except Exception as error:
            code = error.code if isinstance(error, MistakeLearningError) else 'MODEL_MISTAKE_REASONING_CONFIG_REQUIRED' if getattr(error,'code',None)=='MODEL_MISTAKE_REASONING_CONFIG_REQUIRED' else 'MISTAKE_LEARNING_MODEL_FAILED'
            status = 'unknown' if sent[0] and (not isinstance(error, MistakeLearningError) or code == 'MISTAKE_LEARNING_OUTCOME_UNKNOWN') else 'failed'
            if not self._failed(mid, client_action_id, status, code, snapshot['generation']):
                status='unknown'
            _diagnostic('mistake.learning.action_result', 'mistake_learning_'+operation, status, error_code=code, provider_error_code=getattr(error,'code',None), elapsed_ms=(monotonic()-started)*1000)
            if isinstance(error, MistakeLearningError) and error.status in (403, 404):
                raise
            return self.state(mid, child_id=child_id, session_id=session_id) | {'operation': operation, 'action_status': status}
        finally:
            _ACTIVE_ACTIONS.discard(work_key)

    def _coaching_voice_guard(self, c, voice, mid, source_revision, action_id, coach_turn_id, practice_item_id):
        from types import SimpleNamespace
        from .agent_runtime import InvalidAgentPlanError
        from .realtime_native_context import _native_witness
        from .realtime_native_mistake_learning import _onset, _time
        from .realtime_native_mistake_onset import require_current

        sid, child_id = voice['session_id'], voice['child_id']
        original = coach_turn_id is not None
        try:
            snapshot = require_current(c, voice, native_focus(c, sid), allow_original=original, select_scope=True)
        except InvalidAgentPlanError:
            _fail('MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED',403)
        if (snapshot['mistake_id'], snapshot['source_revision'], snapshot['practice_item_id']) != (mid, source_revision, practice_item_id):
            _fail('MISTAKE_LEARNING_FOCUS_CHANGED')

        def validate(connection):
            consent = connection.execute('SELECT cloud_audio_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
            if not consent or consent[0] != 1:
                _fail('MISTAKE_LEARNING_CLOUD_CONSENT_REQUIRED',403)
            current_voice = connection.execute('SELECT * FROM voice_input WHERE voice_input_id=?', (voice['voice_input_id'],)).fetchone()
            if current_voice is None or dict(current_voice) != dict(voice):
                _fail('MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED',403)
            completed = connection.execute('SELECT 1 FROM mistake_learning_action WHERE client_action_id=? AND status="completed"',
                (action_id,)).fetchone() is not None
            try:
                capture = _native_witness(SimpleNamespace(page_source_store=None), connection, current_voice)
                if capture is None:
                    _fail('MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED',403)
                require_current(connection, current_voice, snapshot, allow_original=original, allow_own_progress=completed)
                _onset(capture, snapshot, 'coach' if original else 'answer')
                if original and not completed:
                    turn = _latest_coach_turn(connection, mid, child_id, source_revision)
                    if (not turn or turn['turn_id'] != coach_turn_id or turn['phase'] not in ('question','explain_step')
                        or _time(capture.get('speech_started_at')) < _time(turn['created_at'])):
                        _fail('MISTAKE_LEARNING_FOCUS_CHANGED')
            except InvalidAgentPlanError:
                _fail('MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED',403)
            if not original:
                item = _coach_variant(connection, mid, child_id, source_revision)
                if not item or item['practice_item_id'] != practice_item_id:
                    state = connection.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=? AND child_id=? AND source_revision=?',
                                               (mid, child_id, source_revision)).fetchone()
                    item = connection.execute('SELECT * FROM mistake_learning_item WHERE practice_item_id=? AND mistake_id=? AND child_id=? AND source_revision=? AND batch_id=?',
                                              (practice_item_id, mid, child_id, source_revision, state['batch_id'])).fetchone() if state and state['practice_date'] == datetime.now().astimezone().date().isoformat() else None
                    graph = self._graph(state) if state else None
                    if item and practice_graph_is_stale(item, _hash(graph) if graph else None):
                        item = None
                if not item or item['answered_at'] and not completed:
                    _fail('MISTAKE_LEARNING_PRACTICE_CHANGED')
            return True

        return validate

    async def _coaching_aux(self, action, child_id, session_id, instruction, payload, names, pre_dispatch, deadline, *, verify=False):
        request_type = 'mistake_learning_verify' if verify else 'mistake_learning_coach'
        result = await asyncio.wait_for(self.gateway.complete(ModelRequest(
            request_id=action, child_id=child_id, session_id=session_id or 'mistake-learning',
            request_type=request_type, risk_level='medium',
            context_manifest={'scope':'selected_mistake_formative_v1','raw_media':False,'formal_mastery':False},
            provider_preference='primary',policy_version='selected_mistake_formative_v1',
            prompt_version='mistake_coach_step_scope_v1' if verify else 'mistake_coach_repair_v1',
            knowledge_version=None,output_type='diagnosis' if verify else 'tutoring',evidence_refs=(),
            messages=({'role':'system','content':instruction},
                      {'role':'user','content':sanitize_cloud_text(_json(payload),names).text}),
            pre_dispatch=pre_dispatch)),timeout=max(.001,deadline-monotonic()))
        _diagnostic('mistake.learning.model_result', request_type, 'completed', result=result)
        return result.content

    async def _parse_coaching_reply(self, action, child_id, session_id, reply, context, names, dispatch, deadline, source):
        opening = context['coaching_step']=='opening' and context['style']=='socratic'
        def parse(content):
            parsed = self._parse('coach', content, source, 1, allow_graph_assessments=bool(context.get('graph_options')))
            if opening and parsed['phase']!='question':
                _fail('MISTAKE_LEARNING_MODEL_OUTPUT_INVALID')
            if parsed['phase']=='independent_practice' and (not context['independent_practice_allowed']
                    or context['model_check']['claim_status'] in ('incorrect','uncertain')):
                _fail('MISTAKE_LEARNING_MODEL_OUTPUT_INVALID')
            return parsed
        try:
            return parse(reply), False
        except MistakeLearningError as error:
            if error.code!='MISTAKE_LEARNING_MODEL_OUTPUT_INVALID':
                raise
        guidance = ('只返回一个JSON对象 {"phase":"question","text":"一个简短的关键问题？"}。'
                    '根据原答先问一步，整段只出现一个问号，不给答案。') if opening else (
                    '上一稿的结构或阶段不符合要求。重写眼前这一轮，只返回规定的JSON字段。问题只写一句、一个问号。'
                    '条件不足时只问或说明卡住这一步，不进入独立练习。')
        revised = await self._coaching_aux(action+':format-repair',child_id,session_id,self._instruction('coach'),
            context | {'revision_instruction':guidance},names,dispatch(),deadline)
        return parse(revised), True

    async def _verify_coaching_step(self, action, child_id, session_id, parsed, context, names, dispatch, deadline, source):

        evidence = {k: context[k] for k in ('question','utterance','history','model_check')}
        if 'original_answer' in context:
            evidence['original_answer'] = context['original_answer']
        check = ('你是逐步讲解核对员。根据question、孩子的原答和最近的问题与回答，先判断孩子卡住的最小步骤。'
                 '核对candidate是否仅准确解释这一步，给孩子留下后续步骤独立完成。若讲到尚未问的下一步、把解题过程做完、'
                 '明说或暗示原题最后结果、重复整个解法、发出多个问题或说明有事实错误，标false。即使最后结果正确也标false。'
                 '若卡点就在产生最终结果的那一步，只能解释规则或用另一组小数字示范。只返回JSON {"valid":true} 或 {"valid":false}。')

        async def accepted(candidate, suffix):
            reply = await self._coaching_aux(action+suffix,child_id,session_id,check,
                evidence | {'candidate':candidate['text'], 'focus_step':candidate.get('focus_step')},names,dispatch(),deadline,verify=True)
            try:
                verdict = json.loads(reply)
            except (TypeError,ValueError):
                return False
            return isinstance(verdict,dict) and set(verdict)=={'valid'} and verdict['valid'] is True

        if await accepted(parsed, ':step-check'):
            return parsed, False
        revision = context | {'revision_instruction':'上一稿未通过范围核对。删掉后续操作和最终结果。只讲上一问卡住的微步骤，最后停下或问一个只检查这一步的问题。不要把整题拿来让孩子重抄。必须返回explain_step。'}
        revised = self._parse('coach', await self._coaching_aux(action+':step-repair',child_id,session_id,
            self._instruction('coach'),revision,names,dispatch(),deadline), source, 1)
        if revised['phase']!='explain_step' or not await accepted(revised, ':step-recheck'):
            _fail('MISTAKE_LEARNING_MODEL_OUTPUT_INVALID')
        return revised, True

    async def _generate_coach_variant(self, action, child_id, session_id, context, source, names, dispatch, deadline):
        async def generate_and_check(attempt, candidate_context):
            generated = await asyncio.wait_for(self.gateway.complete(ModelRequest(
                request_id=attempt+':variant', child_id=child_id, session_id=session_id or 'mistake-learning',
                request_type='mistake_learning_practice', risk_level='medium', context_manifest={'scope':'selected_mistake_formative_v1','raw_media':False,'formal_mastery':False},
                provider_preference='primary', policy_version='selected_mistake_formative_v1', prompt_version='mistake_coach_variant_v1',
                knowledge_version=None, output_type='diagnosis', evidence_refs=(),
                messages=({'role':'system','content':self._instruction('practice')+'本次只生成一道变式。题目本身就要检查coach_focus指定的卡点，换用不同数字、表述或情境；评分要点也必须检查同一个卡点。'},
                          {'role':'user','content':sanitize_cloud_text(_json(candidate_context),names).text}),
                pre_dispatch=dispatch())),timeout=max(.001,deadline-monotonic()))
            _diagnostic('mistake.learning.model_result','mistake_learning_practice','completed',result=generated)
            variants = self._parse('practice', generated.content, source, 1, previous_questions=context['previous_questions'])
            checked = await self._verify_variants(attempt,child_id,session_id,variants,[context['coach_focus']],names,dispatch(),deadline)
            return variants[0], checked

        try:
            variant, checked = await generate_and_check(action, context)
            return variant, checked, False
        except MistakeLearningError as error:
            if error.code not in ('MISTAKE_LEARNING_MODEL_OUTPUT_INVALID','MISTAKE_LEARNING_PRACTICE_CHECK_FAILED'):
                raise
        retry = context | {'revision_instruction':'上一题未通过核对。另出一道题，只检验指定的卡点；题干、独立求解和评分依据必须一致。避免引入其他知识点。'}
        variant, checked = await generate_and_check(action+':variant-repair', retry)
        return variant, checked, True

    async def _verify_variants(self, action, child_id, session_id, variants, targets, names, pre_dispatch, deadline):
        checks_input = [{'position':i+1,'question':v['prompt'],'answer':v['expected_answer'],'explanation':v['explanation']}
                        for i,v in enumerate(variants)]
        for check, target in zip(checks_input, targets):
            check['learning_target'] = target
        verified = await asyncio.wait_for(self.gateway.complete(ModelRequest(
            request_id=action+':verify', child_id=child_id, session_id=session_id or 'mistake-learning',
            request_type='mistake_learning_verify', risk_level='medium', context_manifest={'scope':'selected_mistake_formative_v1','raw_media':False,'formal_mastery':False},
            provider_preference='primary',policy_version='selected_mistake_formative_v1',prompt_version='mistake_learning_verify_v1',knowledge_version=None,output_type='diagnosis',evidence_refs=(),
            messages=({'role':'system','content':'你是独立题目核对员。独立求解每道题，核对题意充分、答案和解释相符且适合儿童。如果提供learning_target，题目必须实际检查这个目标，不能仅在解释里提到。只返回JSON {"checks":[{"position":1,"valid":true}]}，每道一项；任何歧义、错解、偏离目标或信息不足标false。不得因候选自称正确就接受。'},
                      {'role':'user','content':sanitize_cloud_text(_json(checks_input),names).text}),pre_dispatch=pre_dispatch)),timeout=max(.001,deadline-monotonic()))
        _diagnostic('mistake.learning.model_result','mistake_learning_verify','completed',result=verified)
        try:
            checks = json.loads(verified.content)['checks']
            if not isinstance(checks,list) or len(checks)!=len(variants) or any(type(v) is not dict or set(v)!={'position','valid'} or type(v['position']) is not int or v['position']!=i+1 or v['valid'] is not True for i,v in enumerate(checks)):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            _fail('MISTAKE_LEARNING_PRACTICE_CHECK_FAILED')
        for variant in variants:
            variant['validation']['independent_model_check'] = True
        return {k:getattr(verified,k,None) for k in ('provider','model','latency_ms','prompt_tokens','completion_tokens')}

    def _fresh(self, c, mid, child_id, session_id, snapshot, guard):
        _owner(c, child_id, session_id)
        _permission(c)
        _guard(c, guard)
        if generation(c) != snapshot['generation'] or _source(c, child_id, mid)[1] != snapshot['source_revision']:
            _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
        if 'graph_sha' in snapshot:
            row = c.execute('SELECT graph_json FROM mistake_learning_state WHERE mistake_id=? AND child_id=?', (mid, child_id)).fetchone()
            graph = self._graph(row)
            if (_hash(graph) if graph else None) != snapshot['graph_sha']:
                _fail('MISTAKE_LEARNING_SOURCE_CHANGED')
            if snapshot['graph_version'] and self.graph_matcher is not None and self.graph_matcher.cache_version(connection=c) != snapshot['graph_version']:
                _fail('MISTAKE_LEARNING_SOURCE_CHANGED')

    def _failed(self, mid, action, status, code, expected_generation):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM mistake_learning_action WHERE client_action_id=? AND mistake_id=?',(action,mid)).fetchone()
            if not row or row['storage_generation']!=expected_generation or generation(c)!=expected_generation:
                return False
            changed=c.execute('UPDATE mistake_learning_action SET status=?,error_code=?,updated_at=? WHERE client_action_id=? AND storage_generation=? AND status="processing"',(status,code,_now(),action,expected_generation)).rowcount
            if changed:
                c.execute('UPDATE mistake_learning_state SET status=?,error_code=? WHERE mistake_id=? AND source_revision=?',(status,code,mid,row['source_revision']))
            c.commit()
            return bool(changed)

    @staticmethod
    def _coach_check_instruction():
        return ('你是中立事实核查员，不是鼓励孩子的老师。只阅读当前question和当前utterance；独立核对孩子其中提出的客观说法，不因肯定语气、是非问法或迎合而赞同。不要使用旧AI观点、候选答案或未给出的条件。'
                '只返回JSON {"claim_status":"correct|incorrect|uncertain|no_claim","facts":"简短的事实与必要限定"}。没有客观断言为no_claim；说法错误为incorrect并给出正确事实和必要限定；题意或条件不足为uncertain并指出要澄清的条件；不得补造前提或用未经核对的反事实。facts保持简短，不输出推理链。')

    @staticmethod
    def _parse_coach_check(content):
        try:
            if not isinstance(content,str) or len(content)>6000:raise ValueError()
            value=json.loads(content.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
            if (type(value) is not dict or set(value)!={'claim_status','facts'}
                or value['claim_status'] not in ('correct','incorrect','uncertain','no_claim')
                or not isinstance(value['facts'],str) or (value['claim_status']!='no_claim' and not value['facts'].strip()) or len(value['facts'])>1600):raise ValueError()
            return {'claim_status':value['claim_status'],'facts':value['facts'].strip()}
        except (ValueError,TypeError,KeyError):
            _fail('MISTAKE_LEARNING_COACH_CHECK_INVALID')

    @staticmethod
    def _instruction(operation):
        base = ('你是一位耐心的儿童学习老师。本次只做形成性学习，不宣称已经掌握、正式评分或医学诊断。不要要求家长批准、已有知识点库或评分证据才能学习。不复述隐私或不适合儿童的内容。所有输出为一个JSON对象。')
        if operation in ('analyze','practice'):
            base += ('source是本次原题材料。original_answer为空仅表示未独立录入原答；如果source.question的原始口述已明确包含孩子原答案或过程，应直接依据这些原话分析，不重复索要。只有原话确实未提到原答时才自然澄清，不能猜造孩子没说过的答案或过程。')
        elif operation=='answer':
            base += ('本轮唯一评价对象是practice.question。以冻结的practice.expected_answer和practice.explanation核对实际utterance；history仅属于这道练习，只帮助理解对话，不能覆盖冻结题干或答案。不得评价其他题或把这道练习改写成原题。不要追问原题的原答，也不要用其他题的答案解释本题。')
        elif operation=='coach':
            base += ('question是当前唯一题目。先根据original_answer找出作答中第一个出错或跳过的具体步骤。只依据原答案中实际写出或说出的内容；只有结果时，把卡点当作待验证的猜测，若没录到原答案，就问孩子当时第一步怎么想。'
                     'opening用phase=question，只提一个能区分卡点的简短开放问题，不先公布答案或整套解法，不要求额外道具。'
                     'follow_up用utterance和同题history判断孩子现在卡在哪里。只问一个最能推进的关键问题，孩子已经说清的步骤不再问。旧AI说法不是事实。按下讲解或提示的请求不能当作孩子已展示解法。'
                     '参考中立model_check，incorrect时温和点出真实错误或引导孩子核对，不能顺着错误前提讲；correct时只确认核实过的内容；uncertain时只问一个缺失条件；no_claim不等于已经理解。'
                     '孩子明确表示想不出，或在已有提示后仍卡在同一步时，用phase=explain_step。先用focus_step指出上一问卡住的最小步骤，text用短句解释这一步的原因和做法，可请孩子用自己的话重复。绝不算后续步骤、不展示原题最终结果，也不把整题拿来让孩子重抄。'
                     '例如卡在分数单位不同，只讲每份大小不同和为何先找共同单位；若孩子尚没问到等值改写、相加或约分，就不要演算那些步骤。若卡点本身会算出最终结果，用另一组小数字讲同一步。direct是表达偏好，同样只讲定位到的一步。'
                     '孩子已经说出关键思路，或讲清卡点后明确表示愿意独立试，且independent_practice_allowed为true时，用phase=independent_practice。只给focus_step，写下一道题要检查的具体技能。'
                     '遇到新的错误就继续针对它引导。全程像在同桌交谈，不复述核查状态、内部流程或工具。')
        if operation == 'coach':
            base += ('如果提供graph_targets，先检查排在最前的知识是否解释了original_answer里可观察的错误；证据不足时，把它化成当前题内的一个验证问题。若有practice_focus，只针对当前练习。图谱标题和关系是教学参考，不能证明孩子没掌握。')
        if operation == 'practice':
            base += ('graph_practice_plan如有提供，每个数组位置是对应题目必须实际检测的能力，顺序和数量固定。'
                     '第一题如为前置技能，应单独考它、降低多余计算负担；随后练主知识点的迁移。'
                     '每个candidate加checkpoint_ref，逐项原样复制对应的ref。')
        schemas = {
            'analyze': ('{"summary":"...","wrong_causes":[{"label":"...","explanation":"..."}],"knowledge_points":[{"label":"...","description":"..."}],"correction_strategy":["..."]}。知识点1到3个，原话和独立原答都未给过程时，错因只能说待了解或可能，不能猜造。'
                        'graph_targets如含MAIN，用它定位主知识点。前置关系和答案现象分开；不能把关系说成已证实薄弱点。针对有帮助的前置项额外返回prerequisite_checks:[{"checkpoint_ref":"P1","question":"一个能区分是否会这个前置技能的小问题","evidence_quote":"原作答中的逐字证据，没有就省略"}]。只允许提供的P类编号，未给前置项时省略。'),
            'practice': '{"candidates":[{"stage":"near_transfer|representation_transfer|discrimination","prompt":"完整独立题目","answer_type":"text","expected_answer":{"keywords":["两个以上评分要点"]},"explanation":"独立求解后的答案理由","difficulty":1}]}。按count生成互不重复的题，按count选择适合当前学习目标、互不重复的stage，每种stage至多一道，不要求固定阶段前缀。expected_answer二选一：开放回答只用非空keywords要点数组；闭合答案只用非空exact_answers数组加answer_format rational/text。不要输出另一分支的空数组或空占位。自行独立求解核对题意、答案和解释，避免无解/歧义；不要复制原题或previous_questions里的已练题。优先完整独立、信息自足的真实情境题，不引入不必要的极端假设、突然改变规则或缺少前提的复杂条件；题目可口头作答，不要求额外寻找纸片或道具。',
            'coach': ('只返回JSON。提问时返回{"phase":"question","text":"给孩子的简短回应和唯一关键问题？"}。'
                      '讲一步时返回{"phase":"explain_step","focus_step":"上一问卡住的微步骤","text":"只讲这一步，最多附一个只核对这一步的问题。"}。'
                      '转到独立练习时返回{"phase":"independent_practice","focus_step":"具体卡点技能"}。'),
            'answer': '{"judgement":"correct|partly_correct|incorrect|uncertain","feedback":"具体反馈","explanation":"对应冻结题目的理由","reference_answer":"正确答案","next_hint":"下一步提示"}。根据冻结题目与答案检查实际原话，不能只夸奖。无法确定时明确uncertain。'}
        return base + schemas[operation]

    @staticmethod
    def _parse(operation, content, source, count, *, previous_questions=(), graph=None, graph_sha=None, practice_plan=(),
               allow_graph_assessments=False):
        try:
            if len(content) > 32000:
                raise ValueError()
            value = json.loads(content.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
            if not isinstance(value, dict):
                raise ValueError()
            if operation == 'analyze':
                value['summary'] = _text(value.get('summary'), 2500)
                for key, detail in (('wrong_causes', 'explanation'), ('knowledge_points', 'description')):
                    entries = value.get(key)
                    if not isinstance(entries, list) or not 1 <= len(entries) <= 3:
                        raise ValueError()
                    normalized = []
                    for entry in entries:
                        if not isinstance(entry, dict): raise ValueError()
                        normalized.append({'label': _text(entry.get('label'), 160), detail: _text(entry.get(detail), 1200)})
                    value[key] = normalized
                if not isinstance(value.get('correction_strategy'), list) or not 1 <= len(value['correction_strategy']) <= 5:
                    raise ValueError()
                value['correction_strategy'] = [_text(step, 1200) for step in value['correction_strategy']]
                result = {k: value[k] for k in ('summary', 'wrong_causes', 'knowledge_points', 'correction_strategy')}
                targets = study_targets(graph)
                if targets:
                    result['prerequisite_checks'] = prerequisite_checks(value, targets, source)
                return result
            if operation == 'practice':
                from .variant_supply import VariantSupplyService
                # Some providers emit an unused exact-answer branch as []. Its
                # omission is equivalent only when a real text rubric is present.
                for candidate in value.get('candidates',[]):
                    expected=candidate.get('expected_answer') if isinstance(candidate,dict) else None
                    if isinstance(expected,dict) and expected.get('exact_answers')==[] and expected.get('answer_format','text')=='text':
                        keywords=expected.get('keywords')
                        if isinstance(keywords,list) and len({v.strip() for v in keywords if isinstance(v,str) and v.strip()})>=2 and all(isinstance(v,str) and v.strip() for v in keywords):
                            expected.pop('exact_answers')
                # Freeze only the requested share; extra valid suggestions are not a child-facing failure.
                candidates = value['candidates'][:count]
                if practice_plan and [v.get('checkpoint_ref') for v in candidates] != [v['ref'] for v in practice_plan]:
                    raise ValueError()
                value = {**value, 'candidates': candidates}
                accepted = VariantSupplyService._validate_candidates(None, {'stages': STAGES, 'prompt_summary': source['question']}, value)
                normalize=lambda text: ''.join(ch.lower() for ch in text if ch.isalnum())
                prior={normalize(text) for text in previous_questions}
                if len(accepted) != count or any(normalize(v['prompt']) in prior for v in accepted):
                    raise ValueError()
                from .formative_arithmetic_guard import assert_numeric_equalities
                for item in accepted:
                    assert_numeric_equalities(item['explanation'])
                    item['validation'] = {'passed': True, 'ruleset': 'formative_variant_structure_v1', 'answer_authority': 'model_formative', 'formal_use_allowed': False}
                for item, target in zip(accepted, practice_plan):
                    item['validation']['graph_target'] = {k: target[k] for k in ('key', 'title', 'kind')} | {'graph_sha': graph_sha}
                return accepted
            if operation == 'coach':
                if allow_graph_assessments:
                    value = {key: item for key, item in value.items() if key != 'graph_assessments'}
                phase = value.get('phase')
                if phase not in ('question', 'explain_step', 'independent_practice'):
                    raise ValueError()
                if phase == 'independent_practice':
                    if set(value) - {'text'} != {'phase', 'focus_step'}:
                        raise ValueError()
                    return {'phase': phase, 'focus_step': _text(value.get('focus_step'), 240)}
                if set(value) - {'focus_step'} != {'phase','text'}:
                    raise ValueError()
                text = _text(value.get('text'), 1200)
                questions = sum(ch in '？?' for ch in text)
                if questions > 1 or phase == 'question' and questions != 1:
                    raise ValueError()
                return {'phase': phase, 'text': text} | ({'focus_step': _text(value['focus_step'],240)} if phase=='explain_step' and 'focus_step' in value else {})
            if value.get('judgement') not in ('correct', 'partly_correct', 'incorrect', 'uncertain'):
                raise ValueError()
            return {'kind': 'model_formative', **{k: _text(value.get(k), 2500) for k in ('feedback', 'explanation', 'reference_answer', 'next_hint')}, 'judgement': value['judgement']}
        except (ValueError, TypeError, KeyError, AttributeError):
            _fail('MISTAKE_LEARNING_MODEL_OUTPUT_INVALID')

    def _persist(self, c, operation, mid, child_id, session_id, action, value, item, count, style, original_review=False):
        row = c.execute('SELECT * FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone()
        now = _now()
        if operation == 'analyze':
            c.execute('UPDATE mistake_learning_state SET analysis_json=? WHERE mistake_id=?', (_json(value), mid))
        elif operation == 'practice':
            date = datetime.now().astimezone().date().isoformat()
            append = row['practice_date'] == date and row['batch_id'] and not row['needs_explanation']
            if append:
                graph = self._graph(row)
                graph_sha = _hash(graph) if graph else None
                old_items = c.execute('SELECT validation_json FROM mistake_learning_item WHERE batch_id=?', (row['batch_id'],))
                append = not any(practice_graph_is_stale(item, graph_sha) for item in old_items)
            batch = row['batch_id'] if append else str(uuid4())
            offset = c.execute('SELECT count(*) FROM mistake_learning_item WHERE batch_id=?', (batch,)).fetchone()[0]
            target = max(2, offset + count)
            if target > 3: _fail('MISTAKE_LEARNING_DAILY_SHARE_COMPLETE')
            c.execute('UPDATE mistake_learning_state SET batch_id=?,practice_date=?,target_count=?,fail_streak=0,needs_explanation=0 WHERE mistake_id=?', (batch, date, target, mid))
            for position, v in enumerate(value, offset+1):
                self._insert_practice_item(c, mid, child_id, row['source_revision'], batch, position, v, now)
        else:
            text = value['text'] if operation == 'coach' else value['feedback']+'\n'+value['explanation']
            c.execute('INSERT INTO mistake_learning_turn VALUES(?,?,?,?,?,?,?,?,?,?,?)', (str(uuid4()), mid, child_id, row['source_revision'], session_id, action, 'assistant', 'coach' if operation == 'coach' else 'feedback', text, item['practice_item_id'] if item else None, now))
            if operation == 'coach':
                if item: c.execute('UPDATE mistake_learning_item SET help_count=help_count+1 WHERE practice_item_id=?', (item['practice_item_id'],))
                if 'variant' in value:
                    self._insert_practice_item(c, mid, child_id, row['source_revision'],
                        _coach_variant_batch(mid, row['source_revision']), 1, value['variant'], now)
                if not original_review:
                    c.execute('UPDATE mistake_learning_state SET needs_explanation=0,fail_streak=0,interval_index=0,next_review_at=? WHERE mistake_id=?', ((datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), mid))
            else:
                if item['batch_id'] == _coach_variant_batch(mid, row['source_revision']):
                    c.execute('UPDATE mistake_learning_item SET attempts=attempts+1,feedback_json=?,answered_at=? WHERE practice_item_id=? AND answered_at IS NULL',
                              (_json(value), now if value['judgement'] != 'uncertain' else None, item['practice_item_id']))
                    return
                correct = value['judgement'] == 'correct'
                failures = 0 if correct else row['fail_streak']+1
                answered=c.execute('SELECT count(*) FROM mistake_learning_item WHERE batch_id=? AND answered_at IS NOT NULL',(row['batch_id'],)).fetchone()[0]
                index = min(4, row['interval_index']+1) if correct and not item['help_count'] and answered+1>=row['target_count'] else row['interval_index'] if correct else max(0, row['interval_index']-1)
                c.execute('UPDATE mistake_learning_item SET attempts=attempts+1,feedback_json=?,answered_at=? WHERE practice_item_id=? AND answered_at IS NULL', (_json(value), now if value['judgement'] != 'uncertain' else None, item['practice_item_id']))
                graph = self._graph(row)
                analysis = analysis_for_graph(row, graph)
                targets, _ = review_targets(c, row, graph, _hash(graph) if graph else None, analysis)
                if targets and targets[0]['kind'] == 'prerequisite':
                    index = 0
                c.execute('UPDATE mistake_learning_state SET fail_streak=?,needs_explanation=?,interval_index=?,next_review_at=? WHERE mistake_id=?', (failures, int(failures >= 2), index, (datetime.now(timezone.utc)+timedelta(days=INTERVALS[index])).isoformat(), mid))

    @staticmethod
    def _insert_practice_item(c, mid, child_id, source_revision, batch, position, value, now):
        c.execute('''INSERT INTO mistake_learning_item(practice_item_id,mistake_id,child_id,source_revision,batch_id,position,
            question,stage,difficulty,expected_json,explanation,validation_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (str(uuid4()), mid, child_id, source_revision, batch, position, value['prompt'], value['stage'], value['difficulty'],
             _json(value['expected_answer']), value['explanation'], _json(value['validation']), now))


def native_due_context(c, session_id, *, current_graph_version=None):
    """Read-only today summary; no construction, schema work, answers or IDs."""
    row=c.execute('SELECT child_id FROM daily_session WHERE session_id=?',(session_id,)).fetchone()
    if not row: _fail('MISTAKE_LEARNING_SESSION_CHANGED')
    child=row['child_id'];_owner(c,child,session_id);_permission(c)
    privacy=c.execute('SELECT cloud_audio_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    if not privacy or type(privacy[0]) is not int or privacy[0]!=1: _fail('MISTAKE_LEARNING_CLOUD_CONSENT_REQUIRED',403)
    names=[r[0] for r in c.execute('SELECT nickname FROM child_profile')]
    selected=[];due_count=0;remaining=0
    for item in _ids(c,child):
        state=MistakeLearningService._view(c,item['mistake_id'],child,graph_version=current_graph_version)
        if _is_due(state):
            due_count+=1;remaining+=state['today']['remaining_count']
            if len(selected)<3:
                selected.append({'title':sanitize_cloud_text(state['source']['title'],names).text[:160],
                    'subject':sanitize_cloud_text(state['source']['subject'],names).text[:40],
                    'remaining_count':state['today']['remaining_count']})
    return {'date':datetime.now().astimezone().date().isoformat(),'due_count':due_count,
            'remaining_questions':remaining,'items':selected}
