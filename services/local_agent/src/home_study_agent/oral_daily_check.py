"""Source-bound next-day oral checks; a model reminder is not a test item."""
import json
from datetime import datetime,timezone

from .local_supply import digest
from .oral_supply_obligation import LOCAL
from .oral_supply_execution import require

SOURCE='oral_daily_check'
VERSION='oral_daily_check_v1'
RECEIPT='oral_daily_check_receipt'


def recover_request(c,turn_id):
    """Re-arm a rolled-back storage failure, never a stale or denied command."""
    from .voice_control_intent import VoiceControlIntent
    row=c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
        s.status AS step_status,s.error_code,s.capability_name,s.idempotency_key
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
          AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
    manifest=json.loads(row['context_manifest_json']) if row else {}
    binding=manifest.get('task_start_binding',{})
    if binding.get('source_type')!=SOURCE:
        return
    require(c.in_transaction and row['capability_name']=='task.start'
        and row['step_status']==row['plan_status']==row['tool_status']=='failed'
        and row['error_code'] in {'IntegrityError','OperationalError','OSError','APP_RESTARTED'}
        and row['tool_error_code']==row['error_code'])
    control=VoiceControlIntent.inspect_execution(c,turn_id,allow_planned=True)
    if manifest.get('voice_control_binding'):
        require(control and control['control']=='task.start' and not control.get('recorded_receipt'))
    from .task_start_binding import TaskStartBinding,TaskStartBindingError
    voice=manifest.get('voice_input_binding')
    latest=c.execute('SELECT voice_input_id FROM voice_capture_binding WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1',
        (row['child_id'],row['session_id'])).fetchone()
    require(voice and latest and latest[0]==voice.get('voice_input_id') and row['intent']!='runtime_continuation')
    try:
        VoiceControlIntent.assert_current_input(c,row['session_id'],row['child_id'],row['client_action_id'],row['transcript'],voice)
    except (ValueError,KeyError,TypeError) as error:
        raise TaskStartBindingError('SUPPLY_RESUME_ORAL_SOURCE_CHANGED') from error
    arrival=c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?',
        (manifest.get('learning_input_arrival_id'),row['child_id'],row['session_id'],row['client_action_id'])).fetchone()
    snapshots=[r['snapshot'] for r in json.loads(arrival[0]) if r.get('kind')=='task_navigation_arrival'] if arrival else []
    frozen_arrival=manifest.get('task_start_arrival') or {}
    expected_arrival=frozen_arrival.get('semantic_start',{}).get('origin',frozen_arrival)
    require(snapshots==[expected_arrival])
    target=TaskStartBinding.validate_start_snapshot(c,row['session_id'],row['child_id'],manifest['task_start_arrival'],turn_id=turn_id)
    require(binding=={**{k:target[k] for k in ('plan_item_id','source_type','source_ref')},'operation':'start'})
    resolve_execution(c,row['session_id'],binding['plan_item_id'])
    require(not c.execute('SELECT 1 FROM diagnostic_prompt WHERE prepare_action_id=?',
        ('goal-task:'+row['idempotency_key'],)).fetchone())
    c.execute("UPDATE agent_turn SET tool_status='pending',tool_error_code=NULL,completed_at=NULL WHERE agent_turn_id=?",(turn_id,))


def original(c,child,commitment_id):
    root=c.execute('''SELECT ac.*,ao.session_id,ao.status AS goal_status,ds.status AS session_status,ds.ended_at
        FROM agent_commitment ac JOIN agent_objective ao ON ao.objective_id=ac.objective_id AND ao.child_id=ac.child_id
        JOIN daily_session ds ON ds.session_id=ao.session_id AND ds.child_id=ao.child_id
        WHERE ac.commitment_id=? AND ac.child_id=?''',(commitment_id,child)).fetchone()
    if not root:
        return None
    payload=json.loads(root['payload_json'])
    if root['commitment_type']!='follow_up' or payload.get('origin')!='daily_learning_closure':
        return None
    # Older learning-only databases have no presented questions to bind.
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
        return None
    rows=c.execute('''SELECT q.*,a.submitted_answer,a.evidence_id,i.answer_type,i.expected_answer_json,
        s.arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id)
        JOIN diagnostic_attempt a ON a.evidence_id=json_extract(s.arguments_json,'$.evidence_id')
          AND a.child_id=p.child_id AND a.session_id=p.session_id
        JOIN diagnostic_prompt q ON q.attempt_id=a.attempt_id AND q.child_id=a.child_id AND q.origin_session_id=a.session_id
        JOIN diagnostic_item i ON i.diagnostic_item_id=q.diagnostic_item_id AND i.diagnostic_item_id=a.diagnostic_item_id
        WHERE s.step_id=? AND p.objective_id=? AND p.child_id=? AND p.session_id=?
          AND s.capability_name='learning.evaluate_response' AND s.status='completed' ''',
        (payload.get('source_step_id'),root['objective_id'],child,root['session_id'])).fetchall()
    if not rows:
        return None
    require(len(rows)==1)
    prompt=rows[0]
    rubric=json.loads(prompt['expected_answer_json'])
    if not (prompt['answer_type']=='open_response' and rubric.get('response_mode')=='spoken_explanation'):
        return None
    from .learning_outcome_projection import LearningOutcomeProjection
    require(root['goal_status'] in ('active','paused') and root['status'] in ('pending','due','completed')
        and root['session_status']=='completed' and root['ended_at'] and prompt['status']=='answered'
        and payload.get('source_outcome_id') and LearningOutcomeProjection(None).commitment_available(c,root))
    version=digest([{k:root[k] for k in ('commitment_id','objective_id','child_id','session_id','payload_json','created_at','due_at','ended_at')},dict(prompt)])
    return root,prompt,version


def planning(c,child,session_id,commitment_id):
    """None leaves non-oral legacy duties untouched; oral failures stay visible."""
    from .agent_runtime import AgentRuntimeStore
    value=original(c,child,commitment_id)
    if value is None:
        return None
    root,prompt,version=value
    require(root['session_id']!=session_id and root['status'] in ('pending','due')
        and datetime.fromisoformat(root['due_at'])<=datetime.now(timezone.utc))
    selected=AgentRuntimeStore._resolve_oral_followup_source(c,child_id=child,session_id=root['session_id'],
        prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],kind='independent_check')
    if selected['status']!='ready':
        return dict(status='needs_supply',reason=selected['reason'],**LOCAL)
    binding=dict(version=VERSION,child_id=child,session_id=session_id,root_commitment_id=commitment_id,
        root_objective_id=root['objective_id'],root_session_id=root['session_id'],original_version=version,
        source=selected['action'],**LOCAL)
    return dict(status='ready',binding=binding,**LOCAL)


def resolve_execution(c,session_id,plan_item_id,*,historical=False):
    from .agent_runtime import AgentRuntimeStore
    from .diagnostic_prompt import DiagnosticPromptService
    require(c.in_transaction)
    task=c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status FROM plan_item pi
        JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=? AND dp.session_id=?''',
        (plan_item_id,session_id)).fetchone()
    require(task and task['source_type']==SOURCE and task['plan_status']!='cancelled'
        and (historical or task['plan_status']=='active')
        and task['status'] in (('active','paused','completed','rescheduled') if historical
                              else ('pending','active','paused','completed')))
    records=c.execute('''SELECT metadata_json,allocated_minutes FROM plan_constraint_decision
        WHERE plan_id=? AND obligation_type=? AND source_ref=? AND decision='scheduled'
        AND json_extract(metadata_json,'$.plan_item_id')=?''',
        (task['plan_id'],SOURCE,task['source_ref'],plan_item_id)).fetchall()
    require(len(records)==1 and records[0]['allocated_minutes']==task['expected_minutes']>0)
    metadata=json.loads(records[0]['metadata_json'])
    require(metadata.get('plan_item_id')==plan_item_id)
    if metadata.get('supply_resume'):
        from .oral_daily_withdrawal import proof_for_gap
        gap=c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=? AND plan_id=?',
            (metadata['supply_resume'].get('obligation_id'),task['plan_id'])).fetchone()
        require(gap and gap['source_ref']==task['source_ref'])
        proof_for_gap(c,gap)
    binding=metadata['planning_evidence']['oral_daily_binding']
    require(binding.get('version')==VERSION and binding.get('child_id')==task['child_id']
        and binding.get('session_id')==session_id and binding.get('root_commitment_id')==task['source_ref']
        and all(binding.get(k) is v for k,v in LOCAL.items()))
    value=original(c,task['child_id'],task['source_ref'])
    require(value)
    root,parent,version=value
    require(version==binding['original_version'] and binding['root_objective_id']==root['objective_id']
        and binding['root_session_id']==root['session_id']!=session_id
        and root['due_at']<=datetime.now(timezone.utc).isoformat())
    rows=c.execute('''SELECT DISTINCT q.* FROM agent_plan p JOIN diagnostic_prompt q
        ON q.prompt_id=json_extract(CASE WHEN json_valid(p.context_manifest_json)
            THEN p.context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')
        AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
        WHERE p.child_id=? AND p.session_id=? AND json_extract(CASE WHEN json_valid(p.context_manifest_json)
            THEN p.context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.plan_item_id')=?''',
        (task['child_id'],session_id,plan_item_id)).fetchall()
    require(len(rows)<=1)
    prompt=rows[0] if rows else None
    action=binding['source']
    selected=AgentRuntimeStore._resolve_oral_followup_source(c,child_id=task['child_id'],session_id=root['session_id'],
        prompt_id=parent['prompt_id'],item_version=parent['item_version'],kind='independent_check',
        _diagnostic_item_id=action['diagnostic_item_id'],_prepared_prompt_id=prompt['prompt_id'] if prompt else None,
        _history_before=prompt['created_at'] if prompt else None)
    require(selected.get('action')==action)
    if prompt:
        origins=c.execute('''SELECT p.*,s.status AS step_status FROM agent_plan p JOIN agent_step s USING(plan_id)
            WHERE p.child_id=? AND p.session_id=? AND s.step_index=0
              AND ?='goal-task:'||s.idempotency_key
              AND json_extract(CASE WHEN json_valid(p.context_manifest_json)
                THEN p.context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')=?''',
            (task['child_id'],session_id,prompt['prepare_action_id'],prompt['prompt_id'])).fetchall()
        require(len(origins)==1)
        origin=origins[0]
        manifest=json.loads(origin['context_manifest_json'])
        if RECEIPT in manifest or origin['step_status']=='completed':
            require(manifest.get(RECEIPT)==_receipt(binding,plan_item_id,prompt,origin['objective_id']))
        if origin['step_status']=='completed':
            from .scheduled_writing_goal import _start_receipt
            _start_receipt(c,origin,prompt,manifest['goal_prompt_binding'])
    if root['status']=='completed':
        require((task['status']=='completed' and prompt and prompt['status']=='answered')
            or (historical and task['status']=='rescheduled' and prompt and prompt['status']=='waiting_answer'))
    if historical and task['status']=='rescheduled':
        require(c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled'",
            (plan_item_id,)).fetchone()[0]==1)
    item=DiagnosticPromptService._item(c,task['child_id'],action['diagnostic_item_id'])
    require(item and item['subject']==task['subject'])
    return dict(status='ready',binding=binding,diagnostic_item=item,diagnostic_item_id=item['diagnostic_item_id'],
        item_version=action['item_version'],child_id=task['child_id'],**LOCAL)


def _receipt(binding,plan_item_id,prompt,objective_id):
    return dict(version=VERSION,root_commitment_id=binding['root_commitment_id'],
        binding_version=digest(binding),prompt_id=prompt['prompt_id'],plan_item_id=plan_item_id,
        objective_id=objective_id,**LOCAL)


def retained_deliveries(c,child):
    """Normalize daily receipts for the existing unanswered-delivery lifecycle."""
    rows=c.execute('''SELECT ac.commitment_id,ac.payload_json,o.session_id AS root_session_id,
        p.agent_turn_id,p.objective_id,q.*,ds.ended_at,ds.status AS session_status,pi.plan_item_id,pi.subject
        FROM agent_commitment ac JOIN agent_objective o ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id
        JOIN child_profile cp ON cp.child_id=ac.child_id AND cp.status='active'
        JOIN plan_item pi ON pi.source_ref=ac.commitment_id AND pi.source_type=?
        JOIN daily_plan dp ON dp.plan_id=pi.plan_id AND dp.child_id=ac.child_id
        JOIN agent_plan p ON p.child_id=ac.child_id AND p.session_id=dp.session_id
          AND json_extract(CASE WHEN json_valid(p.context_manifest_json)
            THEN p.context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.plan_item_id')=pi.plan_item_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        JOIN diagnostic_prompt q ON q.prompt_id=json_extract(CASE WHEN json_valid(p.context_manifest_json)
            THEN p.context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')
          AND q.child_id=ac.child_id AND q.origin_session_id=dp.session_id AND q.prepare_action_id='goal-task:'||s.idempotency_key
        JOIN daily_session ds ON ds.session_id=q.origin_session_id AND ds.child_id=ac.child_id
        WHERE ac.child_id=? AND ac.status IN ('pending','due','completed') AND o.status IN ('active','paused')
          AND ac.commitment_type='follow_up' AND json_extract(ac.payload_json,'$.origin')='daily_learning_closure'
          AND q.status='waiting_answer' AND q.attempt_id IS NULL''',(SOURCE,child)).fetchall()
    result=[]
    for row in rows:
        resolution=dict(turn_id=row['agent_turn_id'],prompt_id=row['prompt_id'],session_id=row['origin_session_id'],
            plan_item_id=row['plan_item_id'],objective_id=row['objective_id'],diagnostic_item_id=row['diagnostic_item_id'],
            item_version=row['item_version'],learning_completed=False)
        result.append({**dict(row),'daily_check':True,'payload_json':json.dumps(dict(resolution=resolution,
            source_hash=digest(json.loads(row['payload_json']))))})
    return result


def initial_delivery(c,child,root_id):
    rows=[r for r in retained_deliveries(c,child) if r['commitment_id']==root_id]
    require(len(rows)==1)
    row=rows[0]
    payload=json.loads(row['payload_json'])
    resolution=payload['resolution']
    resolved=resolve_execution(c,resolution['session_id'],resolution['plan_item_id'],historical=True)
    plan=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(resolution['turn_id'],)).fetchone()
    receipt=json.loads(plan[0]).get(RECEIPT)
    require(receipt==_receipt(resolved['binding'],resolution['plan_item_id'],row,resolution['objective_id']))
    return resolution,digest([payload,receipt])


def delivered(c,turn_id,plan_item_id,prompt):
    resolved=resolve_execution(c,prompt['origin_session_id'],plan_item_id)
    row=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=? AND child_id=? AND session_id=?',
        (turn_id,prompt['child_id'],prompt['origin_session_id'])).fetchone()
    require(row)
    manifest=json.loads(row['context_manifest_json'])
    goal=json.loads(row['plan_json'])['local_learning_binding']
    require(goal['prompt_id']==prompt['prompt_id'] and manifest['goal_prompt_binding']['binding']==resolved['binding'])
    receipt=_receipt(resolved['binding'],plan_item_id,prompt,goal['objective_id'])
    previous=c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json)
            THEN context_manifest_json ELSE '{}' END,'$.oral_daily_check_receipt.plan_item_id')=?''',
        (prompt['child_id'],prompt['origin_session_id'],plan_item_id)).fetchall()
    if previous:
        require(len(previous)==1 and json.loads(previous[0][0])[RECEIPT]==receipt)
        return
    existing=manifest.get(RECEIPT)
    require(not existing or existing==receipt)
    manifest[RECEIPT]=receipt
    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',(json.dumps(manifest,ensure_ascii=False),turn_id))


def settle(c,item,now):
    continuation=item['source_type']=='oral_delivered_continuation'
    if continuation:
        root=c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=? AND child_id=?',
            (item['source_ref'],item['child_id'])).fetchone()
        if not root or json.loads(root[0]).get('origin')!='daily_learning_closure':
            return
    from .oral_supply_execution import resolve_execution as resolve_delivery
    resolved=resolve_delivery(c,item['session_id'],item['plan_item_id'])
    receipt_key='oral_delivery_continuation_receipt' if continuation else RECEIPT
    nested=receipt_key+('.resolution' if continuation else '')
    rows=c.execute('''SELECT p.context_manifest_json,q.status FROM agent_plan p JOIN diagnostic_prompt q
        ON q.prompt_id=json_extract(CASE WHEN json_valid(p.context_manifest_json)
            THEN p.context_manifest_json ELSE '{}' END,?)
        AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
        WHERE p.child_id=? AND p.session_id=? AND json_extract(CASE WHEN json_valid(p.context_manifest_json)
            THEN p.context_manifest_json ELSE '{}' END,?)=?''',
        ('$.'+nested+'.prompt_id',item['child_id'],item['session_id'],'$.'+nested+'.plan_item_id',item['plan_item_id'])).fetchall()
    require(len(rows)==1 and rows[0]['status']=='answered')
    manifest=json.loads(rows[0]['context_manifest_json'])
    receipt=manifest[receipt_key]
    current=receipt['resolution'] if continuation else receipt
    require(receipt['binding_version']==digest(resolved['binding'])
        and receipt['root_commitment_id']==item['source_ref'])
    feedback=c.execute('''SELECT json_extract(e.result_json,'$.evaluation.outcome_id') AS outcome_id
        FROM learning_evaluation_execution e JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id)
        JOIN agent_turn t USING(agent_turn_id) JOIN diagnostic_attempt a
          ON a.evidence_id=json_extract(s.arguments_json,'$.evidence_id') AND a.child_id=p.child_id AND a.session_id=p.session_id
        JOIN diagnostic_prompt q ON q.attempt_id=a.attempt_id AND q.child_id=a.child_id AND q.origin_session_id=a.session_id
        WHERE p.child_id=? AND p.session_id=? AND p.objective_id=? AND q.prompt_id=?
          AND e.state='completed' AND s.status='completed' AND t.tool_status='completed'
          AND s.capability_name='learning.evaluate_response' ORDER BY s.rowid DESC LIMIT 1''',
        (item['child_id'],item['session_id'],current['objective_id'],current['prompt_id'])).fetchone()
    from .learning_outcome_projection import LearningOutcomeProjection
    if not (feedback and feedback['outcome_id'] and LearningOutcomeProjection(None).source_available_outcome(
            c,item['child_id'],feedback['outcome_id'],current['objective_id'])):
        from .task_start_binding import TaskStartBindingError
        raise TaskStartBindingError('ORAL_DAILY_CHECK_FEEDBACK_REQUIRED')
    c.execute("UPDATE agent_commitment SET status='completed',completed_at=? WHERE commitment_id=? AND child_id=? AND status IN ('pending','due')",
        (now,item['source_ref'],item['child_id']))
