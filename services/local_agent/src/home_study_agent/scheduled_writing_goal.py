"""Bind scheduled open answers before submission; preserve original authority."""
from datetime import datetime, timezone
import json
from uuid import uuid4

from .writing_response_origin import writing_item

KEY = 'scheduled_writing_goal'


def require(condition, reason):
    if not condition:
        from .task_start_binding import TaskStartBindingError
        raise TaskStartBindingError('TASK_WRITING_' + reason)


def stage_receipt(c, plan, prompt, task):
    """Validate original scheduled authority without selecting the next task."""
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .stage_execution import _binding, SOURCE_TRACKS
    from .specialized_stages import _item_source
    row = c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status,
        ds.child_id AS session_child,cp.status AS child_status FROM plan_item pi
        JOIN daily_plan dp USING(plan_id) JOIN daily_session ds ON ds.session_id=dp.session_id
        JOIN child_profile cp ON cp.child_id=dp.child_id WHERE pi.plan_item_id=?''',
        (task['plan_item_id'],)).fetchone()
    require(row and row['child_id'] == row['session_child'] == plan['child_id']
        and row['session_id'] == plan['session_id'] and row['child_status'] == 'active'
        and row['plan_status'] != 'cancelled' and row['status'] in {'active','paused','completed'}, 'PLAN_CHANGED')
    track = SOURCE_TRACKS.get(row['source_type'])
    require(track, 'PLAN_CHANGED')
    records = c.execute('''SELECT metadata_json,allocated_minutes FROM plan_constraint_decision
        WHERE plan_id=? AND obligation_type=? AND source_ref IS ? AND decision='scheduled' ''',
        (row['plan_id'],row['source_type'],row['source_ref'])).fetchall()
    count = c.execute('SELECT count(*) FROM plan_item WHERE plan_id=? AND source_type=? AND source_ref IS ?',
        (row['plan_id'],row['source_type'],row['source_ref'])).fetchone()[0]
    bindings = []
    for record in records:
        metadata = json.loads(record['metadata_json'])
        if metadata.get('plan_item_id') not in (None,task['plan_item_id']):
            continue
        require(metadata.get('plan_item_id') is not None or count == 1, 'PLAN_AMBIGUOUS')
        require(record['allocated_minutes'] == row['expected_minutes'] > 0, 'BUDGET_CHANGED')
        bindings.append(_binding(metadata['planning_evidence'],plan['child_id'],track))
    require(bindings and all(b == task['binding'] for b in bindings), 'BINDING_CHANGED')
    binding = task['binding']
    item = DiagnosticPromptService._item(c,plan['child_id'],prompt['diagnostic_item_id'])
    require(item and item['subject'] == row['subject'] and item['knowledge_node_id'] == binding['knowledge_node_id']
        and item['diagnostic_item_id'] == binding['diagnostic_item_id']
        and _version(item) == prompt['item_version'], 'ITEM_CHANGED')
    current, _ = _item_source(c,plan['child_id'],item,datetime.now(timezone.utc))
    require(current and current['source_version'] == binding['source_version'], 'SOURCE_CHANGED')
    if row['source_type'] == 'due_review':
        from .writing_stage_reviews import validate_writing_stage_review
        review = validate_writing_stage_review(c,plan['child_id'],row['source_ref'],allowed_statuses=('pending','completed'))
        require(review['review_task_id'] == binding['review_task_id']
            and review['diagnostic_item_id'] == item['diagnostic_item_id'], 'REVIEW_CHANGED')
    elif track == 'pet':
        require(row['source_ref'] == item['diagnostic_item_id'], 'SOURCE_CHANGED')


def goal_receipt(c, plan, prompt, task):
    """An answered goal is a receipt, not an invitation to select a fresh item."""
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .goal_roadmap import goal_catalog_source_reason
    row = c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status
        FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=?''',
        (task['plan_item_id'],)).fetchone()
    require(row and row['child_id'] == plan['child_id'] == prompt['child_id']
        and row['session_id'] == plan['session_id'] == prompt['origin_session_id']
        and row['plan_status'] == 'active' and row['status'] in {'active','paused','completed'}, 'PLAN_CHANGED')
    require(row['source_type'] in {'junior_exam_goal','competition_math'} and
        not goal_catalog_source_reason(c, plan['session_id'], task['plan_item_id']), 'SOURCE_CHANGED')
    start = json.loads(plan['context_manifest_json']).get('task_start_binding') or {}
    require(all(start.get(k) == row[k] for k in ('plan_item_id','source_type','source_ref')), 'BINDING_CHANGED')
    records = c.execute('''SELECT metadata_json,allocated_minutes FROM plan_constraint_decision
        WHERE plan_id=? AND obligation_type=? AND source_ref=? AND decision='scheduled' ''',
        (row['plan_id'],row['source_type'],row['source_ref'])).fetchall()
    require(records, 'BINDING_CHANGED')
    for record in records:
        evidence = json.loads(record['metadata_json']).get('planning_evidence') or {}
        require(all(evidence.get(k) == v for k,v in task['binding'].items()), 'BINDING_CHANGED')
        require(record['allocated_minutes'] == row['expected_minutes'] > 0, 'BUDGET_CHANGED')
    item = DiagnosticPromptService._item(c, plan['child_id'], prompt['diagnostic_item_id'])
    require(item and _version(item) == prompt['item_version'] and item['subject'] == row['subject']
        and item['knowledge_node_id'] == task['binding']['knowledge_node_id'], 'ITEM_CHANGED')
    _start_receipt(c, plan, prompt, task)


def source(c, plan, prompt):
    manifest = json.loads(plan['context_manifest_json'])
    task = manifest.get('goal_prompt_binding')
    require(isinstance(task, dict) and plan['child_id'] == prompt['child_id']
        and plan['session_id'] == prompt['origin_session_id']
        and task.get('prompt_id') == prompt['prompt_id']
        and task.get('item_version') == prompt['item_version'], 'BINDING_CHANGED')
    if task.get('oral_supply'):
        from .oral_supply_execution import resolve_execution as resolve
    elif task.get('school_review'):
        from .school_review_execution import resolve_execution as resolve
    elif task.get('pet'):
        from .pet_task_execution import resolve_pet_task_execution as resolve
    elif task.get('stage'):
        from .stage_execution import resolve_stage_execution as resolve
    else:
        from .goal_roadmap import GoalRoadmapService
        resolve = GoalRoadmapService._resolve_execution
    if task.get('stage') and prompt['attempt_id']:
        stage_receipt(c,plan,prompt,task)
    elif prompt['attempt_id'] and not any(task.get(k) for k in ('stage','pet','school_review','closed_review','oral_supply')):
        goal_receipt(c,plan,prompt,task)
    else:
        current = resolve(c, plan['session_id'], task['plan_item_id'])
        require(current['status'] == 'ready' and current['binding'] == task['binding']
            and current['item_version'] == task['item_version']
            and current['diagnostic_item_id'] == prompt['diagnostic_item_id'], 'SOURCE_CHANGED')
    from .diagnostic_prompt import DiagnosticPromptService
    item = DiagnosticPromptService._item(c, plan['child_id'], prompt['diagnostic_item_id'])
    if item and item['answer_type'] == 'open_response':
        from .open_response_profile import profile_for_item
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone()
        profile_for_item(item, dict(node) if node else {})
    elif not (task.get('oral_supply') and item and item['answer_type'] in {'speaking','read_aloud'}):
        item, _ = writing_item(c, plan['child_id'], prompt['diagnostic_item_id'])
    return task, json.loads(item['expected_answer_json'])['task_points']


def goal(c, plan, points, *, recovery=False):
    now = datetime.now(timezone.utc).isoformat()
    active = c.execute("SELECT * FROM agent_objective WHERE child_id=? AND status='active'", (plan['child_id'],)).fetchall()
    task=json.loads(plan['context_manifest_json']).get('goal_prompt_binding') or {}
    if not recovery and task.get('oral_supply') and len(active)==1:
        binding=task['binding']
        prior=active[0]
        previous=binding.get('previous_resolution') or {}
        if (prior['objective_id']==previous.get('objective_id',binding['root_objective_id'])
                and prior['session_id']==previous.get('session_id',binding['root_session_id'])!=plan['session_id']):
            # A validated cross-day request starts a linked current-day goal;
            # the original goal and its answer keep their historical ownership.
            c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=?",
                (now,prior['objective_id']))
            active=[]
    require(len(active) <= 1 and all(o['session_id'] == plan['session_id']
        and (recovery or o['objective_id'] == plan['objective_id']) for o in active), 'OTHER_GOAL_ACTIVE')
    deferred = bool(recovery and active and active[0]['objective_id'] != plan['objective_id'])
    if active and not recovery and active[0]['objective_type'] == 'learning' and active[0]['last_agent_turn_id'] == plan['agent_turn_id']:
        oid = active[0]['objective_id']
        c.execute("UPDATE agent_objective SET success_criteria_json=?,phase='check',updated_at=? WHERE objective_id=?",
            (json.dumps(points,ensure_ascii=False),now,oid))
        created = active[0]['created_at']
    else:
        if active and not deferred:
            c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=?", (now,active[0]['objective_id']))
        oid, created = str(uuid4()), now
        c.execute('''INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,summary,
            success_criteria_json,phase,status,created_at,updated_at,last_agent_turn_id)
            VALUES(?,?,?,'learning',?,?,'check',?,?,?,?)''',
            (oid,plan['child_id'],plan['session_id'],
             '保留旧稿，从本机自检继续修改；旧稿不补认掌握' if recovery else '按当前原题要求独立完成作答',
             json.dumps(points,ensure_ascii=False),'paused' if deferred else 'active',created,now,plan['agent_turn_id']))
    return dict(objective_id=oid,child_id=plan['child_id'],session_id=plan['session_id'],
        phase='check',success_criteria=points,created_at=created,local_only=True,cloud_transmission_allowed=False)


def bind_before_answer(c, turn_id, prompt_id):
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    plan = c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt_id,)).fetchone()
    item = c.execute('SELECT answer_type,expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
        (prompt['diagnostic_item_id'],)).fetchone()
    oral_supply=bool(json.loads(plan['context_manifest_json']).get('goal_prompt_binding',{}).get('oral_supply'))
    if item['answer_type'] not in ({'writing','open_response','speaking','read_aloud'} if oral_supply else {'writing','open_response'}):
        return
    # Never retrofit a learning goal onto an already submitted original.
    if prompt['attempt_id'] or prompt['status'] in {'answer_saved','answered'}:
        return
    task, points = source(c, plan, prompt)
    existing = c.execute('''SELECT * FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,
                         '$.scheduled_writing_goal.prompt_id')=? ORDER BY rowid LIMIT 1''',
        (plan['child_id'],plan['session_id'],prompt_id)).fetchone()
    if existing:
        recovering = existing['status']=='failed' and existing['plan_id']!=plan['plan_id']
        if recovering:
            from .task_start_binding import TaskStartBinding
            start = json.loads(plan['context_manifest_json']).get('task_start_binding') or {}
            TaskStartBinding.authorize(c,turn_id,task['plan_item_id'],start.get('operation'))
        validate_origin(c, existing, prompt, prepared_recovery=recovering)
        bound = json.loads(existing['plan_json'])['local_learning_binding']
    else:
        bound = goal(c, plan, points)
        bound.update(version='scheduled_writing_goal_v1',prompt_id=prompt_id,item_version=prompt['item_version'],
            diagnostic_item_id=prompt['diagnostic_item_id'])
        if task.get('oral_supply'):
            bound.update(root_objective_id=task['binding']['root_objective_id'],
                root_commitment_id=task['binding']['root_commitment_id'],root_session_id=task['binding']['root_session_id'])
    marker = dict(task,objective_id=bound['objective_id'],created_at=bound['created_at'])
    c.execute('''UPDATE agent_plan SET objective_id=?,plan_json=json_set(plan_json,'$.local_learning_binding',json(?)),
        context_manifest_json=json_set(context_manifest_json,'$.scheduled_writing_goal',json(?)) WHERE plan_id=?''',
        (bound['objective_id'],json.dumps(bound,ensure_ascii=False),json.dumps(marker),plan['plan_id']))
    c.execute("""UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime.objective_id',?)
        WHERE agent_turn_id=? AND json_type(action_json,'$.agent_runtime')='object'""", (bound['objective_id'],turn_id))


def _start_receipt(c, plan, prompt, task):
    """Read completed execution facts, never reauthorize starting a task now."""
    rows = c.execute('''SELECT s.*,t.child_id AS turn_child,t.session_id AS turn_session,
        t.tool_status,t.tool_error_code,e.occurred_at FROM agent_step s
        JOIN agent_turn t ON t.agent_turn_id=?
        JOIN plan_item_event e ON e.client_action_id=s.idempotency_key
        WHERE s.plan_id=? AND s.step_index=0 AND s.capability_name IN ('task.start','task.resume')
          AND e.plan_item_id=? AND e.event_type='started' ''',
        (plan['agent_turn_id'],plan['plan_id'],task['plan_item_id'])).fetchall()
    require(len(rows) == 1, 'START_RECEIPT_CHANGED')
    step = rows[0]
    require(step['status'] == step['tool_status'] == 'completed'
        and not step['error_code'] and not step['tool_error_code']
        and step['turn_child'] == plan['child_id'] == prompt['child_id']
        and step['turn_session'] == plan['session_id'] == prompt['origin_session_id'], 'START_RECEIPT_CHANGED')
    receipt = json.loads(step['result_json'] or '{}')
    require(isinstance(receipt,dict) and receipt.get('capability') == step['capability_name'], 'START_RECEIPT_CHANGED')
    if step['capability_name']=='task.resume':
        manifest=json.loads(plan['context_manifest_json'])
        require(manifest.get('supply_resume_receipt') or manifest.get('legacy_listening_review_replacement'), 'START_RECEIPT_CHANGED')
    result = receipt.get('result')
    require(isinstance(result,dict) and result.get('plan_item_id') == task['plan_item_id']
        and result.get('started_at') == step['occurred_at'], 'START_RECEIPT_CHANGED')
    saved = result.get('diagnostic_prompt')
    require(isinstance(saved,dict) and isinstance(saved.get('prompt'),dict), 'START_RECEIPT_CHANGED')
    require(all(saved['prompt'].get(k) == prompt[k] for k in
        ('prompt_id','item_version','diagnostic_item_id','child_id','origin_session_id','review_task_id')),
        'START_RECEIPT_CHANGED')
    return step['occurred_at']


def validate_origin(c, plan, prompt, *, prepared_recovery=False):
    if prepared_recovery:
        require(plan['status']=='failed' and prompt['status']=='waiting_answer'
            and not prompt['attempt_id'] and not prompt['answer_action_id'], 'COMMAND_CHANGED')
        previous=c.execute("SELECT step_id FROM agent_step WHERE plan_id=? AND step_index=0 AND status='failed'",(plan['plan_id'],)).fetchone()
        started=c.execute('''SELECT 1 FROM plan_item_event e JOIN agent_step s ON e.client_action_id=s.idempotency_key
            WHERE s.plan_id=? AND e.event_type='started' ''',(plan['plan_id'],)).fetchone()
        require(previous and not started,'START_RECEIPT_CHANGED')
    else:
        require(plan['status'] not in {'cancelled','failed'}, 'COMMAND_CHANGED')
    task, points = source(c, plan, prompt)
    bound = json.loads(plan['plan_json']).get('local_learning_binding')
    marker = json.loads(plan['context_manifest_json']).get(KEY)
    require(isinstance(bound,dict) and isinstance(marker,dict) and marker == dict(task,
        objective_id=bound.get('objective_id'),created_at=bound.get('created_at')), 'GOAL_CHANGED')
    require(all(bound.get(k) == v for k,v in dict(objective_id=plan['objective_id'],child_id=prompt['child_id'],
        session_id=prompt['origin_session_id'],prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
        diagnostic_item_id=prompt['diagnostic_item_id'],success_criteria=points).items()), 'GOAL_CHANGED')
    if task.get('oral_supply'):
        require(all(bound.get(key)==task['binding'].get(key)
            for key in ('root_objective_id','root_commitment_id','root_session_id')), 'GOAL_CHANGED')
    objective = c.execute('SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
        (bound['objective_id'],prompt['child_id'],prompt['origin_session_id'])).fetchone()
    require(objective and objective['objective_type']=='learning' and objective['created_at']==bound['created_at']
        and json.loads(objective['success_criteria_json'])==points, 'GOAL_CHANGED')
    if prepared_recovery:
        require(objective['status'] in {'active','paused'},'GOAL_CHANGED')
    if prompt['attempt_id']:
        answer = c.execute('SELECT created_at FROM diagnostic_attempt WHERE attempt_id=?', (prompt['attempt_id'],)).fetchone()
        require(answer and datetime.fromisoformat(bound['created_at']) <= datetime.fromisoformat(answer[0]), 'GOAL_AFTER_ANSWER')
        started = _start_receipt(c, plan, prompt, task)
        require(datetime.fromisoformat(bound['created_at']) <= datetime.fromisoformat(started)
            <= datetime.fromisoformat(answer[0]), 'ANSWER_WINDOW_CHANGED')


def recover_original(runtime, c, prompt):
    """A new self-check/revision goal, explicitly not authority for the old answer."""
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    plans = c.execute('''SELECT p.* FROM agent_plan p JOIN agent_step s USING(plan_id)
        WHERE p.child_id=? AND p.session_id=? AND s.capability_name='task.start' AND s.status='completed'
        AND p.status NOT IN ('cancelled','failed')
        AND json_extract(CASE WHEN json_valid(p.context_manifest_json) THEN p.context_manifest_json ELSE '{}' END,
                         '$.goal_prompt_binding.prompt_id')=? ORDER BY p.rowid''',
        (prompt['child_id'],prompt['origin_session_id'],prompt['prompt_id'])).fetchall()
    if not plans or any(json.loads(p['context_manifest_json']).get(KEY) for p in plans):
        return None
    plan = plans[0]
    session = c.execute('SELECT status,state FROM daily_session WHERE session_id=? AND child_id=?',
        (prompt['origin_session_id'],prompt['child_id'])).fetchone()
    require(session and session['status']=='active' and session['state'] not in {'evolving','completed'},
        'RECOVERY_SESSION_INACTIVE')
    task, points = source(c, plan, prompt)
    original = runtime.writing_reference_source(c,prompt['origin_session_id'],prompt['prompt_id'],prompt['item_version'])
    started = _start_receipt(c, plan, prompt, task)
    attempt = c.execute('SELECT created_at FROM diagnostic_attempt WHERE attempt_id=?', (prompt['attempt_id'],)).fetchone()
    require(attempt and datetime.fromisoformat(started) <= datetime.fromisoformat(attempt[0]), 'ANSWER_WINDOW_CHANGED')
    key = 'scheduled-writing-recovery:' + original['evidence_id']
    prior = c.execute('''SELECT t.agent_turn_id FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        WHERE t.child_id=? AND t.session_id=? AND t.client_action_id=?
        AND json_extract(CASE WHEN json_valid(p.context_manifest_json) THEN p.context_manifest_json ELSE '{}' END,
                         '$.writing_reference.source_version')=?''',
        (prompt['child_id'],prompt['origin_session_id'],key,original['source_version'])).fetchone()
    if prior:
        tid = prior[0]
    else:
        bound = goal(c,plan,points,recovery=True)
        bound.update(version='scheduled_writing_recovery_v1',prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
            diagnostic_item_id=prompt['diagnostic_item_id'],recovery_only=True)
        tid = runtime.prepare_writing_reference_feedback(c,prompt['origin_session_id'],prompt['prompt_id'],prompt['item_version'],bound,key)
        c.execute('UPDATE agent_plan SET objective_id=? WHERE agent_turn_id=?',(bound['objective_id'],tid))
        c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime.objective_id',?) WHERE agent_turn_id=?",
            (bound['objective_id'],tid))
    return dict(status='local_feedback_required',reason='original_learning_objective_missing_recovery_only',
        local_feedback_turn_id=tid,local_only=True,cloud_transmission_allowed=False)


def activate_recovery_revision(c, row, turn_id, bound):
    """Only an admitted child revision switches away from another active goal."""
    from .writing_teaching import (_reference_receipt, _revision_request,
        _reference_revision_action, _reference_revision_version)
    from .writing_response_origin import _prompt
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    target,_ = _prompt(c,row['session_id'],row['prompt_id'],row['item_version'],live=True)
    parent,_ = _prompt(c,row['session_id'],row['parent_prompt_id'],bound['item_version'],live=False)
    reference,source = _reference_receipt(c,row['session_id'],parent,row['reference_turn_id'])
    # The server has committed prompt creation before the outcome transaction.
    # Recheck that completed request, never re-admit a failed/cancelled command.
    step=c.execute('SELECT * FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?) AND step_index=0',
        (turn_id,)).fetchone()
    require(step and step['status']=='completed' and not step['error_code'], 'REVISION_NOT_COMPLETED')
    result=json.loads(step['result_json'] or '{}')
    require((result.get('result') or {}).get('prompt',{}).get('prompt_id')==row['prompt_id'], 'REVISION_RECEIPT_CHANGED')
    request=_revision_request(c,row['session_id'],parent,source,reference,turn_id,live=False)
    action=_reference_revision_action(parent,row['reference_turn_id'],turn_id)
    version=_reference_revision_version(action,source,reference,request)
    stored=json.loads(row['binding_json'])
    require(version==stored.get('binding_version')
        and target['child_id']==row['child_id'] and target['prompt_id']!=row['parent_prompt_id']
        and target['diagnostic_item_id']==action['diagnostic_item_id'], 'REVISION_AUTHORIZATION_CHANGED')
    require(bound.get('version')=='scheduled_writing_recovery_v1' and bound.get('recovery_only') is True
        and bound['prompt_id']==row['parent_prompt_id'] and bound['child_id']==row['child_id']
        and bound['session_id']==row['session_id'] and bound['success_criteria']==source['task_points'], 'RECOVERY_CHANGED')
    objective=c.execute('SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
        (bound['objective_id'],row['child_id'],row['session_id'])).fetchone()
    require(objective and objective['objective_type']=='learning' and objective['status'] in {'active','paused'}
        and objective['created_at']==bound['created_at']
        and json.loads(objective['success_criteria_json'])==bound['success_criteria'], 'RECOVERY_CHANGED')
    active=c.execute("SELECT * FROM agent_objective WHERE child_id=? AND status='active'",(row['child_id'],)).fetchall()
    require(len(active)<=1 and all(o['session_id']==row['session_id'] for o in active), 'OTHER_GOAL_ACTIVE')
    now=datetime.now(timezone.utc).isoformat()
    for other in active:
        if other['objective_id']!=objective['objective_id']:
            c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=?",(now,other['objective_id']))
    c.execute("UPDATE agent_objective SET status='active',updated_at=? WHERE objective_id=?",(now,objective['objective_id']))
    c.execute('UPDATE agent_plan SET objective_id=? WHERE agent_turn_id=?',(objective['objective_id'],turn_id))
    c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime.objective_id',?) WHERE agent_turn_id=?",
        (objective['objective_id'],turn_id))
    return c.execute('SELECT * FROM agent_objective WHERE objective_id=?',(objective['objective_id'],)).fetchone()


def supply_projection_receipt(c,row,prompt,arguments):
    """A supply resume creates a question; its pre-question voice stays local."""
    from .agent_runtime import AgentRuntimeStore
    from .voice_control_intent import VoiceControlIntent
    manifest=json.loads(row['context_manifest_json'])
    target=manifest.get('supply_resume_receipt') or {}
    require(target.get('operation')=='supply'
        and arguments=={'local_only':True,'cloud_transmission_allowed':False},'SUPPLY_CONTROL_CHANGED')
    origin=c.execute('SELECT * FROM agent_plan WHERE plan_id=?',(row['plan_id'],)).fetchone()
    validate_origin(c,origin,prompt)
    task=manifest['goal_prompt_binding']
    item_id=target.get('diagnostic_item_id')
    if target.get('source_type')=='pet_daily':
        require(item_id in (None,target.get('source_ref')),'SUPPLY_TARGET_CHANGED')
        item_id=target.get('source_ref')
    require(task['plan_item_id']==target.get('plan_item_id')
        and prompt['diagnostic_item_id']==item_id
        and task['item_version']==target.get('item_version'),'SUPPLY_TARGET_CHANGED')
    control=VoiceControlIntent.inspect_execution(c,row['agent_turn_id'])
    require(control and control['control']=='task.resume'
        and control['targets']['task_resume']['item']==target
        and (control.get('recorded_receipt') or {}).get('plan_item_id')==target['plan_item_id'],'SUPPLY_RECEIPT_CHANGED')
    actual=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(row['agent_turn_id'],)).fetchone()
    action=json.loads(actual['action_json'])
    require(actual['tool_status']=='completed' and actual['child_id']==prompt['child_id']
        and actual['session_id']==prompt['origin_session_id']
        and set(action)=={'type','source','local_only','cloud_transmission_allowed',
            'voice_input_id','voice_input_binding','agent_runtime'}
        and action['source']=='content_free_voice_control','SUPPLY_VOICE_CHANGED')
    voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=actual['session_id'],child_id=actual['child_id'],
        voice_input_id=action['voice_input_id'],transcript=actual['transcript'])
    require(AgentRuntimeStore.voice_capture_snapshot(voice)==action['voice_input_binding']
        and datetime.fromisoformat(actual['created_at'])<=datetime.fromisoformat(prompt['created_at']),'SUPPLY_CAPTURE_CHANGED')


def execution_control_receipt(c, row, prompt, objective_id, *, control_prompt=None):
    """Same-task timer controls are not teaching material or answer authority."""
    from .voice_control_intent import VoiceControlIntent
    from .agent_runtime import AgentRuntimeStore
    origin = c.execute('''SELECT * FROM agent_plan WHERE child_id=? AND session_id=? AND objective_id=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,
                         '$.scheduled_writing_goal.prompt_id')=? ORDER BY rowid LIMIT 1''',
        (prompt['child_id'],prompt['origin_session_id'],objective_id,prompt['prompt_id'])).fetchone()
    require(origin, 'CONTROL_UNBOUND')
    validate_origin(c,origin,prompt)
    control_prompt=control_prompt if control_prompt is not None else prompt
    require(all(control_prompt[key]==prompt[key] for key in
        ('child_id','origin_session_id','diagnostic_item_id','item_version')), 'CONTROL_CHANGED')
    marker=json.loads(origin['context_manifest_json'])[KEY]
    receipt=VoiceControlIntent.inspect_execution(c,row['agent_turn_id'])
    actual=c.execute('''SELECT t.*,s.status AS step_status,p.status AS plan_status FROM agent_turn t
        JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id) WHERE s.step_id=?''',(row['step_id'],)).fetchone()
    require(actual and actual['tool_status'] == actual['step_status'] == 'completed'
        and actual['plan_status'] in {'completed','waiting_child'}
        and actual['child_id']==prompt['child_id'] and actual['session_id']==prompt['origin_session_id'], 'CONTROL_CHANGED')
    require(datetime.fromisoformat(control_prompt['created_at'])<=datetime.fromisoformat(actual['created_at']),
        'CONTROL_CHANGED')
    require(receipt and receipt['control']==row['capability_name'] and receipt.get('recorded_receipt')
        and receipt['recorded_receipt']['plan_item_id']==marker['plan_item_id'], 'CONTROL_CHANGED')
    action=json.loads(actual['action_json'])
    require(set(action) == {'type','source','local_only','cloud_transmission_allowed','voice_input_id',
        'voice_input_binding','agent_runtime'} and action['source']=='content_free_voice_control', 'CONTROL_CHANGED')
    capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=prompt['origin_session_id'],child_id=prompt['child_id'],
        voice_input_id=action['voice_input_id'],transcript=actual['transcript'])
    require(AgentRuntimeStore.voice_capture_snapshot(capture)==action['voice_input_binding']
        and action['voice_input_binding']['prompt_id']==control_prompt['prompt_id']
        and action['voice_input_binding']['item_version']==control_prompt['item_version'], 'CONTROL_CHANGED')
