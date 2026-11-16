"""Require a server-bound agent turn for a follow-up Tutor dispatch."""
from __future__ import annotations

import json

from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from .ordinary_learning_text_projection import TextProjectionBlocked
from .tutor_followup_source import snapshot


def _has_tutor_receipt(value, turn_id):
    if type(value) is dict:
        return value.get('tutor_turn_id') == turn_id or any(_has_tutor_receipt(v, turn_id) for v in value.values())
    return type(value) is list and any(_has_tutor_receipt(v, turn_id) for v in value)


def validate(c, *, session_id, child_id, action_id, source_turn_id, question, replay_turn_id=None):
    def require(ok):
        if not ok:
            raise TextProjectionBlocked('tutor_followup_authority_changed')
    require(action_id.endswith('-followup') and len(action_id) > len('-followup'))
    agent_id = action_id[:-len('-followup')]
    row = c.execute('''SELECT t.*,p.plan_id,p.status AS plan_status,p.context_manifest_json,p.plan_json,
        s.status AS step_status,s.capability_name,s.execution_location,s.arguments_json,s.result_json,s.error_code,
        d.status AS session_status,cp.status AS child_status
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s USING(plan_id) JOIN daily_session d ON d.session_id=t.session_id AND d.child_id=t.child_id
        JOIN child_profile cp ON cp.child_id=t.child_id
        WHERE t.agent_turn_id=? AND s.step_index=0''', (agent_id,)).fetchone()
    require(bool(row and row['session_id'] == session_id and row['child_id'] == child_id
                 and row['session_status'] == 'active' and row['child_status'] == 'active'
                 and row['capability_name'] == 'learning.explain' and row['execution_location'] == 'local_model'))
    try:
        action = json.loads(row['action_json']); args = json.loads(row['arguments_json'])
        plan = json.loads(row['plan_json']).get('next_step') or {}
        manifest = json.loads(row['context_manifest_json'])
    except (ValueError, TypeError):
        raise TextProjectionBlocked('tutor_followup_authority_changed')
    binding = action.get('tutor_followup_binding')
    require(bool(action.get('type') == 'ask_tutor' and action.get('question_relation') == 'followup'
                 and args.get('question_relation') == 'followup' and action.get('question') == question
                 and row['transcript'] == question and plan.get('capability') == 'learning.explain'
                 and plan.get('arguments') == args and type(binding) is dict
                 and set(binding) == {'turn_id', 'source_sha256'} and binding['turn_id'] == source_turn_id
                 and manifest.get('tutor_followup_binding') == binding))
    latest = c.execute('SELECT agent_turn_id FROM agent_turn WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                       (session_id, child_id)).fetchone()
    require(bool(latest and latest[0] == agent_id and not row['error_code']))
    try:
        AgentRuntimeStore.assert_turn_source_not_deleted(c, agent_id)
        source = snapshot(c, session_id=session_id, child_id=child_id, turn_id=source_turn_id,
                          replay_turn_id=replay_turn_id)
        require(source['source_sha256'] == binding['source_sha256'])
        native = action.get('source') == 'qwen_realtime_capability'
        if native:
            from .realtime_capability_tools import _hash
            packet = manifest.get('qwen_realtime_capability_v1') or {}
            keys = packet.get('native_manifest_keys') or []
            require('tutor_followup_binding' in keys and packet.get('native_sources_sha256') ==
                    _hash({key: manifest.get(key) for key in keys}))
        if replay_turn_id and row['tool_status'] == 'completed' and row['step_status'] == 'completed':
            require(_has_tutor_receipt(json.loads(row['result_json'] or '{}'), replay_turn_id))
            if native:
                from .realtime_native_client import audit, claim_key, complete_key
                claim, receipt = audit(c, claim_key(agent_id)), audit(c, complete_key(agent_id))
                require(bool(claim and receipt and claim.get('identity_sha256') == _hash(packet.get('identity'))
                             and receipt.get('status') == 'completed'))
            return source
        require(row['tool_status'] == 'pending' and row['plan_status'] == row['step_status']
                and row['plan_status'] in {'planned', 'running', 'waiting_child'})
        if native:
            from .realtime_native_client import require_claim
            require(require_claim(c, agent_id) is True)
        else:
            from .voice_control_intent import VoiceControlIntent
            from .decision_record import validate_current
            from .agent_continuation import ContinuationStore
            require(VoiceControlIntent.scope_valid(c, session_id, child_id, row['client_action_id'], require_cloud=False))
            arrival = c.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?', (row['client_action_id'],)).fetchone()
            if arrival or manifest.get('learning_input_arrival_id'):
                require(bool(arrival and (not manifest.get('learning_input_arrival_id')
                                          or arrival['arrival_id'] == manifest['learning_input_arrival_id'])
                             and (arrival['session_id'], arrival['child_id'], arrival['utterance']) == (session_id, child_id, row['transcript'])))
            else:
                # Typed agent turns have no capture/arrival. Never use this
                # branch to waive evidence for a recorded voice input.
                require(not action.get('voice_input_id'))
            validate_current(c, row['plan_id'])
            require(ContinuationStore.execution_allowed(c, agent_id, running=row['plan_status'] == 'running'))
            if action.get('voice_input_id'):
                capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=session_id, child_id=child_id,
                    voice_input_id=action['voice_input_id'], transcript=row['transcript'])
                require(AgentRuntimeStore.voice_capture_snapshot(capture) == action.get('voice_input_binding'))
        return source
    except (InvalidAgentPlanError, ValueError, TypeError, KeyError) as error:
        raise TextProjectionBlocked('tutor_followup_authority_changed') from error
