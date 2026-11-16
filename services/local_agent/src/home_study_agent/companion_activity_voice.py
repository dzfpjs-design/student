"""W24 child voice adapter: frozen local target + verbatim persisted utterance.

Host contract: before model planning, freeze outcome_id/expected_revision/child_id/
session_id in context_manifest_json.companion_activity_outcome_binding. Model
arguments contain no target or authority fields. The executor calls execute with
the persisted turn ID after claiming its step. No driver or plan is changed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent_continuation import ContinuationStore
from .companion_activity_outcome import CompanionActivityOutcomeService, ActivityConflict


CAPABILITY = 'companion.activity_feedback'
ACTION_TYPE = 'record_companion_activity_feedback'
MANIFEST_KEY = 'companion_activity_outcome_binding'


class ActivityVoiceError(ValueError):
    pass


class ActivityVoiceArguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['feedback', 'decline', 'request_resume', 'defer_resume']
    response_quote: str = Field(min_length=1, max_length=2000)
    execution: Literal['completed', 'partial', 'not_executed', 'unknown', 'refused', 'stopped'] = 'unknown'
    experience: Literal['comfortable', 'neutral', 'fatigued', 'uncomfortable', 'unknown'] = 'unknown'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _read_turn(c, turn_id):
    row = c.execute('''SELECT t.agent_turn_id,t.child_id,t.session_id,t.transcript,t.intent,t.action_json,
        t.tool_status,p.plan_id,p.child_id AS plan_child_id,p.session_id AS plan_session_id,
        p.status AS plan_status,p.context_manifest_json,s.step_id,s.arguments_json,s.capability_name,
        s.status AS step_status,ds.child_id AS session_child_id,ds.status AS session_status
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        JOIN daily_session ds ON ds.session_id=t.session_id
        WHERE t.agent_turn_id=?''', (turn_id,)).fetchone()
    if not row or row['capability_name'] != CAPABILITY:
        raise ActivityVoiceError('ACTIVITY_VOICE_TURN_NOT_FOUND')
    if (row['child_id'] != row['plan_child_id'] or row['child_id'] != row['session_child_id']
            or row['session_id'] != row['plan_session_id']):
        raise ActivityVoiceError('ACTIVITY_VOICE_OWNER_MISMATCH')
    try:
        raw = json.loads(row['arguments_json'])
        arguments = ActivityVoiceArguments.model_validate(raw)
        manifest = json.loads(row['context_manifest_json'])
        binding = manifest[MANIFEST_KEY]
        action = json.loads(row['action_json'])
        if action.get('type') != ACTION_TYPE:
            raise ActivityVoiceError('ACTIVITY_VOICE_ACTION_MISMATCH')
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as error:
        raise ActivityVoiceError('ACTIVITY_VOICE_CONTRACT_INVALID') from error
    required = {'outcome_id','expected_revision','child_id','session_id'}
    if (not isinstance(binding, dict) or not required.issubset(binding)
            or set(binding) - required - {'local_only','cloud_transmission_allowed'}
            or binding.get('local_only', True) is not True
            or binding.get('cloud_transmission_allowed', False) is not False
            or not isinstance(binding['outcome_id'], str) or not binding['outcome_id']
            or type(binding['expected_revision']) is not int or binding['expected_revision'] < 1
            or binding['child_id'] != row['child_id'] or binding['session_id'] != row['session_id']):
        raise ActivityVoiceError('ACTIVITY_VOICE_BINDING_INVALID')
    if arguments.response_quote != row['transcript'] or not row['transcript'].strip():
        raise ActivityVoiceError('ACTIVITY_VOICE_QUOTE_MISMATCH')
    if arguments.operation != 'feedback' and (set(raw) - {'operation','response_quote'}):
        raise ActivityVoiceError('ACTIVITY_VOICE_UNRELATED_CLASSIFICATION')
    if row['intent'] == 'runtime_continuation':
        raise ActivityVoiceError('ACTIVITY_VOICE_CHILD_TURN_REQUIRED')
    snapshot = {key: row[key] for key in ('agent_turn_id','child_id','session_id','transcript','plan_id','step_id')}
    snapshot.update(binding=binding, arguments=arguments.model_dump(), action_type=ACTION_TYPE)
    return dict(row), snapshot


def voice_event_valid(c, payload, child_id, session_id):
    """Read-time provenance check; deleting/revising a source does not erase audit."""
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='companion_activity_voice_binding'").fetchone():
            return False
        binding = c.execute('SELECT * FROM companion_activity_voice_binding WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                            (payload.get('agent_turn_id'), child_id, session_id)).fetchone()
        if not binding or binding['snapshot_hash'] != payload.get('voice_snapshot_hash'):
            return False
        row, current = _read_turn(c, binding['agent_turn_id'])
        frozen = json.loads(binding['snapshot_json'])
        if current != frozen['turn'] or payload.get('response_quote') != row['transcript']:
            return False
        if current['arguments']['operation'] in {'request_resume','defer_resume'}:
            return row['plan_status'] != 'cancelled' and row['step_status'] != 'cancelled'
        return True
    except (ActivityVoiceError, ValueError, TypeError):
        return False


class CompanionActivityVoiceService:
    def __init__(self, database):
        self.database = database
        self.outcomes = CompanionActivityOutcomeService(database)
        self.ensure_schema()

    def ensure_schema(self):
        with self.database.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS companion_activity_voice_binding (
                    agent_turn_id TEXT PRIMARY KEY REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
                    outcome_id TEXT NOT NULL REFERENCES companion_activity_outcome(outcome_id) ON DELETE CASCADE,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                    snapshot_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS companion_activity_voice_binding_no_update
                BEFORE UPDATE ON companion_activity_voice_binding
                BEGIN SELECT RAISE(ABORT, 'voice bindings are immutable'); END;
            ''')
            c.commit()

    def _bind(self, c, turn_id):
        row, turn = _read_turn(c, turn_id)
        target = turn['binding']
        outcome = self.outcomes._owned(c, row['child_id'], target['outcome_id'])
        if outcome['session_id'] != row['session_id']:
            raise ActivityVoiceError('ACTIVITY_VOICE_ACTIVITY_SESSION_MISMATCH')
        frozen = dict(turn=turn, source_fingerprint=outcome['source_fingerprint'])
        saved = c.execute('SELECT * FROM companion_activity_voice_binding WHERE agent_turn_id=?', (turn_id,)).fetchone()
        if saved:
            if saved['snapshot_json'] != _json(frozen):
                raise ActivityVoiceError('ACTIVITY_VOICE_FROZEN_TURN_CHANGED')
            return row, frozen, saved['snapshot_hash']
        if (row['session_status'] != 'active' or row['plan_status'] not in {'planned','running'}
                or row['step_status'] not in {'planned','running'}
                or (row['tool_status'] != 'pending' and not self._decline_storage_retry(c, row))):
            raise ActivityVoiceError('ACTIVITY_VOICE_AUTHORIZATION_CHANGED')
        state = self.outcomes._detail(c, row['child_id'], outcome['outcome_id'])['activity']
        if state['revision'] != target['expected_revision'] or not state['source_valid']:
            raise ActivityConflict('ACTIVITY_VOICE_TARGET_VERSION_CHANGED')
        digest = hashlib.sha256(_json(frozen).encode()).hexdigest()
        c.execute('INSERT INTO companion_activity_voice_binding VALUES (?,?,?,?,?,?,?)',
            (turn_id, outcome['outcome_id'], row['child_id'], row['session_id'], _json(frozen), digest,
             datetime.now(timezone.utc).isoformat()))
        return row, frozen, digest

    def bind_turn(self, agent_turn_id):
        """Optional pre-dispatch freeze; execute also freezes atomically on first use."""
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row, frozen, digest = self._bind(c, agent_turn_id)
            c.commit()
            return {**frozen['turn']['binding'], 'agent_turn_id':row['agent_turn_id'],
                    'snapshot_hash':digest, 'local_only':True, 'cloud_transmission_allowed':False}

    def execute(self, agent_turn_id):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row, frozen, digest = self._bind(c, agent_turn_id)
            self._authorize(c, row)
            turn = frozen['turn']
            args, target = turn['arguments'], turn['binding']
            operation = args['operation']
            payload = dict(agent_turn_id=agent_turn_id, response_quote=row['transcript'],
                           note=row['transcript'], voice_snapshot_hash=digest, report_kind='self_report',
                           classification_status='model_candidate', effectiveness_verified=False)
            if operation in {'feedback','decline'}:
                kind = 'feedback'
                payload.update(evidence_basis='child_report', execution='refused' if operation == 'decline' else args['execution'],
                               experience=args['experience'])
            else:
                kind = 'resume_requested' if operation == 'request_resume' else 'resume_deferred'
            key = 'companion-voice:' + hashlib.sha256(agent_turn_id.encode()).hexdigest()
            saved = None
            if operation == 'decline':
                if json.loads(row['context_manifest_json']).get('voice_control_binding'):
                    from .voice_control_intent import VoiceControlIntent
                    control = VoiceControlIntent.inspect_execution(c, agent_turn_id)
                    if not control or control['control'] != CAPABILITY:
                        raise ActivityVoiceError('ACTIVITY_VOICE_AUTHORIZATION_CHANGED')
                saved = c.execute('''SELECT payload_json FROM companion_activity_outcome_event
                    WHERE child_id=? AND client_action_id=?''', (row['child_id'], key)).fetchone()
                if saved:
                    previous = json.loads(saved['payload_json'])
                    if {k:v for k,v in previous.items() if k != 'activity_stop'} != payload:
                        raise ActivityConflict('ACTIVITY_IDEMPOTENCY_CONFLICT')
                    payload = previous
                else:
                    source = self.outcomes._owned(c, row['child_id'], target['outcome_id'])
                    actual, _, fingerprint = self.outcomes._source(
                        c, row['child_id'], source['source_kind'], source['source_id'])
                    if fingerprint != source['source_fingerprint'] or actual['session_id'] != row['session_id']:
                        raise ActivityConflict('ACTIVITY_SOURCE_CHANGED')
                    active = actual['status'] == 'active'
                    local_stop = active and source['source_kind'] == 'companion'
                    payload['activity_stop'] = dict(
                        status='stopped' if local_stop else 'external_stop_required' if active else 'already_ended',
                        source_kind=source['source_kind'], source_id=source['source_id'],
                        previous_status=actual['status'], status_after='cancelled' if local_stop else actual['status'],
                        ended_at=datetime.now(timezone.utc).isoformat() if local_stop else actual.get('ended_at'))
            result = self.outcomes._write(row['child_id'], target['outcome_id'], kind=kind, actor='child',
                payload=payload, expected_revision=target['expected_revision'],
                client_action_id=key, connection=c)
            if operation == 'decline' and not saved and payload['activity_stop']['status'] == 'stopped':
                receipt = payload['activity_stop']
                stopped = c.execute('''UPDATE companion_activity SET status='cancelled',end_reason='child_requested',
                    ended_at=?,updated_at=? WHERE activity_id=? AND child_id=? AND session_id=? AND status='active' ''',
                    (receipt['ended_at'], receipt['ended_at'], receipt['source_id'], row['child_id'], row['session_id']))
                if stopped.rowcount != 1:
                    raise ActivityConflict('ACTIVITY_VOICE_STOP_SOURCE_CHANGED')
                result.update(self.outcomes._detail(c, row['child_id'], target['outcome_id']))
            c.commit()
        result.update(evidence=['activity_self_report_recorded'] if kind == 'feedback' else ['activity_resume_intent_recorded'],
                      response_text=result['activity']['next_action']['message'])
        if operation == 'decline':
            receipt = result['event']['payload'].get('activity_stop') or {'status': 'legacy_unverified'}
            result['activity_stop'] = receipt
            if receipt['status'] == 'stopped':
                result['evidence'].append('companion_activity_stopped')
                result['response_text'] = '这次活动已停止，原学习事项保留，没有自动恢复学习。'
            elif receipt['status'] == 'already_ended':
                result['response_text'] = '这次活动已经结束，没有停止其他活动或自动恢复学习。'
            elif receipt['status'] == 'external_stop_required':
                result['response_text'] = '已记录停止这次活动的请求，仍需播放器或游戏进程确认停止；没有自动恢复学习。'
            else:
                result['response_text'] = '原记录只有反馈，没有实际停止回执。请重新确认当前要停止的活动。'
        return result

    @staticmethod
    def _decline_storage_retry(c, row):
        # This is the same persisted child command, re-admitted by the executor.
        # No recovery from cancellation, arbitrary failure, or a new target.
        if (row['tool_status'] != 'failed' or row['plan_status'] != 'running' or row['step_status'] != 'running'
                or json.loads(row['arguments_json']).get('operation') != 'decline'):
            return False
        error = c.execute('SELECT tool_error_code FROM agent_turn WHERE agent_turn_id=?', (row['agent_turn_id'],)).fetchone()
        return bool(error and error[0] == 'OperationalError')

    @staticmethod
    def _authorize(c, row):
        storage_retry = CompanionActivityVoiceService._decline_storage_retry(c, row)
        if row['tool_status'] == 'failed':
            saved = c.execute('''SELECT e.payload_json FROM companion_activity_outcome_event e
                JOIN companion_activity_voice_binding b ON b.outcome_id=e.outcome_id
                    AND b.agent_turn_id=json_extract(e.payload_json,'$.agent_turn_id')
                JOIN agent_turn t ON t.agent_turn_id=b.agent_turn_id
                WHERE b.agent_turn_id=? AND b.child_id=? AND b.session_id=?
                    AND t.tool_error_code='OperationalError' AND e.kind='resume_requested'
                    AND json_extract(b.snapshot_json,'$.turn.arguments.operation')='request_resume' ''',
                (row['agent_turn_id'], row['child_id'], row['session_id'])).fetchone()
            storage_retry = storage_retry or bool(saved and voice_event_valid(
                c, json.loads(saved['payload_json']), row['child_id'], row['session_id']))
        if (row['session_status'] != 'active' or row['plan_status'] != 'running'
                or row['step_status'] != 'running' or (row['tool_status'] != 'pending' and not storage_retry)
                or not ContinuationStore.execution_allowed(c, row['agent_turn_id'], running=True)):
            raise ActivityVoiceError('ACTIVITY_VOICE_AUTHORIZATION_CHANGED')
        if storage_retry and json.loads(row['context_manifest_json']).get('voice_control_binding'):
            from .voice_control_intent import VoiceControlIntent
            control = VoiceControlIntent.inspect_execution(c, row['agent_turn_id'])
            if not control or control['control'] != CAPABILITY:
                raise ActivityVoiceError('ACTIVITY_VOICE_AUTHORIZATION_CHANGED')

    def resume_original(self, agent_turn_id):
        """Authorize, resume the original timer, and confirm in one transaction.

        A deterministic event key is correlation only, never authority. The
        repository borrows the actual writer; the original start identifies a
        question source only, never the authority for this new resume request.
        External step completion remains the executor's responsibility.
        """
        from .repository import StudyRepository, PlanInputError
        from .pet_task_execution import is_generic_pet_task
        from .task_start_binding import TaskStartBinding
        from .voice_control_intent import VoiceControlIntent

        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row, frozen, _ = self._bind(c, agent_turn_id)
            self._authorize(c, row)
            turn = frozen['turn']
            if turn['arguments']['operation'] != 'request_resume':
                raise ActivityVoiceError('ACTIVITY_VOICE_RESUME_REQUEST_REQUIRED')
            oid = turn['binding']['outcome_id']
            state = self.outcomes._detail(c, row['child_id'], oid)
            request = next((e for e in state['history'] if e['kind'] == 'resume_requested'
                            and e['payload'].get('agent_turn_id') == agent_turn_id), None)
            if not request or not voice_event_valid(c, request['payload'], row['child_id'], row['session_id']):
                raise ActivityVoiceError('ACTIVITY_VOICE_RESUME_REQUEST_REQUIRED')
            key = 'companion-voice-confirm:' + hashlib.sha256(agent_turn_id.encode()).hexdigest()
            previous = c.execute('SELECT payload_json,request_json FROM companion_activity_outcome_event WHERE child_id=? AND client_action_id=?',
                                 (row['child_id'], key)).fetchone()
            if previous:
                expected_payload = json.loads(previous['payload_json'])
                if expected_payload.get('request_event_id') != request['event_id']:
                    raise ActivityConflict('ACTIVITY_IDEMPOTENCY_CONFLICT')
                replay = self.outcomes._replay(c, row['child_id'], key, json.loads(previous['request_json']))
                return self._resume_result(replay)
            if json.loads(row['context_manifest_json']).get('voice_control_binding'):
                control = VoiceControlIntent.inspect_execution(c, agent_turn_id)
                if not control or control['control'] != CAPABILITY:
                    raise ActivityVoiceError('ACTIVITY_VOICE_AUTHORIZATION_CHANGED')
            view = state['activity']
            original = view['resume']['original']
            if (view['revision'] != request['revision'] or not view['source_valid']
                    or view['resume']['status'] != 'requested'
                    or view['resume']['request_event_id'] != request['event_id']
                    or not original or original['status'] != 'paused'
                    or original['session_status'] != 'active'):
                raise ActivityConflict('ACTIVITY_VOICE_RESUME_STATE_CHANGED')
            if c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
                WHERE dp.child_id=? AND pi.status='active' ''', (row['child_id'],)).fetchone():
                raise ActivityConflict('ACTIVITY_VOICE_OTHER_TASK_ACTIVE')
            execution_key = view['resume']['execution_key']
            if c.execute('SELECT 1 FROM plan_item_event WHERE client_action_id=?', (execution_key,)).fetchone():
                raise ActivityConflict('ACTIVITY_VOICE_UNAUTHORIZED_EXISTING_RECEIPT')
            item = c.execute('''SELECT pi.*,dp.child_id,dp.session_id FROM plan_item pi
                JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=?
                AND dp.child_id=? AND dp.session_id=? AND dp.status='active' ''',
                (original['plan_item_id'], row['child_id'], row['session_id'])).fetchone()
            if not item:
                raise ActivityConflict('ACTIVITY_VOICE_ORIGINAL_PROMPT_CHANGED')
            origin_turn_id = None
            if is_generic_pet_task(c, item):
                # A retained prompt may be started by task.resume. Its actual
                # start receipt and original prepare receipt are separate facts.
                sources = c.execute('''SELECT p.agent_turn_id,p.context_manifest_json,
                    p.status AS plan_status,s.status AS step_status,t.tool_status,
                    s.capability_name,pp.context_manifest_json AS prepare_manifest,
                    pp.status AS prepare_plan_status,ps.status AS prepare_step_status
                    FROM plan_item_event e JOIN agent_step s ON s.idempotency_key=e.client_action_id
                    JOIN agent_plan p ON p.plan_id=s.plan_id
                    JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
                    JOIN diagnostic_prompt q
                        ON q.prompt_id=json_extract(p.context_manifest_json,'$.goal_prompt_binding.prompt_id')
                    JOIN agent_step ps ON q.prepare_action_id='goal-task:'||ps.idempotency_key
                        AND ps.capability_name='task.start'
                    JOIN agent_plan pp ON pp.plan_id=ps.plan_id
                    JOIN agent_turn pt ON pt.agent_turn_id=pp.agent_turn_id
                    WHERE e.plan_item_id=? AND e.event_type='started' AND e.occurred_at=?
                    AND s.capability_name IN ('task.start','task.resume') AND p.child_id=? AND p.session_id=?
                    AND t.child_id=p.child_id AND t.session_id=p.session_id
                    AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
                    AND pp.child_id=p.child_id AND pp.session_id=p.session_id
                    AND pt.child_id=pp.child_id AND pt.session_id=pp.session_id
                    AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?
                    AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.item_version')=q.item_version''',
                    (item['plan_item_id'], item['started_at'], row['child_id'], row['session_id'], item['plan_item_id'])).fetchall()
                if (len(sources) != 1 or sources[0]['plan_status'] not in {'completed', 'waiting_child'}
                        or sources[0]['step_status'] != 'completed' or sources[0]['tool_status'] != 'completed'):
                    raise ActivityConflict('ACTIVITY_VOICE_ORIGINAL_PROMPT_CHANGED')
                source = sources[0]
                origin_turn_id = source['agent_turn_id']
                start_manifest = json.loads(source['context_manifest_json'])
                binding = start_manifest['goal_prompt_binding']
                if (source['prepare_plan_status'] == 'cancelled' or source['prepare_step_status'] == 'cancelled'
                        or json.loads(source['prepare_manifest']).get('goal_prompt_binding') != binding):
                    raise ActivityConflict('ACTIVITY_VOICE_ORIGINAL_PROMPT_CHANGED')
                if source['capability_name'] == 'task.resume':
                    control_binding = start_manifest.get('voice_control_binding') or {}
                    target = ((control_binding.get('targets') or {}).get('task_resume') or {}).get('item') or {}
                    if (control_binding.get('control') != 'task.resume' or target.get('operation') != 'start'
                            or target.get('plan_item_id') != item['plan_item_id']
                            or target.get('goal_prompt_binding') != binding):
                        raise ActivityConflict('ACTIVITY_VOICE_ORIGINAL_PROMPT_CHANGED')
                TaskStartBinding.resume_prompt(c, row['session_id'], {'goal_prompt_binding': binding})

            class OriginalPromptRepository(StudyRepository):
                def _validate_pet_task_start(self, connection, task, turn_id=None):
                    if connection is not c or task['plan_item_id'] != original['plan_item_id']:
                        raise ActivityConflict('ACTIVITY_VOICE_ORIGINAL_PROMPT_CHANGED')
                    return super()._validate_pet_task_start(connection, task, origin_turn_id)

            try:
                OriginalPromptRepository(self.database).resume_plan_item(
                    original['plan_item_id'], execution_key, connection=c)
            except PlanInputError as error:
                raise ActivityConflict('ACTIVITY_VOICE_ORIGINAL_PROMPT_CHANGED') from error
            receipt = c.execute("SELECT event_id FROM plan_item_event WHERE client_action_id=? AND plan_item_id=? AND event_type='resumed'",
                                (execution_key, original['plan_item_id'])).fetchone()
            if not receipt:
                raise ActivityConflict('ACTIVITY_VOICE_RESUME_RECEIPT_MISSING')
            result = self.outcomes._write(row['child_id'], oid, kind='resume_confirmed', actor='runtime',
                payload={'request_event_id':request['event_id'], 'plan_item_event_id':receipt['event_id']},
                expected_revision=request['revision'], client_action_id=key, connection=c)
            c.commit()
            return self._resume_result(result)

    def resume_and_confirm(self, agent_turn_id):
        return self.resume_original(agent_turn_id)

    @staticmethod
    def _resume_result(result):
        confirmed = result['activity']['resume']['status'] == 'confirmed'
        return {**result, 'evidence':['original_task_resume_receipt'] if confirmed else [],
                'response_text':result['activity']['next_action']['message']}
