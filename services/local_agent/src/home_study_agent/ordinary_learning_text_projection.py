"""Private ordinary-text provenance. Never a public, audio, or grading grant.

Only a server-minted input record and an actually admitted Tutor request can
qualify this narrow outlet. Unknown legacy restrictions keep their old fence.
All exported data lives in payload; private facts never enter ModelRequest.
"""
from datetime import datetime
import re
import hashlib
import json

INPUT_KEY = '_ordinary_learning_text_input_policy_v1'
TUTOR_KEY = '_ordinary_learning_text_tutor_source_v1'
RECORD_KEY = '_ordinary_learning_text_projection_v1'
RESULT_KEY = '_ordinary_learning_text_result_source_v1'
KIND = 'ordinary_learning_text_projection_v1'
APP_PROMPT = '先别急着说懂了。请不用照着我刚才的话，用自己的话讲一遍关键点。'
CAPS = {'learning.explain', 'learning.evaluate_response', 'learning.request_response'}
ROLE_KEY = '_orchestrator_answer_role_notice_v1'


class TextProjectionBlocked(ValueError):
    pass


def _require(ok, reason):
    if not ok:
        raise TextProjectionBlocked(reason)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _decode(value):
    result = json.loads(value or '{}')
    _require(type(result) is dict, 'source_object_required')
    return result


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.tzinfo is not None, 'source_time_unknown')
    return parsed


def _privacy(c):
    row = c.execute('SELECT cloud_model_text_allowed,updated_at FROM privacy_preference WHERE singleton_id=1').fetchone()
    _require(row is not None and row['cloud_model_text_allowed'] == 1, 'current_text_consent_required')
    from .voice_scope import text_consent_version
    consent = text_consent_version(c)
    _require(consent is None or consent[1] == 1, 'text_consent_withdrawn')
    return {'text_allowed': True, 'updated_at': row['updated_at'], 'consent_sha256': _hash(consent)}


def _known_family_names(c):
    return tuple(sorted({r[0] for r in c.execute('SELECT nickname FROM child_profile')
                         if type(r[0]) is str and r[0]}))


def _family_names_version(c):
    return _hash([dict(r) for r in c.execute('SELECT child_id,nickname,updated_at FROM child_profile ORDER BY child_id')])


def _plain(text, known_names=None):
    from .cloud_privacy import sanitize_cloud_text, sensitive_categories
    _require(type(text) is str and bool(text.strip()), 'text_unavailable')
    names = (known_names,) if type(known_names) is str else tuple(known_names or ())
    result = sanitize_cloud_text(text, names)
    _require(not sensitive_categories(text) and result.redaction_count == 0 and result.text == text,
             'private_text_requires_local_route')
    return text


def _material(value):
    if type(value) is list:
        return any(_material(v) for v in value)
    if type(value) is not dict:
        return False
    if value.get('local_only') is True or value.get('cloud_transmission_allowed') is False:
        return True
    for k, v in value.items():
        if k in {'artifact_id','revision_id','ocr_confirmation_id','document_text','original_page',
                 'stimulus_asset','stimulus_text','local_path','image','audio','document_binding',
                 'page_source','visual','document_goal_root_turn_id','question_target'} and v:
            return True
        if k == 'source_ref' and v not in (None, 'hsc_original_v1'):
            return True
        if _material(v):
            return True
    return False


def _semantic(action):
    # These exact members are transport/history or registered-owner metadata;
    # their removal is used only after the private input record validates.
    skip = {INPUT_KEY, ROLE_KEY, 'local_only','cloud_transmission_allowed','voice_input_id',
            'voice_input_binding','agent_runtime','capability','execution_location','suppress_speech','local_learning_context','risk_level'}
    if action.get('source') == 'pending_answer_clarification':
        skip.add('pending_answer_candidate')
    return {k:v for k,v in action.items() if k not in skip}


def prepare_input_policy(action, decision, *, current_text_authorized, voice_capture, confirmed_answer):
    """Internal pre-envelope snapshot, never accept a snapshot from an API/model."""
    action.pop(INPUT_KEY, None)  # Discard a caller/model lookalike before any private mint.
    if current_text_authorized is not True or not voice_capture:
        return None
    cap = decision.step.capability.name if decision is not None else None
    role = action.get('source') == 'pending_answer_clarification' and action.get('type') == 'none'
    if cap not in CAPS and not role:
        return None
    if role:
        cap = 'candidate_only'
    known_confirmation = cap == 'learning.evaluate_response' and bool(confirmed_answer)
    known_answer = cap == 'learning.evaluate_response' and ((voice_capture.get('question_reference') or {}).get('kind') == 'learning_reply')
    flags = {'local_only': action.get('local_only'), 'cloud_transmission_allowed': action.get('cloud_transmission_allowed')}
    if (flags['local_only'] is True or flags['cloud_transmission_allowed'] is False) and not (role or known_answer):
        return None
    content_action = dict(action)
    if confirmed_answer:
        content_action['answer_source_turn_id'] = confirmed_answer['turn_id']
    semantic = _semantic(content_action)
    if _material(semantic):
        return None
    from .agent_runtime import AgentRuntimeStore
    return {'capability': cap, 'semantic_sha256': _hash(semantic),
            'voice_input_id': voice_capture['voice_input_id'],
            'voice_binding_sha256': _hash(AgentRuntimeStore.voice_capture_snapshot(voice_capture)), 'content_flags': flags,
            'known_confirmation': known_confirmation, 'known_answer_envelope': known_answer, 'text_request_authorized': True}


def _voice(c, turn, voice_id):
    row = c.execute('''SELECT b.*,v.transcript AS original_transcript,v.status AS voice_status,
        v.child_id AS voice_child,v.session_id AS voice_session,a.status AS action_status,
        a.child_id AS action_child,a.session_id AS action_session
        FROM voice_capture_binding b JOIN voice_input v USING(voice_input_id)
        JOIN voice_transcription_action a ON a.client_action_id=b.client_action_id WHERE b.voice_input_id=?''',
        (voice_id,)).fetchone()
    _require(row is not None and row['status'] == row['voice_status'] == row['action_status'] == 'completed',
             'voice_source_unavailable')
    native = row['purpose'] in ('native_cloud_dialogue','household_local_transcript')
    if native:
        from .realtime_native_context import _native_witness
        from types import SimpleNamespace
        actual=c.execute('SELECT * FROM voice_input WHERE voice_input_id=?',(voice_id,)).fetchone()
        if row['purpose']=='household_local_transcript':
            from .realtime_household_input import verify_household_witness
            witnessed=actual is not None and verify_household_witness(c,dict(actual)) is True
        else:
            witnessed=actual is not None and _native_witness(SimpleNamespace(page_source_store=None),c,actual) is not None
        _require(_decode(turn['action_json']).get('source')=='qwen_realtime_capability' and witnessed,
            'native_input_witness_required')
    _require(all(row[k] == turn['child_id'] for k in ('child_id','voice_child','action_child'))
             and all(row[k] == turn['session_id'] for k in ('session_id','voice_session','action_session'))
             and row['original_transcript'] == turn['transcript'] and (row['purpose']=='agent_command' or native)
             and row['source_kind'] in {'local_wav','realtime_transcript'}, 'voice_scope_changed')
    _require(not row['local_recovery_only'], 'voice_local_recovery_only')
    question = _decode(row['question_reference_json']) if row['question_reference_json'] else None
    _require(question is None or question.get('kind') == 'learning_reply' and row['question_validation'] == 'valid',
             'private_or_unknown_question_source')
    fields = ('voice_input_id','client_action_id','child_id','session_id','purpose','created_at',
              'wav_sha256','source_kind','source_model','speech_started_at','question_reference_json',
              'question_validation','original_transcript')
    return {'sha256': _hash({k:row[k] for k in fields}), 'created_at': row['created_at'], 'question': question}


def _work(c, turn_id):
    row = c.execute('''SELECT t.*,p.plan_id,p.objective_id,p.plan_json,p.context_manifest_json,
        s.step_id,s.step_index,s.capability_name,s.capability_version,s.arguments_json,s.result_json,
        s.status AS step_status,p.created_at AS plan_created_at FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0 WHERE t.agent_turn_id=?''', (turn_id,)).fetchone()
    _require(row is not None, 'owned_plan_unavailable')
    return row


def _independent_question_origin(c, row, reference, *, at_birth=False):
    """A fresh explicit original question; old onset is background, not content."""
    if row['capability_name'] != 'learning.explain':
        return None
    plan = _decode(row['plan_json']); args = _decode(row['arguments_json']); raw = row['transcript']
    if (plan.get('objective') or {}).get('mode') != 'new' or args.get('question') != raw:
        return None
    from .agent_orchestrator import AgentOrchestrator
    if (len(raw) < 8 or not AgentOrchestrator._is_explicit_learning_request(raw)
            or re.search(r'刚才|上一|前面|上面|这(?:题|道|个)|原题|原文|重新|再讲|换.{0,3}讲法|继续|纠正|没听懂|不懂|不会|卡住', raw)):
        return None
    goal = c.execute('SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
                     (row['objective_id'],row['child_id'],row['session_id'])).fetchone()
    first = c.execute('SELECT plan_id,agent_turn_id FROM agent_plan WHERE objective_id=? ORDER BY rowid LIMIT 1',
                      (row['objective_id'],)).fetchone()
    if (goal is None or goal['objective_type'] != 'learning' or goal['created_at'] != row['plan_created_at']
            or first is None or first['plan_id'] != row['plan_id'] or first['agent_turn_id'] != row['agent_turn_id']
            or at_birth and goal['last_agent_turn_id'] != row['agent_turn_id']):
        return None
    if at_birth and reference is not None:
        binding = reference.get('binding') or {}
        previous = _work(c,binding.get('source_agent_turn_id'))
        if previous['child_id'] != row['child_id'] or previous['session_id'] != row['session_id']:
            return None
        previous_goal = c.execute('SELECT summary FROM agent_objective WHERE objective_id=?',
                                  (previous['objective_id'],)).fetchone()
        old_args = _decode(previous['arguments_json']); old_result = _decode(previous['result_json'])
        prior_texts = [old_args.get('question'),old_args.get('prompt'),old_args.get('question_asked'),
                       old_result.get('next_check'), (old_result.get('result') or {}).get('next_check')
                       if type(old_result.get('result')) is dict else None,
                       previous_goal['summary'] if previous_goal else None]
        # A new label never turns a known copied question/summary into new input.
        if any(type(v) is str and len(v.strip()) >= 8 and v.strip() in raw for v in prior_texts):
            return None
    return {'rule':'exact_original_explicit_new_question_v1','creator_turn_id':row['agent_turn_id'],
            'plan_id':row['plan_id'],'objective_id':row['objective_id'],'goal_created_at':goal['created_at'],
            'plan_created_at':row['plan_created_at'],'original_question_sha256':_hash(raw),
            'background_reference_sha256':_hash(reference),'no_inherited_material':True}


def mint_input_policy(c, turn_id, seed):
    """Called after original plan persistence inside the original transaction."""
    row = _work(c, turn_id); action = _decode(row['action_json']); action.pop(INPUT_KEY, None)
    try:
        _require(type(seed) is dict and seed.get('text_request_authorized') is True, 'input_not_authorized')
        _require(row['capability_name'] == seed['capability'] or seed['capability'] == 'candidate_only'
                 and row['capability_name'] == 'conversation.respond', 'input_owner_changed')
        _require(seed['semantic_sha256'] == _hash(_semantic(action)) and not _material(_semantic(action)), 'content_policy_changed')
        _require(action.get('local_only') is True and action.get('cloud_transmission_allowed') is False,
                 'capture_history_fence_missing')
        _require(action.get('voice_input_id') == seed['voice_input_id'] and _hash(action.get('voice_input_binding')) == seed['voice_binding_sha256'], 'input_voice_changed')
        privacy = _privacy(c); voice = _voice(c, row, seed['voice_input_id'])
        _plain(row['transcript'], _known_family_names(c))
        _require(_time(privacy['updated_at']) <= _time(voice['created_at']), 'text_consent_newer_than_capture')
        if seed['capability'] in {'learning.evaluate_response','learning.request_response','candidate_only'}:
            _require(voice['question'] is not None, 'ordinary_lesson_source_required')
        args = _decode(row['arguments_json']); plan = _decode(row['plan_json'])
        _require((plan.get('next_step') or {}).get('arguments') == args and not _material(args), 'plan_arguments_or_material_changed')
        proof = {'version':1,'policy':'ordinary_text_input_v1','turn_id':turn_id,'child_id':row['child_id'],
                 'session_id':row['session_id'],'plan_id':row['plan_id'],'step_id':row['step_id'],
                 'capability':seed['capability'],'arguments_sha256':_hash(args),
                 'semantic_sha256':seed['semantic_sha256'],'content_flags':seed['content_flags'],
                 'known_confirmation':seed['known_confirmation'],'known_answer_envelope':seed['known_answer_envelope'],'voice_input_id':seed['voice_input_id'],
                 'voice_fact_sha256':voice['sha256'],'voice_binding_sha256':_hash(action.get('voice_input_binding')),
                 'source_reference':voice['question'],'privacy':privacy,
                 'independent_question_origin':_independent_question_origin(c,row,voice['question'],at_birth=True)}
        action[INPUT_KEY] = proof
    except (TextProjectionBlocked, KeyError, TypeError, ValueError):
        proof = None
    c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',
              (json.dumps(action, ensure_ascii=False), turn_id))
    return proof


def _input(c, row):
    action = _decode(row['action_json']); p = action.get(INPUT_KEY)
    expected = {'version','policy','turn_id','child_id','session_id','plan_id','step_id','capability',
                'arguments_sha256','semantic_sha256','content_flags','known_confirmation','known_answer_envelope','voice_input_id',
                'voice_fact_sha256','voice_binding_sha256','source_reference','privacy','independent_question_origin'}
    _require(type(p) is dict and set(p) == expected and type(p['version']) is int and p['version'] == 1
             and p['policy'] == 'ordinary_text_input_v1', 'private_input_proof_required')
    _require(all(p[k] == row[k] for k in ('child_id','session_id','plan_id','step_id'))
             and p['turn_id'] == row['agent_turn_id'], 'input_proof_scope_changed')
    cap = row['capability_name']; role = p['capability'] == 'candidate_only' and cap == 'conversation.respond'
    _require(p['capability'] == cap and cap in CAPS or role, 'input_capability_changed')
    args = _decode(row['arguments_json']); plan = _decode(row['plan_json'])
    _require((plan.get('next_step') or {}).get('arguments') == args and p['arguments_sha256'] == _hash(args)
             and p['semantic_sha256'] == _hash(_semantic(action)) and not _material(_semantic(action)) and not _material(args), 'input_content_changed')
    _require(action.get('local_only') is True and action.get('cloud_transmission_allowed') is False
             and action.get('voice_input_id') == p['voice_input_id']
             and p['voice_binding_sha256'] == _hash(action.get('voice_input_binding')), 'capture_envelope_changed')
    flags = p['content_flags']; _require(type(flags) is dict and set(flags) == {'local_only','cloud_transmission_allowed'}, 'content_flags_unknown')
    _require(not (flags['local_only'] is True or flags['cloud_transmission_allowed'] is False)
             or role or cap == 'learning.evaluate_response' and p['known_answer_envelope'] is True and (p['source_reference'] or {}).get('kind') == 'learning_reply', 'original_content_local_only')
    voice = _voice(c, row, p['voice_input_id']); privacy = _privacy(c)
    _plain(row['transcript'], _known_family_names(c))
    _require(voice['sha256'] == p['voice_fact_sha256'] and voice['question'] == p['source_reference']
             and privacy == p['privacy'] and _time(privacy['updated_at']) <= _time(voice['created_at']),
             'input_source_or_consent_changed')
    origin = p['independent_question_origin']
    _require(origin is None or origin == _independent_question_origin(c,row,p['source_reference']),
             'independent_question_origin_changed')
    return p


def tutor_policy_snapshot(c):
    return _privacy(c)


def _questions(tutor):
    # Reuse only the original frozen-narration extraction rule, not typed grants.
    envelope = _decode(tutor['output_envelope_json']); board = (envelope.get('result') or {}).get('blackboard') or {}
    segments = board.get('segments')
    if type(segments) is not list or not segments or type(segments[-1]) is not dict:
        return None
    narration = segments[-1].get('narration')
    if type(narration) is not str:
        return None
    question = narration.strip()
    # Keep the entire authority-bearing final utterance. Never choose one of
    # multiple questions or substitute the shorter keyword board block.
    if not question.endswith(('？','?')) or sum(question.count(x) for x in '?？') != 1 or len(question) > 800:
        return None
    return question


def _tutor_version(tutor):
    return _hash({k:tutor[k] for k in ('turn_id','child_id','session_id','subject','question','answer',
        'status','hint_level','parent_turn_id','ocr_confirmation_id','output_envelope_json','client_action_id','created_at')})


def record_tutor_source(c, tutor_turn_id, request, question_target, privacy_snapshot):
    """Actual ModelRequest/accepted Tutor hook; it never mutates that request."""
    tutor = c.execute('SELECT * FROM tutor_turn WHERE turn_id=?', (tutor_turn_id,)).fetchone()
    model = c.execute('SELECT * FROM model_call WHERE call_id=?', (request.request_id,)).fetchone()
    if tutor is None or model is None:
        return None
    manifest = _decode(model['context_manifest_json']); manifest.pop(TUTOR_KEY, None)
    proof = None
    try:
        _require(_privacy(c) == privacy_snapshot and _time(privacy_snapshot['updated_at']) <= _time(model['created_at']), 'tutor_consent_changed')
        _require(model['status'] == 'completed' and model['purpose'] == request.request_type
                 and request.request_type == 'tutor_hint_level_' + str(tutor['hint_level'])
                 and request.child_id == tutor['child_id'] == model['child_id']
                 and request.session_id == tutor['session_id'] == model['session_id'], 'actual_tutor_request_required')
        _require((request.context_manifest.get('privacy_transform') or {}).get('transmitted') is True
                 and (request.context_manifest.get('privacy_transform') or {}).get('raw_audio_or_video') is False,
                 'actual_text_privacy_admission_required')
        _require(not tutor['ocr_confirmation_id'] and not tutor['parent_turn_id']
                 and question_target.get('status') == 'unknown'
                 and question_target.get('reason_code') == 'no_explicit_question_reference'
                 and question_target.get('question_content_cloud_blocked') is False
                 and not question_target.get('binding'), 'private_or_typed_tutor_source')
        envelope = _decode(tutor['output_envelope_json'])
        _require(envelope.get('request_id') == request.request_id and not _material({k:v for k,v in envelope.items() if k != 'local_teaching_control'}), 'tutor_result_local_or_changed')
        names = _known_family_names(c)
        _plain(tutor['question'], names); q = _questions(tutor)
        if q is not None: _plain(q, names)
        proof = {'version':1,'policy':'actual_tutor_text_source_v1','call_id':request.request_id,
                 'tutor_turn_id':tutor_turn_id,'child_id':tutor['child_id'],'session_id':tutor['session_id'],
                 'subject':tutor['subject'],'privacy':privacy_snapshot,'source_sha256':_tutor_version(tutor),
                 'request_messages_sha256':_hash(request.messages),
                 'admitted_privacy_sha256':_hash(request.context_manifest['privacy_transform']),
                 'unique_question_sha256':_hash(q) if q is not None else None,
                 'model_identity':{k:model[k] for k in ('purpose','prompt_version','policy_version','knowledge_version')}}
        manifest[TUTOR_KEY] = proof
    except (TextProjectionBlocked, KeyError, TypeError, ValueError):
        pass
    c.execute('UPDATE model_call SET context_manifest_json=? WHERE call_id=?',
              (json.dumps(manifest,ensure_ascii=False), request.request_id))
    return proof


def _source(c, reference, cache=None):
    cache = {} if cache is None else cache
    _require(type(reference) is dict and reference.get('kind') == 'learning_reply', 'ordinary_reference_required')
    binding = reference.get('binding') or {}; key = binding.get('source_agent_turn_id')
    _require(type(key) is str and key not in cache and len(cache) < 64, 'source_cycle_or_depth')
    cache[key] = None
    row = _work(c,key); _require(row['plan_id'] == binding.get('plan_id') and row['objective_id'] == binding.get('objective_id')
        and row['capability_name'] == binding.get('capability_name') and row['capability_name'] in CAPS
        and row['tool_status'] == row['step_status'] == 'completed', 'completed_source_required')
    policy = _input(c,row); args = _decode(row['arguments_json']); result = _decode(row['result_json'])
    cap = row['capability_name']; lesson = None
    if cap == 'learning.explain':
        if policy['source_reference'] is not None and policy['independent_question_origin'] is None:
            dependency = _source(c,policy['source_reference'],cache)
            _require(dependency['child_id'] == row['child_id'] and dependency['session_id'] == row['session_id'], 'lesson_dependency_changed')
        receipt = result.get('execution_receipt') or {}; output = receipt.get('output') or {}
        tid = output.get('tutor_turn_id')
        tutor = c.execute('SELECT * FROM tutor_turn WHERE turn_id=? AND child_id=? AND session_id=?',
            (tid,row['child_id'],row['session_id'])).fetchone()
        _require(tutor is not None and tutor['status'] == 'completed' and tutor['question'] == args.get('question')
                 and tutor['subject'] == args.get('subject') and receipt.get('capability') == cap, 'owned_tutor_result_required')
        envelope = _decode(tutor['output_envelope_json']); model = c.execute('SELECT * FROM model_call WHERE call_id=?',
            (envelope.get('request_id'),)).fetchone()
        _require(model is not None and model['status'] == 'completed' and model['child_id'] == row['child_id']
                 and model['session_id'] == row['session_id'], 'accepted_tutor_model_required')
        proof = _decode(model['context_manifest_json']).get(TUTOR_KEY)
        expected = {'version','policy','call_id','tutor_turn_id','child_id','session_id','subject','privacy',
                    'source_sha256','request_messages_sha256','admitted_privacy_sha256','unique_question_sha256','model_identity'}
        _require(type(proof) is dict and set(proof) == expected and type(proof['version']) is int and proof['version'] == 1
                 and proof['policy'] == 'actual_tutor_text_source_v1' and proof['call_id'] == model['call_id']
                 and proof['tutor_turn_id'] == tid and proof['source_sha256'] == _tutor_version(tutor)
                 and model['purpose'] == 'tutor_hint_level_' + str(tutor['hint_level'])
                 and proof['model_identity'] == {k:model[k] for k in ('purpose','prompt_version','policy_version','knowledge_version')}
                 and proof['admitted_privacy_sha256'] == _hash(_decode(model['context_manifest_json']).get('privacy_transform'))
                 and proof['privacy'] == _privacy(c) and proof['privacy'] == policy['privacy']
                 and proof['child_id'] == row['child_id'] and proof['session_id'] == row['session_id']
                 and not _material({k:v for k,v in envelope.items() if k != 'local_teaching_control'}), 'tutor_content_proof_changed')
        question = _questions(tutor)
        _require(proof['unique_question_sha256'] == (_hash(question) if question is not None else None), 'tutor_question_version_changed')
        lesson = tutor['question']; model_id = model['call_id']
    elif cap == 'learning.request_response':
        _require(args.get('prompt') == APP_PROMPT and policy['source_reference'] is not None, 'application_question_authority_required')
        prior = _source(c,policy['source_reference'],cache)
        _require(prior['child_id'] == row['child_id'] and prior['session_id'] == row['session_id']
                 and prior['subject'] == args.get('subject'), 'application_lesson_scope_changed')
        lesson = prior['lesson_question']; question = lesson + '\n' + APP_PROMPT
        model_id = prior['model_call_id']
        _require(len(question) <= 800, 'application_question_too_long')
    else:
        execution = c.execute('SELECT * FROM learning_evaluation_execution WHERE step_id=?', (row['step_id'],)).fetchone()
        _require(execution is not None and execution['state'] == 'completed', 'accepted_evaluation_source_required')
        frozen_record = _decode(execution['record_json']); evaluation = _decode(execution['evaluation_json'])
        seal = frozen_record.get(RESULT_KEY)
        _require(type(seal) is dict and set(seal) == {'version','call_id','evaluation_sha256','projection_sha256'}
                 and type(seal['version']) is int and seal['version'] == 1
                 and seal['evaluation_sha256'] == _hash(evaluation), 'evaluation_source_proof_required')
        projection = build_projection(c,frozen_record,check_current=False,cache=cache)
        _require(seal['projection_sha256'] == _hash(projection), 'evaluation_source_projection_changed')
        model = c.execute('SELECT * FROM model_call WHERE call_id=?', (seal['call_id'],)).fetchone()
        _require(model is not None and model['status'] == 'completed' and model['child_id'] == row['child_id']
                 and model['session_id'] == row['session_id'], 'accepted_evaluation_model_required')
        question = evaluation.get('next_check') or None; lesson = projection['local_audit']['lesson_question']; model_id = model['call_id']
    names = _known_family_names(c)
    if question is not None: _plain(question, names)
    _plain(lesson, names)
    descriptor = {'child_id':row['child_id'],'session_id':row['session_id'],'turn_id':key,'plan_id':row['plan_id'],
                  'step_id':row['step_id'],'capability':cap,'capability_version':row['capability_version'],
                  'reference_source_version':binding.get('source_version'),
                  'arguments_sha256':_hash(args),'result_sha256':_hash(result),'policy_sha256':_hash(policy),
                  'question':question,'lesson_question':lesson,'subject':args.get('subject'),'model_call_id':model_id}
    descriptor['version_sha256'] = _hash(descriptor)
    cache.pop(key)
    return descriptor


def source_question_policy(c,pending):
    """A blocked source is distinct from a trusted lesson with no posed check."""
    from .agent_runtime import AgentRuntimeStore
    binding = AgentRuntimeStore.learning_reply_binding(pending)
    if binding is None:
        return {'status':'blocked'}
    try:
        source = _source(c,{'kind':'learning_reply','binding':binding})
    except (TextProjectionBlocked,KeyError,TypeError,ValueError):
        return {'status':'blocked'}
    return {'status':'trusted_question' if source['question'] is not None else 'trusted_lesson_no_question',
            'question':source['question'],'source_version_sha256':source['version_sha256']}


def source_question(c,pending):
    return source_question_policy(c,pending).get('question')


def managed(c, record):
    row = _work(c, record['turn_id'] if 'turn_id' in record else c.execute(
        'SELECT p.agent_turn_id FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE s.step_id=?',
        (record['step_id'],)).fetchone()[0])
    return INPUT_KEY in _decode(row['action_json']) or RECORD_KEY in record


def build_projection(c, record, *, check_current=True, cache=None):
    row = c.execute('SELECT p.agent_turn_id FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE s.step_id=?',
                    (record['step_id'],)).fetchone(); _require(row is not None, 'evaluation_work_missing')
    work = _work(c,row[0]); policy = _input(c,work)
    _require(work['capability_name'] == 'learning.evaluate_response'
             and all(work[k] == record[k] for k in ('child_id','session_id','plan_id','step_id','objective_id')), 'evaluation_scope_changed')
    if check_current:
        from .learning_reply_reservation import validate
        ref = validate(c,work['agent_turn_id'])
        _require(ref == policy['source_reference'], 'current_source_reference_changed')
    else:
        ref = policy['source_reference']
    source = _source(c,ref,cache)
    _require(source['question'] is not None and source['child_id'] == work['child_id']
             and source['session_id'] == work['session_id'], 'unique_authoritative_question_required')
    args = _decode(work['arguments_json']); expected = {'question_asked','response','subject'}
    _require(set(args) == expected and args == {k:record[{'response':'child_response'}.get(k,k)] for k in expected}
             and args['question_asked'] == source['question'] and args['subject'] == source['subject'], 'evaluation_content_changed')
    from .learning_reply_reservation import inputs
    input_rows = inputs(c,work,args['response']); inputs_audit = []
    for item in input_rows:
        owned = _work(c,item['agent_turn_id']); proof = _input(c,owned)
        _require(proof['source_reference'] == ref and proof['privacy'] == policy['privacy'], 'confirmed_input_scope_changed')
        inputs_audit.append({'turn_id':owned['agent_turn_id'],'policy_sha256':_hash(proof)})
    names = _known_family_names(c)
    payload = {k:_plain(v,names) for k,v in {'question_asked':args['question_asked'],
                'child_response':args['response'],'subject':args['subject']}.items()}
    projection = {'kind':KIND,'payload':payload,'local_audit':{'work_turn_id':work['agent_turn_id'],
        'plan_id':work['plan_id'],'step_id':work['step_id'],'privacy':policy['privacy'],
        'source':source,'input_policies':inputs_audit,'lesson_question':source['lesson_question'],
        'payload_sha256':_hash(payload),'known_family_names_sha256':_family_names_version(c)}}
    frozen = record.get(RECORD_KEY)
    _require(frozen is None or frozen == projection, 'frozen_text_projection_changed')
    # Original table-wide checks still inspect all unknown/local material. Only
    # exact validated server-minted capture envelopes get the narrow projection.
    if check_current:
        from .learning_rubric import evaluation_source_boundary
        boundary = evaluation_source_boundary(c,work['child_id'],objective_id=work['objective_id'],
                                             question=source['question'],ordinary_projection=projection)
        _require(not boundary['cloud_evaluator_blocked'], 'objective_material_requires_local_route')
    return projection


def boundary_action(c, turn_id, action, projection):
    """Not a general voice-input exception: proof/scope are required per row."""
    _require(type(projection) is dict and projection.get('kind') == KIND, 'typed_text_projection_required')
    row = _work(c,turn_id); _require(row['child_id'] == projection['local_audit']['source']['child_id']
        and row['session_id'] == projection['local_audit']['source']['session_id'], 'boundary_scope_changed')
    policy = _input(c,row)
    if row['agent_turn_id'] != projection['local_audit']['work_turn_id']:
        if policy['capability'] == 'candidate_only':
            _source(c,policy['source_reference'])
        else:
            _source(c,{'kind':'learning_reply','binding':{'source_agent_turn_id':row['agent_turn_id'],
                'plan_id':row['plan_id'],'objective_id':row['objective_id'],'capability_name':row['capability_name']}})
    _require(_decode(row['action_json']) == action, 'boundary_action_changed')
    return {k:v for k,v in action.items() if k not in {INPUT_KEY,'local_only','cloud_transmission_allowed','voice_input_binding'}}


def seal_evaluation_source(c, record, evaluation, call_id):
    projection = build_projection(c,record)
    record[RECORD_KEY] = projection
    record[RESULT_KEY] = {'version':1,'call_id':call_id,'evaluation_sha256':_hash(evaluation),
                          'projection_sha256':_hash(projection)}


PRIVATE_EXPORT_KEYS = frozenset({INPUT_KEY,TUTOR_KEY,RECORD_KEY,RESULT_KEY,
    '_learning_answer_source_reservation_v1',ROLE_KEY})
STRUCTURED_JSON_COLUMNS = frozenset({'action_json','context_manifest_json','record_json'})


def public_export_copy(value):
    """Remove only six reserved internal keys, only in structured data.

    Do not parse transcript/message/draft strings, even if they look like JSON.
    Leave an unchanged JSON column byte-for-byte unchanged.
    """
    if type(value) is list:
        return [public_export_copy(v) for v in value]
    if type(value) is not dict:
        return value
    result = {}
    for key, item in value.items():
        if key in PRIVATE_EXPORT_KEYS:
            continue
        if key in STRUCTURED_JSON_COLUMNS and type(item) is str:
            try:
                parsed = json.loads(item)
            except (ValueError,TypeError):
                result[key] = item
                continue
            if type(parsed) in (dict,list):
                filtered = public_export_copy(parsed)
                result[key] = item if parsed == filtered else json.dumps(filtered,ensure_ascii=False)
            else:
                result[key] = item
        else:
            result[key] = public_export_copy(item)
    return result


def reject_model_policy_namespaces(value):
    """Model-authored dictionaries cannot mint or imitate private DB policy."""
    if type(value) is dict:
        _require(not (set(value) & PRIVATE_EXPORT_KEYS), 'model_private_policy_namespace_forbidden')
        for item in value.values():
            reject_model_policy_namespaces(item)
    elif type(value) is list:
        for item in value:
            reject_model_policy_namespaces(item)
