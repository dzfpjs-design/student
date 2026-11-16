"""Resume a retained daily oral check only after qualified supply and consent."""
from uuid import NAMESPACE_URL,uuid5

from .local_supply import digest
from .oral_daily_check import SOURCE,planning,resolve_execution
from .oral_supply_execution import require
from .oral_supply_obligation import LOCAL


def _gaps(c,plan_id):
    from .oral_daily_withdrawal import active_gaps
    rows=c.execute('''SELECT * FROM plan_constraint_decision WHERE plan_id=?
        AND obligation_type=? AND decision='deferred' ORDER BY rowid''',(plan_id,SOURCE)).fetchall()
    return active_gaps(c,rows)


def _candidate(c,child,session_id,plan,gap):
    from .oral_daily_withdrawal import proof_for_gap
    proof_for_gap(c,gap)
    from .supply_gap_resume import VERSION,_consent_version
    from .diagnostic_prompt import DiagnosticPromptService
    current=planning(c,child,session_id,gap['source_ref'])
    require(current and current['status']=='ready')
    binding=current['binding']
    action=binding['source']
    item=DiagnosticPromptService._item(c,child,action['diagnostic_item_id'])
    require(item and gap['requested_minutes']>0)
    title=item['subject']+'：继续今天待补题的口述复查'
    source=dict(source_type=SOURCE,source_ref=gap['source_ref'],subject=item['subject'],title=title,
        minutes=gap['requested_minutes'],completion_standard='回答已核对的同能力不同题并保存反馈；完成复查不等于掌握。',
        cognitive_load='medium',task_type='oral',planning_evidence={'oral_daily_binding':binding})
    return dict(version=VERSION,operation='supply',obligation_id=gap['decision_id'],obligation_version=digest(dict(gap)),
        plan_id=plan['plan_id'],plan_version=plan['version'],control_consent_version=_consent_version(c),
        plan_item_id=str(uuid5(NAMESPACE_URL,'hsc-oral-daily-supply:'+gap['decision_id'])),child_id=child,session_id=session_id,
        source_type=SOURCE,source_ref=gap['source_ref'],task_type='oral',subject=item['subject'],title=title,homework_id=None,
        expected_minutes=source['minutes'],started_at=None,active_seconds=0,diagnostic_item_id=action['diagnostic_item_id'],
        item_version=action['item_version'],goal_prompt_binding=None,pause_event_id=None,origin_turn_id=None,supply_item=source)


def candidates(c,session_id,child,plan):
    from .supply_gap_resume import budget
    require(c.in_transaction)
    gaps=_gaps(c,plan['plan_id'])
    if not gaps:
        return []
    if c.execute("SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=? AND status IN ('waiting_answer','answer_saved')",(session_id,)).fetchone():
        return []
    if c.execute("SELECT 1 FROM plan_item WHERE plan_id=? AND status='active'",(plan['plan_id'],)).fetchone():
        return []
    result=[]
    for gap in gaps:
        if c.execute("SELECT 1 FROM plan_item WHERE source_type=? AND source_ref=? AND status!='cancelled'",(SOURCE,gap['source_ref'])).fetchone():
            continue
        try:
            target=_candidate(c,child,session_id,plan,gap)
            budget(c,target)
            result.append(target)
        except (ValueError,KeyError,TypeError):
            continue
    return result


def validate_gap(c,target,gap):
    from .oral_daily_withdrawal import proof_for_gap
    proof_for_gap(c,gap)
    require(gap['decision_id'] in {r['decision_id'] for r in _gaps(c,gap['plan_id'])})
    require(gap['obligation_type']==SOURCE and gap['decision']=='deferred' and gap['source_ref']==target['source_ref']
        and gap['plan_id']==target['plan_id'] and digest(dict(gap))==target['obligation_version']
        and gap['requested_minutes']==target['expected_minutes']>0)
    return True


def validate_source(c,target):
    require(c.in_transaction and target['source_type']==SOURCE and target['task_type']=='oral')
    task=c.execute('''SELECT pi.plan_item_id FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?''',
        (target['plan_item_id'],target['child_id'],target['session_id'])).fetchone()
    if task:
        current=resolve_execution(c,target['session_id'],task['plan_item_id'])
    else:
        current=planning(c,target['child_id'],target['session_id'],target['source_ref'])
    require(current and current['status']=='ready')
    binding=current['binding']
    require(binding==target['supply_item']['planning_evidence']['oral_daily_binding']
        and binding['root_commitment_id']==target['source_ref']
        and binding['source']['diagnostic_item_id']==target['diagnostic_item_id']
        and binding['source']['item_version']==target['item_version'])
    return binding


def workspace(c,child,session_id,plan_id):
    from .supply_gap_resume import _plan,budget
    from .supply_gap_presentation import conflict_message
    from .oral_supply_execution import delivery_projection
    require(c.in_transaction)
    gaps=_gaps(c,plan_id)
    if not gaps:
        return []
    plan=_plan(c,session_id,child)
    if not plan or plan['plan_id']!=plan_id:
        return []
    result=[]
    for gap in gaps:
        record=dict(obligation_id=gap['decision_id'],root_obligation_id=gap['source_ref'],title='待补题的口述复查',
            status='missing_source',message='同能力的新题还没补齐，原复查保留，不以旧题或模型提醒冒充新题。',
            plan_item_id=None,task_status=None,**LOCAL)
        from .oral_daily_withdrawal import proof_for_gap,withdrawn_task
        try:
            proof_for_gap(c,gap)
        except (ValueError,KeyError,TypeError):
            record.update(status='source_changed',message='撤回与补题记录需要重新核对，原答案保留，暂不开始新题。')
            result.append(record)
            continue
        tasks=c.execute('SELECT * FROM plan_item WHERE plan_id=? AND source_type=? AND source_ref=? ORDER BY rowid DESC',
            (plan_id,SOURCE,gap['source_ref'])).fetchall()
        task=next((t for t in tasks if not withdrawn_task(c,t)),None)
        root=c.execute('''SELECT ac.status,o.status AS goal_status FROM agent_commitment ac JOIN agent_objective o
            ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id WHERE ac.commitment_id=? AND ac.child_id=?''',
            (gap['source_ref'],child)).fetchone()
        if (root and (root['status']=='cancelled' or root['goal_status'] in ('cancelled','failed'))) or (task and task['status']=='cancelled'):
            record.update(status='cancelled',message='这项复查已取消，历史记录保留，不再安排或催促继续。',
                plan_item_id=task['plan_item_id'] if task else None,task_status=task['status'] if task else None)
            result.append(record)
            continue
        try:
            if task:
                resolve_execution(c,session_id,task['plan_item_id'],historical=task['status']=='rescheduled')
                record.update(plan_item_id=task['plan_item_id'],task_status=task['status'])
                rows=c.execute('''SELECT p.agent_turn_id,p.objective_id,q.* FROM agent_plan p JOIN diagnostic_prompt q
                    ON q.prompt_id=json_extract(p.context_manifest_json,'$.oral_daily_check_receipt.prompt_id')
                    AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
                    WHERE p.child_id=? AND p.session_id=? AND json_extract(p.context_manifest_json,'$.oral_daily_check_receipt.plan_item_id')=?''',
                    (child,session_id,task['plan_item_id'])).fetchall()
                if not rows:
                    record.update(status='scheduled',message='复查材料已安排，等待完成题目交付。')
                else:
                    require(len(rows)==1)
                    prompt=rows[0]
                    resolution=dict(turn_id=prompt['agent_turn_id'],objective_id=prompt['objective_id'],session_id=session_id,
                        plan_item_id=task['plan_item_id'],prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
                        diagnostic_item_id=prompt['diagnostic_item_id'])
                    projected=delivery_projection(c,child,gap['source_ref'],resolution)
                    record.update(status='active' if projected['status']=='ready' else projected['status'],message=projected['next_action'])
            else:
                value=planning(c,child,session_id,gap['source_ref'])
                require(value)
                if value['status']=='ready':
                    target=_candidate(c,child,session_id,plan,gap)
                    budget(c,target)
                    busy=(c.execute("SELECT 1 FROM plan_item WHERE plan_id=? AND status='active'",(plan_id,)).fetchone()
                        or c.execute("SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=? AND status IN ('waiting_answer','answer_saved')",(session_id,)).fetchone())
                    record.update(status='needs_replan' if busy else 'ready',message=('还有题目或任务正在进行，先处理当前学习。'
                        if busy else '合格新题已补齐；可以告诉我继续这项口述复查，确认后才安排和出题。'))
        except (ValueError,KeyError,TypeError) as error:
            code=str(error)
            record.update(status='waiting_constraints' if code in ('SUPPLY_RESUME_BUDGET_UNKNOWN','SUPPLY_RESUME_BUDGET_INSUFFICIENT')
                else 'source_changed',message=conflict_message(code))
        result.append(record)
    return result
