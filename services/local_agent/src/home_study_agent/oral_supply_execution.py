"""Cross-day oral delivery: original evidence, current admission, one transaction."""
import json
from datetime import datetime,timezone
from uuid import NAMESPACE_URL,uuid5

from .local_supply import digest
from .oral_supply_obligation import LOCAL,ORIGIN,identity
from .task_start_binding import TaskStartBindingError

SOURCE='oral_question_supply'


def require(value):
    if not value:
        raise TaskStartBindingError('SUPPLY_RESUME_ORAL_SOURCE_CHANGED')


def candidates(c,session_id,child_id,plan):
    from .oral_supply_carryover import pending,resolve_request
    from .supply_gap_resume import VERSION
    result=[]
    for root in pending(c,child_id):
        resolved=resolve_request(c,child_id,session_id,root['commitment_id'])
        if resolved['status']!='ready':
            continue
        binding=resolved['binding']
        action=binding['source']
        title=root['subject']+'：继续上次请求的口述新题'
        selected=dict(source_type=SOURCE,source_ref=root['commitment_id'],subject=root['subject'],
            title=title,minutes=binding['expected_minutes'],completion_standard='独立提交这道新题的口述过程并核对反馈；作答不等于掌握。',
            cognitive_load='medium',task_type='oral',planning_evidence=binding)
        result.append(dict(version=VERSION,operation='supply',obligation_id=binding['obligation_id'],
            obligation_version=binding['obligation_version'],plan_id=plan['plan_id'],plan_version=plan['version'],
            control_consent_version=binding['control_consent_version'],
            plan_item_id=str(uuid5(NAMESPACE_URL,'hsc-oral-supply:'+binding['obligation_id']+':'+action['item_version'])),
            child_id=child_id,session_id=session_id,source_type=SOURCE,source_ref=root['commitment_id'],
            task_type='oral',subject=root['subject'],title=title,homework_id=None,expected_minutes=binding['expected_minutes'],
            started_at=None,active_seconds=0,diagnostic_item_id=action['diagnostic_item_id'],item_version=action['item_version'],
            goal_prompt_binding=None,pause_event_id=None,origin_turn_id=None,supply_item=selected))
    return result


def validate_source(c,target):
    from .agent_runtime import AgentRuntimeStore
    from .oral_supply_carryover import RESUME_VERSION,request_proof_version
    require(c.in_transaction)
    binding=target['supply_item']['planning_evidence']
    require(binding.get('version')==RESUME_VERSION and binding['child_id']==target['child_id']
        and binding['session_id']==target['session_id'] and binding['root_commitment_id']==target['source_ref']
        and binding['original_request_version']==request_proof_version(c,target['child_id'],target['source_ref']))
    root=c.execute('''SELECT ac.*,o.session_id,o.status AS goal_status FROM agent_commitment ac
        JOIN agent_objective o ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id
        WHERE ac.commitment_id=? AND ac.child_id=? AND o.objective_type='learning' ''',
        (target['source_ref'],target['child_id'])).fetchone()
    require(root and root['objective_id']==binding['root_objective_id']
        and root['session_id']==binding['root_session_id'] and root['goal_status'] in ('active','paused'))
    payload=json.loads(root['payload_json'])
    require(payload.get('origin')==ORIGIN and payload.get('source_hash')==binding['source_hash']
        and payload.get('source')==identity(binding['source']))
    rows=c.execute('''SELECT DISTINCT q.* FROM agent_plan p JOIN diagnostic_prompt q
        ON q.prompt_id=json_extract(p.context_manifest_json,'$.goal_prompt_binding.prompt_id')
        WHERE p.child_id=? AND p.session_id=? AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
        AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?''',
        (target['child_id'],target['session_id'],target['plan_item_id'])).fetchall()
    require(len(rows)<=1)
    prompt=rows[0] if rows else None
    if root['status']=='completed':
        resolution=payload.get('resolution') or {}
        require(prompt and payload.get('phase')=='question_prepared'
            and resolution.get('prompt_id')==prompt['prompt_id'] and resolution.get('session_id')==target['session_id']
            and resolution.get('plan_item_id')==target['plan_item_id'] and resolution.get('item_version')==prompt['item_version'])
        goal=c.execute('SELECT status FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
            (resolution.get('objective_id'),target['child_id'],target['session_id'])).fetchone()
        require(goal and goal['status'] in ('active','paused','completed'))
    else:
        require(root['status'] in ('pending','due') and payload.get('phase')=='awaiting_qualified_question')
    action=binding['source']
    checked=AgentRuntimeStore._resolve_oral_followup_source(c,child_id=target['child_id'],
        session_id=binding['root_session_id'],prompt_id=action['parent_prompt_id'],item_version=action['parent_item_version'],
        kind='independent_check',_diagnostic_item_id=action['diagnostic_item_id'],
        _prepared_prompt_id=prompt['prompt_id'] if prompt else None,_history_before=prompt['created_at'] if prompt else None)
    require(checked.get('action')==action)
    return binding


def validate_gap(c,target,gap):
    from .oral_supply_carryover import current_plan
    binding=target['supply_item']['planning_evidence']
    row=current_plan(c,target['child_id'],target['source_ref'],binding['source_hash'])
    require(row and row['decision_id']==gap['decision_id'] and row['plan_id']==target['plan_id']
        and digest(dict(row))==target['obligation_version'])
    return True


def resolve_execution(c,session_id,plan_item_id):
    return _resolve_delivery(c,session_id,plan_item_id,historical=False)


def _resolve_delivery(c,session_id,plan_item_id,*,historical):
    from .diagnostic_prompt import DiagnosticPromptService
    require(c.in_transaction)
    kind=c.execute('SELECT source_type FROM plan_item WHERE plan_item_id=?',(plan_item_id,)).fetchone()
    if kind and kind[0]=='oral_daily_check':
        from .oral_daily_check import resolve_execution as resolve_daily
        return resolve_daily(c,session_id,plan_item_id,historical=historical)
    if kind and kind[0]=='oral_delivered_continuation':
        from .oral_delivery_resume import resolve_execution as resolve_continuation
        return resolve_continuation(c,session_id,plan_item_id,historical=historical)
    item=c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status
        FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE pi.plan_item_id=? AND dp.session_id=?''',(plan_item_id,session_id)).fetchone()
    require(item and item['source_type']==SOURCE and item['plan_status']!='cancelled'
        and (historical or item['plan_status']=='active')
        and item['status'] in (('active','paused','completed','rescheduled') if historical
                              else ('pending','active','paused','completed')))
    if historical and item['status']=='rescheduled':
        require(c.execute("SELECT 1 FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled'",
            (plan_item_id,)).fetchone())
    records=c.execute('''SELECT metadata_json FROM plan_constraint_decision WHERE plan_id=? AND decision='scheduled'
        AND obligation_type=? AND json_extract(metadata_json,'$.plan_item_id')=?''',
        (item['plan_id'],SOURCE,plan_item_id)).fetchall()
    require(len(records)==1)
    receipt=json.loads(records[0][0])
    target=receipt.get('supply_resume')
    require(isinstance(target,dict) and all(target.get(k)==item[k] for k in
        ('plan_item_id','plan_id','child_id','session_id','source_type','source_ref','subject','task_type','expected_minutes')))
    origin=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=? AND child_id=? AND session_id=?',
        (receipt.get('requested_by_turn_id'),item['child_id'],session_id)).fetchone()
    require(origin and json.loads(origin[0]).get('supply_resume_receipt')==target)
    binding=validate_source(c,target)
    action=binding['source']
    question=DiagnosticPromptService._item(c,item['child_id'],action['diagnostic_item_id'])
    require(question and question['answer_type'] in ('open_response','speaking','read_aloud'))
    return dict(status='ready',binding=binding,diagnostic_item=question,
        diagnostic_item_id=action['diagnostic_item_id'],item_version=action['item_version'],child_id=item['child_id'],**LOCAL)


def delivered(c,turn_id,plan_item_id,prompt):
    kind=c.execute('SELECT source_type FROM plan_item WHERE plan_item_id=?',(plan_item_id,)).fetchone()
    if kind and kind[0]=='oral_daily_check':
        from .oral_daily_check import delivered as daily_delivered
        return daily_delivered(c,turn_id,plan_item_id,prompt)
    if kind and kind[0]=='oral_delivered_continuation':
        from .oral_delivery_resume import delivered as continued
        return continued(c,turn_id,plan_item_id,prompt)
    resolved=resolve_execution(c,prompt['origin_session_id'],plan_item_id)
    binding=resolved['binding']
    plan=c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?',(turn_id,)).fetchone()
    goal=json.loads(plan[0]).get('local_learning_binding') or {}
    require(goal.get('root_objective_id')==binding['root_objective_id'] and goal.get('prompt_id')==prompt['prompt_id'])
    item=c.execute('SELECT status,started_at FROM plan_item WHERE plan_item_id=?',(plan_item_id,)).fetchone()
    require(item and item['status']=='active' and item['started_at'])
    row=c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(binding['root_commitment_id'],)).fetchone()
    payload=json.loads(row['payload_json'])
    if row['status']=='completed':
        saved=payload.get('resolution') or {}
        require(saved.get('prompt_id')==prompt['prompt_id'] and saved.get('plan_item_id')==plan_item_id
            and saved.get('objective_id')==goal['objective_id'])
        return
    require(row['status'] in ('pending','due'))
    payload.update(phase='question_prepared',resolution=dict(turn_id=turn_id,prompt_id=prompt['prompt_id'],
        session_id=prompt['origin_session_id'],plan_item_id=plan_item_id,objective_id=goal['objective_id'],
        diagnostic_item_id=prompt['diagnostic_item_id'],item_version=prompt['item_version'],learning_completed=False))
    c.execute("UPDATE agent_commitment SET status='completed',completed_at=?,payload_json=? WHERE commitment_id=?",
        (datetime.now(timezone.utc).isoformat(),json.dumps(payload,ensure_ascii=False),binding['root_commitment_id']))


def delivery_projection(c,child_id,root_id,resolution):
    """Parent reads the linked current-day delivery, never the old prompt's state."""
    from .scheduled_writing_goal import _start_receipt
    plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=? AND child_id=? AND session_id=?',
        (resolution.get('turn_id'),child_id,resolution.get('session_id'))).fetchone()
    prompt=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
        (resolution.get('prompt_id'),child_id,resolution.get('session_id'))).fetchone()
    require(plan and prompt and plan['objective_id']==resolution.get('objective_id')
        and prompt['item_version']==resolution.get('item_version')
        and prompt['diagnostic_item_id']==resolution.get('diagnostic_item_id'))
    task=json.loads(plan['context_manifest_json']).get('goal_prompt_binding') or {}
    require(task.get('oral_supply') and task.get('plan_item_id')==resolution.get('plan_item_id')
        and task.get('prompt_id')==prompt['prompt_id'] and task['binding']['root_commitment_id']==root_id)
    owner=c.execute('''SELECT ds.status AS session_status,o.status AS goal_status,pi.status AS task_status
        FROM daily_session ds JOIN agent_objective o ON o.session_id=ds.session_id AND o.child_id=ds.child_id
        JOIN daily_plan dp ON dp.session_id=ds.session_id AND dp.child_id=ds.child_id
        JOIN plan_item pi USING(plan_id) WHERE o.objective_id=? AND ds.child_id=? AND pi.plan_item_id=?''',
        (resolution['objective_id'],child_id,resolution['plan_item_id'])).fetchone()
    require(owner)
    state='source_changed'
    message='今天的新题交付或来源需要核对，原回答保留，不重新计时或出题。'
    if owner['goal_status'] in ('cancelled','failed') or owner['task_status']=='cancelled':
        state,message='cancelled','今天的续行已取消，历史题目与回答保留，不再执行旧指令。'
    else:
        _start_receipt(c,plan,prompt,task)
        _resolve_delivery(c,resolution['session_id'],resolution['plan_item_id'],historical=True)
        if prompt['status']=='answered' and prompt['attempt_id']:
            state,message='practice_completed','今天的新题已有实际作答记录；作答不等于掌握，还需反馈与后续保持检查。'
        elif owner['session_status']!='active' or owner['task_status']=='rescheduled':
            state,message='needs_plan','今天这道新题的进度已保留，后续继续仍需核对新的时间安排。'
        elif owner['goal_status']=='paused' or owner['task_status']=='paused':
            state,message='paused','今天这道新题已暂停，原进度保留，等孩子愿意继续。'
        elif prompt['status']=='waiting_answer' and owner['task_status']=='active':
            state,message='ready','今天的新题已准备并开始，等待孩子自己的回答，不重复出题。'
        elif prompt['status']=='answer_saved':
            state,message='needs_replan','今天的回答已保存，正在核对结果，不要求孩子重答。'
    return dict(status=state,next_action=message,plan_item_id=resolution['plan_item_id'],
        current_session_id=resolution['session_id'] if owner['session_status']=='active' else None)
