"""Local authority for a separate question while the original assessment waits."""
import hashlib
import json
import re


CAPABILITIES = frozenset({'conversation.respond', 'system.inspect_learning', 'learning.explain'})
ROLES = frozenset({'question', 'greeting', 'companionship', 'topic_mention'})
VERSION = 'foreground_independent_dialogue_v1'


def capabilities(packet):
    if 'exam' not in packet:
        return CAPABILITIES
    # A question-shaped input cannot disguise teaching as a plain response
    # during an independent assessment.
    return frozenset({'system.inspect_learning'} if packet.get('role') == 'question' else
                     {'conversation.respond', 'system.inspect_learning'})


def owner_of(packet):
    return next(key for key in ('diagnostic', 'vocabulary', 'exam', 'mistake') if key in packet)


def validate_presentation(answer, presentation):
    """Do not open another answer slot while the original exercise owns it."""
    from .model_gateway import ModelResponseError
    from .tutor_blackboard import presentation_visible_text
    segments = presentation.get('segments', [])
    asks_for_reply = re.compile(r'(?:你(?:能|可以|先|来|试着|试试)[^。！；\n]{0,24}(?:回答|说|做|算|选)|'
                               r'告诉我|轮到你|(?:^|[。！；\n])\s*请(?:你)?(?:回答|说|做|算|选))')
    has_question = any(block.get('kind') == 'question' for segment in segments
                       for block in segment.get('blocks', []))
    visible = answer + '\n' + presentation_visible_text(presentation)
    if has_question or any(mark in visible for mark in ('?', '？')) or asks_for_reply.search(visible):
        raise ModelResponseError('FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED', fallback_eligible=False)


def validate(connection, session_id, child_id, action_id, transcript, packet):
    from .agent_runtime import InvalidAgentPlanError
    from .voice_control_intent import VoiceControlIntent

    def require(value):
        if not value:
            raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_SCOPE_CHANGED')

    require(isinstance(packet, dict) and set(packet) in (
        {'version', 'request_key', 'diagnostic', 'role'}, {'version', 'request_key', 'vocabulary', 'role'},
        {'version', 'request_key', 'exam', 'role'}, {'version', 'request_key', 'mistake', 'role'}))
    owner = owner_of(packet)
    require(packet['version'] == VERSION and packet['role'] in ROLES)
    try:
        VoiceControlIntent.assert_current_input(connection, session_id, child_id, action_id, transcript)
    except ValueError as error:
        raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_SCOPE_CHANGED') from error
    require(packet['request_key'] == hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest())
    receipt = connection.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
                                 (packet['request_key'],)).fetchone()
    fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
    require(receipt and receipt['status'] == 'completed' and receipt['fingerprint'] == fingerprint
            and (receipt['child_id'], receipt['session_id']) == (child_id, session_id))
    require(json.loads(receipt['decision_json']) == {'status': 'non_control', 'role': packet['role'],
            'question_scope': 'independent', 'role_version': VERSION})
    targets = json.loads(receipt['binding_json'])
    require(targets.get('foreground_' + owner if owner in {'exam', 'mistake'} else owner) == packet[owner]
            and not targets.get('pending_evaluation'))
    require(targets.get('foreground_text_consent') == list(VoiceControlIntent.text_consent_version(connection) or ()))
    if owner == 'mistake':
        from .agent_runtime import AgentRuntimeStore
        try:
            AgentRuntimeStore.validate_captured_question(connection, session_id, child_id,
                                                        {'kind': owner, 'binding': packet[owner]})
        except ValueError as error:
            raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_SOURCE_CHANGED') from error
        return
    if owner in {'vocabulary', 'exam'}:
        if owner == 'vocabulary':
            from .pet_vocabulary import PetVocabularyService as SourceService
        else:
            from .exam_simulation import ExamSimulationService as SourceService
        try:
            SourceService.validate_capture_reference(connection, session_id=session_id,
                child_id=child_id, binding=packet[owner])
        except ValueError as error:
            raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_SOURCE_CHANGED') from error
        return
    prompt = connection.execute('SELECT * FROM diagnostic_prompt WHERE origin_session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                                (session_id, child_id)).fetchone()
    require(prompt and prompt['status'] == 'waiting_answer'
            and {key: prompt[key] for key in ('prompt_id', 'item_version')} == packet['diagnostic'])
    from .diagnostic_prompt import DiagnosticPromptService, _version
    item = DiagnosticPromptService._item(connection, child_id, prompt['diagnostic_item_id'])
    from .local_supply import validate_item
    require(item and _version(item) == prompt['item_version'])
    require(not DiagnosticPromptService._scheduled_source_reason(connection, prompt))
    try:
        validate_item(connection, child_id, prompt['diagnostic_item_id'])
    except ValueError as error:
        raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_SOURCE_CHANGED') from error


def validate_turn(connection, turn_id):
    from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
    row = connection.execute('''SELECT t.*,p.context_manifest_json,p.objective_id,p.plan_json,
        p.status AS plan_status,s.status AS step_status,s.capability_name,s.arguments_json,s.capability_version
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0''', (turn_id,)).fetchone()
    packet = json.loads(row['context_manifest_json']).get(VERSION) if row else None
    if not packet:
        return False
    plan = json.loads(row['plan_json'])
    if (row['objective_id'] is not None or row['plan_status'] == 'cancelled' or row['step_status'] == 'cancelled'
            or row['capability_name'] not in capabilities(packet) or plan['objective']['mode'] != 'none'
            or plan.get('requires_child_reply') or plan.get('after_execution', 'stop') != 'stop'
            or plan.get('continuation_scope')):
        raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_PLAN_CHANGED')
    step = plan.get('next_step') or {}
    if (step.get('capability') != row['capability_name'] or step.get('capability_version') != row['capability_version']
            or step.get('arguments') != json.loads(row['arguments_json'])
            or row['capability_name'] == 'learning.explain' and step['arguments'].get('question') != row['transcript']):
        raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_SOURCE_CHANGED')
    validate(connection, row['session_id'], row['child_id'], row['client_action_id'], row['transcript'], packet)
    action = json.loads(row['action_json'])
    if action.get('voice_input_id'):
        capture = AgentRuntimeStore.resolve_captured_voice(connection, session_id=row['session_id'],
            child_id=row['child_id'], voice_input_id=action['voice_input_id'], transcript=row['transcript'])
        owner = owner_of(packet)
        if (AgentRuntimeStore.voice_capture_snapshot(capture) != action.get('voice_input_binding')
                or AgentRuntimeStore.captured_question(capture) != {'kind': owner, 'binding': packet[owner]}):
            raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_VOICE_CHANGED')
    return True


def tutor_request(connection, *, session_id, child_id, action_id, question, subject,
                  prompt_id=None, item_version=None, parent_turn_id=None, ocr_confirmation_id=None, hint_level=None):
    if not action_id.startswith('foreground-question:'):
        return False
    from .pending_evaluation_control import tutor_request as pending_tutor_request
    if pending_tutor_request(connection, session_id=session_id, child_id=child_id, action_id=action_id,
            question=question, subject=subject, prompt_id=prompt_id, item_version=item_version,
            parent_turn_id=parent_turn_id, ocr_confirmation_id=ocr_confirmation_id, hint_level=hint_level):
        return True
    from .agent_runtime import InvalidAgentPlanError
    row = connection.execute('''SELECT agent_turn_id,action_json FROM agent_turn WHERE session_id=? AND child_id=?
        AND json_extract(action_json,'$.tutor_request_action_id')=?''', (session_id, child_id, action_id)).fetchone()
    action = json.loads(row['action_json']) if row else {}
    if (not row or action.get('type') != 'ask_tutor' or action.get('question') != question
            or action.get('subject') != subject or any((prompt_id, item_version, parent_turn_id, ocr_confirmation_id))
            or hint_level is not None and hint_level != action.get('hint_level', 1)
            or not validate_turn(connection, row['agent_turn_id'])):
        raise InvalidAgentPlanError('FOREGROUND_INDEPENDENT_TUTOR_CHANGED')
    return True
