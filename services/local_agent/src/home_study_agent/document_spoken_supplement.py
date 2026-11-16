"""Original post-feedback speech, bound to its already presented parent task.

The derivative prompt is intentionally created after capture. Its authority is
the frozen review focus and answer-role receipt, never a backdated timestamp.
"""
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json

from . import document_goal_handoff as goal

KEY = 'document_spoken_supplement'
VERSION = 'document_spoken_supplement_v1'


def marker(transcript, voice_id, binding, *, revision=None):
    value = dict(version=VERSION, parent_prompt_id=binding['spoken_retry']['prompt_id'],
        voice_input_id=voice_id, transcript_hash=goal.digest(transcript))
    if revision is not None:
        value['revision']=revision
    return value


def original(c, prompt, *, live=False):
    """Capture-bound text identity; acoustic scoring must also verify raw audio."""
    from .agent_runtime import AgentRuntimeStore
    from .document_revisit import parse_journal, current_session, retained
    from .document_spoken_retry import validate
    from .voice_control_intent import VoiceControlIntent
    ref = json.loads(prompt['request_json']).get('document_independent') if 'request_json' in prompt.keys() else None
    if not ref:
        return None
    turn = c.execute('''SELECT t.*,p.status AS plan_status,s.status AS step_status,s.capability_name
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0''', (ref.get('turn_id'),)).fetchone()
    action = json.loads(turn['action_json']) if turn else {}
    if not action.get(KEY):
        return None
    binding = ref.get('binding') or {}
    goal.require(turn and turn['child_id']==prompt['child_id'] and turn['session_id']==prompt['origin_session_id']
        and prompt['authorized_turn_id'] in (None,turn['agent_turn_id'])
        and turn['capability_name']=='learning.review_revisit'
        and turn['tool_status']!='cancelled' and turn['plan_status']!='cancelled'
        and action.get('document_independent_check') == binding
        and action.get('source_voice_input_id') == action.get('voice_input_id'), 'DOCUMENT_SUPPLEMENT_OWNER_CHANGED')
    journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
        (action.get('document_revisit_request'),)).fetchone()
    goal.require(journal and journal['status']=='completed'
        and journal['fingerprint']==hashlib.sha256(json.dumps([turn['child_id'],turn['session_id'],turn['transcript']],ensure_ascii=False).encode()).hexdigest()
        and (journal['child_id'],journal['session_id'])==(turn['child_id'],turn['session_id']),
        'DOCUMENT_SUPPLEMENT_SEMANTICS_MISSING')
    frozen = json.loads(journal['binding_json'])
    decision = json.loads(journal['decision_json'])
    goal.require(not frozen.get('document_feedback_projection')
        and frozen['document_revisit']['phase']=='check_feedback'
        and frozen['document_revisit']['commitment_id']==binding['commitment_id']
        and parse_journal(decision,turn['transcript'],frozen['review_clock'])==decision
        and decision.get('meaning')=='answer'
        and not AgentRuntimeStore.local_answer_requires_clarification(turn['transcript']),
        'DOCUMENT_SUPPLEMENT_ANSWER_ROLE_REQUIRED')
    voice = AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
        voice_input_id=action['voice_input_id'],transcript=turn['transcript'])
    goal.require(action.get('voice_input_binding')==AgentRuntimeStore.voice_capture_snapshot(voice),
        'DOCUMENT_SUPPLEMENT_VOICE_CHANGED')
    captured = AgentRuntimeStore.captured_question(voice)
    parent_id = binding['spoken_retry']['prompt_id']
    goal.require(not captured or captured == dict(kind='diagnostic',binding=dict(prompt_id=parent_id,
        item_version=binding['spoken_retry']['item_version'])), 'DOCUMENT_SUPPLEMENT_CAPTURE_TARGET_CHANGED')
    parent = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (parent_id,)).fetchone()
    capture = c.execute('SELECT created_at,wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?',
        (action['voice_input_id'],)).fetchone()
    goal.require(parent and parent['status']=='answered' and capture, 'DOCUMENT_SUPPLEMENT_PARENT_MISSING')
    from .voice_capture_binding import capture_started_at
    onset = capture_started_at(c, action['voice_input_id'])
    goal.require(onset, 'DOCUMENT_SUPPLEMENT_CAPTURE_ORDER_CHANGED')
    times = [datetime.fromisoformat(t) for t in
        (parent['updated_at'],onset,turn['created_at'],prompt['created_at'])]
    goal.require(all(t.tzinfo for t in times) and all(a<=b for a,b in zip(times,times[1:])),
        'DOCUMENT_SUPPLEMENT_CAPTURE_ORDER_CHANGED')
    packet = retained(c,binding['commitment_id'],turn['child_id'])
    validate(c,prompt,packet,binding)
    from .document_spoken_revision import prepare as prepare_revision
    from .document_spoken_retry import parent_packet
    revision=prepare_revision(c,parent_packet(packet,parent_id),decision.get('revision'),before_turn_id=turn['agent_turn_id'])
    goal.require(action[KEY] == marker(turn['transcript'],action.get('voice_input_id'),binding,revision=revision),
        'DOCUMENT_SUPPLEMENT_REVISION_CHANGED')
    if live:
        current_session(c,turn['session_id'],turn['child_id'])
        VoiceControlIntent.assert_current_input(c,turn['session_id'],turn['child_id'],turn['client_action_id'],turn['transcript'],voice)
        latest = c.execute('SELECT agent_turn_id FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
            (turn['session_id'],)).fetchone()
        goal.require(latest and latest[0]==turn['agent_turn_id'] and turn['step_status'] in {'running','failed'},
            'DOCUMENT_SUPPLEMENT_NEWER_INPUT')
    return dict(version=VERSION, authorized_turn_id=turn['agent_turn_id'], source_turn_id=turn['agent_turn_id'],
        source_voice_input_id=action['voice_input_id'],voice_input_id=action['voice_input_id'],
        capture_audio_sha256=capture['wav_sha256'])


def validate_submission(c, turn_id, child_id, session_id, binding, answer):
    row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND item_version=?',
        (binding.get('prompt_id'),binding.get('item_version'))).fetchone()
    if not row:
        return False
    proof = original(c,row,live=True)
    if proof is None:
        return False
    turn = c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
    goal.require(proof['authorized_turn_id']==turn_id and row['child_id']==child_id
        and row['origin_session_id']==session_id and turn and turn[0]==answer, 'DOCUMENT_SUPPLEMENT_ANSWER_CHANGED')
    return True


def submit(c, turn, prompt):
    from .diagnostic_prompt import DiagnosticPromptService
    class TransactionPrompt(DiagnosticPromptService):
        def __init__(self):
            self.database = None
        def _transaction(self):
            return nullcontext(c)
    return TransactionPrompt().answer(turn['session_id'],prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
        answer=turn['transcript'],client_action_id='document-supplement:'+turn['agent_turn_id'],
        authorized_turn_id=turn['agent_turn_id'],hint_level=1)


def revision_feedback(database, turn_id, saved, latest_feedback):
    from .document_revisit import retained
    from .document_spoken_retry import parent_packet
    from .document_spoken_conversation import read, response
    with database.connect() as c:
        c.execute('BEGIN')
        turn=c.execute('SELECT child_id,action_json FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
        action=json.loads(turn['action_json']) if turn else {}
        if not (action.get(KEY) or {}).get('revision'):
            return None
        packet=retained(c,action['document_revisit_commitment'],turn['child_id'])
        chain=read(c,parent_packet(packet,saved['prompt']['prompt_id']))
        return dict(chain=chain,response_text=response(chain,latest_feedback))


def saved_result(database, turn_id):
    from .diagnostic_prompt import DiagnosticPromptService
    with database.connect() as c:
        c.execute('BEGIN')
        turn=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
        action=json.loads(turn['action_json']) if turn else {}
        goal.require(action.get(KEY),'DOCUMENT_SUPPLEMENT_MISSING')
        journal=c.execute('SELECT execution_receipt_json FROM voice_control_intent_request WHERE request_key=?',
            (action.get('document_revisit_request'),)).fetchone()
        saved=json.loads(journal[0])['result']['diagnostic_prompt'] if journal and journal[0] else {}
        row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
            ((saved.get('prompt') or {}).get('prompt_id'),)).fetchone()
        goal.require(row and row['authorized_turn_id']==turn_id and row['status']=='answered'
            and original(c,row),'DOCUMENT_SUPPLEMENT_SAVED_SOURCE_CHANGED')
        receipt=DiagnosticPromptService._receipt(c,row)
        goal.require(receipt and receipt['submitted_answer']==turn['transcript'],'DOCUMENT_SUPPLEMENT_ANSWER_CHANGED')
        return DiagnosticPromptService._result(c,row)


def teaching_followup(c, prompt):
    """Derive a local timeline from durable sources, never a learning-effect score.

    Revoking a teacher receipt invalidates attribution, not the child's answer.
    Recompute on read so old cached attribution cannot survive source changes.
    """
    from .document_spoken_review import SpokenReview
    from .agent_runtime import AgentRuntimeStore
    proof=original(c,prompt)
    if not proof or prompt['status']!='answered':
        return None
    turn=c.execute('SELECT rowid AS sequence,* FROM agent_turn WHERE agent_turn_id=?',
        (proof['source_turn_id'],)).fetchone()
    action=json.loads(turn['action_json'])
    parent_id=action[KEY]['parent_prompt_id']
    review=c.execute('''SELECT rowid AS sequence,* FROM agent_turn WHERE child_id=? AND session_id=?
        AND rowid<? AND json_valid(action_json)
        AND json_extract(action_json,'$.document_spoken_review.source_prompt_id')=?
        ORDER BY rowid DESC LIMIT 1''',(prompt['child_id'],prompt['origin_session_id'],turn['sequence'],parent_id)).fetchone()
    if not review:
        return None
    base=dict(version='document_teaching_followup_v1',child_id=prompt['child_id'],session_id=prompt['origin_session_id'],
        parent_prompt_id=parent_id,response_prompt_id=prompt['prompt_id'],response_turn_id=turn['agent_turn_id'],
        response_voice_input_id=proof['source_voice_input_id'],review_request_turn_id=review['agent_turn_id'],
        relation='response_after_offered_followup',delivery_verified=False,learning_effect='not_established',
        next_step='evaluate_response_then_independent_check',**goal.LOCAL)
    try:
        value=SpokenReview(None,None,None,None,None,None).saved(c,review['agent_turn_id'],historical=True)
        goal.require(value is not None,'DOCUMENT_TEACHING_REVIEW_UNAVAILABLE')
        voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
            voice_input_id=proof['source_voice_input_id'],transcript=turn['transcript'])
        from .voice_capture_binding import capture_started_at
        capture = capture_started_at(c, voice['voice_input_id'])
        teacher=c.execute('SELECT created_at FROM tutor_turn WHERE turn_id=?',(value['tutor_turn_id'],)).fetchone()
        goal.require(capture and teacher and goal.instant(teacher[0])<=goal.instant(capture),
            'DOCUMENT_TEACHING_RESPONSE_PREDATES_REVIEW')
        attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(prompt['attempt_id'],)).fetchone()
        goal.require(attempt and attempt['child_id']==prompt['child_id'],'DOCUMENT_TEACHING_RESPONSE_CHANGED')
        return {**base,'status':'linked_reference','tutor_turn_id':value['tutor_turn_id'],
            'composition_version':value['composition_version'],'response_evidence_id':attempt['evidence_id'],
            'followup_question':value['review']['next_prompt'],
            'criteria':list(dict.fromkeys(r['criterion'] for r in value['review']['observations']))}
    except (ValueError,TypeError,KeyError):
        return {**base,'status':'attribution_unavailable','next_step':'evaluate_original_response'}


def bind_independent_teaching_context(c,packet,prompt_id):
    source_id=packet[1].get('practice',{}).get('independent_prompt_id')
    source=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(source_id,)).fetchone()
    if not source:
        return None
    try:
        trace=teaching_followup(c,source)
    except (ValueError,TypeError,KeyError):
        return None
    if not trace or trace['status']!='linked_reference':
        return None
    marker=dict(version='document_independent_teaching_context_v1',source_prompt_id=source_id,
        source_trace=trace,trace_hash=goal.digest(trace))
    c.execute("UPDATE diagnostic_prompt SET request_json=json_set(request_json,'$.document_teaching_context',json(?)) WHERE prompt_id=?",
        (json.dumps(marker),prompt_id))
    row=c.execute('SELECT rowid AS sequence,* FROM diagnostic_prompt WHERE prompt_id=?',(prompt_id,)).fetchone()
    return independent_teaching_context(c,row)


def independent_teaching_context(c,row):
    request=json.loads(row['request_json'])
    marker=request.get('document_teaching_context')
    if not marker:
        return None
    base=dict(version='document_independent_teaching_context_v1',prompt_id=row['prompt_id'],
        relation='independent_check_after_assisted_response',delivery_verified=False,
        learning_effect='not_established',**goal.LOCAL)
    try:
        ref=request['document_independent']
        goal.require(not ref['binding'].get('spoken_retry'),'DOCUMENT_TEACHING_CHECK_NOT_NEW')
        sequence=c.execute('SELECT rowid FROM diagnostic_prompt WHERE prompt_id=?',(row['prompt_id'],)).fetchone()[0]
        source=c.execute('''SELECT * FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=?
            AND rowid<? AND json_valid(request_json)
            AND json_extract(request_json,'$.document_independent.binding.commitment_id')=?
            ORDER BY rowid DESC LIMIT 1''',(row['child_id'],row['origin_session_id'],sequence,ref['binding']['commitment_id'])).fetchone()
        goal.require(source and source['prompt_id']==marker['source_prompt_id'],'DOCUMENT_TEACHING_PARENT_CHANGED')
        trace=teaching_followup(c,source)
        goal.require(trace and trace['status']=='linked_reference' and marker==dict(version=base['version'],
            source_prompt_id=source['prompt_id'],source_trace=trace,trace_hash=goal.digest(trace)),
            'DOCUMENT_TEACHING_TRACE_CHANGED')
        goal.require(source['diagnostic_item_id']!=row['diagnostic_item_id'],'DOCUMENT_TEACHING_CHECK_REUSED')
        nodes={r[0]:r[1] for r in c.execute('SELECT diagnostic_item_id,knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id IN (?,?)',
            (source['diagnostic_item_id'],row['diagnostic_item_id']))}
        goal.require(len(nodes)==2 and set(nodes.values())=={ref['binding']['knowledge_node_id']},
            'DOCUMENT_TEACHING_CHECK_GOAL_CHANGED')
        return {**base,'status':'linked_reference','source_trace':trace,
            'assessment_authority':'current_independent_attempt_only'}
    except (ValueError,TypeError,KeyError):
        return {**base,'status':'attribution_unavailable'}
