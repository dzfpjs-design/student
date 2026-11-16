"""Local safety-only input, independent of unavailable learning references."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from uuid import uuid4

from .agent_runtime import InvalidAgentPlanError, ObjectiveDraft
from .child_safety import classify_child_safety, persist_safety_event
from .repository import utc_now


MARKER = {'version': 'local_voice_recovery_v1', 'mode': 'safety_only'}
FALLBACK = '本机恢复记录异常。这句话没有用于学习或评分；请直接找身边可信任的大人帮忙。'


def _require(valid, reason):
    if not valid:
        raise InvalidAgentPlanError('LOCAL_VOICE_RECOVERY_' + reason)


def _session(c, session_id, child_id=None):
    row = c.execute('''SELECT s.*,p.status AS child_status FROM daily_session s
        JOIN child_profile p USING(child_id) WHERE s.session_id=?''', (session_id,)).fetchone()
    _require(row and row['status'] == row['child_status'] == 'active'
             and (child_id is None or row['child_id'] == child_id), 'SESSION_CHANGED')
    return row


def _response(transcript):
    safety = classify_child_safety(transcript)
    intent, response, original = safety or ('local_recovery_clarification', FALLBACK, {'type': 'none'})
    _require(original['type'] in {'none', 'find_parent', 'find_parent_urgent'}, 'ACTION_FORBIDDEN')
    action = {**original, 'source': 'local_voice_recovery', 'local_only': True,
              'cloud_transmission_allowed': False, 'local_recovery_control': dict(MARKER)}
    return intent, response, action, safety


def assert_request_mode(c, session_id, key, local_recovery_only):
    _require(type(local_recovery_only) is bool, 'MODE_INVALID')
    if local_recovery_only and c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='voice_control_intent_request'").fetchone():
        reserved = c.execute('SELECT 1 FROM voice_control_intent_request WHERE request_key=?',
                             (sha256(('voice-control:' + key).encode()).hexdigest(),)).fetchone()
        _require(not reserved, 'REQUEST_CONFLICT')
    old = c.execute('SELECT session_id,action_json FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
    if old:
        marker = json.loads(old['action_json']).get('local_recovery_control')
        _require(old['session_id'] == session_id and (marker == MARKER) == local_recovery_only
                 and (marker is None or marker == MARKER), 'REQUEST_CONFLICT')


def _validate_turn(c, child_id, session_id, turn_id, manifest):
    from .agent_runtime import AgentRuntimeStore
    _session(c, session_id, child_id)
    row = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                    (turn_id, child_id, session_id)).fetchone()
    _require(row and row['tool_error_code'] != 'SOURCE_DELETED', 'REQUEST_CHANGED')
    action = json.loads(row['action_json'])
    intent, response, expected, _ = _response(row['transcript'])
    _require(manifest.get('local_recovery_control') == MARKER
             and not any(k in manifest for k in ('pending_evaluation', 'voice_control_binding',
                         'local_learning_binding', 'goal_prompt_binding')), 'BINDING_CHANGED')
    frozen = manifest.get('local_recovery_request')
    _require(frozen == {'child_id': child_id, 'session_id': session_id,
        'client_action_id': row['client_action_id'], 'transcript_sha256': sha256(row['transcript'].encode()).hexdigest(),
        'voice_input_id': action.get('voice_input_id')}, 'REQUEST_CHANGED')
    _require(row['intent'] == intent and row['response_text'] == response
             and all(action.get(k) == v for k, v in expected.items())
             and action.get('local_only') is True and action.get('cloud_transmission_allowed') is False
             and not (set(action) - set(expected) - {'voice_input_id', 'voice_input_binding', 'agent_runtime'}),
             'ACTION_CHANGED')
    if action.get('voice_input_id'):
        capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=session_id, child_id=child_id,
            voice_input_id=action['voice_input_id'], transcript=row['transcript'])
        _require(capture.get('local_recovery_only') is True
                 and AgentRuntimeStore.voice_capture_snapshot(capture) == action.get('voice_input_binding'),
                 'CAPTURE_CHANGED')
    else:
        _require('voice_input_binding' not in action, 'CAPTURE_CHANGED')
    return row, action


def validate_plan(connection, *, child_id, session_id, agent_turn_id, decision, context_manifest):
    """Borrowed write transaction validation; no initialization or learning reads."""
    _require(connection.in_transaction, 'TRANSACTION_REQUIRED')
    _, action = _validate_turn(connection, child_id, session_id, agent_turn_id, context_manifest)
    expected_cap = 'conversation.respond' if action['type'] == 'none' else 'safety.find_parent'
    _require(decision.step.capability.name == expected_cap and decision.objective.mode == 'none'
             and decision.objective.objective_type is None and not decision.objective.success_criteria
             and decision.after_execution == 'stop' and not decision.continuation_scope
             and not decision.requires_child_reply, 'PLAN_FORBIDDEN')
    expected_args = {k: v for k, v in action.items() if k not in {
        'type', 'source', 'confidence', 'capability', 'execution_location', 'risk_level',
        'voice_input_id', 'voice_input_binding', 'agent_runtime'}}
    _require(decision.step.arguments == expected_args, 'ARGUMENTS_CHANGED')


class LocalVoiceRecovery:
    def __init__(self, orchestrator):
        self.o = orchestrator

    async def interpret(self, session_id, transcript, key, voice_input_id=None):
        o = self.o
        transcript = transcript.strip()
        _require(bool(transcript), 'TEXT_REQUIRED')
        with o.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            session = _session(c, session_id)
            child = session['child_id']
            assert_request_mode(c, session_id, key, True)
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                o._assert_action_payload(old, session_id, transcript)
                o._assert_replayed_voice(old, voice_input_id)
                self.inspect_execution(c, old['agent_turn_id'], allow_planned=True)
                return o._get_replayed(c, old['agent_turn_id'])
            capture = o.agent_runtime.resolve_captured_voice(c, session_id=session_id, child_id=child,
                voice_input_id=voice_input_id, transcript=transcript) if voice_input_id else None
            _require(capture is None or capture.get('local_recovery_only') is True, 'CAPTURE_MISMATCH')
            intent, response, action, safety = _response(transcript)
            manifest = {'local_recovery_control': dict(MARKER), 'local_recovery_request': {
                'child_id': child, 'session_id': session_id, 'client_action_id': key,
                'transcript_sha256': sha256(transcript.encode()).hexdigest(), 'voice_input_id': voice_input_id}}
            plan = o._deterministic_action_plan(
                owner=o.capability_registry.owner_for_action_type(action['type'], session['state']),
                action=action, response=response)
            plan = replace(plan, objective=ObjectiveDraft('none', None, None, (), 'execute'),
                           requires_child_reply=False, after_execution='stop', continuation_scope=())
            o._freeze_local_voice(c, session_id, child, transcript, capture, action, manifest)
            turn, now = str(uuid4()), utc_now()
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (turn, child, session_id, transcript, intent, response, json.dumps(action, ensure_ascii=False),
                 MARKER['version'], now, key))
            metadata = o.agent_runtime.persist_plan(c, child_id=child, session_id=session_id, agent_turn_id=turn,
                client_action_id=key, decision=plan, context_manifest=manifest)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
                      (json.dumps(metadata), turn))
            if safety:
                persist_safety_event(c, child, session_id, turn, action, now)
            self.inspect_execution(c, turn, allow_planned=True)
            c.commit()
            return o._get(c, turn)

    @staticmethod
    def inspect_execution(c, turn_id, *, allow_planned=False):
        row = c.execute('''SELECT t.child_id,t.session_id,t.action_json,t.tool_status,p.plan_id,p.objective_id,
            p.context_manifest_json,p.plan_json,p.status AS plan_status,s.status AS step_status,
            s.capability_name,s.arguments_json,s.execution_location,s.result_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
            JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0
            AND p.child_id=t.child_id AND p.session_id=t.session_id''', (turn_id,)).fetchone()
        if not row or not json.loads(row['action_json']).get('local_recovery_control'):
            return None
        manifest = json.loads(row['context_manifest_json'])
        turn, action = _validate_turn(c, row['child_id'], row['session_id'], turn_id, manifest)
        plan = json.loads(row['plan_json'])
        cap = 'conversation.respond' if action['type'] == 'none' else 'safety.find_parent'
        _require(row['objective_id'] is None and row['capability_name'] == cap
                 and plan['objective']['mode'] == 'none' and plan.get('after_execution') == 'stop'
                 and not plan.get('continuation_scope')
                 and c.execute('SELECT count(*) FROM agent_step WHERE plan_id=?', (row['plan_id'],)).fetchone()[0] == 1,
                 'PLAN_CHANGED')
        expected_args = {k: v for k, v in action.items() if k not in {
            'type', 'source', 'confidence', 'capability', 'execution_location', 'risk_level',
            'voice_input_id', 'voice_input_binding', 'agent_runtime'}}
        _require(json.loads(row['arguments_json']) == expected_args, 'ARGUMENTS_CHANGED')
        if row['tool_status'] == row['step_status'] == row['plan_status'] == 'completed':
            return {'local_recovery_only': True, 'canonical_response': turn['response_text'],
                'recorded_receipt': {'agent_turn_id': turn_id, 'status': 'completed'}}
        if (row['tool_status'] == 'pending' and row['step_status'] == row['plan_status'] == 'completed'
                and row['execution_location'] == 'server' and cap == 'conversation.respond' and row['result_json']):
            recorded = json.loads(row['result_json'])
            _require(recorded.get('capability') == cap and recorded.get('response_text') == turn['response_text'],
                     'RECEIPT_CHANGED')
            return {'local_recovery_only': True, 'canonical_response': turn['response_text'],
                    'recorded_receipt': {'agent_turn_id': turn_id, 'status': 'completed'}}
        allowed = {'planned', 'running'} if allow_planned else {'running'}
        _require(row['plan_status'] in allowed and row['step_status'] == row['plan_status']
                 and row['tool_status'] == 'pending', 'EXECUTION_CANCELLED')
        return {'local_recovery_only': True, 'canonical_response': turn['response_text']}
