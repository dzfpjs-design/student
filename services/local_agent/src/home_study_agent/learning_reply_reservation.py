"""Server-only consumption of an admitted ordinary learning source.

This is not a public capture, replay, scoring or independent-evidence grant.
The host supplies an actual DB work turn; status and capture admission never do.
"""
import hashlib
import json
import re

KEY = '_learning_answer_source_reservation_v1'
VERSION = 'learning_answer_source_reservation_v1'
GOAL = ('objective_id', 'child_id', 'session_id', 'objective_type', 'summary',
        'success_criteria_json', 'phase', 'status', 'last_agent_turn_id', 'updated_at')


def fail():
    from .agent_runtime import InvalidAgentPlanError
    raise InvalidAgentPlanError('ANSWER_LEARNING_SOURCE_RESERVATION_CHANGED')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def inputs(c, turn, response=None):
    """Only the current DB turn and its existing confirmed-answer source link."""
    from .agent_runtime import AgentRuntimeStore
    action = json.loads(turn['action_json'])
    source_id = action.get('answer_source_turn_id')
    rows = [turn]
    if source_id is not None:
        if not isinstance(source_id, str) or not source_id.strip() or source_id == turn['agent_turn_id']:
            fail()
        previous = c.execute('''SELECT agent_turn_id FROM agent_turn WHERE child_id=? AND session_id=?
            AND rowid<(SELECT rowid FROM agent_turn WHERE agent_turn_id=?) ORDER BY rowid DESC LIMIT 1''',
            (turn['child_id'],turn['session_id'],turn['agent_turn_id'])).fetchone()
        if previous is None or previous[0] != source_id:
            fail()
        source = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                           (source_id, turn['child_id'], turn['session_id'])).fetchone()
        if source is None or not AgentRuntimeStore.is_answer_role_confirmation(turn['transcript']):
            fail()
        saved = json.loads(source['action_json'])
        candidate = saved.get('pending_answer_candidate') or {}
        target = candidate.get('target') or {}
        question = (saved.get('voice_input_binding') or {}).get('question_reference') or {}
        if (saved.get('source') != 'pending_answer_clarification' or saved.get('local_only') is not True
                or target.get('action') != 'evaluate_child_response'
                or question.get('kind') != 'learning_reply'
                or target.get('binding') != question.get('binding')
                or response is not None and candidate.get('answer') != response):
            fail()
        rows.append(source)
    return rows


def admit(c, turn_id, child_id, session_id, capability, arguments):
    if capability not in {'learning.explain','learning.evaluate_response','learning.request_response'}:
        return None
    from .agent_runtime import AgentRuntimeStore
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                     (turn_id, child_id, session_id)).fetchone()
    if turn is None:
        fail()
    action = json.loads(turn['action_json'])
    current_question = (action.get('voice_input_binding') or {}).get('question_reference') or {}
    source_id = action.get('answer_source_turn_id')
    if current_question.get('kind') != 'learning_reply' and source_id is None:
        return None
    if capability == 'learning.explain' and source_id is not None:
        fail()
    rows = inputs(c, turn, arguments.get('response'))
    managed = [r for r in rows if ((json.loads(r['action_json']).get('voice_input_binding') or {})
               .get('question_reference') or {}).get('kind') == 'learning_reply']
    if not managed:
        return None
    if not c.in_transaction:
        fail()
    reference = None
    sealed = []
    for row in rows:
        saved = json.loads(row['action_json'])
        voice_id = saved.get('voice_input_id')
        if not voice_id:
            if saved.get('voice_input_binding'):
                fail()
            continue
        capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=session_id, child_id=child_id,
            voice_input_id=voice_id, transcript=row['transcript'])
        snapshot = AgentRuntimeStore.voice_capture_snapshot(capture)
        question = snapshot.get('question_reference') or {}
        if (snapshot != saved.get('voice_input_binding') or snapshot.get('question_validation') != 'valid'
                or question.get('kind') != 'learning_reply' or reference is not None and question != reference
                or any(snapshot.get(k) for k in ('activity_reference','task_choice_reference','pending_evaluation_turn_id','local_recovery_only'))):
            fail()
        reference = question
        sealed.append({'turn_id': row['agent_turn_id'], 'voice_input_id': voice_id,
                       'snapshot_sha256': digest(snapshot)})
    if reference is None:
        fail()
    # This strict path has no reservation, and revalidates the ordinary source before objective writes.
    AgentRuntimeStore.validate_captured_question(c, session_id, child_id, reference)
    goal = c.execute('SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
                     (reference['binding']['objective_id'], child_id, session_id)).fetchone()
    if goal is None:
        fail()
    return {'version': VERSION, 'work_capability': capability, 'child_id': child_id, 'session_id': session_id, 'turn_id': turn_id,
            'reference': reference, 'inputs': sealed,
            'source_goal': {'summary': goal['summary'], 'success_criteria': json.loads(goal['success_criteria_json']),
                            'phase': goal['phase']}}


def seal(c, admitted, plan_id, objective_id):
    if admitted is None:
        return None
    source_id = admitted['reference']['binding']['objective_id']
    source = c.execute('SELECT * FROM agent_objective WHERE objective_id=?', (source_id,)).fetchone()
    work = c.execute('SELECT * FROM agent_objective WHERE objective_id=?', (objective_id,)).fetchone()
    same = objective_id == source_id
    if (source is None or work is None or work['status'] != 'active'
            or source['objective_type'] != 'learning' or work['objective_type'] != 'learning'
            or work['last_agent_turn_id'] != admitted['turn_id']
            or any(r['child_id'] != admitted['child_id'] or r['session_id'] != admitted['session_id'] for r in (source,work))
            or not same and (admitted['work_capability'] != 'learning.explain' or source['status'] != 'paused')
            or same and source['status'] != 'active'):
        fail()
    return {**admitted, 'plan_id': plan_id,
            'goal_transition': 'same_goal' if same else 'new_explain',
            'objective_after': {k: source[k] for k in GOAL},
            'work_objective_after': {k: work[k] for k in GOAL}}


def validate(c, turn_id, *, voice_input_id=None, reference=None, child_id=None, session_id=None):
    """Read the actual work's private manifest, never a supplied reservation object."""
    from .agent_runtime import AgentRuntimeStore
    row = c.execute('''SELECT t.*,p.plan_id,p.context_manifest_json,p.status AS plan_status,
        s.capability_name,s.status AS step_status,s.arguments_json,p.plan_json
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0 WHERE t.agent_turn_id=?''', (turn_id,)).fetchone()
    if row is None:
        fail()
    item = json.loads(row['context_manifest_json']).get(KEY)
    fields = {'version','work_capability','child_id','session_id','turn_id','reference','inputs','source_goal','plan_id','goal_transition','objective_after','work_objective_after'}
    if (not isinstance(item, dict) or set(item) != fields or item['version'] != VERSION
            or item['turn_id'] != turn_id or item['plan_id'] != row['plan_id']
            or item['child_id'] != row['child_id'] or item['session_id'] != row['session_id']
            or child_id is not None and child_id != row['child_id']
            or session_id is not None and session_id != row['session_id']
            or row['capability_name'] not in {'learning.explain','learning.evaluate_response','learning.request_response'}
            or item['work_capability'] != row['capability_name']
            or row['plan_status'] not in {'planned','running','waiting_child','failed'}
            or row['step_status'] not in {'planned','running','waiting_child','failed'}
            or row['tool_status'] not in {'pending','failed'}):
        fail()
    planned = json.loads(row['plan_json']).get('next_step') or {}
    if planned.get('capability') != row['capability_name'] or planned.get('arguments') != json.loads(row['arguments_json']):
        fail()
    ref = item['reference']
    from .voice_capture_binding import normalize_question_reference
    try:
        normalized = normalize_question_reference(ref)
    except (ValueError, TypeError):
        fail()
    if not normalized or normalized.get('kind') != 'learning_reply' or reference is not None and reference != ref:
        fail()
    if row['capability_name'] == 'learning.explain' and json.loads(row['action_json']).get('answer_source_turn_id') is not None:
        fail()
    expected_inputs = []
    for source in inputs(c, row, json.loads(row['arguments_json']).get('response')):
        saved = json.loads(source['action_json'])
        snapshot = saved.get('voice_input_binding') or {}
        if saved.get('voice_input_id'):
            if (snapshot.get('question_reference') != ref or snapshot.get('question_validation') != 'valid'
                    or snapshot.get('voice_input_id') != saved['voice_input_id']
                    or any(snapshot.get(k) for k in ('activity_reference','task_choice_reference','pending_evaluation_turn_id','local_recovery_only'))):
                fail()
            expected_inputs.append({'turn_id':source['agent_turn_id'],'voice_input_id':saved['voice_input_id'],
                                    'snapshot_sha256':digest(snapshot)})
            # Re-read the durable capture and transcription, without recursively invoking this reservation.
            raw = c.execute('''SELECT b.*,v.transcript AS original_transcript,v.status AS voice_status,
                v.child_id AS voice_child,v.session_id AS voice_session,a.status AS action_status,
                a.child_id AS action_child,a.session_id AS action_session
                FROM voice_capture_binding b JOIN voice_input v USING(voice_input_id)
                JOIN voice_transcription_action a ON a.client_action_id=b.client_action_id
                WHERE b.voice_input_id=?''',(saved['voice_input_id'],)).fetchone()
            if (raw is None or raw['status'] != 'completed' or raw['voice_status'] != 'completed'
                    or raw['action_status'] != 'completed'
                    or re.sub(r'\s+', ' ',raw['original_transcript']).strip() != re.sub(r'\s+', ' ',source['transcript']).strip()
                    or any(raw[k] != row['child_id'] for k in ('child_id','voice_child','action_child'))
                    or any(raw[k] != row['session_id'] for k in ('session_id','voice_session','action_session'))):
                fail()
            from .voice_capture_binding import (stored_question_reference,stored_activity_reference,
                stored_pending_evaluation_reference,stored_task_choice_reference,stored_local_recovery_only)
            actual = {**dict(raw),'question_reference':stored_question_reference(raw),
                'activity_reference':stored_activity_reference(raw),
                'pending_evaluation_turn_id':stored_pending_evaluation_reference(raw),
                'task_choice_reference':stored_task_choice_reference(raw),
                'local_recovery_only':stored_local_recovery_only(raw),
                'admitted_at':raw['created_at'],
                'onset_provenance':'client_reported' if raw['speech_started_at'] else 'unknown'}
            if AgentRuntimeStore.voice_capture_snapshot(actual) != snapshot:
                fail()
        elif snapshot:
            fail()
    if (not 1 <= len(expected_inputs) <= 2 or item['inputs'] != expected_inputs
            or voice_input_id is not None and voice_input_id not in [v['voice_input_id'] for v in expected_inputs]):
        fail()
    binding = ref['binding']
    source = c.execute('''SELECT ao.*,p.created_at AS source_created_at,p.status AS source_plan_status,
        s.capability_name AS source_capability,s.arguments_json,s.result_json,s.status AS source_step_status,
        t.tool_status AS source_tool_status,d.status AS session_status,cp.status AS child_status
        FROM agent_plan p JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id AND t.session_id=p.session_id
        JOIN agent_objective ao ON ao.objective_id=p.objective_id AND ao.child_id=p.child_id AND ao.session_id=p.session_id
        JOIN daily_session d ON d.session_id=p.session_id AND d.child_id=p.child_id
        JOIN child_profile cp ON cp.child_id=p.child_id
        WHERE p.plan_id=? AND p.agent_turn_id=? AND p.objective_id=? AND p.child_id=? AND p.session_id=?''',
        (binding['plan_id'],binding['source_agent_turn_id'],binding['objective_id'],row['child_id'],row['session_id'])).fetchone()
    if (source is None or source['source_plan_status'] != 'waiting_child' or source['source_step_status'] != 'completed'
            or source['source_tool_status'] != 'completed' or source['source_capability'] != binding['capability_name']
            or source['session_status'] != 'active' or source['child_status'] != 'active'
            or item['objective_after'] != {k:source[k] for k in GOAL}):
        fail()
    if (not isinstance(item['work_objective_after'], dict) or set(item['work_objective_after']) != set(GOAL)
            or not isinstance(item['work_objective_after'].get('objective_id'), str)):
        fail()
    work_goal = c.execute('SELECT * FROM agent_objective WHERE objective_id=?',
                          (item['work_objective_after'].get('objective_id'),)).fetchone()
    actual_work_goal = c.execute('SELECT objective_id FROM agent_plan WHERE plan_id=?',(row['plan_id'],)).fetchone()
    if (work_goal is None or actual_work_goal is None or actual_work_goal[0] != work_goal['objective_id']
            or item['work_objective_after'] != {k:work_goal[k] for k in GOAL}
            or work_goal['status'] != 'active' or work_goal['objective_type'] != 'learning' or work_goal['last_agent_turn_id'] != turn_id
            or work_goal['child_id'] != row['child_id'] or work_goal['session_id'] != row['session_id']):
        fail()
    same_goal = work_goal['objective_id'] == binding['objective_id']
    mode = (json.loads(row['plan_json']).get('objective') or {}).get('mode')
    if mode != ('continue' if same_goal else 'new'):
        fail()
    if (same_goal and (item['goal_transition'] != 'same_goal' or source['status'] != 'active')
            or not same_goal and (item['goal_transition'] != 'new_explain'
                or row['capability_name'] != 'learning.explain' or source['status'] != 'paused')):
        fail()
    goal = item['source_goal']
    if not isinstance(goal,dict) or set(goal) != {'summary','success_criteria','phase'}:
        fail()
    pending = {**binding, **goal, 'arguments':json.loads(source['arguments_json']),
               'result':json.loads(source['result_json'] or 'null')}
    if AgentRuntimeStore.learning_reply_binding(pending) != binding:
        fail()
    # The actual work may consume its own source; no other new work/source may replace it.
    if c.execute('''SELECT 1 FROM agent_plan p JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE p.objective_id IN (?,?) AND p.child_id=? AND p.session_id=? AND p.plan_id NOT IN (?,?)
        AND s.capability_name IN ('learning.explain','learning.evaluate_response','learning.request_response') AND p.created_at>=? LIMIT 1''',
        (binding['objective_id'],work_goal['objective_id'],row['child_id'],row['session_id'],binding['plan_id'],row['plan_id'],source['source_created_at'])).fetchone():
        fail()
    selected = c.execute('''SELECT p.plan_id,p.updated_at FROM agent_plan p JOIN agent_objective o USING(objective_id)
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE p.session_id=? AND p.status='waiting_child' AND o.status='active' AND o.objective_type='learning'
        AND p.plan_id<>? ORDER BY p.updated_at DESC LIMIT 2''',(row['session_id'],row['plan_id'])).fetchall()
    if (same_goal and (not selected or selected[0]['plan_id'] != binding['plan_id']
            or len(selected)>1 and selected[0]['updated_at']==selected[1]['updated_at'])
            or not same_goal and selected):
        fail()
    for table, column, values in [('diagnostic_prompt','origin_session_id',"status='waiting_answer'"),
            ('pet_vocabulary_prompt_session','session_id',"status='active'"),
            ('specialized_exam_run','session_id',"status IN ('active','paused')"),
            ('companion_activity','session_id',"status='active'"),
            ('mistake_dialogue','session_id',"phase IN ('attempt','diagnosis_probe','answer')")]:
        if c.execute(f'SELECT 1 FROM {table} WHERE {column}=? AND {values} LIMIT 1',(row['session_id'],)).fetchone():
            fail()
    return ref


def validate_model_work(c, plan_id, step_id):
    row = c.execute('''SELECT t.*,p.plan_id AS actual_plan_id,p.context_manifest_json,s.capability_name AS actual_capability FROM agent_plan p JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id WHERE s.step_id=?''', (step_id,)).fetchone()
    if row is None:
        return
    action = json.loads(row['action_json'])
    question = (action.get('voice_input_binding') or {}).get('question_reference') or {}
    managed = KEY in json.loads(row['context_manifest_json']) or question.get('kind') == 'learning_reply'
    if not managed and action.get('answer_source_turn_id'):
        source = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                           (action['answer_source_turn_id'], row['child_id'], row['session_id'])).fetchone()
        managed = bool(source and ((json.loads(source['action_json']).get('voice_input_binding') or {})
            .get('question_reference') or {}).get('kind') == 'learning_reply')
    if managed:
        if row['actual_plan_id'] != plan_id or row['actual_capability'] != 'learning.evaluate_response':
            fail()
        validate(c, row['agent_turn_id'])
