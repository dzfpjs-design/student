"""Read current spoken process feedback without replaying a cloud request."""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from . import document_goal_handoff as goal
from .learning_rubric import RubricEvidenceService


_READ_SCOPE = ContextVar('document_feedback_read_scope', default=None)


@contextmanager
def read_scope(c):
    """Reuse ancestor proofs only within this synchronous read transaction."""
    scope = _READ_SCOPE.get()
    if scope is not None and scope['connection'] is c:
        goal.require(c.in_transaction and c.total_changes == scope['changes'], 'DOCUMENT_FEEDBACK_READ_MUTATED')
        yield scope
    elif not c.in_transaction:
        yield None
    else:
        scope = dict(connection=c, changes=c.total_changes, values={}, visiting=set())
        token = _READ_SCOPE.set(scope)
        try:
            yield scope
            goal.require(c.in_transaction and c.total_changes == scope['changes'], 'DOCUMENT_FEEDBACK_READ_MUTATED')
        finally:
            _READ_SCOPE.reset(token)


def spoken_prompt(c, packet):
    item, payload, *_ = packet
    work = payload.get('practice', {})
    if (work.get('phase') != 'check_feedback' and not
            (work.get('phase') in {'target_choice', 'target_search_pending'}
             and work.get('search_return_phase') == 'check_feedback')):
        return None
    row = c.execute('''SELECT p.* FROM diagnostic_prompt p JOIN diagnostic_item i USING(diagnostic_item_id)
        WHERE p.prompt_id=? AND p.child_id=? AND p.origin_session_id=? AND p.status='answered'
        AND i.answer_type='open_response' ''',
        (work.get('independent_prompt_id'), item['child_id'], work.get('session_id'))).fetchone()
    if not row:
        return None
    source = c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
        (row['diagnostic_item_id'],)).fetchone()
    expected = json.loads(source[0])
    return row if isinstance(expected,dict) and expected.get('response_mode') == 'spoken_explanation' else None


def evaluation_row(c, packet, *, turn_id=None):
    prompt = spoken_prompt(c, packet)
    if prompt is None:
        return None
    work = packet[1]['practice']
    saved = (work.get('independent_results') or [{}])[-1]
    if saved.get('prompt_id') != prompt['prompt_id'] or saved.get('attempt_id') != prompt['attempt_id']:
        return None
    rows = c.execute('''SELECT e.*,p.agent_turn_id,p.objective_id,p.status AS plan_status,
        s.status AS step_status,s.arguments_json,t.tool_status FROM learning_evaluation_execution e
        JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id)
        JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id AND t.session_id=p.session_id
        WHERE p.child_id=? AND p.session_id=? AND s.capability_name='learning.evaluate_response'
        AND json_extract(e.record_json,'$.source_evidence_id')=?
        AND json_extract(e.record_json,'$.formative_projection.local_audit.prompt_id')=?
        AND json_extract(e.record_json,'$.formative_projection.local_audit.item_version')=?
        ORDER BY e.rowid DESC''',
        (prompt['child_id'], prompt['origin_session_id'], saved.get('evidence_id'),
         prompt['prompt_id'], prompt['item_version'])).fetchall()
    if len(rows) != 1 or (turn_id is not None and rows[0]['agent_turn_id'] != turn_id):
        return None
    return rows[0]


def owns_latest_evaluation(c, packet, turn_id):
    """Focus identity is not teaching authority; withdrawn feedback can be refused."""
    row = evaluation_row(c, packet, turn_id=turn_id)
    return bool(row and row['state'] == 'completed'
        and all(row[k] == 'completed' for k in ('plan_status', 'step_status', 'tool_status')))


def read(c, packet, *, help_requested=False):
    with read_scope(c) as scope:
        if scope is None:
            return _read(c, packet, help_requested=help_requested)
        key = goal.digest([[dict(part) for part in packet], help_requested])
        goal.require(key not in scope['visiting'], 'DOCUMENT_FEEDBACK_READ_CYCLE')
        if key not in scope['values']:
            scope['visiting'].add(key)
            try:
                value = _read(c, packet, help_requested=help_requested)
                goal.require(c.in_transaction and c.total_changes == scope['changes'], 'DOCUMENT_FEEDBACK_READ_MUTATED')
                scope['values'][key] = value
            finally:
                scope['visiting'].remove(key)
        return deepcopy(scope['values'][key])


def _read(c, packet, *, help_requested=False):
    from .document_check_followup import current
    from .document_open_check import validate_projection
    from .learning_outcome_evaluator import LearningOutcomeEvaluator
    from .open_response_profile import profile_for_item
    prompt, source, attempt = current(c, packet)
    row = evaluation_row(c, packet)
    goal.require(row and row['state'] == 'completed'
        and all(row[k] == 'completed' for k in ('plan_status', 'step_status', 'tool_status')),
        'DOCUMENT_OPEN_FEEDBACK_NOT_READY')
    origin_id = json.loads(prompt['request_json'])['document_independent']['turn_id']
    origin = c.execute('''SELECT p.*,t.action_json,s.step_index,s.arguments_json FROM agent_plan p
        JOIN agent_turn t USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE p.agent_turn_id=? AND s.step_index=0''', (origin_id,)).fetchone()
    goal.require(origin, 'DOCUMENT_OPEN_FEEDBACK_ORIGIN_CHANGED')
    validate_projection(c, origin, prompt)
    record = json.loads(row['record_json'])
    args = json.loads(row['arguments_json'])
    LearningOutcomeEvaluator._assert_not_erased(c, record)
    from .pet_speaking_origin import original_answer_transcript
    original = (record.get('formative_projection') or {}).get('local_audit', {}).get('original') or {}
    voice = original_answer_transcript(c, prompt, attempt,
        dict(child_id=prompt['child_id'], session_id=prompt['origin_session_id'], prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'], voice_input_id=original.get('source_voice_input_id')),
        original.get('capture_audio_sha256'))
    goal.require(voice == original, 'DOCUMENT_OPEN_FEEDBACK_VOICE_CHANGED')
    goal.require(record['child_id'] == prompt['child_id'] and record['session_id'] == prompt['origin_session_id']
        and args.get('response') == record.get('child_response') == attempt['submitted_answer']
        and args.get('evidence_id') == record.get('source_evidence_id') == attempt['evidence_id']
        and args.get('question_asked') == source['prompt'], 'DOCUMENT_OPEN_FEEDBACK_ANSWER_CHANGED')
    evaluation = json.loads(row['result_json'])['evaluation']
    assessment_id = (evaluation.get('rubric_assessment') or {}).get('assessment_id')
    goal.require(evaluation.get('formative_only') and assessment_id, 'DOCUMENT_OPEN_FEEDBACK_NOT_READY')
    service = RubricEvidenceService(None)
    assessment = service.get(prompt['child_id'], assessment_id, connection=c)
    goal.require(assessment['source_status'] == 'current' and assessment['step_id'] == row['step_id']
        and assessment['source_evidence_id'] == attempt['evidence_id'], 'DOCUMENT_OPEN_FEEDBACK_SOURCE_CHANGED')
    review = assessment['review']
    goal.require(not review or review['operation'] != 'retract', 'DOCUMENT_OPEN_FEEDBACK_WITHDRAWN')
    if not review:
        call = c.execute('''SELECT m.* FROM rubric_assessment r JOIN model_call m ON m.call_id=r.model_call_id
            WHERE r.assessment_id=? AND m.child_id=? AND m.session_id=?
            AND m.purpose='learning_outcome_evaluation' ''',
            (assessment_id, prompt['child_id'], prompt['origin_session_id'])).fetchone()
        goal.require(call and call['status']=='completed' and call['safety_status']=='passed'
            and call['verification_status'] in {'verified','partially_verified'}, 'DOCUMENT_OPEN_FEEDBACK_NOT_READY')
    node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (source['knowledge_node_id'],)).fetchone())
    definitions = profile_for_item(source, node)['dimensions']
    dimensions = review['dimensions'] if review else assessment['dimensions']
    goal.require({d['dimension_id'] for d in dimensions} == {d['dimension_id'] for d in definitions}
        and all(service._quotes(d.get('response_quotes'), attempt['submitted_answer'])
            and isinstance(d.get('rationale'), str) and d['rationale'].strip() not in {'','not_assessed'}
            for d in dimensions), 'DOCUMENT_OPEN_FEEDBACK_INCOMPLETE')
    by_id = {d['dimension_id']: d for d in definitions}
    selected = min(dimensions, key=lambda d: d['score'] / by_id[d['dimension_id']]['max_score'])
    criterion = by_id[selected['dimension_id']]['criterion']
    prefix = '这是一份参考反馈，还不能据此确认掌握。'
    if help_requested:
        prefix += '刚才的新题是：'+source['prompt']+'。'
    return dict(response_text=prefix+selected['rationale']+'。接下来重点核对：'+criterion+
        '。你可以自己再解释这道题、换道新题独立检查，或先结束这次回顾。',
        prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], attempt_id=attempt['attempt_id'],
        assessment_id=assessment_id, review_revision=assessment['review_revision'],
        feedback_version=goal.digest([assessment, voice, definitions]),
        dimensions=dimensions, definitions=definitions, task_points=json.loads(source['expected_answer_json'])['task_points'],
        **goal.LOCAL)


def feedback(c, packet, *, help_requested=False):
    from .document_spoken_conversation import read as conversation, response
    return response(conversation(c, packet), read(c, packet, help_requested=help_requested)['response_text'])


def private_feedback_texts(c, prompt):
    """Redaction includes withdrawn reviews; deletion of authority is not consent."""
    texts = []
    for row in c.execute('''SELECT r.payload_json,v.payload_json AS review_json FROM rubric_assessment r
        LEFT JOIN rubric_review_event v USING(assessment_id)
        WHERE r.child_id=? AND json_extract(r.payload_json,'$.source_evidence_id')=
            (SELECT evidence_id FROM diagnostic_attempt WHERE attempt_id=? AND child_id=?)''',
        (prompt['child_id'], prompt['attempt_id'], prompt['child_id'])):
        for raw in row:
            value = json.loads(raw) if raw else {}
            for dimension in value.get('dimensions') or []:
                if isinstance(dimension.get('rationale'), str):
                    texts.append(dimension['rationale'])
                texts.extend(q for q in dimension.get('response_quotes', []) if isinstance(q, str))
    return texts
