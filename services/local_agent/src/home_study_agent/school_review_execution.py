"""Exact scheduled school review prompts; no selection or score authority."""
import json
from contextlib import nullcontext

from .school_open_reviews import (VERSION, MINUTES, SchoolReviewError, require, is_school_review,
    validate_review_source, _source, _now, _time, _result, _object)


def freeze_review(c, child, review_id, *, as_of=None):
    from .diagnostic_prompt import DiagnosticPromptService, _version
    row = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (review_id,)).fetchone()
    require(row and row['child_id'] == child, 'OWNER_CHANGED')
    validate_review_source(c, child, row, as_of=as_of)
    _, source, _ = _source(c, child, row['diagnostic_item_id'], _now(as_of))
    item = DiagnosticPromptService._item(c, child, row['diagnostic_item_id'])
    require(item, 'ITEM_UNAVAILABLE')
    return {'version': VERSION, **{k: row[k] for k in ('review_task_id', 'child_id', 'knowledge_node_id',
        'diagnostic_item_id', 'source_evidence_id', 'interval_label', 'due_at', 'created_at')},
        'item_version': _version(item), 'source_version': source['source_version']}


def resolve_execution(c, session_id, plan_item_id, *, completed=False):
    from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return resolve_execution(c, session_id, plan_item_id, completed=completed)
        finally:
            c.rollback()
    blocked = {'status': 'blocked', 'reason_code': 'school_review_binding_changed',
        'reason': '本次复习的原题、审核或到期依据已变化，请核对原计划；不会自动换成另一题。',
        'local_only': True, 'cloud_transmission_allowed': False, 'mastery_claim_allowed': False}
    row = c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status,ds.status AS session_status,
        ds.child_id AS session_child,cp.status AS child_status FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN daily_session ds ON ds.session_id=dp.session_id JOIN child_profile cp ON cp.child_id=dp.child_id
        WHERE pi.plan_item_id=? AND dp.session_id=?''', (plan_item_id, session_id)).fetchone()
    try:
        require(row and is_school_review(c, row) and row['child_id']==row['session_child'] and row['child_status']=='active'
            and row['plan_status']=='active' and row['session_status']=='active'
            and row['status'] in ({'active','paused','completed'} if completed else {'pending','active','paused'}), 'PLAN_CHANGED')
        records = c.execute('''SELECT metadata_json,allocated_minutes FROM plan_constraint_decision
            WHERE plan_id=? AND obligation_type='due_review' AND source_ref=? AND decision='scheduled' ''',
            (row['plan_id'], row['source_ref'])).fetchall()
        bindings = []
        for record in records:
            metadata = _object(record['metadata_json'], 'SNAPSHOT_INVALID')
            if metadata.get('plan_item_id') != plan_item_id:
                continue
            require(record['allocated_minutes'] == row['expected_minutes'] == MINUTES, 'BUDGET_CHANGED')
            bindings.append(metadata['planning_evidence']['school_review_binding'])
        require(bindings and all(b == bindings[0] for b in bindings), 'SNAPSHOT_MISSING')
        binding = freeze_review(c, row['child_id'], row['source_ref'])
        require(binding == bindings[0] and _time(binding['due_at']) <= _now(), 'SOURCE_OR_DUE_CHANGED')
        item = DiagnosticPromptService._item(c, row['child_id'], binding['diagnostic_item_id'])
        require(row['subject'] == item['subject'] and row['task_type']=='review', 'PLAN_KIND_CHANGED')
    except (ValueError, TypeError, KeyError) as error:
        return {**blocked, 'reason_code': str(error) if isinstance(error, SchoolReviewError) else 'school_review_binding_invalid'}
    return {**blocked, 'status': 'ready', 'diagnostic_item': {k: item[k] for k in _ITEM_FIELDS},
        'diagnostic_item_id': item['diagnostic_item_id'], 'item_version': binding['item_version'],
        'review_task_id': binding['review_task_id'], 'child_id': row['child_id'], 'binding': binding}


def _bindings(c, child, sid, pid):
    return [json.loads(r[0])['goal_prompt_binding'] for r in c.execute('''SELECT context_manifest_json FROM agent_plan
        WHERE child_id=? AND session_id=? AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.school_review')=1
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.plan_item_id')=?''', (child,sid,pid)).fetchall()]


def validate_start(c, item, turn_id):
    if not is_school_review(c, item):
        return
    resolved = resolve_execution(c, item['session_id'], item['plan_item_id'])
    require(resolved['status']=='ready' and turn_id, 'BOUND_PROMPT_REQUIRED')
    command = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=? AND child_id=? AND session_id=? AND status!=?',
        (turn_id,item['child_id'],item['session_id'],'cancelled')).fetchone()
    binding = _object(command[0], 'BOUND_PROMPT_REQUIRED').get('goal_prompt_binding') if command else None
    require(binding and binding.get('school_review') is True and binding.get('binding')==resolved['binding']
        and binding.get('plan_item_id')==item['plan_item_id'] and binding.get('item_version')==resolved['item_version'], 'BOUND_PROMPT_REQUIRED')
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
        (binding['prompt_id'],item['child_id'],item['session_id'])).fetchone()
    require(prompt and prompt['status'] in {'waiting_answer','answer_saved','answered'}
        and prompt['review_task_id']==item['source_ref'] and prompt['item_version']==resolved['item_version'], 'PROMPT_CHANGED')
    if prompt['status']=='answered':
        started = c.execute('''SELECT pi.started_at,a.created_at FROM plan_item pi
            JOIN daily_plan dp USING(plan_id)
            JOIN plan_item_event pe ON pe.plan_item_id=pi.plan_item_id
                AND pe.event_type='started' AND pe.occurred_at=pi.started_at
            JOIN diagnostic_attempt a ON a.attempt_id=? AND a.child_id=dp.child_id
                AND a.session_id=dp.session_id AND a.review_task_id=pi.source_ref
                AND a.diagnostic_item_id=?
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?''',
            (prompt['attempt_id'],prompt['diagnostic_item_id'],item['plan_item_id'],item['child_id'],item['session_id'])).fetchone()
        require(started and _time(started['started_at']) <= _time(started['created_at']) <= _now(), 'ANSWER_BEFORE_START')
    validate_budget(c,item['session_id'],item['child_id'],item['plan_item_id'])


def validate_budget(c, sid, child, pid):
    from .learning_input_replan import LearningInputReplanService, _spent, ZONE
    now = _now()
    plan = c.execute("SELECT * FROM daily_plan WHERE session_id=? AND child_id=? AND status='active'",
        (sid,child)).fetchone()
    require(plan and plan['plan_date']==now.astimezone(ZONE).date().isoformat(), 'PLAN_DAY_CHANGED')
    task = c.execute('SELECT * FROM plan_item WHERE plan_item_id=? AND plan_id=?',(pid,plan['plan_id'])).fetchone()
    rule = c.execute('SELECT * FROM family_rule WHERE child_id=?',(child,)).fetchone()
    session = c.execute('''SELECT s.*,cp.grade FROM daily_session s JOIN child_profile cp USING(child_id)
        WHERE session_id=? AND s.child_id=?''',(sid,child)).fetchone()
    context = c.execute('SELECT * FROM session_planning_context WHERE session_id=?',(sid,)).fetchone()
    require(task and rule and session, 'BUDGET_UNKNOWN')
    context = dict(context) if context else {'energy_level':'medium','fixed_commitment_minutes':0}
    items = [dict(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=? AND plan_item_id!=?',(plan['plan_id'],pid))]
    info,available,reason = LearningInputReplanService(None)._budget(c,child,session,plan,items,rule,context,{},now)
    # generate_plan treats explicit NULL as no configured family deadline.
    # Keep all spent/protected/parent caps, without inventing a bedtime.
    if rule['latest_end_time'] is None and reason=='latest_end_time_unknown' and not info['actual_time_unknown']:
        available,reason = info['total_budget_remaining_seconds'],None
    needed = max(0,task['expected_minutes']*60-_spent(task,now))
    require(reason is None, 'BUDGET_UNKNOWN')
    require(0 < needed <= available and task['expected_minutes']<=info['effective_task_segment_minutes_cap'], 'BUDGET_INSUFFICIENT')


def pending_start(c, prompt):
    if prompt['status'] not in {'waiting_answer','answer_saved'}:
        return None
    rows = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.school_review')=1
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')=?''',
        (prompt['child_id'],prompt['origin_session_id'],prompt['prompt_id'])).fetchall()
    for row in rows:
        binding = json.loads(row[0])['goal_prompt_binding']
        valid = c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            JOIN plan_item_event pe ON pe.plan_item_id=pi.plan_item_id AND pe.event_type='started' AND pe.occurred_at=pi.started_at
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=? AND pi.source_type='due_review' AND pi.source_ref=?''',
            (binding['plan_item_id'],prompt['child_id'],prompt['origin_session_id'],prompt['review_task_id'])).fetchone()
        if not valid:
            return binding['plan_item_id']
    return None


def pending_task_start(c, prompt):
    from .pet_task_execution import pet_prompt_pending_start
    return pet_prompt_pending_start(c, prompt) or pending_start(c, prompt)


def pending_original_task_binding(c, child_id, session_id):
    from .pet_task_execution import pending_pet_task_binding
    from .task_start_binding import TaskStartBindingError
    pet = pending_pet_task_binding(c, child_id, session_id)
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
        return pet
    rows = c.execute("SELECT * FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=? AND status IN ('waiting_answer','answer_saved')",
        (child_id,session_id)).fetchall()
    pending = [(p,pending_start(c,p)) for p in rows]
    pending = [(p,pid) for p,pid in pending if pid]
    if not pending:
        return pet
    if len(pending)!=1 or pet:
        raise TaskStartBindingError('TASK_START_SCHOOL_RECOVERY_AMBIGUOUS')
    p,pid=pending[0]
    resolved=resolve_execution(c,session_id,pid)
    if (resolved['status']!='ready' or resolved['child_id']!=child_id or p['status']!='waiting_answer'
            or resolved['item_version']!=p['item_version'] or resolved['diagnostic_item_id']!=p['diagnostic_item_id']):
        raise TaskStartBindingError('TASK_START_SCHOOL_RECOVERY_SOURCE_CHANGED')
    if c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE dp.child_id=? AND dp.session_id=? AND dp.status='active' AND pi.status='active' ''',(child_id,session_id)).fetchone():
        raise TaskStartBindingError('TASK_START_SCHOOL_RECOVERY_ACTIVE_TASK')
    task=c.execute('SELECT * FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()
    if not task or task['status']!='pending' or task['started_at']:
        raise TaskStartBindingError('TASK_START_SCHOOL_RECOVERY_STATE_CHANGED')
    target={'plan_item_id':pid,'source_type':task['source_type'],'source_ref':task['source_ref'],'operation':'start'}
    commands=c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')=?''',(child_id,session_id,p['prompt_id'])).fetchall()
    for command in commands:
        manifest=json.loads(command[0])
        binding=manifest['goal_prompt_binding']
        if (manifest.get('task_start_binding')!=target or binding.get('school_review') is not True
                or binding.get('binding')!=resolved['binding'] or binding.get('item_version')!=p['item_version']):
            raise TaskStartBindingError('TASK_START_SCHOOL_RECOVERY_BINDING_CHANGED')
    return target


def validate_completion(c, item):
    resolved = resolve_execution(c,item['session_id'],item['plan_item_id'],completed=True)
    require(resolved['status']=='ready' and item['started_at'], 'COMPLETION_SOURCE_CHANGED')
    bindings = _bindings(c,item['child_id'],item['session_id'],item['plan_item_id'])
    require(bindings and all(b==bindings[0] for b in bindings), 'PROMPT_AMBIGUOUS')
    bound = bindings[0]
    require(bound['binding']==resolved['binding'], 'COMPLETION_BINDING_CHANGED')
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
        (bound['prompt_id'],item['child_id'],item['session_id'])).fetchone()
    require(prompt and prompt['status']=='answered' and prompt['item_version']==bound['item_version'], 'ORIGINAL_ANSWER_REQUIRED')
    review = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (item['source_ref'],)).fetchone()
    require(review['status']=='completed' and review['result_evidence_id'], 'RELIABLE_REVIEW_REQUIRED')
    result = _result(c,item['child_id'],review['result_evidence_id'],_now())
    require(result['prompt_id']==prompt['prompt_id'] and result['attempt_id']==prompt['attempt_id']
        and result['session_id']==item['session_id'], 'RESULT_BINDING_CHANGED')
    require(_time(item['started_at']) <= _time(result['created_at']) <= _time(review['completed_at']) <= _now(), 'RESULT_TIME_CHANGED')
    from .learning_state import LearningStateService
    return LearningStateService._attempt(c,result['attempt_id'])


def reading_execution_owner(c, turn, prompt, active):
    """Permit a learning subgoal only for this started, source-bound review."""
    if active['objective_type'] != 'execution' or active['session_id'] != turn['session_id']:
        return False
    rows = c.execute('''SELECT p.agent_turn_id,p.context_manifest_json FROM agent_plan p
        JOIN agent_step s USING(plan_id) WHERE p.objective_id=? AND p.child_id=? AND p.session_id=?
        AND s.capability_name IN ('task.start','task.resume') AND s.status='completed'
        AND json_extract(CASE WHEN json_valid(p.context_manifest_json) THEN p.context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.school_review')=1
        AND json_extract(CASE WHEN json_valid(p.context_manifest_json) THEN p.context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')=?''',
        (active['objective_id'],turn['child_id'],turn['session_id'],prompt['prompt_id'])).fetchall()
    if not rows:
        return False
    bindings = [json.loads(r['context_manifest_json'])['goal_prompt_binding'] for r in rows]
    require(all(b == bindings[0] for b in bindings), 'PROMPT_AMBIGUOUS')
    binding = bindings[0]
    resolved = resolve_execution(c,turn['session_id'],binding['plan_item_id'])
    require(resolved['status']=='ready' and resolved['binding']==binding['binding']
        and resolved['item_version']==prompt['item_version']==binding['item_version']
        and resolved['review_task_id']==prompt['review_task_id'], 'LEARNING_SOURCE_CHANGED')
    started = c.execute('''SELECT pi.started_at FROM plan_item pi JOIN plan_item_event e USING(plan_item_id)
        WHERE pi.plan_item_id=? AND pi.status='active' AND e.event_type='started'
        AND e.occurred_at=pi.started_at''', (binding['plan_item_id'],)).fetchone()
    require(started and _time(started[0])<=_time(turn['created_at']), 'PROMPT_NOT_STARTED')
    return True


def validate_answer(c, *, child_id, session_id, item_id, review_task_id, prompt_id, answer, as_of):
    from .writing_response_origin import VERSION as ORIGINAL_VERSION, _original_text
    require(prompt_id, 'TRUSTED_PROMPT_REQUIRED')
    review = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (review_task_id,)).fetchone()
    validate_review_source(c,child_id,review,as_of=as_of)
    require(review['status']=='pending' and review['diagnostic_item_id']==item_id and _time(review['due_at'])<=_time(as_of), 'NOT_DUE')
    p = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
        (prompt_id,child_id,session_id)).fetchone()
    require(c.execute('''SELECT 1 FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.school_review')=1
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')=?''',
        (child_id,session_id,prompt_id)).fetchone(), 'SCHEDULED_PROMPT_REQUIRED')
    require(p and p['status']=='answer_saved' and p['review_task_id']==review_task_id and not pending_start(c,p), 'PROMPT_NOT_STARTED')
    require(not c.execute('SELECT 1 FROM diagnostic_attempt WHERE review_task_id=?',(review_task_id,)).fetchone(), 'ANSWER_ALREADY_SAVED')
    artifact = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=? AND artifact_type='writing_original' AND status='completed'",
        (p['writing_original_artifact_id'],child_id,session_id)).fetchone()
    require(artifact, 'ORIGINAL_REQUIRED')
    details = _object(artifact['details_json'], 'ORIGINAL_CHANGED')
    require(details.get('schema')==ORIGINAL_VERSION and details.get('prompt_id')==prompt_id
        and details.get('item_version')==p['item_version'] and not details.get('attempt_binding'), 'ORIGINAL_CHANGED')
    require(_original_text(c,p,artifact,details['origin'])==answer, 'ORIGINAL_CHANGED')
