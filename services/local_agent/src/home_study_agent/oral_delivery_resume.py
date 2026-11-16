"""Explicit continuation of an unanswered delivery, with immutable session lineage."""
import json
from datetime import datetime,timedelta
from uuid import NAMESPACE_URL, uuid5

from .family_clock import evidence_calendar_date
from .local_supply import digest
from .oral_supply_obligation import LOCAL, ORIGIN
from .oral_supply_execution import require

SOURCE = 'oral_delivered_continuation'
VERSION = 'oral_delivery_resume_v1'
RECEIPT = 'oral_delivery_continuation_receipt'


def _records(c, child, resolution):
    plan = c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=? AND child_id=? AND session_id=?',
        (resolution.get('turn_id'),child,resolution.get('session_id'))).fetchone()
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
        (resolution.get('prompt_id'),child,resolution.get('session_id'))).fetchone()
    task = c.execute('''SELECT pi.*,dp.child_id,dp.session_id,ds.status AS session_status,ds.ended_at
        FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
        WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?''',
        (resolution.get('plan_item_id'),child,resolution.get('session_id'))).fetchone()
    require(plan and prompt and task and plan['objective_id']==resolution.get('objective_id')
        and prompt['diagnostic_item_id']==resolution.get('diagnostic_item_id')
        and prompt['item_version']==resolution.get('item_version'))
    return plan,prompt,task


def _predecessor(c, child, resolution):
    plan,prompt,task = _records(c,child,resolution)
    require(prompt['status']=='waiting_answer' and not prompt['attempt_id']
        and task['status']=='rescheduled' and task['session_status']=='completed' and task['ended_at'])
    events=c.execute("SELECT * FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled' ORDER BY occurred_at",
        (task['plan_item_id'],)).fetchall()
    require(len(events)==1 and prompt['created_at']<=events[0]['occurred_at']<=task['ended_at'])
    goal=c.execute('SELECT status FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?',
        (resolution['objective_id'],child,resolution['session_id'])).fetchone()
    require(goal and goal['status'] in ('active','paused'))
    from .learning_obligations import _question_catalog,_tutor_exposure_index,_tutor_refs
    catalog,identities=_question_catalog(c)
    ended=datetime.fromisoformat(task['ended_at'])
    support=_tutor_refs(catalog,identities,_tutor_exposure_index(c,child,catalog,identities),
        prompt['diagnostic_item_id'],ended,window=ended-datetime.fromisoformat(prompt['created_at'])+timedelta(microseconds=1))
    inherited=((json.loads(plan['context_manifest_json']).get('supply_resume_receipt') or {}).get('supply_item') or {}).get('planning_evidence',{}).get('minimum_hint_level',0)
    require(type(inherited) is int and 0<=inherited<=5)
    minimum_hint=max(inherited,1 if support else 0)
    return prompt,task,digest([dict(prompt),dict(events[0]),resolution,task['ended_at'],support,minimum_hint]),minimum_hint


def lineage(c, child, root_id, *, until_prompt=None):
    """Walk completed receipts iteratively; a read cannot make an old command live."""
    from .oral_supply_execution import delivery_projection
    from .scheduled_writing_goal import _start_receipt
    require(c.in_transaction)
    root=c.execute('SELECT * FROM agent_commitment WHERE commitment_id=? AND child_id=?',(root_id,child)).fetchone()
    require(root)
    payload=json.loads(root['payload_json'])
    if payload.get('origin')=='daily_learning_closure':
        from .oral_daily_check import initial_delivery
        resolution,version=initial_delivery(c,child,root_id)
    else:
        require(root['status']=='completed' and payload.get('origin')==ORIGIN and payload.get('phase')=='question_prepared')
        resolution=payload.get('resolution')
        require(isinstance(resolution,dict))
        version=digest(payload)
    initial=delivery_projection(c,child,root_id,resolution)
    require(initial['status']!='source_changed')
    seen=set()
    while True:
        require(resolution['prompt_id'] not in seen)
        seen.add(resolution['prompt_id'])
        if until_prompt==resolution['prompt_id']:
            return resolution,version
        rows=c.execute('''SELECT * FROM agent_plan WHERE child_id=?
            AND json_extract(context_manifest_json,'$.oral_delivery_continuation_receipt.root_commitment_id')=?
            AND json_extract(context_manifest_json,'$.oral_delivery_continuation_receipt.previous_resolution.prompt_id')=?''',
            (child,root_id,resolution['prompt_id'])).fetchall()
        if not rows:
            orphan=c.execute('''SELECT 1 FROM agent_plan p JOIN agent_step s USING(plan_id)
                JOIN plan_item pi ON pi.plan_item_id=json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')
                JOIN daily_plan dp ON dp.plan_id=pi.plan_id AND dp.child_id=p.child_id AND dp.session_id=p.session_id
                WHERE p.child_id=? AND pi.source_type=? AND pi.source_ref=? AND s.step_index=0 AND s.status='completed'
                AND json_extract(p.context_manifest_json,'$.supply_resume_receipt.supply_item.planning_evidence.previous_resolution.prompt_id')=?
                LIMIT 1''',(child,SOURCE,root_id,resolution['prompt_id'])).fetchone()
            require(not orphan)
            require(until_prompt is None)
            return resolution,version
        require(len(rows)==1)
        plan=rows[0]
        manifest=json.loads(plan['context_manifest_json'])
        receipt=manifest[RECEIPT]
        binding=(manifest.get('supply_resume_receipt') or {}).get('supply_item',{}).get('planning_evidence',{})
        require(receipt.get('version')==VERSION and receipt.get('previous_resolution')==resolution
            and receipt.get('binding_version')==digest(binding) and binding.get('previous_resolution')==resolution
            and binding.get('lineage_version')==version and binding.get('root_commitment_id')==root_id)
        previous,old_task,previous_version,_=_predecessor(c,child,resolution)
        require(previous_version==binding.get('previous_version'))
        following=receipt.get('resolution')
        require(isinstance(following,dict) and following.get('turn_id')==plan['agent_turn_id'])
        actual,prompt,task=_records(c,child,following)
        require(actual['plan_id']==plan['plan_id'] and task['source_type']==SOURCE and task['source_ref']==root_id
            and prompt['diagnostic_item_id']==previous['diagnostic_item_id']
            and prompt['item_version']==previous['item_version'] and prompt['created_at']>=old_task['ended_at']
            and prompt['origin_session_id']!=previous['origin_session_id'])
        goal_binding=manifest.get('goal_prompt_binding') or {}
        require(goal_binding.get('binding')==binding and goal_binding.get('oral_supply') is True
            and goal_binding.get('plan_item_id')==task['plan_item_id'] and goal_binding.get('prompt_id')==prompt['prompt_id'])
        _start_receipt(c,plan,prompt,goal_binding)
        resolution,version=following,digest([version,receipt])


def validate_source(c,target):
    from .diagnostic_prompt import DiagnosticPromptService,_version
    require(c.in_transaction)
    binding=target['supply_item']['planning_evidence']
    require(binding.get('version')==VERSION and binding.get('child_id')==target['child_id']
        and binding.get('session_id')==target['session_id'] and binding.get('root_commitment_id')==target['source_ref']
        and target['source_type']==SOURCE and target['task_type']=='oral'
        and all(binding.get(k) is v for k,v in LOCAL.items()))
    root=c.execute('''SELECT ac.objective_id,o.session_id FROM agent_commitment ac JOIN agent_objective o
        ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id WHERE ac.commitment_id=? AND ac.child_id=?''',
        (target['source_ref'],target['child_id'])).fetchone()
    require(root and binding.get('root_objective_id')==root['objective_id'] and binding.get('root_session_id')==root['session_id'])
    previous,version=lineage(c,target['child_id'],target['source_ref'],until_prompt=binding['previous_resolution']['prompt_id'])
    require(previous==binding['previous_resolution'] and version==binding['lineage_version'])
    prompt,task,previous_version,minimum_hint=_predecessor(c,target['child_id'],previous)
    require(previous_version==binding['previous_version'] and previous['session_id']!=target['session_id']
        and type(binding.get('minimum_hint_level')) is int and binding['minimum_hint_level']==minimum_hint)
    item=DiagnosticPromptService._item(c,target['child_id'],prompt['diagnostic_item_id'])
    require(item and _version(item)==prompt['item_version']==target['item_version']
        and item['diagnostic_item_id']==target['diagnostic_item_id']
        and item['subject']==target['subject'] and target['expected_minutes']==task['expected_minutes'])
    return binding


def candidates(c,session_id,child,plan):
    from .oral_supply_carryover import unfinished_deliveries
    from .supply_gap_resume import VERSION as SUPPLY_VERSION,_consent_version,budget
    retained=unfinished_deliveries(c,child,session_id)
    if not retained:
        return []
    if c.execute("SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=? AND status IN ('waiting_answer','answer_saved')",(session_id,)).fetchone():
        return []
    if c.execute("SELECT 1 FROM plan_item WHERE plan_id=? AND status='active'",(plan['plan_id'],)).fetchone():
        return []
    result=[]
    for kept in retained:
        if not kept['decision_id'] or kept['status']!='needs_plan':
            continue
        gap=c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=? AND plan_id=?',
            (kept['decision_id'],plan['plan_id'])).fetchone()
        if not gap or c.execute("SELECT 1 FROM plan_constraint_decision WHERE plan_id=? AND decision='scheduled' AND json_extract(metadata_json,'$.supply_resume.obligation_id')=?",
                (plan['plan_id'],gap['decision_id'])).fetchone():
            continue
        try:
            root_id=kept['commitment_id']
            previous,version=lineage(c,child,root_id)
            prompt,old_task,previous_version,minimum_hint=_predecessor(c,child,previous)
            require(str(evidence_calendar_date(old_task['ended_at']))<plan['plan_date'])
            root=c.execute('''SELECT ac.objective_id,o.session_id FROM agent_commitment ac JOIN agent_objective o
                ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id WHERE ac.commitment_id=? AND ac.child_id=?''',
                (root_id,child)).fetchone()
            binding=dict(version=VERSION,child_id=child,session_id=session_id,root_commitment_id=root_id,
                root_objective_id=root['objective_id'],root_session_id=root['session_id'],
                previous_resolution=previous,previous_version=previous_version,lineage_version=version,
                minimum_hint_level=minimum_hint,**LOCAL)
            title=kept['subject']+'：接着回答上次未答完的原题'
            selected=dict(source_type=SOURCE,source_ref=root_id,subject=kept['subject'],title=title,
                minutes=old_task['expected_minutes'],completion_standard='接着回答同一道题并核对反馈；不冒充新题迁移测试。',
                cognitive_load='medium',task_type='oral',planning_evidence=binding)
            target=dict(version=SUPPLY_VERSION,operation='supply',obligation_id=gap['decision_id'],
                obligation_version=digest(dict(gap)),plan_id=plan['plan_id'],plan_version=plan['version'],
                control_consent_version=_consent_version(c),plan_item_id=str(uuid5(NAMESPACE_URL,'hsc-oral-continuation:'+gap['decision_id'])),
                child_id=child,session_id=session_id,source_type=SOURCE,source_ref=root_id,task_type='oral',
                subject=kept['subject'],title=title,homework_id=None,expected_minutes=old_task['expected_minutes'],
                started_at=None,active_seconds=0,diagnostic_item_id=prompt['diagnostic_item_id'],item_version=prompt['item_version'],
                goal_prompt_binding=None,pause_event_id=None,origin_turn_id=None,supply_item=selected)
            validate_source(c,target)
            budget(c,target)
            result.append(target)
        except (ValueError,KeyError,TypeError):
            continue
    return result


def validate_gap(c,target,gap):
    require(gap['obligation_type']==SOURCE and gap['decision']=='deferred' and gap['source_ref']==target['source_ref']
        and gap['plan_id']==target['plan_id'] and digest(dict(gap))==target['obligation_version'])
    stored=json.loads(gap['metadata_json']).get(SOURCE) or {}
    require(stored.get('child_id')==target['child_id'] and stored.get('root_commitment_id')==target['source_ref']
        and stored.get('prompt_id')==target['supply_item']['planning_evidence']['previous_resolution']['prompt_id'])
    return True


def resolve_execution(c,session_id,plan_item_id,*,historical=False):
    from .diagnostic_prompt import DiagnosticPromptService
    require(c.in_transaction)
    task=c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status FROM plan_item pi
        JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=? AND dp.session_id=?''',(plan_item_id,session_id)).fetchone()
    require(task and task['source_type']==SOURCE and task['plan_status']!='cancelled'
        and (historical or task['plan_status']=='active')
        and task['status'] in (('active','paused','completed','rescheduled') if historical else ('pending','active','paused','completed')))
    rows=c.execute('''SELECT metadata_json FROM plan_constraint_decision WHERE plan_id=? AND decision='scheduled'
        AND obligation_type=? AND json_extract(metadata_json,'$.plan_item_id')=?''',(task['plan_id'],SOURCE,plan_item_id)).fetchall()
    require(len(rows)==1)
    receipt=json.loads(rows[0][0])
    target=receipt.get('supply_resume') or {}
    require(all(target.get(k)==task[k] for k in ('plan_item_id','plan_id','child_id','session_id','source_type','source_ref',
        'subject','task_type','expected_minutes')))
    plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=? AND child_id=? AND session_id=?',
        (receipt.get('requested_by_turn_id'),task['child_id'],session_id)).fetchone()
    require(plan and json.loads(plan['context_manifest_json']).get('supply_resume_receipt')==target)
    binding=validate_source(c,target)
    manifest=json.loads(plan['context_manifest_json'])
    step=c.execute('SELECT status FROM agent_step WHERE plan_id=? AND step_index=0',(plan['plan_id'],)).fetchone()
    if RECEIPT in manifest or step and step['status']=='completed':
        saved=manifest.get(RECEIPT) or {}
        q=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
            ((manifest.get('goal_prompt_binding') or {}).get('prompt_id'),task['child_id'],session_id)).fetchone()
        require(q and saved==_receipt(binding,plan['agent_turn_id'],plan_item_id,q,plan['objective_id']))
        if step and step['status']=='completed':
            from .scheduled_writing_goal import _start_receipt
            _start_receipt(c,plan,q,manifest['goal_prompt_binding'])
    if historical and task['status']=='rescheduled':
        require(c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled'",
            (plan_item_id,)).fetchone()[0]==1)
    item=DiagnosticPromptService._item(c,task['child_id'],target['diagnostic_item_id'])
    return dict(status='ready',binding=binding,diagnostic_item=item,diagnostic_item_id=target['diagnostic_item_id'],
        item_version=target['item_version'],child_id=task['child_id'],**LOCAL)


def _receipt(binding,turn_id,plan_item_id,prompt,objective_id):
    return dict(version=VERSION,root_commitment_id=binding['root_commitment_id'],
        previous_resolution=binding['previous_resolution'],binding_version=digest(binding),
        resolution=dict(turn_id=turn_id,prompt_id=prompt['prompt_id'],session_id=prompt['origin_session_id'],
            plan_item_id=plan_item_id,objective_id=objective_id,diagnostic_item_id=prompt['diagnostic_item_id'],
            item_version=prompt['item_version'],learning_completed=False),**LOCAL)


def answer_hint_floor(c,prompt):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='agent_plan'").fetchone():
        return 0
    rows=c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(context_manifest_json,'$.scheduled_writing_goal.prompt_id')=?
        AND json_extract(context_manifest_json,'$.supply_resume_receipt.source_type')=?''',
        (prompt['child_id'],prompt['origin_session_id'],prompt['prompt_id'],SOURCE)).fetchall()
    if not rows:
        return 0
    require(len(rows)==1)
    task=json.loads(rows[0][0])['goal_prompt_binding']
    resolved=resolve_execution(c,prompt['origin_session_id'],task['plan_item_id'])
    require(resolved['binding']==task['binding'])
    return resolved['binding']['minimum_hint_level']


def delivered(c,turn_id,plan_item_id,prompt):
    resolved=resolve_execution(c,prompt['origin_session_id'],plan_item_id)
    binding=resolved['binding']
    plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?',(turn_id,)).fetchone()
    manifest=json.loads(plan['context_manifest_json'])
    bound=json.loads(plan['plan_json'])['local_learning_binding']
    require(bound['prompt_id']==prompt['prompt_id'] and bound['root_commitment_id']==binding['root_commitment_id'])
    existing=c.execute('''SELECT agent_turn_id,context_manifest_json FROM agent_plan WHERE child_id=?
        AND json_extract(context_manifest_json,'$.oral_delivery_continuation_receipt.previous_resolution.prompt_id')=?''',
        (prompt['child_id'],binding['previous_resolution']['prompt_id'])).fetchall()
    if existing:
        require(len(existing)==1)
        saved=json.loads(existing[0]['context_manifest_json'])[RECEIPT]
        require(saved['resolution']['prompt_id']==prompt['prompt_id'] and saved['resolution']['plan_item_id']==plan_item_id)
        return
    task=c.execute('SELECT status,started_at FROM plan_item WHERE plan_item_id=?',(plan_item_id,)).fetchone()
    require(task and task['status']=='active' and task['started_at'])
    receipt=_receipt(binding,turn_id,plan_item_id,prompt,bound['objective_id'])
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_oral_delivery_single_successor
        ON agent_plan(json_extract(context_manifest_json,'$.oral_delivery_continuation_receipt.previous_resolution.prompt_id'))
        WHERE json_type(context_manifest_json,'$.oral_delivery_continuation_receipt')='object' ''')
    manifest[RECEIPT]=receipt
    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',(json.dumps(manifest,ensure_ascii=False),turn_id))


def workspace(c,child,session_id,plan_id):
    from .oral_supply_execution import delivery_projection
    rows=c.execute('''SELECT d.decision_id,d.source_ref,pi.plan_item_id,pi.status FROM plan_constraint_decision d
        JOIN plan_item pi ON pi.plan_id=d.plan_id AND pi.source_ref=d.source_ref AND pi.source_type=?
        WHERE d.plan_id=? AND d.obligation_type=? AND d.decision='deferred' ''',(SOURCE,plan_id,SOURCE)).fetchall()
    result=[]
    for row in rows:
        try:
            resolution,_=lineage(c,child,row['source_ref'])
            require(resolution['session_id']==session_id and resolution['plan_item_id']==row['plan_item_id'])
            state=delivery_projection(c,child,row['source_ref'],resolution)
        except (ValueError,KeyError,TypeError):
            state=dict(status='source_changed',next_action='原题接续记录需要核对，已保存的回答保留。')
        result.append(dict(obligation_id=row['decision_id'],root_obligation_id=row['source_ref'],title='接着回答原题',
            status='active' if state['status']=='ready' else state['status'],message=state['next_action'],
            plan_item_id=row['plan_item_id'],task_status=row['status'],**LOCAL))
    return result
