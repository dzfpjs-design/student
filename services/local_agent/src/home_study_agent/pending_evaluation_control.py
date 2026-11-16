"""A restrictive control lane, never permission to submit or evaluate an answer."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError, ObjectiveDraft, PlannedStep
from .repository import utc_now

VERSION = 'pending_evaluation_control_v1'
INDEPENDENT_VERSION = 'pending_evaluation_independent_v1'
QUERY_CONTROLS = {
    'evaluation.status': ('none', {}),
    'device.status': ('query_device_status', {'device': 'all'}),
    'planning.describe': ('describe_plan', {}),
    'system.inspect_learning': ('inspect_learning_system', {'scope': 'requests', 'track': 'all'}),
    'parent.request': ('find_parent', {}),
}
ALLOWED_CONTROLS = frozenset({
    *QUERY_CONTROLS, 'task.pause', 'task.resume', 'exam.pause', 'exam.resume',
    'break.begin', 'break.resume', 'speech.stop', 'speech.repeat',
    'observation.pause', 'observation.resume', 'document.pause', 'document.resume',
    'tutor.pause', 'tutor.resume', 'tutor.previous', 'tutor.restart', 'tutor.cancel',
    'pet_audio.pause', 'pet_audio.resume', 'pet_audio.restart', 'pet_audio.cancel',
    'companion.start_activity', 'companion.activity_feedback',
})


def _reject(reason):
    raise InvalidAgentPlanError('PENDING_EVALUATION_CONTROL_' + reason)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def marker(binding):
    return {'version': VERSION, 'evaluation_turn_id': binding['evaluation_turn_id'], 'mode': 'control_only'}


def _safety_binding(c, session_id, child_id, turn_id, *, purpose='local_safety'):
    # Safety assistance uses only owned request identity, not withdrawn learning
    # material or permission to resume the original evaluation.
    row = c.execute('''SELECT s.step_id,p.plan_id FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) JOIN daily_session ds ON ds.session_id=t.session_id
        JOIN child_profile cp ON cp.child_id=t.child_id
        WHERE t.agent_turn_id=? AND t.session_id=? AND t.child_id=? AND p.child_id=t.child_id
        AND p.session_id=t.session_id AND ds.child_id=t.child_id AND ds.status='active' AND cp.status='active'
        AND s.step_index=0 AND s.capability_name='learning.evaluate_response' ''',
        (turn_id,session_id,child_id)).fetchone()
    if not row:
        _reject('REFERENCE_UNAVAILABLE')
    return {'version':VERSION,'purpose':purpose,'evaluation_turn_id':turn_id,'step_id':row['step_id'],
            'child_id':child_id,'session_id':session_id,'source_version':_hash([row['plan_id'],row['step_id']])}


def validate_pending_evaluation_reference(c, session_id, child_id, turn_id, *, question_reference=None):
    """Borrowed read only. Return identities/hashes, never question/answer text."""
    from .learning_outcome_evaluator import LearningOutcomeEvaluator, InvalidLearningEvaluationError
    from .voice_capture_binding import normalize_question_reference
    if not isinstance(turn_id, str) or not turn_id.strip() or len(turn_id) > 128:
        _reject('REFERENCE_INVALID')
    row = c.execute('''SELECT t.transcript,t.action_json,t.tool_error_code,p.plan_id,p.status AS plan_status,
        p.context_manifest_json,s.step_id,s.status AS step_status,s.error_code,s.arguments_json,
        e.record_json,e.state FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) JOIN learning_evaluation_execution e USING(step_id)
        JOIN daily_session ds ON ds.session_id=t.session_id JOIN child_profile cp ON cp.child_id=t.child_id
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND p.child_id=t.child_id
        AND p.session_id=t.session_id AND ds.child_id=t.child_id AND ds.status='active' AND cp.status='active'
        AND s.step_index=0 AND s.capability_name='learning.evaluate_response' ''', (turn_id, child_id, session_id)).fetchone()
    if not row:
        _reject('REFERENCE_UNAVAILABLE')
    if row['plan_status'] == 'cancelled' or row['step_status'] == 'cancelled' or 'SOURCE_DELETED' in (row['error_code'], row['tool_error_code']):
        _reject('REFERENCE_CANCELLED')
    try:
        record, arguments = json.loads(row['record_json']), json.loads(row['arguments_json'])
        if (record['step_id'] != row['step_id'] or record['child_id'] != child_id or record['session_id'] != session_id
                or record.get('plan_id') != row['plan_id']
                or record['child_response'] != arguments.get('response')
                or record.get('subject') != arguments.get('subject')
                or record.get('source_evidence_id') != arguments.get('evidence_id')
                or record.get('question_asked') != (str(arguments.get('question_asked') or '').strip()[:800] or None)):
            _reject('SOURCE_CHANGED')
        LearningOutcomeEvaluator._assert_not_erased(c, record)
    except (ValueError, KeyError, TypeError, InvalidLearningEvaluationError):
        _reject('SOURCE_CHANGED')
    reference, legacy_source = None, None
    evidence_id = arguments.get('evidence_id')
    if evidence_id:
        from .diagnostic_prompt import DiagnosticPromptService, _version
        from .learning_obligations import withdrawn_evidence_ids
        rows = c.execute('''SELECT p.* FROM diagnostic_prompt p JOIN diagnostic_attempt a ON a.attempt_id=p.attempt_id
            JOIN learning_evidence e ON e.evidence_id=a.evidence_id
            WHERE a.evidence_id=? AND a.child_id=? AND a.session_id=? AND p.child_id=a.child_id
            AND p.origin_session_id=a.session_id AND p.diagnostic_item_id=a.diagnostic_item_id
            AND e.child_id=a.child_id AND e.session_id=a.session_id AND a.submitted_answer=?''',
            (evidence_id, child_id, session_id, record['child_response'])).fetchall()
        if evidence_id in withdrawn_evidence_ids(c, child_id):
            _reject('SOURCE_CHANGED')
        if not rows:
            # Older real local evaluations have an attempt and a frozen rubric,
            # not a typed prompt. The rubric snapshot is not grading permission.
            from .learning_rubric import RubricEvidenceService
            snapshot = c.execute('SELECT * FROM rubric_assessment WHERE step_id=? AND child_id=?',
                                 (row['step_id'],child_id)).fetchone()
            payload = json.loads(snapshot['payload_json']) if snapshot else {}
            item_id = (payload.get('material') or {}).get('diagnostic_item_id')
            if (not snapshot or payload.get('source_evidence_id') != evidence_id
                    or payload.get('session_id') != session_id
                    or not item_id or not DiagnosticPromptService._item(c,child_id,item_id)
                    or not RubricEvidenceService._source_current(c,snapshot,payload)):
                _reject('SOURCE_CHANGED')
            legacy_source = [snapshot['assessment_id'],snapshot['fingerprint'],payload['source_evidence_version_hash']]
        elif len(rows) == 1:
            prompt = rows[0]
            item = DiagnosticPromptService._item(c, child_id, prompt['diagnostic_item_id'])
            if (prompt['status'] != 'answered' or not item or _version(item) != prompt['item_version']
                    or DiagnosticPromptService._scheduled_source_reason(c, prompt)):
                _reject('SOURCE_CHANGED')
            reference = {'kind':'diagnostic', 'binding':{'prompt_id':prompt['prompt_id'], 'item_version':prompt['item_version']}}
        else:
            _reject('SOURCE_CHANGED')
    if question_reference is not None:
        try:
            captured = normalize_question_reference(question_reference)
        except ValueError:
            _reject('QUESTION_MISMATCH')
        if captured != reference:
            _reject('QUESTION_MISMATCH')
    return {'version': VERSION, 'evaluation_turn_id': turn_id, 'step_id':row['step_id'],
        'child_id':child_id, 'session_id':session_id, 'question_reference':reference,
        'source_version':_hash([row['plan_id'], row['step_id'], row['transcript'], arguments,
                               json.loads(row['context_manifest_json']), reference, legacy_source])}


def assert_request_mode(c, session_id, key, evaluation_turn_id):
    old = c.execute('SELECT session_id,action_json FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
    if old:
        saved = (json.loads(old['action_json']).get('pending_evaluation_control') or {}).get('evaluation_turn_id')
        if old['session_id'] != session_id or saved != evaluation_turn_id:
            _reject('REQUEST_CONFLICT')
    journal = c.execute('SELECT binding_json FROM voice_control_intent_request WHERE request_key=?',
                        (hashlib.sha256(('voice-control:' + key).encode()).hexdigest(),)).fetchone()
    if journal:
        saved = (json.loads(journal[0]).get('pending_evaluation') or {}).get('evaluation_turn_id')
        if saved != evaluation_turn_id:
            _reject('REQUEST_CONFLICT')


def revalidate(c, binding, *, question_reference=None, require_consent=True):
    from .voice_control_intent import VoiceControlIntent
    if binding.get('purpose') in {'local_safety','local_document_stop'}:
        if _safety_binding(c,binding['session_id'],binding['child_id'],binding['evaluation_turn_id'],purpose=binding['purpose']) != binding:
            _reject('BINDING_CHANGED')
        return
    actual = validate_pending_evaluation_reference(c, binding['session_id'], binding['child_id'],
        binding['evaluation_turn_id'], question_reference=question_reference)
    if actual != {k:v for k,v in binding.items() if k != 'consent_version'}:
        _reject('SOURCE_CHANGED')
    if require_consent and list(VoiceControlIntent.text_consent_version(c) or ()) != binding['consent_version']:
        _reject('CONSENT_CHANGED')


def validate_independent(c, session_id, child_id, key, transcript, manifest, *, decision=None):
    from .voice_control_intent import VoiceControlIntent
    packet = manifest.get(INDEPENDENT_VERSION)
    binding = manifest.get('pending_evaluation')
    if (not isinstance(packet, dict) or set(packet) != {'request_key', 'role'} or not binding
            or packet['role'] not in {'question', 'greeting', 'companionship', 'topic_mention'}
            or binding['child_id'] != child_id or binding['session_id'] != session_id):
        _reject('INDEPENDENT_BINDING_CHANGED')
    revalidate(c, binding)
    VoiceControlIntent.assert_current_input(c, session_id, child_id, key, transcript)
    expected_key = hashlib.sha256(('voice-control:' + key).encode()).hexdigest()
    journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (expected_key,)).fetchone()
    fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
    expected = {'status': 'non_control', 'role': packet['role'], 'question_scope': 'independent',
                'role_version': INDEPENDENT_VERSION}
    if (packet['request_key'] != expected_key or not journal or journal['status'] != 'completed'
            or journal['fingerprint'] != fingerprint or (journal['child_id'], journal['session_id']) != (child_id, session_id)
            or json.loads(journal['decision_json']) != expected
            or json.loads(journal['binding_json']).get('pending_evaluation') != binding):
        _reject('INDEPENDENT_JOURNAL_CHANGED')
    if decision and (decision.step.capability.name not in independent_capabilities(packet['role'])
            or decision.objective.mode != 'none' or decision.requires_child_reply
            or decision.after_execution != 'stop' or decision.continuation_scope):
        _reject('INDEPENDENT_PLAN_CHANGED')
    if (decision and decision.step.capability.name == 'learning.explain'
            and c.execute("SELECT 1 FROM specialized_exam_run WHERE child_id=? AND session_id=? AND status='active' LIMIT 1",
                          (child_id, session_id)).fetchone()):
        _reject('INDEPENDENT_TUTOR_CHANGED')


def independent_capabilities(role):
    return {'system.inspect_learning', 'learning.explain'} if role == 'question' else {'conversation.respond'}


def validate_tutor_scope(c, row, action, step):
    from .learning_system_facts import detect_learning_system_query
    if (step['arguments'].get('question') != row['transcript'] or action.get('question') != row['transcript']
            or action.get('type') != 'ask_tutor' or action.get('subject') != str(step['arguments'].get('subject', ''))[:40]
            or action.get('hint_level') != min(6, max(1, int(step['arguments'].get('support_level', 6))))
            or action.get('tutor_request_action_id') != 'foreground-question:' + hashlib.sha256(row['client_action_id'].encode()).hexdigest()
            or any(action.get(key) for key in ('diagnostic_prompt_id', 'diagnostic_item_version',
                                               'ocr_confirmation_id', 'parent_turn_id'))
            or detect_learning_system_query(row['transcript'], ())
            or c.execute("SELECT 1 FROM specialized_exam_run WHERE child_id=? AND session_id=? AND status='active' LIMIT 1",
                         (row['child_id'], row['session_id'])).fetchone()):
        _reject('INDEPENDENT_TUTOR_CHANGED')


def tutor_request(c, *, session_id, child_id, action_id, question, subject,
                  prompt_id=None, item_version=None, parent_turn_id=None, ocr_confirmation_id=None, hint_level=None):
    row = c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
        s.status AS step_status,s.capability_name,s.arguments_json,s.capability_version
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.session_id=? AND t.child_id=? AND s.step_index=0
        AND json_extract(t.action_json,'$.tutor_request_action_id')=?''', (session_id, child_id, action_id)).fetchone()
    manifest = json.loads(row['context_manifest_json']) if row else {}
    if INDEPENDENT_VERSION not in manifest:
        return False
    action = json.loads(row['action_json'])
    if (row['capability_name'] != 'learning.explain' or action.get('question') != question
            or action.get('subject') != subject or any((prompt_id, item_version, parent_turn_id, ocr_confirmation_id))
            or hint_level is not None and hint_level != action.get('hint_level')):
        _reject('INDEPENDENT_TUTOR_CHANGED')
    inspect_bound_execution(c, row, manifest, allow_planned=True)
    return True


def inspect_bound_execution(c, row, manifest, *, allow_planned=False):
    binding = manifest.get('pending_evaluation')
    if not binding:
        return
    action = json.loads(row['action_json'])
    if (binding['child_id'] != row['child_id'] or binding['session_id'] != row['session_id']
            or action.get('pending_evaluation_control') != marker(binding)):
        _reject('BINDING_CHANGED')
    local = manifest.get('pending_local_control')
    if binding.get('purpose') == 'local_safety' and local != 'child_safety':
        _reject('CAPABILITY_FORBIDDEN')
    if binding.get('purpose') == 'local_document_stop' and (
            local != 'document_stop' or (manifest.get('voice_control_binding') or {}).get('control') != 'document.pause'):
        _reject('CAPABILITY_FORBIDDEN')
    revalidate(c, binding, require_consent=local is None)
    if INDEPENDENT_VERSION in manifest:
        from .agent_runtime import AgentRuntimeStore
        validate_independent(c, row['session_id'], row['child_id'], row['client_action_id'], row['transcript'], manifest)
        plan_row = c.execute('SELECT objective_id,plan_json FROM agent_plan WHERE agent_turn_id=?', (row['agent_turn_id'],)).fetchone()
        plan = json.loads(plan_row['plan_json'])
        step = plan['next_step']
        if (local is not None or plan_row['objective_id'] is not None or plan['objective']['mode'] != 'none'
                or plan.get('requires_child_reply') or plan.get('after_execution') != 'stop' or plan.get('continuation_scope')
                or row['capability_name'] not in independent_capabilities(manifest[INDEPENDENT_VERSION]['role'])
                or step['capability'] != row['capability_name'] or step['arguments'] != json.loads(row['arguments_json'])
                or manifest.get('voice_control_binding')):
            _reject('INDEPENDENT_PLAN_CHANGED')
        frozen_voice = manifest.get('voice_input_binding')
        if (not isinstance(frozen_voice, dict) or not frozen_voice.get('voice_input_id')
                or action.get('voice_input_id') != frozen_voice['voice_input_id']
                or action.get('voice_input_binding') != frozen_voice):
            _reject('CAPTURE_MISMATCH')
        capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=row['session_id'], child_id=row['child_id'],
            voice_input_id=frozen_voice['voice_input_id'], transcript=row['transcript'])
        if (capture.get('pending_evaluation_turn_id') != binding['evaluation_turn_id']
                or AgentRuntimeStore.voice_capture_snapshot(capture) != frozen_voice):
            _reject('CAPTURE_MISMATCH')
        if row['plan_status'] == 'cancelled' or row['step_status'] == 'cancelled':
            _reject('REFERENCE_CANCELLED')
        if row['capability_name'] == 'learning.explain':
            validate_tutor_scope(c, row, action, step)
        return {'pending_evaluation_control': marker(binding)}
    control = (manifest.get('voice_control_binding') or {}).get('control')
    if local not in {None,'child_safety','playback_overlap','document_stop'}:
        _reject('CAPABILITY_FORBIDDEN')
    if local == 'child_safety':
        from .child_safety import classify_child_safety
        safety = classify_child_safety(row['transcript'])
        expected_cap = {'none':'conversation.respond','find_parent':'safety.find_parent',
                        'find_parent_urgent':'safety.find_parent'}
        if (not safety or control is not None or row['intent'] != safety[0]
                or row['response_text'] != safety[1]
                or any(action.get(k) != v for k,v in safety[2].items())
                or row['capability_name'] != expected_cap[safety[2]['type']]):
            _reject('CAPABILITY_FORBIDDEN')
        expected_args = {k:v for k,v in action.items() if k not in {
            'type','source','confidence','capability','execution_location','risk_level',
            'voice_input_id','voice_input_binding','agent_runtime'}}
        if json.loads(row['arguments_json']) != expected_args:
            _reject('BINDING_CHANGED')
        result = {'pending_evaluation_control':marker(binding),'canonical_response':safety[1]}
        if row['tool_status'] == row['step_status'] == row['plan_status'] == 'completed':
            return {**result,'recorded_receipt':{'agent_turn_id':row['agent_turn_id'],'status':'completed'}}
        allowed = {'planned','running'} if allow_planned else {'running'}
        if (row['plan_status'] not in allowed or row['step_status'] != row['plan_status']
                or row['tool_status'] != 'pending'):
            _reject('EXECUTION_NOT_ADMITTED')
        return result
    elif control is None:
        if row['capability_name'] != 'conversation.respond' or action.get('type') != 'none':
            _reject('CAPABILITY_FORBIDDEN')
    elif control not in ALLOWED_CONTROLS:
        _reject('CAPABILITY_FORBIDDEN')


def _activity_targets(c, session_id, child_id):
    from .companion_activity_outcome import CompanionActivityOutcomeService
    candidates, active, paused_tasks = [], [], []
    for row in c.execute('SELECT outcome_id,source_snapshot_json FROM companion_activity_outcome WHERE child_id=? AND session_id=?',
                         (child_id, session_id)).fetchall():
        activity = CompanionActivityOutcomeService._detail(c, child_id, row['outcome_id'])['activity']
        if not activity['source_valid'] or activity['resume']['status'] in {'confirmed','deferred'}:
            continue
        source = json.loads(row['source_snapshot_json'])
        target = {'outcome_id':row['outcome_id'],'expected_revision':activity['revision'],'child_id':child_id,'session_id':session_id}
        candidates.append({'binding':target,'label':str(source.get('topic') or source.get('title') or activity['source_kind'])[:120]})
        if activity['lifecycle']['status']=='active':
            active.append(target)
        if source.get('paused_plan_item_id'):
            paused_tasks.append(source['paused_plan_item_id'])
    selected = active[0] if len(active)==1 else candidates[0]['binding'] if len(candidates)==1 else None
    return {'status':'ready' if selected else 'ambiguous' if candidates else 'missing',
            'binding':selected,'candidates':candidates,'selection':None}, sorted(set(paused_tasks))


class PendingEvaluationControl:
    def __init__(self, orchestrator):
        self.o = orchestrator

    async def interpret(self, session_id, transcript, key, evaluation_turn_id, *, voice_input_id=None, playback_overlap_id=None,
                        cloud_text_allowed=True):
        from .child_safety import classify_child_safety
        from .voice_playback import normalized_speech
        from .voice_control_intent import VoiceControlIntent
        from .task_start_binding import TaskStartBinding
        from .break_control import BreakControl
        from .repository import PlanInputError
        o = self.o
        with o.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            session = c.execute('SELECT * FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if not session:
                _reject('REFERENCE_UNAVAILABLE')
            child = session['child_id']
            capture = o.agent_runtime.resolve_captured_voice(c, session_id=session_id, child_id=child,
                voice_input_id=voice_input_id, transcript=transcript) if voice_input_id else None
            if capture and capture.get('pending_evaluation_turn_id') != evaluation_turn_id:
                _reject('CAPTURE_MISMATCH')
            question = o.agent_runtime.captured_question(capture) if capture else None
            safety = classify_child_safety(transcript)
            local_stop = VoiceControlIntent.direct_document_stop(transcript)
            if safety:
                binding = _safety_binding(c,session_id,child,evaluation_turn_id)
            elif local_stop:
                binding = _safety_binding(c,session_id,child,evaluation_turn_id,purpose='local_document_stop')
            else:
                binding = validate_pending_evaluation_reference(c, session_id, child, evaluation_turn_id, question_reference=question)
                binding['consent_version'] = list(VoiceControlIntent.text_consent_version(c) or ())
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                o._assert_action_payload(old, session_id, transcript)
                o._assert_replayed_voice(old, voice_input_id)
                manifest = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                     (old['agent_turn_id'],)).fetchone()
                frozen = json.loads(manifest[0]).get('pending_evaluation') if manifest else None
                if not frozen:
                    _reject('BINDING_CHANGED')
                revalidate(c, frozen, question_reference=question,
                           require_consent=not json.loads(manifest[0]).get('pending_local_control'))
                return o._get_replayed(c, old['agent_turn_id'])
            if safety:
                return self._persist_safety(c, session, transcript, key, binding, capture, safety)
            if local_stop:
                arrival = o.learning_input_revisions.capture_arrival(session_id,key,transcript,connection=c)
                monitor = c.execute('SELECT monitor_id FROM document_monitor_session WHERE session_id=? AND child_id=?',
                                    (session_id,child)).fetchone()
                decision = o.voice_control_intent.accept_document_stop(session_id,child,key,transcript,
                    {'pending_evaluation':binding,'document_stop':{'monitor_id':monitor[0] if monitor else None}},connection=c)
                c.commit()
                return o._persist_voice_control(session_id,child,session['state'],transcript,key,
                    arrival['arrival_id'],decision,capture)
            recent = c.execute('''SELECT response_text FROM agent_turn WHERE child_id=? AND session_id=?
                AND julianday(created_at)>=julianday(?) ORDER BY created_at DESC LIMIT 8''',
                (child,session_id,(datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat())).fetchall()
            heard = normalized_speech(transcript)
            # Matching text is only an ambiguity signal, never acoustic proof.
            overlap = bool(playback_overlap_id and o.voice_playback.matches(session_id,playback_overlap_id,transcript))
            overlap = overlap or (len(heard)>=16 and any(heard in normalized_speech(r[0] or '') for r in recent))
            if overlap:
                c.commit()
                return o._persist_overlap_clarification(session_id,child,session['state'],transcript,key,
                    playback_overlap_id,capture,pending_evaluation=binding)
            arrival = o.learning_input_revisions.capture_arrival(session_id, key, transcript, connection=c)
            tasks = c.execute('''SELECT pi.plan_item_id,pi.plan_id,dp.version FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active' AND pi.status='active' ''', (session_id, child)).fetchall()
            targets = {'pending_evaluation':binding, 'task':dict(tasks[0]) if len(tasks)==1 else None,
                'task_resume':TaskStartBinding.resume_snapshot(c, session_id, child, question), 'diagnostic':None,
                'tutor':None, 'pet_listening':None, 'exam_run_id':None}
            targets['companion_activity'], targets['companion_paused_tasks'] = _activity_targets(c, session_id, child)
            focus = (capture or {}).get('activity_reference')
            if focus and capture.get('activity_validation') == 'valid':
                try:
                    VoiceControlIntent.validate_activity_focus(c,session_id,child,focus)
                    targets['activity_focus'] = focus
                except ValueError:
                    pass
            exams = c.execute("SELECT run_id,status FROM specialized_exam_run WHERE session_id=? AND child_id=? AND status IN ('active','paused')",
                              (session_id,child)).fetchall()
            if len(exams)==1:
                targets['exam_run_id'] = exams[0]['run_id']
            playbacks = c.execute("SELECT playback_id,source_version FROM tutor_playback_checkpoint WHERE session_id=? AND child_id=? AND status IN ('playing','paused')",
                                  (session_id,child)).fetchall() if c.execute(
                                      "SELECT 1 FROM sqlite_master WHERE name='tutor_playback_checkpoint'").fetchone() else []
            if len(playbacks)==1:
                targets['tutor'] = dict(playbacks[0])
            media = o.agent_runtime.capture_pet_listening_arrival(c, arrival['arrival_id'])
            targets['pet_listening'] = media.get('binding')
            try:
                targets['break'] = BreakControl.snapshot(c, session_id, child)
            except PlanInputError:
                targets['break'] = None
            names = [r[0] for r in c.execute('SELECT nickname FROM child_profile').fetchall()]
            origin = c.execute('SELECT arguments_json FROM agent_step WHERE step_id=?', (binding['step_id'],)).fetchone()
            args = json.loads(origin[0])
            c.commit()
        def guard(connection):
            revalidate(connection, binding, question_reference=question)
        state = {name:False for name in ('has_pending_question','has_active_exam','has_paused_exam',
            'has_active_task','has_tutor_playback','has_pet_listening','is_study_phase','is_break_phase')}
        state.update(has_pending_evaluation=True, has_pending_question=question is not None,
            has_foreground_independent_dialogue=bool(capture and not focus),
            has_active_task=bool(targets['task']), has_active_exam=len(exams)==1 and exams[0]['status']=='active',
            has_paused_exam=len(exams)==1 and exams[0]['status']=='paused',
            has_tutor_playback=bool(targets['tutor']), has_pet_listening=bool(targets['pet_listening']),
            has_companion_activity=targets['companion_activity']['status'] in {'ready','ambiguous'},
            has_active_activity_focus=bool(targets.get('activity_focus')),
            is_study_phase=session['state']=='study', is_break_phase=session['state']=='break',
            has_paused_task=any(t.get('operation')=='resume' for t in targets['task_resume']['items']),
            has_pending_task_start=any(t.get('operation')=='start' for t in targets['task_resume']['items']))
        consent = binding['consent_version']
        if focus and not targets.get('activity_focus'):
            decision = {'status':'clarify','reason':'CONTROL_ACTIVITY_FOCUS_CHANGED','binding':targets}
        else:
            decision = await o.voice_control_intent.resolve(session_id=session_id, child_id=child, action_id=key,
                transcript=transcript, state=state, local_sources=[args.get('response'),args.get('question_asked')],
                names=names, allowed=bool(cloud_text_allowed and consent and consent[1] and consent[3]=='parent_ui'), binding=targets,
                voice_capture=capture, pre_dispatch=guard)
        with o.database.connect() as c:
            c.execute('BEGIN')
            guard(c)
        if decision['status']=='control' and decision['control'] in ALLOWED_CONTROLS:
            return o._persist_voice_control(session_id, child, session['state'], transcript, key,
                arrival['arrival_id'], decision, capture)
        if focus:
            return await o._persist_activity_reply(session_id,child,session['state'],transcript,key,
                arrival['arrival_id'],decision,capture,names,[args.get('response'),args.get('question_asked')],
                guard,pending_evaluation=binding)
        if (capture and decision.get('status') == 'non_control' and decision.get('question_scope') == 'independent'
                and decision.get('role_version') == INDEPENDENT_VERSION):
            independent = await self._persist_independent(session, transcript, key, arrival['arrival_id'],
                decision, capture, names, args, binding, guard)
            if independent:
                return independent
        response = '原作答评价记录已保留。这句话没有作为答案提交；可以查询评价状态，或说明要操作哪个设备、暂停哪项任务。'
        with o.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            guard(c)
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                o._assert_action_payload(old, session_id, transcript)
                o._assert_replayed_voice(old, voice_input_id)
                return o._get_replayed(c, old['agent_turn_id'])
            turn = str(uuid4())
            action = {'type':'none','source':'pending_answer_clarification','local_only':True,
                'cloud_transmission_allowed':False,'pending_evaluation_control':marker(binding)}
            manifest = {'learning_input_arrival_id':arrival['arrival_id'], 'pending_evaluation':binding}
            plan = o._deterministic_action_plan(owner=o.capability_registry.owner_for_action_type('none',session['state']),
                action=action,response=response)
            plan = replace(plan, objective=ObjectiveDraft('none',None,None,(),'execute'), after_execution='stop',continuation_scope=())
            o._freeze_local_voice(c,session_id,child,transcript,capture,action,manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (turn,child,session_id,transcript,'pending_evaluation_clarification',response,json.dumps(action),VERSION,utc_now(),key))
            o.agent_runtime.persist_plan(c,child_id=child,session_id=session_id,agent_turn_id=turn,
                client_action_id=key,decision=plan,context_manifest=manifest)
            c.commit()
            return o._get(c,turn)

    async def _persist_independent(self, session, transcript, key, arrival_id, semantic, capture, names, args, binding, guard):
        o = self.o
        if not o.voice_control_intent.text_eligible(transcript, names, [args.get('response'), args.get('question_asked')]):
            return None
        sid, child = session['session_id'], session['child_id']
        local_manifest = {'learning_input_arrival_id': arrival_id, 'pending_evaluation': binding,
            INDEPENDENT_VERSION: {'request_key': semantic['request_key'], 'role': semantic['role']}}

        def authorize():
            with o.database.connect() as c:
                c.execute('BEGIN')
                guard(c)
                validate_independent(c, sid, child, key, transcript, local_manifest)

        with o.database.connect() as c:
            active_exam = bool(c.execute("SELECT 1 FROM specialized_exam_run WHERE child_id=? AND session_id=? AND status='active' LIMIT 1",
                                        (child, sid)).fetchone())
        planned = await o._resolve_agent_plan(session_id=sid, child_id=child, transcript=transcript,
            known_child_names=names, recent_dialogue=(), require_utterance_binding=True,
            independent_document_dialogue=True, foreground_independent_dialogue=True,
            pending_evaluation_dialogue=True, foreground_exam_dialogue=active_exam,
            independent_document_role=semantic['role'], pre_dispatch=authorize)
        if not planned:
            return None
        plan, cloud_manifest = planned
        from .learning_system_facts import detect_learning_system_query
        fact_query = detect_learning_system_query(transcript, ())
        if fact_query and semantic['role'] == 'question':
            capability = o.capability_registry.require('system.inspect_learning', session['state'])
            plan = replace(plan, step=PlannedStep(capability, {'scope': fact_query.scope, 'track': fact_query.track},
                'Read persisted local facts instead of a model claim.', capability.expected_evidence),
                spoken_transition='我先核对本机保存的真实记录。')
        from .agent_runtime import action_from_plan
        action = {**action_from_plan(plan, transcript, None), 'source': 'agent_runtime_planner',
            'local_only': True, 'cloud_transmission_allowed': False, 'pending_evaluation_control': marker(binding)}
        if plan.step.capability.name == 'learning.explain':
            action['tutor_request_action_id'] = 'foreground-question:' + hashlib.sha256(key.encode()).hexdigest()
        manifest = {**cloud_manifest, **local_manifest}
        with o.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            guard(c)
            validate_independent(c, sid, child, key, transcript, manifest, decision=plan)
            old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
            if old:
                o._assert_action_payload(old, sid, transcript)
                o._assert_replayed_voice(old, capture['voice_input_id'])
                return o._get_replayed(c, old['agent_turn_id'])
            turn = str(uuid4())
            o._freeze_local_voice(c, sid, child, transcript, capture, action, manifest)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (turn,child,sid,transcript,plan.step.capability.name,plan.spoken_transition,json.dumps(action),
                 INDEPENDENT_VERSION,utc_now(),key))
            o.agent_runtime.persist_plan(c,child_id=child,session_id=sid,agent_turn_id=turn,
                client_action_id=key,decision=plan,context_manifest=manifest)
            c.commit()
            return o._get(c,turn)

    def _persist_safety(self, c, session, transcript, key, binding, capture, safety):
        from .child_safety import persist_safety_event
        o = self.o
        intent, response, original = safety
        action = {**original, 'local_only':True, 'cloud_transmission_allowed':False,
                  'pending_evaluation_control':marker(binding)}
        manifest = {'pending_evaluation':binding, 'pending_local_control':'child_safety'}
        plan = o._deterministic_action_plan(
            owner=o.capability_registry.owner_for_action_type(action['type'],session['state']),
            action=action,response=response)
        plan = replace(plan,objective=ObjectiveDraft('none',None,None,(),'execute'),
                       after_execution='stop',continuation_scope=())
        turn, now = str(uuid4()), utc_now()
        o._freeze_local_voice(c,session['session_id'],session['child_id'],transcript,capture,action,manifest)
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (turn,session['child_id'],session['session_id'],transcript,intent,response,json.dumps(action),VERSION,now,key))
        o.agent_runtime.persist_plan(c,child_id=session['child_id'],session_id=session['session_id'],
            agent_turn_id=turn,client_action_id=key,decision=plan,context_manifest=manifest)
        persist_safety_event(c,session['child_id'],session['session_id'],turn,action,now)
        c.commit()
        return o._get(c,turn)
