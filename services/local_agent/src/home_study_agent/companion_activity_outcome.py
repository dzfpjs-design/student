"""Local outcome journal, separate from entertainment process/time limits.

This service never launches/stops media, updates plans, or writes habit evidence.
Runtime owns those actions and supplies a correlated durable resume receipt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
import hashlib
import json
import sqlite3
from uuid import uuid4

from .database import Database


class UnknownActivity(KeyError):
    pass


class ActivityConflict(ValueError):
    pass


class _HistoryUnavailable(ActivityConflict):
    pass


class ActivityInputError(ValueError):
    pass


SOURCES = {
    'companion': ('companion_activity', 'activity_id'),
    'controlled_game': ('controlled_game_session', 'game_session_id'),
    'controlled_content': ('controlled_content_session', 'content_session_id'),
}
EXECUTIONS = {'completed', 'partial', 'not_executed', 'unknown', 'refused', 'stopped'}
EXPERIENCES = {'comfortable', 'neutral', 'fatigued', 'uncomfortable', 'unknown'}
ACTORS = {'child', 'parent', 'runtime'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _history_object(raw):
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _history_snapshot(row):
    snapshot = _history_object(row['source_snapshot_json'])
    if (snapshot is None or snapshot.get('child_id') != row['child_id']
            or snapshot.get('session_id') != row['session_id']
            or 'paused_plan_item_id' not in snapshot
            or (snapshot['paused_plan_item_id'] is not None
                and not isinstance(snapshot['paused_plan_item_id'], str))
            or any(snapshot.get(k) is not None and not isinstance(snapshot[k], dict)
                   for k in ('original_identity', 'return_task_source', 'request_provenance_v1'))
            or not isinstance(snapshot.get('started_at'), str)):
        return None
    try:
        _time(snapshot['started_at'])
    except ValueError:
        return None
    return snapshot


def _text(value, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ActivityInputError('ACTIVITY_TEXT_INVALID')
    return value


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class CompanionActivityOutcomeService:
    def __init__(self, database: Database):
        self.database = database
        self.ensure_schema()

    def ensure_schema(self):
        # DELETE is intentionally allowed for PrivacyLifecycle; business changes
        # are append-only. Source references are polymorphic and checked on read.
        with self.database.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS companion_activity_outcome (
                    outcome_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    success_description TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(child_id, source_kind, source_id)
                );
                CREATE TABLE IF NOT EXISTS companion_activity_outcome_event (
                    event_id TEXT PRIMARY KEY,
                    outcome_id TEXT NOT NULL REFERENCES companion_activity_outcome(outcome_id) ON DELETE CASCADE,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    client_action_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(outcome_id, revision),
                    UNIQUE(child_id, client_action_id)
                );
                CREATE TRIGGER IF NOT EXISTS companion_activity_outcome_event_no_update
                BEFORE UPDATE ON companion_activity_outcome_event
                BEGIN SELECT RAISE(ABORT, 'activity events are append-only'); END;
            ''')
            c.commit()

    @staticmethod
    def _child(c, child_id):
        if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
            raise UnknownActivity('CHILD_NOT_FOUND')

    @staticmethod
    def _source(c, child_id, source_kind, source_id):
        if source_kind not in SOURCES:
            raise ActivityInputError('ACTIVITY_SOURCE_KIND_INVALID')
        table, key = SOURCES[source_kind]
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            raise UnknownActivity('ACTIVITY_SOURCE_NOT_FOUND')
        row = c.execute(f'SELECT * FROM {table} WHERE {key}=? AND child_id=?', (source_id, child_id)).fetchone()
        if row is None:
            raise UnknownActivity('ACTIVITY_SOURCE_NOT_FOUND')
        source = dict(row)
        if not c.execute('SELECT 1 FROM daily_session WHERE session_id=? AND child_id=?',
                         (source['session_id'], child_id)).fetchone():
            raise UnknownActivity('ACTIVITY_SESSION_NOT_FOUND')
        # No executable path, local media path, or family asset is exposed.
        snapshot = {key: source.get(key) for key in ('child_id', 'session_id', 'started_at',
                    'paused_plan_item_id', 'activity_type', 'topic', 'title', 'approval_id',
                    'curriculum_row_id', 'client_action_id', 'duration_minutes', 'allowed_minutes')}
        original = c.execute('''SELECT pi.plan_item_id,pi.title,pi.subject,pi.source_type,pi.source_ref,pi.task_type
            FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?''',
            (source.get('paused_plan_item_id'), child_id, source['session_id'])).fetchone()
        snapshot['original_identity'] = dict(original) if original else None
        fingerprint = hashlib.sha256(_json(snapshot).encode()).hexdigest()
        return source, snapshot, fingerprint

    @staticmethod
    def _owned(c, child_id, outcome_id):
        row = c.execute('SELECT * FROM companion_activity_outcome WHERE outcome_id=? AND child_id=?',
                        (outcome_id, child_id)).fetchone()
        if row is None:
            raise UnknownActivity('ACTIVITY_NOT_FOUND')
        return dict(row)

    @staticmethod
    def _events(c, outcome_id):
        rows = c.execute(
            'SELECT event_id,outcome_id,revision,kind,actor,payload_json,created_at FROM companion_activity_outcome_event '
            'WHERE outcome_id=? ORDER BY revision', (outcome_id,)).fetchall()
        events = []
        for row in rows:
            payload = _history_object(row['payload_json'])
            strings = {'feedback':('execution','experience','evidence_basis'),
                       'resume_confirmed':('request_event_id','plan_item_event_id')}.get(row['kind'], ())
            if (payload is None
                    or any(not isinstance(payload.get(k), str) or not payload[k] for k in strings)
                    or (payload.get('agent_turn_id') is not None
                        and not isinstance(payload['agent_turn_id'], str))
                    or (row['kind'] == 'retracted' and
                        (type(payload.get('target_revision')) is not int or payload['target_revision'] < 1))
                    or (row['kind'] == 'feedback' and
                        (payload['execution'] not in EXECUTIONS or payload['experience'] not in EXPERIENCES
                         or payload['evidence_basis'] not in {'child_report','parent_observed','parent_report'}))):
                raise _HistoryUnavailable('ACTIVITY_HISTORY_UNAVAILABLE')
            events.append(dict(row, payload=payload))
        return events

    @staticmethod
    def _public_event(event):
        return {key: value for key, value in event.items() if key != 'payload_json'}

    def _append(self, c, row, revision, kind, actor, payload, request, key):
        event = dict(event_id=str(uuid4()), outcome_id=row['outcome_id'], revision=revision,
                     kind=kind, actor=actor, payload=payload, created_at=datetime.now(timezone.utc).isoformat())
        c.execute('INSERT INTO companion_activity_outcome_event VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (event['event_id'], row['outcome_id'], row['child_id'], revision, kind, actor,
                   _json(payload), _json(request), key, event['created_at']))
        return event

    def _replay(self, c, child_id, key, request):
        row = c.execute('SELECT * FROM companion_activity_outcome_event WHERE child_id=? AND client_action_id=?',
                        (child_id, key)).fetchone()
        if row is None:
            return None
        if row['request_json'] != _json(request):
            raise ActivityConflict('ACTIVITY_IDEMPOTENCY_CONFLICT')
        event = next(e for e in self._events(c, row['outcome_id']) if e['event_id'] == row['event_id'])
        return {'event': self._public_event(event), **self._detail(c, child_id, row['outcome_id'])}

    def open(self, child_id, *, source_kind, source_id, intent, success_description, client_action_id,
             actor='runtime', connection=None):
        for value in (source_id, intent, success_description):
            _text(value)
        _text(client_action_id, 200)
        if actor not in ACTORS:
            raise ActivityInputError('ACTIVITY_ACTOR_INVALID')
        request = dict(kind='open', source_kind=source_kind, source_id=source_id,
                       intent=intent, success_description=success_description, actor=actor)
        if connection is not None and (not isinstance(connection, sqlite3.Connection) or not connection.in_transaction):
            raise ActivityInputError('ACTIVITY_TRANSACTION_REQUIRED')
        with nullcontext(connection) if connection is not None else self.database.connect() as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            self._child(c, child_id)
            replay = self._replay(c, child_id, client_action_id, request)
            if replay is not None:
                return replay
            source, snapshot, fingerprint = self._source(c, child_id, source_kind, source_id)
            if c.execute('SELECT 1 FROM companion_activity_outcome WHERE child_id=? AND source_kind=? AND source_id=?',
                         (child_id, source_kind, source_id)).fetchone():
                raise ActivityConflict('ACTIVITY_ALREADY_REGISTERED')
            if source.get('paused_plan_item_id') and self._original(c, snapshot) is None:
                raise ActivityConflict('ACTIVITY_ORIGINAL_SCOPE_INVALID')
            # Supplemental return qualification only. Keep the existing lifecycle
            # fingerprint stable; never backfill this source snapshot on old rows.
            snapshot['return_task_source'] = self._homework_source(c, snapshot)
            try:
                snapshot['request_provenance_v1'] = (self._request_binding(c, source, snapshot, live=True)
                    if source_kind == 'companion' else None)
            except (KeyError, TypeError, json.JSONDecodeError):
                raise ActivityConflict('ACTIVITY_REQUEST_BINDING_INVALID') from None
            row = dict(outcome_id=str(uuid4()), child_id=child_id)
            c.execute('INSERT INTO companion_activity_outcome VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (row['outcome_id'], child_id, source['session_id'], source_kind, source_id,
                       _json(snapshot), fingerprint, intent, success_description, datetime.now(timezone.utc).isoformat()))
            event = self._append(c, row, 1, 'opened', actor, request, request, client_action_id)
            result = {'event': event, **self._detail(c, child_id, row['outcome_id'])}
            if connection is None:
                c.commit()
            return result

    @staticmethod
    def _request_binding(c, source, snapshot, *, live=False, require_arrival=False, task_status='paused'):
        """Original session request provenance, never offer causality or autonomy."""
        from .agent_runtime import AgentRuntimeStore
        from .agent_continuation import ContinuationStore
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='agent_step'").fetchone():
            return None
        rows = c.execute('''SELECT t.*,p.plan_id,p.child_id AS plan_child,p.session_id AS plan_session,
            p.status AS plan_status,p.objective_id,p.plan_json,p.context_manifest_json,
            s.step_id,s.capability_name,s.capability_version,s.arguments_json,
            s.status AS step_status,s.error_code AS step_error
            FROM agent_step s JOIN agent_plan p USING(plan_id)
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id WHERE s.idempotency_key=?''',
            (source['client_action_id'],)).fetchall()
        if not rows:
            if require_arrival:
                raise ActivityConflict('ACTIVITY_REQUEST_ARRIVAL_UNBOUND')
            return None
        if len(rows) != 1:
            raise ActivityConflict('ACTIVITY_REQUEST_BINDING_INVALID')
        turn = rows[0]
        if (turn['child_id'] != source['child_id'] or turn['session_id'] != source['session_id']
                or turn['plan_child'] != source['child_id'] or turn['plan_session'] != source['session_id']
                or turn['capability_name'] != 'companion.start_activity'
                or turn['plan_status'] == 'cancelled' or turn['step_status'] == 'cancelled'
                or turn['tool_status'] == 'cancelled'
                or turn['tool_error_code'] == 'SOURCE_DELETED' or turn['step_error'] == 'SOURCE_DELETED'):
            raise ActivityConflict('ACTIVITY_REQUEST_BINDING_INVALID')
        storage_retry = (turn['tool_status'] == 'failed'
            and turn['tool_error_code'] in {'OperationalError', 'IntegrityError'}
            and not c.execute('''SELECT 1 FROM agent_turn WHERE child_id=? AND session_id=?
                AND rowid>(SELECT rowid FROM agent_turn WHERE agent_turn_id=?)
                AND intent!='runtime_continuation' ''',
                (source['child_id'], source['session_id'], turn['agent_turn_id'])).fetchone())
        if live and (turn['plan_status'] != 'running' or turn['step_status'] != 'running'
                or not (turn['tool_status'] == 'pending' or storage_retry)
                or not ContinuationStore.execution_allowed(c, turn['agent_turn_id'], running=True)):
            raise ActivityConflict('ACTIVITY_REQUEST_NOT_EXECUTABLE')
        action = json.loads(turn['action_json'])
        args = json.loads(turn['arguments_json'])
        semantic = json.loads(turn['context_manifest_json']).get('voice_control_binding') or {}
        if live and require_arrival and (action.get('source') == 'content_free_voice_control'
                or semantic.get('control') == 'companion.start_activity'):
            # This is the original writer transaction, before the atomic pause.
            # The post-pause open call must not require the task to be active again.
            from .voice_control_intent import VoiceControlIntent
            checked = VoiceControlIntent.inspect_execution(c, turn['agent_turn_id'])
            if not checked or checked['control'] != 'companion.start_activity':
                raise ActivityConflict('ACTIVITY_REQUEST_SEMANTIC_CHANGED')
        saved_step = json.loads(turn['plan_json'])['next_step']
        if (action.get('type') != 'start_companion_activity'
                or saved_step['capability'] != turn['capability_name']
                or saved_step['capability_version'] != turn['capability_version']
                or saved_step['arguments'] != args
                or args.get('activity_type') != source['activity_type']
                or str(args.get('topic') or '孩子现在感兴趣的主题')[:200] != source['topic']):
            raise ActivityConflict('ACTIVITY_REQUEST_ACTION_CHANGED')
        # Continuations and text-only/legacy requests retain their functionality,
        # but cannot be upgraded into an original child voice request.
        if turn['intent'] == 'runtime_continuation':
            if require_arrival:
                raise ActivityConflict('ACTIVITY_REQUEST_ARRIVAL_UNBOUND')
            return None
        capture = voice = None
        if action.get('voice_input_id'):
            capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=source['session_id'],
                child_id=source['child_id'], voice_input_id=action['voice_input_id'], transcript=turn['transcript'])
            voice = AgentRuntimeStore.voice_capture_snapshot(capture)
            if voice != action.get('voice_input_binding'):
                raise ActivityConflict('ACTIVITY_REQUEST_VOICE_CHANGED')
        elif not require_arrival:
            return None
        arrival = c.execute('''SELECT * FROM learning_input_arrival
            WHERE client_action_id=? AND child_id=? AND session_id=? AND utterance=?''',
            (turn['client_action_id'], source['child_id'], source['session_id'], turn['transcript'])).fetchone()
        if not arrival:
            raise ActivityConflict('ACTIVITY_REQUEST_ARRIVAL_CHANGED')
        hash_value = lambda value: hashlib.sha256(_json(value).encode()).hexdigest()
        task = snapshot.get('original_identity')
        captured_tasks = {p['plan_item_id']: p for entry in json.loads(arrival['snapshot_json'])
                          for p in entry.get('plans', []) if p['status'] == 'active'
                          and p['source_type'] != 'planned_break'}
        if captured_tasks and set(captured_tasks) != {source.get('paused_plan_item_id')}:
            raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
        task_binding = None
        if task:
            current = CompanionActivityOutcomeService._original(c, snapshot, execution_identity=True)
            if current is None or (live and current['status'] != task_status):
                raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
            frozen_task = captured_tasks.get(task['plan_item_id'])
            if frozen_task is None:
                # Legacy arrivals may lack the exact goal task. Never backfill them.
                if require_arrival:
                    raise ActivityConflict('ACTIVITY_REQUEST_TASK_UNBOUND')
                return None
            identity_fields = ('title', 'subject', 'source_type', 'source_ref', 'task_type')
            if (any(k not in frozen_task for k in (*identity_fields, 'plan_id', 'daily_plan_version', 'started_at'))
                    or any(frozen_task[k] != task[k] or current[k] != frozen_task[k] for k in identity_fields)
                    or current['plan_id'] != frozen_task['plan_id']
                    or current['daily_plan_version'] != frozen_task['daily_plan_version']
                    or current['started_at'] != frozen_task['started_at']):
                raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
            if live:
                if (current['daily_plan_status'] != 'active' or current['session_status'] != 'active'
                        or not frozen_task.get('started_at') or not frozen_task.get('active_segment_started_at')):
                    raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
                if task_status == 'active':
                    if current['active_segment_started_at'] != frozen_task['active_segment_started_at']:
                        raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
                else:
                    # Opening follows the atomic pause. A persisted shutdown
                    # receipt, not missing events, authorizes restart recovery.
                    paused = c.execute('''SELECT * FROM plan_item_event WHERE plan_item_id=?
                        AND event_type='paused' AND client_action_id=?''',
                        (task['plan_item_id'], source['client_action_id'] + ':pause')).fetchone()
                    own_pause = (paused and json.loads(paused['payload_json']).get('reason') == 'companion_activity'
                        and _time(paused['occurred_at']) >= _time(arrival['created_at']))
                    shutdown = None if own_pause else CompanionActivityOutcomeService._shutdown_paused_original(
                        c, source['child_id'], source['session_id'], source['client_action_id'])
                    if (current['active_segment_started_at'] is not None
                            or current['active_seconds'] < frozen_task['active_seconds']
                            or not (own_pause or (shutdown and shutdown['plan_item_id'] == task['plan_item_id']))):
                        raise ActivityConflict('ACTIVITY_REQUEST_PAUSE_RECEIPT_INVALID')
            task_binding = dict(plan_item_id=task['plan_item_id'],
                identity_version=hash_value(task), arrival_version=hash_value(frozen_task),
                daily_plan_id=frozen_task['plan_id'], daily_plan_version=frozen_task['daily_plan_version'],
                source=CompanionActivityOutcomeService._homework_source(c, snapshot))
            if task['source_type'] == 'school_homework':
                entries = [entry for entry in json.loads(arrival['snapshot_json'])
                           if entry.get('kind') == 'homework' and entry.get('target_id') == task['source_ref']]
                if len(entries) != 1 or not task_binding['source']:
                    raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
                original = {k: v for k, v in entries[0]['record'].items() if k != 'status'}
                original.update(plan_item_id=task['plan_item_id'], plan_version=frozen_task['daily_plan_version'])
                if hash_value(original) != task_binding['source']['source_version']:
                    raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
        objective = None
        if turn['objective_id']:
            row = c.execute('SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
                (turn['objective_id'], source['child_id'], source['session_id'])).fetchone()
            if row is None or row['status'] == 'cancelled':
                raise ActivityConflict('ACTIVITY_REQUEST_GOAL_CHANGED')
            objective = dict(objective_id=row['objective_id'], objective_type=row['objective_type'],
                criteria_version=hash_value(json.loads(row['success_criteria_json'])))
        return dict(version='activity_request_v1', child_id=source['child_id'], session_id=source['session_id'],
            source_kind='companion', source_id=source['activity_id'],
            source_version=hash_value({k: v for k, v in snapshot.items()
                if k not in {'request_provenance_v1', 'return_task_source'}}),
            agent_turn_id=turn['agent_turn_id'], step_id=turn['step_id'], plan_id=turn['plan_id'],
            capability_version=turn['capability_version'], arguments_version=hash_value(args),
            action_version=hash_value(action), arrival_id=arrival['arrival_id'],
            arrival_version=hash_value(json.loads(arrival['snapshot_json'])), occurred_at=arrival['created_at'],
            transcript_hash=hash_value(turn['transcript']), voice_input_id=capture['voice_input_id'] if capture else None,
            voice_binding_version=hash_value(voice) if voice else None, task_binding=task_binding, command_objective=objective)

    @classmethod
    def inspect_companion_start(cls, c, *, session_id, client_action_id, activity_type, topic,
                                expected_plan_item_id):
        """Run under the same writer lock as pause, activity and outcome writes."""
        if not isinstance(c, sqlite3.Connection) or not c.in_transaction:
            raise ActivityInputError('ACTIVITY_TRANSACTION_REQUIRED')
        session = c.execute("SELECT child_id FROM daily_session WHERE session_id=? AND status='active'", (session_id,)).fetchone()
        if not session:
            raise ActivityConflict('ACTIVITY_REQUEST_SESSION_CHANGED')
        active = c.execute('''SELECT pi.plan_item_id,pi.title,pi.subject,pi.source_type,pi.source_ref,pi.task_type
            FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE dp.child_id=? AND dp.session_id=? AND dp.status='active'
            AND pi.status='active' AND pi.source_type!='planned_break' ''', (session[0], session_id)).fetchall()
        expected = {expected_plan_item_id} if expected_plan_item_id else set()
        if {r['plan_item_id'] for r in active} != expected:
            raise ActivityConflict('ACTIVITY_REQUEST_TASK_CHANGED')
        task_status = 'active'
        original = dict(active[0]) if active else None
        if not active and expected_plan_item_id is None:
            try:
                restored = cls._shutdown_paused_original(c, session[0], session_id, client_action_id)
            except (ValueError, TypeError, KeyError):
                raise ActivityConflict('ACTIVITY_REQUEST_PAUSE_RECEIPT_INVALID') from None
            if restored:
                expected_plan_item_id, task_status = restored['plan_item_id'], 'paused'
                original = {k: restored[k] for k in ('plan_item_id', 'title', 'subject', 'source_type', 'source_ref', 'task_type')}
        source = dict(child_id=session[0], session_id=session_id, client_action_id=client_action_id,
            activity_type=activity_type, topic=topic[:200], paused_plan_item_id=expected_plan_item_id, activity_id=None)
        snapshot = {**source, 'original_identity': original}
        try:
            binding = cls._request_binding(c, source, snapshot, live=True, require_arrival=True, task_status=task_status)
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ActivityConflict('ACTIVITY_REQUEST_BINDING_INVALID') from None
        if binding is None:
            raise ActivityConflict('ACTIVITY_REQUEST_ARRIVAL_UNBOUND')
        return dict(child_id=session[0], session_id=session_id, plan_item_id=expected_plan_item_id,
            request_key=client_action_id, task_binding=binding['task_binding'],
            requires_pause=bool(expected_plan_item_id and task_status == 'active'))

    @staticmethod
    def _shutdown_paused_original(c, child_id, session_id, key):
        """Positive shutdown receipt only; missing historical events prove nothing."""
        prior = c.execute('''SELECT t.*,a.snapshot_json,a.created_at AS arrival_at FROM agent_step s
            JOIN agent_plan p USING(plan_id) JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
            JOIN learning_input_arrival a ON a.client_action_id=t.client_action_id
                AND a.child_id=t.child_id AND a.session_id=t.session_id AND a.utterance=t.transcript
            WHERE s.idempotency_key=? AND t.child_id=? AND t.session_id=?''', (key, child_id, session_id)).fetchone()
        if (not prior or prior['tool_status'] != 'failed' or not prior['completed_at']
                or prior['tool_error_code'] not in {'OperationalError', 'IntegrityError'}):
            return None
        frozen = {p['plan_item_id']: p for entry in json.loads(prior['snapshot_json'])
            for p in entry.get('plans', []) if p['status'] == 'active' and p['source_type'] != 'planned_break'}
        if len(frozen) != 1:
            return None
        task_id, before = next(iter(frozen.items()))
        events = c.execute('''SELECT e.* FROM plan_item_event e JOIN plan_item pi USING(plan_item_id)
            JOIN daily_plan dp USING(plan_id) WHERE dp.child_id=? AND dp.session_id=?
            AND julianday(e.occurred_at)>julianday(?)''', (child_id, session_id, prior['arrival_at'])).fetchall()
        if len(events) != 1:
            return None
        event = events[0]
        payload = json.loads(event['payload_json'])
        run_id = payload.get('runtime_run_id')
        current_run = c.execute('SELECT run_id FROM runtime_state WHERE singleton_id=1').fetchone()
        if (event['plan_item_id'] != task_id or event['event_type'] != 'paused' or not run_id
                or set(payload) != {'reason', 'runtime_run_id', 'source', 'run_started_at', 'active_segment_started_at'}
                or payload['reason'] != 'runtime_shutdown' or payload['source'] != 'system'
                or payload['active_segment_started_at'] != before['active_segment_started_at']
                or event['client_action_id'] != f'runtime-shutdown:{run_id}:{task_id}'
                or not current_run or current_run[0] == run_id
                or not payload['run_started_at']
                or not (_time(payload['run_started_at']) <= _time(prior['arrival_at']) <= _time(event['occurred_at']))
                or _time(event['occurred_at']) < _time(prior['completed_at'])):
            return None
        paused = c.execute('''SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=? AND dp.status='active'
            AND pi.status='paused' AND pi.active_segment_started_at IS NULL''', (task_id, child_id, session_id)).fetchone()
        if (not paused or paused['started_at'] != before['started_at']
                or paused['active_seconds'] < before['active_seconds']):
            return None
        return dict(paused)

    @classmethod
    def _request_provenance(cls, c, snapshot, source, source_valid):
        from .agent_runtime import InvalidAgentPlanError
        binding = snapshot.get('request_provenance_v1')
        proof = dict(version='activity_request_v1', status='unknown', classification='unknown',
            reason='original_request_unbound', initiative='unknown', autonomy='unknown',
            speaker_identity='unverified', actor_basis='child_session_voice_capture',
            offer_binding=None, binding=binding, local_only=True, cloud_transmission_allowed=False)
        if not binding:
            return proof
        proof['reason'] = 'original_request_changed'
        if source_valid:
            try:
                if cls._request_binding(c, source, snapshot) == binding:
                    proof.update(status='verified', classification='session_voice_request',
                        reason='session_voice_request_not_autonomy')
            except (ActivityConflict, InvalidAgentPlanError, ValueError, KeyError, TypeError):
                pass
        return proof

    @staticmethod
    def _original(c, snapshot, *, execution_identity=False):
        row = c.execute('''SELECT pi.plan_item_id,pi.title,pi.status,pi.plan_id,pi.subject,
            pi.source_type,pi.source_ref,pi.task_type,pi.started_at,pi.active_segment_started_at,
            pi.active_seconds,dp.version AS daily_plan_version,dp.status AS daily_plan_status,
            ds.status AS session_status
            FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
            JOIN daily_session ds ON ds.session_id=dp.session_id
            WHERE pi.plan_item_id=? AND dp.child_id=? AND ds.child_id=? AND dp.session_id=?''',
            (snapshot['paused_plan_item_id'], snapshot['child_id'], snapshot['child_id'], snapshot['session_id'])).fetchone()
        if row is None:
            return None
        return dict(row) if execution_identity else {
            key: row[key] for key in ('plan_item_id', 'title', 'status', 'session_status')}

    @staticmethod
    def _resume_receipt(c, snapshot, outcome_id, request, receipt_id):
        receipt = c.execute("SELECT * FROM plan_item_event WHERE event_id=? AND event_type='resumed' AND plan_item_id=?",
                            (receipt_id, snapshot['paused_plan_item_id'])).fetchone()
        if not receipt:
            return False
        try:
            payload = json.loads(receipt['payload_json'])
            correlated = ((payload.get('companion_outcome_id') == outcome_id
                           and payload.get('resume_request_event_id') == request['event_id'])
                          or receipt['client_action_id'] == 'companion-outcome-resume:' + request['event_id'])
            return correlated and _time(receipt['occurred_at']) >= _time(request['created_at'])
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _other_active(c, row):
        for kind, (table, key) in SOURCES.items():
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            if c.execute(f'''SELECT 1 FROM {table} WHERE child_id=? AND session_id=? AND status='active'
                AND NOT (?=? AND {key}=?)''',
                (row['child_id'], row['session_id'], kind, row['source_kind'], row['source_id'])).fetchone():
                return True
        return False

    @classmethod
    def _detail(cls, c, child_id, outcome_id, *, _return_cache=None):
        row = cls._owned(c, child_id, outcome_id)
        snapshot = _history_snapshot(row)
        try:
            events = cls._events(c, outcome_id)
        except _HistoryUnavailable:
            return cls._unavailable_history(c, row, None)
        if snapshot is None or not events:
            return cls._unavailable_history(c, row, events)
        source, valid = {}, False
        try:
            source, _, fingerprint = cls._source(c, child_id, row['source_kind'], row['source_id'])
            valid = fingerprint == row['source_fingerprint']
        except UnknownActivity:
            pass
        retracted = {e['payload']['target_revision'] for e in events if e['kind'] == 'retracted'}
        latest = lambda kind: next((e for e in reversed(events) if e['kind'] == kind), None)
        # A retracted latest report leaves uncertainty; it never revives an old report.
        feedback = latest('feedback')
        feedback = feedback if feedback and feedback['revision'] not in retracted else None
        def provenance_valid(event):
            if not event or event['payload'].get('report_kind') != 'self_report':
                return True
            from .companion_activity_voice import voice_event_valid
            return voice_event_valid(c, event['payload'], child_id, row['session_id'])
        if not provenance_valid(feedback):
            feedback = None
        report = feedback['payload'] if feedback else None
        ended = bool(source) and source.get('status') != 'active'
        end_reason = source.get('end_reason')
        execution_status = 'unknown'
        if report:
            execution_status = report['execution']
            if execution_status == 'completed':
                execution_status = 'observed_completed' if report['evidence_basis'] == 'parent_observed' else 'reported_completed'
        if not valid:
            execution_status = 'source_invalid'
        original = cls._original(c, snapshot)
        resume_status = 'pending' if snapshot['paused_plan_item_id'] else 'no_original_bound'
        request = latest('resume_requested')
        decision = next((e for e in reversed(events) if e['kind'] in {'resume_requested', 'resume_deferred'}), None)
        if decision and decision['revision'] not in retracted:
            resume_status = 'deferred' if decision['kind'] == 'resume_deferred' else 'requested'
        confirmed = latest('resume_confirmed')
        if (resume_status == 'requested' and confirmed and confirmed['revision'] not in retracted
                and confirmed['payload']['request_event_id'] == request['event_id']):
            resume_status = 'confirmed' if cls._resume_receipt(c, snapshot, outcome_id, request,
                confirmed['payload']['plan_item_event_id']) else 'receipt_invalid'
        if (resume_status == 'requested' and request
                and any(e['revision'] > request['revision'] and e['kind'] in {'feedback','retracted'} for e in events)):
            resume_status = 'needs_reconfirmation'
        if decision and not provenance_valid(decision):
            resume_status = 'needs_reconfirmation'
        if snapshot['paused_plan_item_id'] and original is None:
            resume_status = 'original_unavailable'
        elif original and (original['session_status'] != 'active' or original['status'] not in {'active','paused'}):
            resume_status = 'original_unavailable'
        if not valid:
            resume_status = 'source_invalid'
        elif resume_status in {'pending','requested','confirmed'} and cls._other_active(c, row):
            resume_status = 'blocked_by_activity'
        stop_reasons = {'child_requested', 'stopped', 'refused', 'fatigued', 'switched_activity',
                        'launch_failed', 'process_error', 'app_restarted'}
        burden = bool(report and (report['execution'] in {'refused', 'stopped', 'not_executed'}
                      or report['experience'] in {'fatigued', 'uncomfortable'}))
        action = 'ask_feedback'
        if not valid:
            action = 'review_source'
        elif not ended:
            action = 'stop_activity' if burden else 'await_activity_end'
        elif resume_status == 'deferred':
            action = 'respect_deferral'
        elif resume_status == 'requested':
            action = 'resume_original'
        elif burden and (not confirmed or feedback['revision'] > confirmed['revision']):
            action = 'offer_rest_or_defer'
        elif resume_status == 'confirmed':
            action = 'continue_original'
        elif burden or end_reason in stop_reasons:
            action = 'offer_rest_or_defer'
        elif not report:
            action = 'ask_feedback'
        elif resume_status in {'original_unavailable', 'receipt_invalid', 'blocked_by_activity'}:
            action = 'resolve_original'
        elif original and (original['session_status'] != 'active' or original['status'] not in {'active', 'paused'}):
            action = 'resolve_original'
        elif resume_status == 'no_original_bound':
            action = 'clarify_original'
        else:
            action = 'offer_resume'
        messages = {
            'ask_feedback': '刚才实际做了哪些？没做、只做了一部分或觉得累，都可以直接告诉我。',
            'offer_resume': '要回到刚才的学习事项，还是先休息一下？',
            'offer_rest_or_defer': '先停下来。需要休息或把原学习事项留到稍后吗？',
            'respect_deferral': '已保留原学习事项，暂不催促继续。',
            'stop_activity': '先停止这次活动，等待实际停止回执。',
            'await_activity_end': '等待活动结束；到时仍需确认实际执行情况。',
            'resume_original': '已记录恢复意愿，等待原学习事项的执行回执。',
            'continue_original': '已恢复原学习事项；这不代表学习已经完成。',
            'resolve_original': '原事项或恢复回执已变化，需要重新确认接下来做什么。',
            'review_source': '活动来源已变化，旧反馈不再作为当前结果。',
            'clarify_original': '反馈已保存；没有绑定原学习事项，需要确认接下来做什么。',
        }
        activity = {key: row[key] for key in ('outcome_id','child_id','session_id','source_kind','source_id',
                                             'intent','success_description','created_at')}
        activity.update(revision=events[-1]['revision'], source_valid=valid,
            lifecycle={'status':source.get('status', 'missing'), 'ended':ended, 'end_reason':end_reason},
            execution_status=execution_status, feedback=cls._public_event(feedback) if feedback else None,
            resume={'status':resume_status,'original':original,
                    'request_event_id':request['event_id'] if request else None,
                    'execution_key':'companion-outcome-resume:' + request['event_id'] if resume_status == 'requested' else None,
                    'actual_child_return_verified':False},
            next_action={'kind':action,'message':messages[action]}, habit_evidence_eligible=False,
            evidence_limits=['SINGLE_ACTIVITY_IS_NOT_HABIT', 'W20_OBSERVATION_QUALIFICATION_REQUIRED'])
        confirmations = cls._confirmed_resumes(c, row, snapshot, events) if valid else []
        proof = cls._return_verification(c, row, snapshot, confirmations, _return_cache)
        proof['activity_revision'] = activity['revision']
        proof['fingerprint'] = hashlib.sha256(_json(proof).encode()).hexdigest()
        activity['return_verification'] = proof
        activity['request_provenance'] = cls._request_provenance(c, snapshot, source, valid)
        activity['resume']['actual_child_return_verified'] = proof['status'] == 'verified'
        return {'activity':activity, 'history':[cls._public_event(e) for e in events],
                'local_only':True, 'cloud_transmission_allowed':False}

    @classmethod
    def _unavailable_history(cls, c, row, events):
        # Keep the damaged journal intact, visible, and unusable as evidence.
        raw_events = c.execute('SELECT event_id,revision,payload_json FROM companion_activity_outcome_event '
                              'WHERE outcome_id=? ORDER BY revision', (row['outcome_id'],)).fetchall()
        revision = raw_events[-1]['revision'] if raw_events else 0
        history_hash = hashlib.sha256(_json([row['source_snapshot_json'], [dict(e) for e in raw_events]]).encode()).hexdigest()
        proof = cls._return_verification(c, row, {}, [])
        proof.update(reason='activity_history_unavailable', activity_revision=revision)
        proof['fingerprint'] = hashlib.sha256(_json(proof).encode()).hexdigest()
        provenance = cls._request_provenance(c, {}, {}, False)
        provenance['reason'] = 'activity_history_unavailable'
        activity = {k:row[k] for k in ('outcome_id','child_id','session_id','source_kind','source_id',
                                      'intent','success_description','created_at')}
        activity.update(revision=revision, source_valid=False,
            lifecycle={'status':'unknown', 'ended':False, 'end_reason':None},
            execution_status='source_invalid', feedback=None,
            resume={'status':'source_invalid','original':None,'request_event_id':None,
                    'execution_key':None,'actual_child_return_verified':False},
            next_action={'kind':'review_source', 'message':'这条活动历史暂时无法核对，未计为返回学习或习惯改善。'},
            habit_evidence_eligible=False, evidence_limits=['ACTIVITY_HISTORY_UNAVAILABLE'],
            history_fingerprint=history_hash,
            return_verification=proof,
            request_provenance=provenance)
        return {'activity':activity, 'history':[cls._public_event(e) for e in (events or [])],
                'local_only':True, 'cloud_transmission_allowed':False}

    @classmethod
    def _confirmed_resumes(cls, c, row, snapshot, events):
        withdrawn = {e['payload']['target_revision'] for e in events if e['kind'] == 'retracted'}
        by_id, confirmed = {e['event_id']: e for e in events}, []
        for event in events:
            if event['kind'] != 'resume_confirmed' or event['revision'] in withdrawn:
                continue
            request = by_id.get(event['payload'].get('request_event_id'))
            if (not request or request['kind'] != 'resume_requested' or request['revision'] in withdrawn
                    or request['revision'] >= event['revision']):
                continue
            if any(request['revision'] < e['revision'] < event['revision'] and e['kind'] in
                   {'feedback', 'resume_requested', 'resume_deferred', 'retracted'} for e in events):
                continue
            if request['payload'].get('report_kind') == 'self_report':
                from .companion_activity_voice import voice_event_valid
                if not voice_event_valid(c, request['payload'], row['child_id'], row['session_id']):
                    continue
            if cls._resume_receipt(c, snapshot, row['outcome_id'], request, event['payload'].get('plan_item_event_id')):
                confirmed.append((event, request))
        return confirmed

    @staticmethod
    def _homework_source(c, snapshot):
        row = c.execute('''SELECT h.*,pi.plan_item_id,dp.version AS plan_version FROM plan_item pi
            JOIN daily_plan dp USING(plan_id) JOIN homework_item h ON h.homework_id=pi.homework_id
            AND h.homework_id=pi.source_ref AND h.child_id=dp.child_id
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?
            AND pi.source_type='school_homework' ''',
            (snapshot.get('paused_plan_item_id'), snapshot['child_id'], snapshot['session_id'])).fetchone()
        if not row or row['status'] in {'cancelled','deleted','withdrawn'}:
            return None
        # Completion changes lifecycle, not the identity of the assigned work.
        source = {k: row[k] for k in row.keys() if k != 'status'}
        return {'plan_item_id':row['plan_item_id'], 'homework_id':row['homework_id'],
                'source_version':hashlib.sha256(_json(source).encode()).hexdigest()}

    @staticmethod
    def _task_prompts(c, row, task):
        """Use persisted task-to-question ownership, never subject or recency."""
        from .diagnostic_prompt import DiagnosticPromptService, _version
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
            return []
        prompts = c.execute('''SELECT * FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=?
            AND status IN ('waiting_answer','answer_saved','answered')''', (row['child_id'], row['session_id'])).fetchall()
        commands = c.execute('''SELECT p.context_manifest_json FROM agent_plan p
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id AND t.session_id=p.session_id
            WHERE p.child_id=? AND p.session_id=?''', (row['child_id'], row['session_id'])).fetchall()
        manifests = [_history_object(command[0]) for command in commands]
        uncertain = any(m is None or ('goal_prompt_binding' in m
                        and not isinstance(m['goal_prompt_binding'], dict)) for m in manifests)
        matched = []
        for prompt in prompts:
            item = DiagnosticPromptService._item(c, row['child_id'], prompt['diagnostic_item_id'])
            if not item or _version(item) != prompt['item_version']:
                continue
            bindings = [m for m in manifests if m is not None
                        and isinstance(m.get('goal_prompt_binding'), dict)
                        and m['goal_prompt_binding'].get('prompt_id') == prompt['prompt_id']]
            exact = bool(bindings) and all(b['goal_prompt_binding'].get('plan_item_id') == task['plan_item_id']
                                          for b in bindings) and any(
                b['goal_prompt_binding'].get('item_version') == prompt['item_version']
                and isinstance(b.get('task_start_binding'), dict)
                and all(b.get('task_start_binding', {}).get(k) == task[k]
                        for k in ('plan_item_id', 'source_type', 'source_ref')) for b in bindings)
            if not bindings and not uncertain and task['source_type'] == 'due_review' and prompt['review_task_id'] == task['source_ref']:
                owners = c.execute('''SELECT pi.plan_item_id FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                    WHERE dp.child_id=? AND dp.session_id=? AND pi.source_type='due_review' AND pi.source_ref=?''',
                    (row['child_id'], row['session_id'], task['source_ref'])).fetchall()
                exact = [r[0] for r in owners] == [task['plan_item_id']]
            if exact:
                matched.append(dict(prompt))
        return matched

    @classmethod
    def _return_verification(cls, c, row, snapshot, confirmations, cache=None):
        """Current, read-only proof of return. Never certify a habit or mastery."""
        proof = dict(version='companion_return_v1', status='unknown', reason='no_qualified_after_resume_evidence',
            outcome_id=row['outcome_id'], plan_item_id=snapshot.get('paused_plan_item_id'),
            subject=(snapshot.get('original_identity') or {}).get('subject'),
            task_type=(snapshot.get('original_identity') or {}).get('task_type'),
            activity_initiated_by='unknown', evidence_refs=[], basis=[], episodes=[],
            independent=False, habit_success=False, local_only=True, cloud_transmission_allowed=False)
        if not confirmations:
            proof['reason'] = 'no_current_resume_receipt'
            return proof
        task = c.execute('''SELECT pi.*,dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?''',
            (snapshot['paused_plan_item_id'], row['child_id'], row['session_id'])).fetchone()
        if not task:
            proof['reason'] = 'original_task_unavailable'
            return proof
        homework_source = snapshot.get('return_task_source')
        if homework_source and homework_source != cls._homework_source(c, snapshot):
            proof['reason'] = 'original_homework_source_changed'
            return proof
        prompts = cls._task_prompts(c, row, task)
        if not prompts and not homework_source:
            proof['reason'] = 'exact_task_question_binding_unavailable'
            return proof
        from .learning_obligations import project_child_mastery_evidence
        cache = {} if cache is None else cache
        if prompts and 'mastery' not in cache:
            cache['mastery'] = project_child_mastery_evidence(c, row['child_id'])
        effective = {e['evidence_id']: e for node in cache.get('mastery', {}).values() for e in node['effective_evidence']}
        proof['task_source_version'] = (homework_source['source_version'] if homework_source else
            hashlib.sha256(_json(sorted((p['prompt_id'], p['item_version']) for p in prompts)).encode()).hexdigest())
        now = datetime.now(timezone.utc)
        from .companion_reviewed_return import reviewed_return_evidence
        reviewed = reviewed_return_evidence(c, row['child_id'], row['session_id'], prompts, now)
        for confirmation, request in confirmations:
            receipt = c.execute('SELECT rowid AS sequence,* FROM plan_item_event WHERE event_id=?',
                                (confirmation['payload']['plan_item_event_id'],)).fetchone()
            start = _time(receipt['occurred_at'])
            # A later task/segment cannot donate its evidence to an earlier return.
            boundaries = c.execute('''SELECT e.occurred_at FROM plan_item_event e
                JOIN plan_item pi USING(plan_item_id) JOIN daily_plan dp USING(plan_id)
                WHERE dp.child_id=? AND dp.session_id=? AND e.rowid>?
                AND ((pi.plan_item_id=? AND e.event_type IN ('paused','completed','rescheduled','cancelled'))
                  OR e.event_type IN ('started','resumed'))''',
                (row['child_id'], row['session_id'], receipt['sequence'], task['plan_item_id'])).fetchall()
            end = min([now, *(_time(r[0]) for r in boundaries)])
            for table, _ in SOURCES.values():
                if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    for activity in c.execute(f'SELECT started_at FROM {table} WHERE child_id=? AND session_id=?',
                                              (row['child_id'], row['session_id'])):
                        if _time(activity[0]) > start:
                            end = min(end, _time(activity[0]))
            if end <= start:
                continue
            evidence = []
            for prompt in prompts:
                answer = c.execute('''SELECT a.evidence_id,a.created_at FROM diagnostic_attempt a
                    JOIN learning_evidence le ON le.evidence_id=a.evidence_id AND le.child_id=a.child_id AND le.session_id=a.session_id
                    WHERE a.attempt_id=? AND a.child_id=? AND a.session_id=? AND a.diagnostic_item_id=?''',
                    (prompt['attempt_id'], row['child_id'], row['session_id'], prompt['diagnostic_item_id'])).fetchone()
                e = (effective.get(answer['evidence_id']) or reviewed.get(answer['evidence_id'])) if answer else None
                if (e and start < _time(e['occurred_at']) <= end and e['independent'] == 1 and e['hint_level'] == 0):
                    evidence.append(dict(evidence_id=e['evidence_id'], occurred_at=e['occurred_at'],
                        basis='independent_answer', independent=True, prompt_id=prompt['prompt_id'],
                        item_version=prompt['item_version'], question_identity=e['question_identity'],
                        source_version=hashlib.sha256(_json(e).encode()).hexdigest(),
                        **({'review_refs': e['review_refs'], 'qualification_basis': e['score_authority']}
                           if e.get('review_refs') else {})))
            observation = cls._return_observation(c, row, task, start, end)
            if observation:
                evidence.extend(observation)
            if evidence:
                proof['episodes'].append(dict(resume_confirmation_event_id=confirmation['event_id'],
                    resume_confirmation_revision=confirmation['revision'], resume_receipt_id=receipt['event_id'],
                    resume_request_event_id=request['event_id'], resume_request_revision=request['revision'],
                    resume_requested_by=request['actor'], resumed_at=receipt['occurred_at'],
                    closed_at=end.isoformat() if end < now else None,
                    evidence=evidence))
        if proof['episodes']:
            evidence = [e for episode in proof['episodes'] for e in episode['evidence']]
            proof.update(status='verified', reason=None, evidence_refs=sorted({e['evidence_id'] for e in evidence}),
                basis=sorted({e['basis'] for e in evidence}), independent=any(e['independent'] for e in evidence))
        return proof

    @staticmethod
    def _return_observation(c, row, task, start, end):
        from .behavior_model.service import BehaviorModelService
        from .behavior_model.outcome_evidence import coverage_projection, task_context_snapshot
        if not all(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                   for table in ('observable_event', 'behavior_retraction')):
            return []
        context = task_context_snapshot({k: task[k] for k in
            ('plan_item_id', 'subject', 'task_type', 'cognitive_load', 'source_type', 'plan_version')})
        rows = []
        for raw in BehaviorModelService._coverage_records(c, row['child_id'], row['session_id']):
            record = dict(raw)
            payload = json.loads(record['payload_json'])
            if record['event_type'] == 'observation.sample':
                state = c.execute('''SELECT event_type,payload_json FROM observable_event WHERE event_id=?
                    AND child_id=? AND session_id=? AND source='local_vision' ''',
                    ('observation:' + str(payload.get('state_event_id')), row['child_id'], row['session_id'])).fetchone()
                # Retain rejected points as continuity barriers, not as gaps to bridge.
                if (payload.get('task_context') != context or payload.get('state') != 'writing_like'
                        or not state or state['event_type'] != 'work.writing'
                        or json.loads(state['payload_json']).get('shadow') is not False
                        or not start < _time(payload['captured_at']) <= end):
                    record['behavior_source_invalid'] = True
            rows.append(record)
        result = coverage_projection(rows, row['child_id'], start, end)
        if not result['behavior_observed_seconds'] or not any(s['event_counts'].get('work.writing') for s in result['strata']):
            return []
        by_id = {r['event_id']: r for r in rows}
        return [dict(evidence_id=eid, occurred_at=by_id[eid]['occurred_at'], basis='qualified_writing_observation',
                     independent=False, observation_projection_version=result['projection_version'],
                     task_context_version=context['context_version'],
                     source_version=hashlib.sha256(_json(by_id[eid]).encode()).hexdigest())
                for eid in result['evidence_refs'] if by_id[eid]['event_type'] == 'observation.sample'
                and not by_id[eid]['behavior_source_invalid'] and not by_id[eid]['retracted']]

    def get(self, child_id, outcome_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self._detail(c, child_id, outcome_id)

    def list(self, child_id, *, offset=0, limit=30):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ActivityInputError('ACTIVITY_PAGE_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN')
            self._child(c, child_id)
            total = c.execute('SELECT COUNT(*) FROM companion_activity_outcome WHERE child_id=?', (child_id,)).fetchone()[0]
            ids = c.execute('SELECT outcome_id FROM companion_activity_outcome WHERE child_id=? ORDER BY created_at DESC,outcome_id LIMIT ? OFFSET ?',
                            (child_id, limit, offset)).fetchall()
            return {'child_id':child_id, 'total':total, 'offset':offset,
                    'items':[self._detail(c, child_id, r[0])['activity'] for r in ids],
                    'local_only':True,'cloud_transmission_allowed':False}

    def _write(self, child_id, outcome_id, *, kind, actor, payload, expected_revision, client_action_id, connection=None):
        if actor not in ACTORS or type(expected_revision) is not int or expected_revision < 1:
            raise ActivityInputError('ACTIVITY_WRITE_INVALID')
        _text(client_action_id, 200)
        request = dict(kind=kind, actor=actor, outcome_id=outcome_id, payload=payload, expected_revision=expected_revision)
        with (self.database.connect() if connection is None else nullcontext(connection)) as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            row = self._owned(c, child_id, outcome_id)
            replay = self._replay(c, child_id, client_action_id, request)
            if replay is not None:
                return replay
            before = self._detail(c, child_id, outcome_id)
            state = before['activity']
            if state['revision'] != expected_revision:
                raise ActivityConflict('ACTIVITY_REVISION_CONFLICT')
            if not state['source_valid'] and kind != 'retracted':
                raise ActivityConflict('ACTIVITY_SOURCE_CHANGED')
            if kind == 'retracted':
                target = next((e for e in before['history'] if e['revision'] == payload['target_revision']), None)
                if not target or target['kind'] in {'opened', 'retracted'}:
                    raise ActivityInputError('ACTIVITY_RETRACTION_TARGET_INVALID')
                if actor == 'child' and target['actor'] != 'child':
                    raise ActivityInputError('ACTIVITY_RETRACTION_AUTHORITY_INVALID')
                if any(e['kind'] == 'retracted' and e['payload']['target_revision'] == payload['target_revision'] for e in before['history']):
                    raise ActivityConflict('ACTIVITY_ALREADY_RETRACTED')
            if kind in {'resume_requested', 'resume_confirmed'}:
                if self._other_active(c, row):
                    raise ActivityConflict('ACTIVITY_OTHER_ACTIVITY_ACTIVE')
                original = state['resume']['original']
                if not state['lifecycle']['ended'] or not original or original['session_status'] != 'active' or original['status'] not in {'active', 'paused'}:
                    raise ActivityConflict('ACTIVITY_RESUME_NOT_AVAILABLE')
            if kind == 'resume_confirmed':
                request_event = next((e for e in before['history'] if e['event_id'] == payload['request_event_id']), None)
                if (state['resume']['status'] != 'requested' or not request_event
                        or state['resume']['request_event_id'] != request_event['event_id']
                        or state['resume']['original']['status'] != 'active'
                        or not self._resume_receipt(c, json.loads(row['source_snapshot_json']), outcome_id, request_event, payload['plan_item_event_id'])):
                    raise ActivityConflict('ACTIVITY_RESUME_RECEIPT_INVALID')
            event = self._append(c, row, expected_revision + 1, kind, actor, payload, request, client_action_id)
            result = {'event':event, **self._detail(c, child_id, outcome_id)}
            if connection is None:
                c.commit()
            return result

    def feedback(self, child_id, outcome_id, *, execution, experience, note, evidence_basis, actor,
                 expected_revision, client_action_id):
        if execution not in EXECUTIONS or experience not in EXPERIENCES or evidence_basis not in {'child_report', 'parent_observed', 'parent_report'}:
            raise ActivityInputError('ACTIVITY_FEEDBACK_INVALID')
        if ((evidence_basis == 'child_report' and actor != 'child')
                or (evidence_basis in {'parent_observed','parent_report'} and actor != 'parent')):
            raise ActivityInputError('ACTIVITY_FEEDBACK_AUTHORITY_INVALID')
        return self._write(child_id, outcome_id, kind='feedback', actor=actor,
            payload=dict(execution=execution, experience=experience, note=_text(note), evidence_basis=evidence_basis),
            expected_revision=expected_revision, client_action_id=client_action_id)

    def retract(self, child_id, outcome_id, *, target_revision, reason, actor, expected_revision, client_action_id):
        if type(target_revision) is not int or target_revision < 1:
            raise ActivityInputError('ACTIVITY_RETRACTION_TARGET_INVALID')
        return self._write(child_id, outcome_id, kind='retracted', actor=actor,
            payload=dict(target_revision=target_revision, reason=_text(reason)),
            expected_revision=expected_revision, client_action_id=client_action_id)

    def request_resume(self, child_id, outcome_id, *, note, actor, expected_revision, client_action_id):
        if actor not in {'child', 'parent'}:
            raise ActivityInputError('ACTIVITY_EXPLICIT_RESUME_REQUIRED')
        return self._write(child_id, outcome_id, kind='resume_requested', actor=actor, payload={'note':_text(note)},
                           expected_revision=expected_revision, client_action_id=client_action_id)

    def defer_resume(self, child_id, outcome_id, *, note, actor, expected_revision, client_action_id):
        if actor not in {'child','parent'}:
            raise ActivityInputError('ACTIVITY_EXPLICIT_DEFERRAL_REQUIRED')
        return self._write(child_id, outcome_id, kind='resume_deferred', actor=actor, payload={'note':_text(note)},
                           expected_revision=expected_revision, client_action_id=client_action_id)

    def confirm_resume(self, child_id, outcome_id, *, request_event_id, plan_item_event_id, expected_revision, client_action_id):
        return self._write(child_id, outcome_id, kind='resume_confirmed', actor='runtime',
            payload=dict(request_event_id=_text(request_event_id,200), plan_item_event_id=_text(plan_item_event_id,200)),
            expected_revision=expected_revision, client_action_id=client_action_id)


def project_companion_activity_evidence(connection, child_id, since):
    """Read within the caller's snapshot, without migration or materialization.

    Historical task restoration is not child return, habit improvement, or current
    permission to restart a timer. Raw utterances and local paths stay in journals.
    """
    _time(since)
    service = CompanionActivityOutcomeService
    service._child(connection, child_id)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('companion_activity_outcome','companion_activity_outcome_event')")}
    activities, return_cache = [], {}
    if len(tables) == 2:
        rows = connection.execute('''SELECT * FROM companion_activity_outcome
            WHERE child_id=? AND COALESCE(julianday(CASE WHEN json_valid(source_snapshot_json)
                THEN CASE WHEN json_type(source_snapshot_json,'$.started_at')='text'
                    THEN json_extract(source_snapshot_json,'$.started_at') END END),
                julianday(created_at))>=julianday(?)
            ORDER BY outcome_id''', (child_id, since)).fetchall()
        for row in rows:
            detail = service._detail(connection, child_id, row['outcome_id'], _return_cache=return_cache)
            current, history = detail['activity'], detail['history']
            snapshot = _history_snapshot(row)
            if current['return_verification']['reason'] == 'activity_history_unavailable':
                activities.append({
                    **{key:current[key] for key in ('outcome_id','source_kind','source_id','session_id','revision','source_valid')},
                    'current_resume_status':'source_invalid', 'historical_resume_confirmed':False,
                    'resume_confirmation_event_ids':[], 'feedback_basis':None, 'feedback_revision':None,
                    'execution_classification':'unknown',
                    'evidence_input_hash':current['history_fingerprint'],
                    'current_source_fingerprint':None, 'voice_provenance_fingerprint':None,
                    'return_verification':current['return_verification'],
                    'request_provenance':current['request_provenance'],
                })
                continue
            try:
                _, _, current_source_fingerprint = service._source(connection, child_id, current['source_kind'], current['source_id'])
            except UnknownActivity:
                current_source_fingerprint = None
            voice_inputs = []
            for turn_id in sorted({e['payload']['agent_turn_id'] for e in history if e['payload'].get('agent_turn_id')}):
                source_turn = connection.execute('''SELECT t.agent_turn_id,t.child_id,t.session_id,t.transcript,t.intent,t.action_json,
                    p.plan_id,p.context_manifest_json,s.capability_name,s.arguments_json
                    FROM agent_turn t LEFT JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                    LEFT JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
                    WHERE t.agent_turn_id=? AND t.child_id=?''',(turn_id,child_id)).fetchone()
                voice_inputs.append(dict(source_turn) if source_turn else {'agent_turn_id':turn_id,'missing':True})
            confirmed = ([e['event_id'] for e, _ in service._confirmed_resumes(connection,
                {'child_id':child_id, **current}, snapshot, history)] if current['source_valid'] else [])
            feedback = current['feedback'] if current['source_valid'] else None
            activities.append({
                **{key:current[key] for key in ('outcome_id','source_kind','source_id','session_id','revision','source_valid')},
                'current_resume_status':current['resume']['status'],
                'historical_resume_confirmed':bool(confirmed), 'resume_confirmation_event_ids':confirmed,
                'feedback_basis':feedback['payload']['evidence_basis'] if feedback else None,
                'feedback_revision':feedback['revision'] if feedback else None,
                'execution_classification':feedback['payload']['execution'] if feedback else 'unknown',
                'evidence_input_hash':hashlib.sha256(_json(history).encode()).hexdigest(),
                'current_source_fingerprint':current_source_fingerprint,
                'voice_provenance_fingerprint':hashlib.sha256(_json(voice_inputs).encode()).hexdigest(),
                'return_verification':current['return_verification'],
                'request_provenance':current['request_provenance'],
            })
    result = dict(activities=activities,
        task_resume_receipt_count=sum(int(a['historical_resume_confirmed']) for a in activities),
        self_report_count=sum(a['feedback_basis'] in {'child_report','parent_report'} for a in activities),
        parent_observed_count=sum(a['feedback_basis']=='parent_observed' for a in activities),
        verified_child_return_count=sum(a['return_verification']['status'] == 'verified' for a in activities),
        habit_success_count=0,
        local_only=True, cloud_transmission_allowed=False)
    result['fingerprint'] = hashlib.sha256(_json({'child_id':child_id,**result}).encode()).hexdigest()
    return result
