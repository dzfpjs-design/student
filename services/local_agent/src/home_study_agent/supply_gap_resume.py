"""A child's exact resume request can place an existing PET supply obligation.

Publishing only changes inventory. Deferred decisions remain immutable; the
execution transaction appends a scheduled decision and an exact task, not a
learning fact or a replacement for completed work.
"""
import json
from datetime import datetime, timezone
from uuid import uuid5, NAMESPACE_URL

from .local_supply import digest, validate_item, LocalSupplyError
from .task_start_binding import TaskStartBindingError


VERSION = 'supply_gap_resume_v1'
SCHOOL_VERSION = 'school_supply_gap_v1'


def _require(value, code='SUPPLY_RESUME_SOURCE_CHANGED'):
    if not value:
        raise TaskStartBindingError(code)


def _consent_version(c):
    from .voice_control_intent import VoiceControlIntent
    return list(VoiceControlIntent.text_consent_version(c) or ())


def _plan(c, session_id, child_id, *, as_of=None):
    from .learning_input_replan import ZONE
    today = (as_of if as_of is not None else datetime.now(timezone.utc)).astimezone(ZONE).date().isoformat()
    rows = c.execute('''SELECT p.* FROM daily_plan p JOIN daily_session s USING(session_id)
        JOIN child_profile cp ON cp.child_id=p.child_id WHERE p.session_id=? AND p.child_id=?
        AND s.child_id=p.child_id AND p.status='active' AND s.status='active' AND cp.status='active'
        AND s.state NOT IN ('evolving','completed')''', (session_id, child_id)).fetchall()
    return rows[0] if len(rows) == 1 and rows[0]['plan_date'] == today else None


def _writing(c, child_id):
    from .pet_writing_stage_plan import project_writing_stage_plan
    return project_writing_stage_plan(c, child_id, as_of=datetime.now(timezone.utc).isoformat())['items']


def _source_current(c, child_id, target):
    if target['source_type'] in {'oral_question_supply','oral_delivered_continuation','oral_daily_check'}:
        if target['source_type']=='oral_daily_check':
            from .oral_daily_supply import validate_source
        elif target['source_type']=='oral_delivered_continuation':
            from .oral_delivery_resume import validate_source
        else:
            from .oral_supply_execution import validate_source
        try:
            validate_source(c,target)
            return True
        except TaskStartBindingError:
            return False
    from .diagnostic_prompt import DiagnosticPromptService, _version
    school = target['source_type'] == 'junior_exam_goal'
    item_id = target['diagnostic_item_id'] if school else target['source_ref']
    original = DiagnosticPromptService._item(c, child_id, item_id)
    if not original or _version(original) != target['item_version']:
        return False
    try:
        validate_item(c, child_id, item_id)
    except LocalSupplyError:
        return False
    evidence = target['supply_item']['planning_evidence']
    if school:
        from .goal_question_inventory import practice_item
        gap = c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=? AND plan_id=?',
            (target['obligation_id'],target['plan_id'])).fetchone()
        current = school_gap_context(c, target['session_id'], child_id, gap)
        return bool(current and original['knowledge_node_id'] == evidence['knowledge_node_id']
            and evidence['school_item_binding'] == {'diagnostic_item_id':item_id,'item_version':target['item_version']}
            and practice_item(c,child_id,item_id,datetime.now(timezone.utc),allow_recent=True))
    if target['task_type'] == 'writing':
        from .specialized_stages import revalidate_stage_action
        checked = revalidate_stage_action(c, child_id, 'pet', binding={**evidence, 'child_id':child_id, 'track':'pet'})
        return checked['status'] == 'ready'
    from .pet_task_execution import freeze_pet_question, PetTaskExecutionError
    try:
        return freeze_pet_question(c, child_id, target['source_ref']) == evidence['pet_question_binding']
    except PetTaskExecutionError:
        return False


def is_school_gap(row):
    if row['obligation_type'] != 'junior_exam_goal' or row['decision'] != 'deferred':
        return False
    try:
        value = json.loads(row['metadata_json'])['planning_evidence']['school_supply_gap']
        return isinstance(value,dict) and value.get('version') == SCHOOL_VERSION
    except (ValueError, TypeError, KeyError):
        return False


def school_gap_context(c, session_id, child_id, gap):
    """Project original node/action and real graph bindings; never select a new node."""
    if not gap or not is_school_gap(gap):
        return None
    plan = _plan(c,session_id,child_id)
    if not plan or gap['plan_id'] != plan['plan_id']:
        return None
    from .goal_roadmap import build_goal_roadmap, _as_of, _catalog, _catalog_source_reason, assessment_binding
    try:
        evidence = json.loads(gap['metadata_json'])['planning_evidence']
        frozen = evidence['school_supply_gap']
        program = c.execute('''SELECT gp.*,gc.child_id AS cycle_child_id,gc.week_start,gc.week_end
            FROM goal_cycle gc JOIN goal_program gp USING(program_id) WHERE gc.cycle_id=?''', (gap['source_ref'],)).fetchone()
        if (not program or program['program_id'] != frozen['program_id'] or gap['source_ref'] != frozen['cycle_id']
                or program['child_id'] != child_id or program['cycle_child_id'] != child_id
                or program['status'] != 'active' or program['track'] != 'junior_exam_readiness'
                or json.loads(program['target_horizon_json']) != frozen['target_horizon']
                or not program['week_start'] <= plan['plan_date'] <= program['week_end']):
            return None
        today, now = _as_of(None)
        roadmap = build_goal_roadmap(c,dict(program),today=today,feedback_now=now)
        if (roadmap['target_date'] and roadmap['target_date'] < today.isoformat()
                or any(row['code'] in {'target_catalog_missing','target_source_unavailable','prerequisite_source_unavailable',
                    'prerequisite_cycle','invalid_target_date','assessment_skill_mapping_unavailable'} for row in roadmap['risks'])):
            return None
        node = next((row for row in roadmap['prerequisite_path'] if row['knowledge_node_id'] == evidence['knowledge_node_id']), None)
        if not node or node['blocked_by'] or not node['steps'] or node['steps'][0]['action'] != evidence['action']:
            return None
        child = c.execute('SELECT * FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
        catalog = _catalog(c,child,today)
        if (catalog[0][node['knowledge_node_id']]['node_type'] != 'skill'
                or _catalog_source_reason(c,child_id,evidence,today)
                or evidence['assessment_binding'] != assessment_binding(c,node['knowledge_node_id'],catalog)):
            return None
        return {'evidence':evidence,'node':node,'roadmap':roadmap}
    except (ValueError,TypeError,KeyError):
        return None


def _school_candidates(c, session_id, child_id, plan):
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .goal_question_inventory import practice_item
    result = []
    gaps = c.execute("SELECT * FROM plan_constraint_decision WHERE plan_id=? AND obligation_type='junior_exam_goal' AND decision='deferred' ORDER BY rowid",
        (plan['plan_id'],)).fetchall()
    for gap in gaps:
        current = school_gap_context(c,session_id,child_id,gap)
        if not current or c.execute("""SELECT 1 FROM plan_constraint_decision WHERE plan_id=? AND decision='scheduled'
            AND json_extract(metadata_json,'$.supply_resume.obligation_id')=?""", (plan['plan_id'],gap['decision_id'])).fetchone():
            continue
        evidence = current['evidence']
        rows = c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=? AND status='active' ORDER BY difficulty,diagnostic_item_id",
            (evidence['knowledge_node_id'],)).fetchall()
        original = next((DiagnosticPromptService._item(c,child_id,row[0]) for row in rows
            if practice_item(c,child_id,row[0],datetime.now(timezone.utc))),None)
        if not original:
            continue
        item_id, version = original['diagnostic_item_id'], _version(original)
        metadata = json.loads(gap['metadata_json'])
        selected = {'source_type':'junior_exam_goal','source_ref':gap['source_ref'],
            'subject':metadata['subject'],'title':metadata['title'],'minutes':gap['requested_minutes'],
            'task_type':'test_prep','cognitive_load':'medium',
            'completion_standard':'有效独立作答与后续复测；参与分钟不作为能力达标。',
            'planning_evidence':{**evidence,'school_item_binding':{'diagnostic_item_id':item_id,'item_version':version}}}
        target = dict(version=VERSION,operation='supply',obligation_id=gap['decision_id'],obligation_version=digest(dict(gap)),
            plan_id=plan['plan_id'],plan_version=plan['version'],control_consent_version=_consent_version(c),
            plan_item_id=str(uuid5(NAMESPACE_URL,'hsc-supply:'+gap['decision_id']+':'+version)),
            child_id=child_id,session_id=session_id,source_type='junior_exam_goal',source_ref=gap['source_ref'],
            task_type=selected['task_type'],subject=selected['subject'],title=selected['title'],homework_id=None,
            expected_minutes=selected['minutes'],started_at=None,active_seconds=0,diagnostic_item_id=item_id,
            item_version=version,goal_prompt_binding=None,pause_event_id=None,origin_turn_id=None,supply_item=selected)
        if _source_current(c,child_id,target):
            result.append(target)
    return result


def project_school_obligations(c, session_id, child_id, plan_id):
    """Use the existing workspace shape; all facts remain in the original plan."""
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return project_school_obligations(c,session_id,child_id,plan_id)
        finally:
            c.rollback()
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .supply_gap_presentation import MESSAGES, conflict_message
    plan = c.execute("SELECT * FROM daily_plan WHERE plan_id=? AND session_id=? AND child_id=? AND status='active'",
        (plan_id,session_id,child_id)).fetchone()
    if not plan:
        return []
    rows = c.execute('SELECT * FROM plan_constraint_decision WHERE plan_id=? ORDER BY rowid',(plan_id,)).fetchall()
    gaps = [row for row in rows if is_school_gap(row)]
    if not gaps:
        return []
    available = {target['obligation_id']:target for target in _school_candidates(c,session_id,child_id,plan)}
    result = []
    for gap in gaps:
        metadata = json.loads(gap['metadata_json'])
        view = dict(obligation_id=gap['decision_id'],title=metadata['title'],status='missing_source',
            message='原知识点练习已保留，仍需合格题目材料。',plan_item_id=None,task_status=None,
            local_only=True,cloud_transmission_allowed=False,mastery_claim_allowed=False)
        receipts = [json.loads(row['metadata_json']) for row in rows if row['decision']=='scheduled'
            and json.loads(row['metadata_json']).get('supply_resume',{}).get('obligation_id') == gap['decision_id']]
        if receipts:
            view.update(status='needs_replan',message='原安排需要重新核对，不会自动换题。')
            if len(receipts) == 1:
                binding = receipts[0]['supply_resume']
                task = c.execute('SELECT * FROM plan_item WHERE plan_item_id=? AND plan_id=?',
                    (binding.get('plan_item_id'),plan_id)).fetchone()
                valid = (binding.get('version') == VERSION and binding.get('child_id') == child_id
                    and binding.get('session_id') == session_id and binding.get('obligation_version') == digest(dict(gap))
                    and task and all(task[key] == binding.get(key) for key in
                        ('source_type','source_ref','subject','task_type','expected_minutes')))
                if valid:
                    view.update(plan_item_id=task['plan_item_id'],task_status=task['status'])
                    original = DiagnosticPromptService._item(c,child_id,binding['diagnostic_item_id'])
                    source_ok = bool(original and _version(original) == binding.get('item_version'))
                    if source_ok:
                        try:
                            validate_item(c,child_id,binding['diagnostic_item_id'])
                        except LocalSupplyError:
                            source_ok = False
                    if not source_ok:
                        view.update(status='source_changed',message=MESSAGES['SUPPLY_RESUME_SOURCE_CHANGED'])
                    elif task['status'] != 'completed' and not school_gap_context(c,session_id,child_id,gap):
                        view.update(status='needs_replan',message='原目标或复测关系已变化，先核对原练习，不自动继续。')
                    elif task['status'] in {'pending','active','paused','completed'}:
                        states = {'pending':('scheduled','原题已安排，等待开始。'),
                            'active':('active','正在练习原知识点的这道题。'),
                            'paused':('paused','原题进度已保留，可以继续原任务。'),
                            'completed':('practice_completed','这项原题练习已完成；练习完成不等于已经掌握。')}
                        view.update(zip(('status','message'),states[task['status']]))
        elif not school_gap_context(c,session_id,child_id,gap):
            view.update(status='needs_replan',message='原目标、图关系或今天的计划已变化，需核对原练习后再安排。')
        elif gap['decision_id'] in available:
            try:
                budget(c,available[gap['decision_id']])
                view.update(status='ready',message='原知识点的题目已补好；可以说继续原练习，确认后才会开始。')
            except TaskStartBindingError as error:
                view.update(status='waiting_constraints',message=conflict_message(str(error)))
        result.append(view)
    return result


def candidates(c, session_id, child_id):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return candidates(c, session_id, child_id)
        finally:
            c.rollback()
    plan = _plan(c, session_id, child_id)
    if not plan:
        return []
    school = _school_candidates(c,session_id,child_id,plan)
    from .oral_supply_execution import candidates as oral_candidates
    school += oral_candidates(c,session_id,child_id,plan)
    from .oral_delivery_resume import candidates as continuation_candidates
    school += continuation_candidates(c,session_id,child_id,plan)
    from .oral_daily_supply import candidates as daily_candidates
    school += daily_candidates(c,session_id,child_id,plan)
    rows = c.execute('''SELECT * FROM plan_constraint_decision WHERE plan_id=?
        AND obligation_type='pet_daily' AND decision='deferred' ORDER BY rowid''', (plan['plan_id'],)).fetchall()
    from .supply_obligation_history import is_obligation, validate_inherited
    gaps = [dict(r) for r in rows if is_obligation(r)]
    if not gaps:
        return school
    supplied = c.execute('''SELECT metadata_json FROM plan_constraint_decision
        WHERE plan_id=? AND decision='scheduled' AND json_type(metadata_json,'$.supply_resume')='object' ''',
        (plan['plan_id'],)).fetchall()
    receipts = {json.loads(r[0])['supply_resume']['obligation_id']: json.loads(r[0])['supply_resume'] for r in supplied}
    writing = _writing(c, child_id)
    from .pet_task_execution import freeze_pet_question, PetTaskExecutionError
    from .diagnostic_prompt import DiagnosticPromptService, _version
    result = []
    has_writing_nodes = any(str(r['source_ref']).startswith('pet-writing-gap:') for r in gaps)
    for gap in gaps:
        if not validate_inherited(c, child_id, gap):
            continue
        if gap['source_ref'] == 'pet-skill-gap:writing' and has_writing_nodes:
            continue
        old = receipts.get(gap['decision_id'])
        if old:
            # Placement already happened. Its pending original is now an ordinary
            # exact task-start candidate, not permission to reuse the old command.
            continue
        carried = json.loads(gap['metadata_json']).get('supply_carryover') or {}
        if carried.get('fixed_item'):
            options = [carried['fixed_item']]
        elif str(gap['source_ref']).startswith('pet-writing-gap:'):
            node_id = gap['source_ref'].split(':', 1)[1]
            options = [x for x in writing if x['planning_evidence']['knowledge_node_id']==node_id]
            frozen = json.loads(gap['metadata_json']).get('planning_evidence', {})
            options = [x for x in options if x['planning_evidence']['stage_constraint']['current_stage'] ==
                       frozen.get('stage_constraint', {}).get('current_stage')]
        elif gap['source_ref'] == 'pet-skill-gap:writing':
            options = writing
        else:
            skill = gap['source_ref'].split(':', 1)[1]
            if skill not in {'reading','speaking','listening'}:
                continue
            from .pet_training_plan import allocate_pet_practice
            rule = c.execute('SELECT pet_min_minutes,task_segment_minutes FROM family_rule WHERE child_id=?', (child_id,)).fetchone()
            options = allocate_pet_practice(c, child_id, min(rule), 0, requested_skill=skill)['items'] if rule else []
        if not options:
            continue
        selected = sorted(options, key=lambda x:x['source_ref'])[0]
        original = DiagnosticPromptService._item(c, child_id, selected['source_ref'])
        if not original:
            continue
        version = _version(original)
        target = dict(version=VERSION, operation='supply', obligation_id=gap['decision_id'],
            obligation_version=digest(gap), plan_id=plan['plan_id'], plan_version=plan['version'],
            control_consent_version=_consent_version(c),
            plan_item_id=str(uuid5(NAMESPACE_URL, 'hsc-supply:'+gap['decision_id']+':'+version)),
            child_id=child_id, session_id=session_id, source_type='pet_daily', source_ref=selected['source_ref'],
            task_type=selected['task_type'], subject=selected['subject'], title=selected['title'],
            homework_id=None, expected_minutes=selected['minutes'], started_at=None, active_seconds=0,
            item_version=version, goal_prompt_binding=None, pause_event_id=None, origin_turn_id=None,
            supply_item=selected)
        if _source_current(c, child_id, target):
            result.append(target)
    return result + school


def budget(c, target):
    """Use the existing pinned-time replan policy, spending the PET reservation."""
    from .learning_input_replan import LearningInputReplanService
    now = datetime.now(timezone.utc)
    rule = c.execute('SELECT * FROM family_rule WHERE child_id=?', (target['child_id'],)).fetchone()
    _require(rule, 'SUPPLY_RESUME_BUDGET_UNKNOWN')
    plan = _plan(c, target['session_id'], target['child_id'], as_of=now)
    _require(plan, 'SUPPLY_RESUME_PLAN_CHANGED')
    context = c.execute('SELECT * FROM session_planning_context WHERE session_id=?', (target['session_id'],)).fetchone()
    context = dict(context) if context else {'energy_level':'medium','fixed_commitment_minutes':0}
    session = c.execute('SELECT ds.*,cp.grade FROM daily_session ds JOIN child_profile cp USING(child_id) WHERE session_id=?',
                        (target['session_id'],)).fetchone()
    items = [dict(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=? AND plan_item_id!=?',
                                      (plan['plan_id'],target['plan_item_id'])).fetchall()]
    info, remaining, reason = LearningInputReplanService(None)._budget(
        c, target['child_id'], session, plan, items, rule, context, {}, now,
        consumes_pet_reserve=target['source_type'] == 'pet_daily')
    _require(reason is None, 'SUPPLY_RESUME_BUDGET_UNKNOWN')
    _require(target['expected_minutes'] <= info['effective_task_segment_minutes_cap'] and target['expected_minutes']*60 <= remaining,
             'SUPPLY_RESUME_BUDGET_INSUFFICIENT')
    if target['source_type'] == 'junior_exam_goal':
        from .goal_roadmap import _budget
        program = c.execute('SELECT gp.* FROM goal_program gp JOIN goal_cycle gc USING(program_id) WHERE gc.cycle_id=? AND gc.child_id=?',
            (target['source_ref'],target['child_id'])).fetchone()
        _require(program and program['status']=='active', 'SUPPLY_RESUME_SOURCE_CHANGED')
        allowance = _budget(c,program,datetime.fromisoformat(plan['plan_date']).date())
        existing = c.execute('SELECT * FROM plan_item WHERE plan_item_id=? AND plan_id=?', (target['plan_item_id'],plan['plan_id'])).fetchone()
        own_reservation = existing['expected_minutes'] if existing and existing['status'] in {'pending','active','paused'} else 0
        _require(allowance['configured_budget_valid'] and allowance['weekly_sessions'] > 0
            and target['expected_minutes'] <= allowance['remaining_weekly_minutes']+own_reservation,
            'SUPPLY_RESUME_BUDGET_INSUFFICIENT')
    return {**info, 'remaining_seconds':remaining, 'task_minutes':target['expected_minutes']}


def validate_target(c, session_id, child_id, target):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return validate_target(c,session_id,child_id,target)
        finally:
            c.rollback()
    plan = _plan(c, session_id, child_id)
    _require(plan and target.get('version')==VERSION and target.get('child_id')==child_id
             and target.get('session_id')==session_id and target.get('plan_id')==plan['plan_id'])
    _require(target.get('control_consent_version')==_consent_version(c), 'SUPPLY_RESUME_AUTHORIZATION_CHANGED')
    gap = c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=? AND plan_id=?',
                    (target['obligation_id'],plan['plan_id'])).fetchone()
    from .supply_obligation_history import validate_inherited
    if target['source_type'] in {'oral_question_supply','oral_delivered_continuation','oral_daily_check'}:
        if target['source_type']=='oral_daily_check':
            from .oral_daily_supply import validate_gap
        elif target['source_type']=='oral_delivered_continuation':
            from .oral_delivery_resume import validate_gap
        else:
            from .oral_supply_execution import validate_gap
        _require(gap and validate_gap(c,target,gap))
    else:
        _require(gap and (school_gap_context(c,session_id,child_id,gap) if target['source_type']=='junior_exam_goal'
                         else validate_inherited(c, child_id, gap)))
        _require(digest(dict(gap))==target['obligation_version'])
    _require(_source_current(c, child_id, target))
    placed = c.execute('''SELECT metadata_json FROM plan_constraint_decision WHERE plan_id=?
        AND json_extract(metadata_json,'$.supply_resume.obligation_id')=? AND decision='scheduled' ''',
        (plan['plan_id'],target['obligation_id'])).fetchall()
    if placed:
        _require(len(placed)==1)
        receipt = json.loads(placed[0][0])
        _require(receipt['supply_resume']==target and plan['version'] == receipt['placement_plan_version']
                 and receipt['placement_plan_version']==target['plan_version']+1)
        task = c.execute('SELECT * FROM plan_item WHERE plan_item_id=? AND plan_id=?', (target['plan_item_id'],plan['plan_id'])).fetchone()
        _require(task and task['status']=='pending' and task['started_at'] is None
                 and all(task[k]==target[k] for k in ('source_type','source_ref','subject','title','task_type','expected_minutes')))
    else:
        _require(plan['version']==target['plan_version'] and target in candidates(c, session_id, child_id))
    return target


def validate_placed_start_budget(c, session_id, child_id, plan_item_id):
    """Recheck a persisted supply placement for either authorized start command."""
    _require(c.in_transaction, 'SUPPLY_RESUME_TRANSACTION_REQUIRED')
    item = c.execute('''SELECT pi.*,dp.child_id,dp.session_id FROM plan_item pi
        JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=? AND dp.child_id=?
        AND dp.session_id=? AND dp.status='active' ''', (plan_item_id, child_id, session_id)).fetchone()
    _require(item, 'SUPPLY_RESUME_PLAN_CHANGED')
    rows = c.execute('''SELECT metadata_json FROM plan_constraint_decision
        WHERE plan_id=? AND decision='scheduled' AND
        (json_extract(metadata_json,'$.plan_item_id')=?
         OR json_extract(metadata_json,'$.supply_resume.plan_item_id')=?)
        AND json_type(metadata_json,'$.supply_resume') IS NOT NULL''',
        (item['plan_id'], plan_item_id, plan_item_id)).fetchall()
    if not rows:
        return
    _require(len(rows) == 1)
    receipt = json.loads(rows[0]['metadata_json'])
    target = receipt.get('supply_resume')
    _require(isinstance(target, dict) and target.get('version') == VERSION)
    _require(receipt.get('plan_item_id') == plan_item_id
        and all(target.get(key) == item[key] for key in (
            'plan_item_id', 'plan_id', 'child_id', 'session_id', 'source_type',
            'source_ref', 'subject', 'title', 'task_type', 'expected_minutes')))
    original = c.execute('''SELECT context_manifest_json FROM agent_plan
        WHERE agent_turn_id=? AND child_id=? AND session_id=?''',
        (receipt.get('requested_by_turn_id'), child_id, session_id)).fetchone()
    _require(original and json.loads(original[0]).get('supply_resume_receipt') == target,
             'SUPPLY_RESUME_AUTHORIZATION_CHANGED')
    # The receipt proves placement, not current permission. Use today's rules
    # and actual task duration, without restoring the old request's authority.
    budget(c, dict(item))


def materialize(c, turn_id, target):
    from .agent_runtime import AgentRuntimeStore
    _require(c.in_transaction, 'SUPPLY_RESUME_TRANSACTION_REQUIRED')
    authority = AgentRuntimeStore.inspect_voice_control_execution(c, turn_id)
    _require(authority and authority['control']=='task.resume'
             and authority['targets']['task_resume']['item']==target, 'SUPPLY_RESUME_AUTHORIZATION_CHANGED')
    validate_target(c, target['session_id'], target['child_id'], target)
    allowance = budget(c, target)
    item = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (target['plan_item_id'],)).fetchone()
    if item is None:
        source = target['supply_item']
        position = c.execute('SELECT COALESCE(MAX(position),0)+1 FROM plan_item WHERE plan_id=?', (target['plan_id'],)).fetchone()[0]
        c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,
            source_type,source_ref,completion_standard,cognitive_load,task_type) VALUES(?,?,NULL,?,?,?,1,'pending',?,?,?,?,?,?)''',
            (target['plan_item_id'],target['plan_id'],source['subject'],source['title'],source['minutes'],position,
             source['source_type'],source['source_ref'],source['completion_standard'],source['cognitive_load'],source['task_type']))
        metadata = {'title':source['title'],'subject':source['subject'],'plan_item_id':target['plan_item_id'],
            'planning_evidence':source['planning_evidence'],'supply_resume':target,'budget_receipt':allowance,
            'requested_by_turn_id':turn_id, 'placement_plan_version':target['plan_version']+1}
        c.execute('''INSERT INTO plan_constraint_decision VALUES(?,?,? ,?,'scheduled',?,?,?,?,?)''',
            (str(uuid5(NAMESPACE_URL, 'hsc-supply-decision:'+target['plan_item_id'])),target['plan_id'],source['source_type'],source['source_ref'],
             '孩子明确请求继续原供给义务；按当前来源与剩余预算追加，不替换已完成任务。',source['minutes'],source['minutes'],
             json.dumps(metadata,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        updated = c.execute('UPDATE daily_plan SET total_minutes=total_minutes+?,version=version+1 WHERE plan_id=? AND version=? AND status=\'active\'',
                            (source['minutes'],target['plan_id'],target['plan_version']))
        _require(updated.rowcount==1, 'SUPPLY_RESUME_PLAN_CHANGED')
    row = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()
    manifest = json.loads(row[0])
    manifest['supply_resume_receipt'] = target
    manifest['task_start_binding'] = {k:target[k] for k in ('plan_item_id','source_type','source_ref')}
    manifest['task_start_binding']['operation'] = 'start'
    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?', (json.dumps(manifest),turn_id))
    return dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (target['plan_item_id'],)).fetchone())
