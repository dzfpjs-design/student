"""Historical report facts and separate local, current supply follow-up reads."""
import json
from datetime import datetime, timezone

from .local_supply import digest
from .supply_obligation_history import GAPS

VERSION = 'parent_supply_followup_v1'


def _object(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('SUPPLY_FOLLOWUP_SOURCE_INVALID')
    return value


def _metadata(row):
    value = _object(row['metadata_json'])
    for key in ('supply_carryover', 'supply_resume', 'planning_evidence'):
        if value.get(key) is not None and not isinstance(value[key], dict):
            raise ValueError('SUPPLY_FOLLOWUP_SOURCE_INVALID')
    origin = (value.get('supply_carryover') or {}).get('task_origin')
    if origin is not None and not isinstance(origin, dict):
        raise ValueError('SUPPLY_FOLLOWUP_SOURCE_INVALID')
    return value


def _rows(c, child_id, as_of=None):
    return [dict(r) for r in c.execute('''SELECT d.*,p.child_id,p.session_id,p.plan_date,
        s.status AS session_status,s.ended_at FROM plan_constraint_decision d
        JOIN daily_plan p USING(plan_id) JOIN daily_session s
        ON s.session_id=p.session_id AND s.child_id=p.child_id
        WHERE p.child_id=? AND d.obligation_type='pet_daily'
        AND (? IS NULL OR julianday(d.created_at)<=julianday(?))
        ORDER BY julianday(d.created_at),d.rowid''', (child_id, as_of, as_of)).fetchall()]


def _report_rows(c, child_id, session_id, as_of):
    # Only this report's decisions and exact ancestors, not every historical
    # report times the full child's history during approval/list reads.
    return [dict(r) for r in c.execute('''WITH RECURSIVE ancestors(decision_id) AS (
        SELECT d.decision_id FROM plan_constraint_decision d JOIN daily_plan p USING(plan_id)
        WHERE p.child_id=? AND p.session_id=? AND d.obligation_type='pet_daily'
        AND julianday(d.created_at)<=julianday(?)
        UNION
        SELECT parent.decision_id FROM ancestors a
        JOIN plan_constraint_decision child ON child.decision_id=a.decision_id
        JOIN plan_constraint_decision parent ON parent.decision_id IN (
            json_extract(CASE WHEN json_valid(child.metadata_json) THEN child.metadata_json
                ELSE '{}' END,'$.supply_carryover.root_decision_id'),
            json_extract(CASE WHEN json_valid(child.metadata_json) THEN child.metadata_json
                ELSE '{}' END,'$.supply_resume.obligation_id'))
        JOIN daily_plan p ON p.plan_id=parent.plan_id
        WHERE p.child_id=? AND parent.obligation_type='pet_daily'
        AND julianday(parent.created_at)<=julianday(?))
        SELECT d.*,p.child_id,p.session_id,p.plan_date,s.status AS session_status,s.ended_at
        FROM ancestors a JOIN plan_constraint_decision d USING(decision_id)
        JOIN daily_plan p USING(plan_id) JOIN daily_session s
        ON s.session_id=p.session_id AND s.child_id=p.child_id
        ORDER BY julianday(d.created_at),d.rowid''',
        (child_id, session_id, as_of, child_id, as_of)).fetchall()]


def _groups(rows):
    roots, aliases = {}, {}
    detailed = {r['plan_id'] for r in rows if str(r['source_ref']).startswith('pet-writing-gap:')}
    for row in rows:
        m = _metadata(row)
        carry, resume = m.get('supply_carryover'), m.get('supply_resume')
        if carry:
            root_id = carry['root_decision_id']
        elif resume:
            root_id = aliases.get(resume['obligation_id'])
        else:
            evidence = m.get('planning_evidence') or {}
            if evidence.get('review_task_id'):
                continue
            if not (str(row['source_ref']).startswith(GAPS) or evidence.get('stage_constraint')
                    or evidence.get('pet_question_binding')):
                continue
            if row['source_ref'] == 'pet-skill-gap:writing' and row['plan_id'] in detailed:
                continue
            root_id = row['decision_id']
        if not root_id:
            continue
        aliases[row['decision_id']] = root_id
        roots.setdefault(root_id, []).append(row)
    return roots


def _task(c, row, as_of=None):
    m = _metadata(row)
    task_id = m.get('plan_item_id')
    task = c.execute('SELECT * FROM plan_item WHERE plan_item_id=? AND plan_id=?',
        (task_id, row['plan_id'])).fetchone() if task_id else None
    origin = (m.get('supply_carryover') or {}).get('task_origin')
    if not task and origin:
        task = c.execute('''SELECT pi.* FROM plan_item pi JOIN daily_plan p USING(plan_id)
            WHERE pi.plan_item_id=? AND pi.plan_id=? AND p.session_id=? AND p.child_id=?''',
            (origin['plan_item_id'], origin['plan_id'], origin['session_id'], row['child_id'])).fetchone()
        task_id = task['plan_item_id'] if task else None
    if not task:
        return None, None
    event = c.execute('''SELECT * FROM plan_item_event WHERE plan_item_id=?
        AND event_type IN ('started','resumed','paused','completed','cancelled','rescheduled')
        AND (? IS NULL OR julianday(occurred_at)<=julianday(?))
        ORDER BY julianday(occurred_at) DESC,rowid DESC LIMIT 1''', (task_id, as_of, as_of)).fetchone()
    state = (dict(task)['status'] if as_of is None else
        {'started':'active','resumed':'active','paused':'paused','completed':'completed',
         'cancelled':'cancelled','rescheduled':'rescheduled'}.get(event['event_type'] if event else '',
            'rescheduled' if row['decision']=='deferred' else 'pending'))
    reason = _object(event['payload_json']).get('reason') if event and event['event_type']=='rescheduled' else None
    return {**dict(task), 'status':state}, reason


def _base(root_id, chain):
    root = next((r for r in chain if r['decision_id']==root_id), None)
    if root is None:
        raise ValueError('SUPPLY_FOLLOWUP_ROOT_MISSING')
    m = _metadata(root)
    return dict(root_obligation_id=root_id, original_session_id=root['session_id'],
        original_date=root['plan_date'], title=m.get('title') or '原PET练习',
        original_reason=root['reason'])


def report_supply_facts(c, child_id, session_id):
    """Only events known by this closed session's end. Never today's readiness."""
    session = c.execute('SELECT * FROM daily_session WHERE session_id=? AND child_id=?',
        (session_id, child_id)).fetchone()
    if not session or session['status']!='completed':
        raise ValueError('SUPPLY_REPORT_SESSION_NOT_CLOSED')
    if not session['ended_at']:
        return []  # The report consumer separately exposes unavailable-as-of, not zero work.
    as_of = session['ended_at']
    result = []
    for root_id, chain in _groups(_report_rows(c, child_id, session_id, as_of)).items():
        here = [r for r in chain if r['session_id']==session_id]
        if not here:
            continue
        latest = here[-1]
        task, reason = _task(c, latest, as_of)
        if task and task['status'] in {'completed','cancelled'}:
            continue
        result.append({**_base(root_id, chain), 'decision_id':latest['decision_id'],
            'decision_version':digest(latest), 'as_of':as_of,
            'historical_status':'explicitly_deferred' if task and task['status']=='rescheduled' else 'unfulfilled',
            'deferral_reason':reason or latest['reason'], 'mastery_claim_allowed':False})
    return result


def report_supply_matches(c, child_id, session_id, summary):
    """Legacy reports make no supply claim; new claims require current as-of inputs."""
    if 'supply_obligations_as_of' not in summary:
        return True
    try:
        return summary['supply_obligations_as_of'] == report_supply_facts(c, child_id, session_id)
    except (ValueError, KeyError, TypeError):
        return False


def period_supply_facts(c, child_id, start, end, as_of):
    """Exact roots seen in the period, with lifecycle events at a frozen instant."""
    instant = datetime.fromisoformat(as_of)
    if instant.tzinfo is None or start > end:
        raise ValueError('SUPPLY_PERIOD_SCOPE_INVALID')
    items = []
    for root_id, chain in _groups(_rows(c, child_id, as_of)).items():
        period_rows = [r for r in chain if start <= r['plan_date'] <= end]
        if not period_rows:
            continue
        latest = period_rows[-1]
        task, reason = _task(c, latest, as_of)
        state = task['status'] if task else 'pending'
        items.append({**_base(root_id, chain), 'decision_id': latest['decision_id'],
            'last_plan_date': latest['plan_date'], 'decision_version': digest(latest),
            'historical_status': {'completed':'practice_completed', 'cancelled':'cancelled',
                'rescheduled':'explicitly_deferred'}.get(state, 'unfulfilled'),
            'deferral_reason': reason or latest['reason'], 'mastery_claim_allowed': False})
    return {'version':'period_supply_history_v1', 'status':'available', 'as_of':as_of,
        'period_start':start, 'period_end':end, 'items':items, 'mastery_claim_allowed':False}


MESSAGES = {
    'needs_plan':'原事项已保留；先确认今天的计划与可用时间，再由孩子说出要继续的原练习。',
    'ready':'题目已备好。孩子愿意继续时说出原练习，系统会再次核对今天的时间和许可。',
    'awaiting_resources':'先补齐并核对这道原题的听力原音；不要朗读听力文本代替，也不要开始计时。',
    'scheduled':'原练习尚未开始。由孩子确认继续，开始前仍需核对材料和时间。',
    'active':'孩子正在进行原练习；无需追加或重复启动。',
    'paused':'原练习已暂停，进度保留。孩子准备好后可以说继续原任务。',
    'practice_completed':'原练习已有完成记录，不再安排重复启动；完成练习不等于掌握。',
    'cancelled':'原练习已取消，不使用旧指令重新开始。',
    'source_changed':'原题或来源已失效。先核对材料；不自动换题或恢复旧授权。',
}


def current_followups(c, child_id):
    """Caller owns one read transaction; no initialization, event or cache writes."""
    if not c.in_transaction:
        raise ValueError('SUPPLY_FOLLOWUP_TRANSACTION_REQUIRED')
    if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
        raise KeyError('SUPPLY_FOLLOWUP_CHILD_NOT_FOUND')
    from .learning_input_replan import ZONE
    from .supply_gap_presentation import project_supply_obligations
    plans = c.execute('''SELECT p.* FROM daily_plan p JOIN daily_session s USING(session_id)
        WHERE p.child_id=? AND s.child_id=p.child_id AND p.status='active' AND s.status='active'
        AND s.state NOT IN ('evolving','completed')''', (child_id,)).fetchall()
    today = datetime.now(timezone.utc).astimezone(ZONE).date().isoformat()
    plan = plans[0] if len(plans)==1 and plans[0]['plan_date']==today else None
    projected = {r.get('root_obligation_id',r['obligation_id']):r for r in
        project_supply_obligations(c,plan['session_id'],child_id,plan['plan_id'])} if plan else {}
    from .supply_gap_resume import candidates
    targets = {r['obligation_id']:r for r in candidates(c,plan['session_id'],child_id)} if plan else {}
    material_catalog_present = bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='curriculum_supply_package'").fetchone())
    result = []
    for root_id, chain in _groups(_rows(c, child_id)).items():
        latest = chain[-1]
        task, reason = _task(c, latest)
        base = _base(root_id, chain)
        status, message = 'needs_plan', MESSAGES['needs_plan']
        view = projected.get(root_id)
        if task and task['status'] in {'completed','cancelled'}:
            status = 'practice_completed' if task['status']=='completed' else 'cancelled'
            message = MESSAGES[status]
        elif view:
            status, message = view['status'], view['message']
        elif plan and latest['plan_id']==plan['plan_id'] and task:
            status = {'pending':'scheduled','active':'active','paused':'paused'}.get(task['status'],'needs_plan')
            message = MESSAGES[status]
        if status in {'scheduled','paused','active'} and task:
            from .diagnostic_prompt import DiagnosticPromptService
            from .local_supply import LocalSupplyError, validate_item
            evidence = _metadata(latest).get('planning_evidence') or {}
            binding = evidence.get('pet_question_binding')
            from .pet_task_execution import revalidate_pet_question, PetTaskExecutionError
            try:
                validate_item(c, child_id, task['source_ref'])
                item = DiagnosticPromptService._item(c, child_id, task['source_ref'])
                if not item:
                    raise LocalSupplyError('SOURCE_UNAVAILABLE')
                if binding:
                    revalidate_pet_question(c, child_id, binding)
            except (LocalSupplyError, PetTaskExecutionError):
                status, message = 'source_changed', MESSAGES['source_changed']
            # Generic PET listening uses a speaking task, not a listening enum.
            if status in {'scheduled','paused'} and (binding or {}).get('skill')=='listening':
                prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=?
                    AND diagnostic_item_id=? AND status='waiting_answer' ORDER BY rowid DESC LIMIT 1''',
                    (child_id, latest['session_id'], task['source_ref'])).fetchone()
                from .pet_listening_admission import resolve_pet_listening_resource
                if not prompt or resolve_pet_listening_resource(c, latest['session_id'], child_id,
                        prompt['prompt_id'], prompt['item_version'])['status']!='ready':
                    status, message = 'awaiting_resources', MESSAGES['awaiting_resources']
            if status in {'scheduled','paused'} and plan and task['plan_id']==plan['plan_id']:
                from .supply_gap_resume import budget
                from .supply_gap_presentation import conflict_message
                from .task_start_binding import TaskStartBindingError
                try:
                    budget(c, dict(child_id=child_id, session_id=plan['session_id'],
                        plan_item_id=task['plan_item_id'], expected_minutes=task['expected_minutes'],
                        source_type=task['source_type'], source_ref=task['source_ref']))
                except TaskStartBindingError as error:
                    status, message = 'waiting_constraints', conflict_message(str(error))
        row = {**base, 'child_id':child_id, 'current_session_id':plan['session_id'] if plan else None,
            'decision_id':latest['decision_id'], 'status':status,
            'deferral_reason':reason or latest['reason'], 'next_action':MESSAGES.get(status,message),
            'plan_item_id':task['plan_item_id'] if task else (view or {}).get('plan_item_id'),
            'local_only':True, 'cloud_transmission_allowed':False, 'mastery_claim_allowed':False}
        target = targets.get((view or {}).get('obligation_id')) or {}
        source = task['source_ref'] if task and plan and latest['plan_id']==plan['plan_id'] else target.get('source_ref')
        material = c.execute('''SELECT p.candidate_id FROM curriculum_supply_package p JOIN diagnostic_item d
            ON d.source_ref='parent_approved_specialized:'||p.candidate_id
            WHERE p.child_id=? AND d.diagnostic_item_id=?''', (child_id,source)).fetchone() if source and material_catalog_present else None
        row['material_object_id'] = material['candidate_id'] if material else 'selection:'+child_id
        row['version'] = VERSION + ':' + digest(row)
        result.append(row)
    from .oral_supply_followup import current_followups as oral_followups
    result.extend(oral_followups(c,child_id))
    return result
