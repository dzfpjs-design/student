"""Retain missing questions and unfinished deliveries without granting execution."""
import json
import hashlib
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from .family_clock import evidence_calendar_date
from .local_supply import digest
from .oral_supply_obligation import LOCAL, ORIGIN

VERSION = 'oral_supply_carryover_v1'
RESUME_VERSION = 'oral_supply_cross_day_v1'
DELIVERED_VERSION = 'oral_delivered_continuation_v1'


def unfinished_deliveries(c, child_id, session_id=None):
    """Read original deliveries; never reset a fulfilled supply promise to pending."""
    from .oral_supply_execution import delivery_projection
    if not c.in_transaction:
        raise ValueError('ORAL_SUPPLY_CARRYOVER_TRANSACTION_REQUIRED')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
        return []
    rows = c.execute('''SELECT ac.commitment_id,ac.payload_json,o.session_id AS root_session_id,
        q.prompt_id,q.origin_session_id,q.created_at,ds.ended_at,ds.status AS session_status,n.subject
        FROM agent_commitment ac JOIN agent_objective o ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id
        JOIN child_profile cp ON cp.child_id=ac.child_id AND cp.status='active'
        JOIN diagnostic_prompt q ON q.prompt_id=json_extract(ac.payload_json,'$.resolution.prompt_id')
            AND q.child_id=ac.child_id AND q.origin_session_id=json_extract(ac.payload_json,'$.resolution.session_id')
        JOIN daily_session ds ON ds.session_id=q.origin_session_id AND ds.child_id=ac.child_id
        JOIN diagnostic_item di ON di.diagnostic_item_id=q.diagnostic_item_id
        JOIN knowledge_node n ON n.knowledge_node_id=di.knowledge_node_id
        WHERE ac.child_id=? AND ac.status='completed' AND o.status IN ('active','paused')
        AND json_extract(ac.payload_json,'$.origin')=? AND json_extract(ac.payload_json,'$.phase')='question_prepared'
        AND q.status='waiting_answer' AND q.attempt_id IS NULL ORDER BY q.created_at,q.prompt_id''', (child_id,ORIGIN)).fetchall()
    from .oral_daily_check import retained_deliveries
    rows=[*rows,*retained_deliveries(c,child_id)]
    result = []
    for row in rows:
        payload = json.loads(row['payload_json'])
        source = payload.get('source')
        source_valid = (dict(row).get('daily_check') is True or (isinstance(source,dict) and source.get('child_id')==child_id
            and source.get('session_id')==row['root_session_id'] and digest(source)==payload.get('source_hash'))
        )
        try:
            if not source_valid:
                raise ValueError('ORAL_SUPPLY_CARRYOVER_SOURCE_CHANGED')
            from .oral_delivery_resume import lineage
            latest,_=lineage(c,child_id,row['commitment_id'])
            if latest!=payload['resolution']:
                current=c.execute('''SELECT q.created_at,q.origin_session_id,q.status,q.attempt_id,ds.ended_at,
                    ds.status AS session_status FROM diagnostic_prompt q JOIN daily_session ds
                    ON ds.session_id=q.origin_session_id AND ds.child_id=q.child_id
                    WHERE q.prompt_id=? AND q.child_id=?''',(latest['prompt_id'],child_id)).fetchone()
                if not current or current['status']!='waiting_answer' or current['attempt_id']:
                    continue
                row={**dict(row),**dict(current)}
                payload={**payload,'resolution':latest}
            state = delivery_projection(c,child_id,row['commitment_id'],payload['resolution'])
        except (ValueError,KeyError,TypeError):
            state = dict(status='source_changed')
        if state['status']=='cancelled':
            continue
        binding = {key:payload['resolution'].get(key) for key in ('turn_id','prompt_id','session_id','plan_item_id',
            'objective_id','diagnostic_item_id','item_version')}
        binding.update(version=DELIVERED_VERSION,child_id=child_id,root_commitment_id=row['commitment_id'],
            source_hash=payload.get('source_hash'),**LOCAL)
        retained_at, decision_id = row['created_at'], None
        if session_id and session_id!=row['origin_session_id']:
            decisions = c.execute('''SELECT d.* FROM plan_constraint_decision d JOIN daily_plan dp USING(plan_id)
                JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
                WHERE dp.child_id=? AND dp.session_id=? AND dp.status!='cancelled'
                AND d.obligation_type='oral_delivered_continuation' AND d.source_ref=? AND d.decision='deferred'
                ORDER BY d.created_at DESC''', (child_id,session_id,row['commitment_id'])).fetchall()
            if not decisions:
                continue
            decision = decisions[0]
            try:
                metadata = json.loads(decision['metadata_json'])
            except (ValueError,TypeError):
                metadata = None
            if not isinstance(metadata,dict) or metadata.get('oral_delivered_continuation')!=binding:
                state = dict(status='source_changed')
            retained_at, decision_id = decision['created_at'], decision['decision_id']
        result.append(dict(binding=binding,commitment_id=row['commitment_id'],subject=row['subject'],
            objective_id=payload['resolution'].get('objective_id'),origin_session_id=row['origin_session_id'],
            session_status=row['session_status'],ended_at=row['ended_at'],retained_at=retained_at,
            decision_id=decision_id,status='source_changed' if state['status']=='source_changed' else 'needs_plan',**LOCAL))
    return result


def request_proof_version(c, child_id, root_id):
    row=c.execute('''SELECT ac.objective_id,o.session_id,ac.payload_json,t.agent_turn_id,
        t.transcript,t.action_json,p.context_manifest_json,r.fingerprint,r.decision_json,r.binding_json,
        a.utterance,a.snapshot_json FROM agent_commitment ac
        JOIN agent_objective o ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id
        JOIN agent_turn t ON t.agent_turn_id=json_extract(ac.payload_json,'$.request_turn_id')
            AND t.child_id=ac.child_id AND t.session_id=o.session_id
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN voice_control_intent_request r ON r.request_key=json_extract(ac.payload_json,'$.request_key')
            AND r.child_id=t.child_id AND r.session_id=t.session_id AND r.status='completed'
        JOIN learning_input_arrival a ON a.child_id=t.child_id AND a.session_id=t.session_id AND a.client_action_id=t.client_action_id
        WHERE ac.child_id=? AND ac.commitment_id=?''',(child_id,root_id)).fetchone()
    if not row:
        return None
    value=dict(row)
    payload=json.loads(value.pop('payload_json'))
    value['source']=payload.get('source')
    value['source_hash']=payload.get('source_hash')
    value['marker']=json.loads(value.pop('context_manifest_json')).get('oral_supply_obligation')
    return digest(value)


def resolve_request(c, child_id, session_id, root_id):
    """Recheck historical evidence and today's budget without granting a voice action."""
    from .agent_runtime import AgentRuntimeStore
    from .oral_supply_obligation import identity
    from .supply_gap_resume import _plan, _consent_version, budget
    from .task_start_binding import TaskStartBindingError
    base = dict(status='blocked',reason=None,requires_runtime_admission=True,
        requires_new_prompt_and_capture=True,**LOCAL)
    if not c.in_transaction:
        raise ValueError('ORAL_SUPPLY_CARRYOVER_TRANSACTION_REQUIRED')
    roots = pending(c,child_id,commitment_id=root_id)
    root = roots[0] if roots else None
    plan = _plan(c,session_id,child_id)
    if not root:
        return {**base,'reason':'oral_supply_original_unavailable'}
    if not plan:
        return {**base,'reason':'oral_supply_current_plan_required'}
    if (root['root_session_id']==session_id or root['session_status']!='completed'
            or not root['ended_at'] or str(evidence_calendar_date(root['ended_at']))>=plan['plan_date']):
        return {**base,'reason':'oral_supply_closed_day_required'}
    carried = current_plan(c,child_id,root_id,root['source_hash'])
    if not carried or (carried['session_id'],carried['plan_id'])!=(session_id,plan['plan_id']):
        return {**base,'reason':'oral_supply_current_plan_required'}
    # The stored root is a promise, not evidence that the child really requested it.
    original = c.execute('''SELECT ac.payload_json,p.context_manifest_json,t.transcript,
        t.client_action_id,t.agent_turn_id,t.action_json FROM agent_commitment ac
        JOIN agent_turn t ON t.agent_turn_id=json_extract(ac.payload_json,'$.request_turn_id')
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        WHERE ac.commitment_id=? AND t.child_id=? AND t.session_id=?
        AND p.child_id=t.child_id AND p.session_id=t.session_id''',
        (root_id,child_id,root['root_session_id'])).fetchone()
    if not original:
        return {**base,'reason':'oral_supply_original_request_changed'}
    saved = json.loads(original['payload_json'])
    marker = json.loads(original['context_manifest_json']).get('oral_supply_obligation')
    arrival = c.execute('''SELECT utterance,snapshot_json FROM learning_input_arrival
        WHERE child_id=? AND session_id=? AND client_action_id=?''',
        (child_id,root['root_session_id'],original['client_action_id'])).fetchone()
    snapshots = [x.get('record') for x in json.loads(arrival['snapshot_json'])
        if x.get('kind')=='oral_followup_arrival'] if arrival else []
    request = c.execute('''SELECT decision_json,binding_json,fingerprint FROM voice_control_intent_request
        WHERE request_key=? AND child_id=? AND session_id=? AND status='completed' ''',
        (saved.get('request_key'),child_id,root['root_session_id'])).fetchone()
    if (marker!=dict(commitment_id=root_id,source_hash=root['source_hash'],**LOCAL)
            or not arrival or arrival['utterance']!=original['transcript']
            or len(snapshots)!=1 or not isinstance(snapshots[0],dict)
            or snapshots[0].get('objective_id')!=root['objective_id'] or not request):
        return {**base,'reason':'oral_supply_original_request_changed'}
    decision = json.loads(request['decision_json'])
    turn_action = json.loads(original['action_json'])
    fingerprint = hashlib.sha256(json.dumps([child_id,root['root_session_id'],original['transcript']],
        ensure_ascii=False).encode()).hexdigest()
    frozen = (snapshots[0].get('packets') or {}).get('retry') or {}
    if (decision.get('status')!='control' or decision.get('control')!='oral.independent_check'
            or turn_action.get('type')!='none' or turn_action.get('source')!='oral_followup_clarification'
            or request['fingerprint']!=fingerprint
            or json.loads(request['binding_json']).get('oral_followups')!=snapshots[0].get('packets')
            or not isinstance(frozen.get('action'),dict)
            or frozen.get('binding_version')!=digest(frozen['action'])
            or identity(frozen['action'])!=root['source']):
        return {**base,'reason':'oral_supply_original_request_changed'}
    source = root['source']
    checked = AgentRuntimeStore._resolve_oral_followup_source(c,child_id=child_id,
        session_id=root['root_session_id'],prompt_id=source['parent_prompt_id'],
        item_version=source['parent_item_version'],kind='independent_check')
    if checked['status']!='ready':
        return {**base,'reason':checked['reason']}
    action = checked['action']
    # A later correction or substituted old answer must not inherit the old request.
    original_keys = ('original','feedback_version','review_ref','audio_work_ref')
    if identity(action)!=source or any(action.get(k)!=frozen['action'].get(k) for k in original_keys):
        return {**base,'reason':'oral_supply_original_answer_changed'}
    if c.execute('''SELECT 1 FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=?
        AND status IN ('waiting_answer','answer_saved')''',(child_id,session_id)).fetchone():
        return {**base,'reason':'pending_prompt_exists'}
    if c.execute("SELECT 1 FROM plan_item WHERE plan_id=? AND status='active'",(plan['plan_id'],)).fetchone():
        return {**base,'reason':'oral_supply_other_task_active'}
    # Oral sources have no declared duration. Reserve one explicit five-minute
    # check; the common budget still enforces the child's cap and remaining time.
    minutes = 5
    target = dict(child_id=child_id,session_id=session_id,source_type='oral_question_supply',
        plan_item_id=str(uuid5(NAMESPACE_URL,'hsc-oral-supply:'+carried['decision_id']+':'+action['item_version'])),
        expected_minutes=minutes)
    try:
        allowance = budget(c,target)
    except TaskStartBindingError as error:
        return {**base,'reason':str(error)}
    binding = dict(version=RESUME_VERSION,child_id=child_id,session_id=session_id,
        root_commitment_id=root_id,root_objective_id=root['objective_id'],root_session_id=root['root_session_id'],
        source_hash=root['source_hash'],obligation_id=carried['decision_id'],
        obligation_version=digest(dict(carried)),plan_id=plan['plan_id'],plan_version=plan['version'],
        control_consent_version=_consent_version(c),source=action,expected_minutes=minutes,**LOCAL)
    binding['original_request_version']=request_proof_version(c,child_id,root_id)
    return {**base,'status':'ready','binding':binding,'binding_version':digest(binding),'budget':allowance}


def revalidate_request(c, binding, binding_version):
    """A frozen choice is invalid after source, plan, consent or capacity changes."""
    blocked = dict(status='blocked',reason='oral_supply_resume_binding_changed',**LOCAL)
    if (not isinstance(binding,dict) or binding.get('version')!=RESUME_VERSION
            or digest(binding)!=binding_version):
        return blocked
    try:
        current = resolve_request(c,binding['child_id'],binding['session_id'],binding['root_commitment_id'])
    except KeyError:
        return blocked
    if current.get('binding')!=binding or current.get('binding_version')!=binding_version:
        return blocked
    return current


def projection(c, child_id, session_id, root_id):
    current = resolve_request(c,child_id,session_id,root_id)
    reason = current.get('reason')
    if current['status']=='ready':
        state, message = 'needs_plan','合格的同能力新题已补齐，当前预算可留约5分钟；尚未安排或出题，继续前仍需孩子明确同意与执行核验。'
    elif reason=='oral_no_unexposed_source':
        state, message = 'missing_source','原请求和原回答已保留；仍缺同能力、未展示过的新题，需补齐并审核发布，不能重做原题代替。'
    elif reason in ('SUPPLY_RESUME_BUDGET_UNKNOWN','SUPPLY_RESUME_BUDGET_INSUFFICIENT'):
        state, message = 'needs_plan',('合格新题已补齐，但今天的结束时间或用时尚不能核实；先核对安排，不出题或计时。'
            if reason=='SUPPLY_RESUME_BUDGET_UNKNOWN' else '合格新题已补齐，但今天剩余时间不足；原请求保留，不挤占现有安排。')
    elif reason in ('oral_supply_current_plan_required','oral_supply_closed_day_required'):
        state, message = 'needs_plan','原请求已保留；先核对今天的有效计划，不沿用昨天的时间安排。'
    elif reason in ('pending_prompt_exists','oral_supply_other_task_active'):
        state, message = 'needs_replan','孩子还有正在进行的题目或任务；先处理当前学习，不覆盖它来开始新题。'
    else:
        state, message = 'source_changed','原请求、回答或题目来源已变化，需重新核对；不会沿用旧请求开始练习。'
    return dict(status=state,message=message,version=digest([state,message,current.get('binding_version')]),**LOCAL)


def pending(c, child_id, *, session_id=None, commitment_id=None):
    if not c.in_transaction:
        raise ValueError('ORAL_SUPPLY_CARRYOVER_TRANSACTION_REQUIRED')
    rows = c.execute('''SELECT ac.*,o.session_id,o.status AS objective_status,
        ds.status AS session_status,ds.ended_at FROM agent_commitment ac
        JOIN agent_objective o ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id
        JOIN daily_session ds ON ds.session_id=o.session_id AND ds.child_id=o.child_id
        JOIN child_profile cp ON cp.child_id=ac.child_id
        WHERE ac.child_id=? AND cp.status='active' AND o.objective_type='learning'
        AND o.status IN ('active','paused') AND ac.status IN ('pending','due')
        AND (? IS NULL OR o.session_id=?)
        AND (? IS NULL OR ac.commitment_id=?)
        AND CASE WHEN json_valid(ac.payload_json) THEN json_extract(ac.payload_json,'$.origin') END=?
        ORDER BY ac.created_at,ac.commitment_id''',(child_id,session_id,session_id,commitment_id,commitment_id,ORIGIN))
    result = []
    for row in rows:
        payload = json.loads(row['payload_json'])
        source = payload.get('source')
        if (not isinstance(source,dict) or payload.get('source_hash')!=digest(source)
                or source.get('child_id')!=child_id or source.get('session_id')!=row['session_id']
                or payload.get('phase')!='awaiting_qualified_question'):
            raise ValueError('ORAL_SUPPLY_CARRYOVER_SOURCE_CHANGED')
        node = c.execute('SELECT subject FROM knowledge_node WHERE knowledge_node_id=?',
            (source.get('knowledge_node_id'),)).fetchone()
        result.append(dict(commitment_id=row['commitment_id'],objective_id=row['objective_id'],
            root_session_id=row['session_id'],source_hash=payload['source_hash'],source=source,
            subject=node[0] if node else '复习',created_at=row['created_at'],
            session_status=row['session_status'],ended_at=row['ended_at'],**LOCAL))
    return result


def pending_for_session(c, child_id, session_id):
    """Include a retained earlier promise in each day's closure, without copying it."""
    result = []
    for root in pending(c,child_id):
        if root['root_session_id']==session_id:
            result.append(root)
            continue
        rows = c.execute('''SELECT d.metadata_json,d.decision_id,d.created_at,dp.plan_date FROM plan_constraint_decision d
            JOIN daily_plan dp USING(plan_id) JOIN daily_session ds USING(session_id)
            WHERE dp.child_id=? AND ds.child_id=dp.child_id AND dp.session_id=?
            AND dp.status!='cancelled' AND d.obligation_type='oral_question_supply'
            AND d.source_ref=? AND d.decision='deferred' ''',(child_id,session_id,root['commitment_id']))
        for row in rows:
            metadata = json.loads(row['metadata_json'])
            expected = {k:root[k] for k in ('commitment_id','objective_id','root_session_id','source_hash')}
            expected.update(version=VERSION,**LOCAL)
            if not isinstance(metadata,dict) or metadata.get('oral_supply_carryover')!=expected:
                raise ValueError('ORAL_SUPPLY_CARRYOVER_SOURCE_CHANGED')
            if (root['session_status']!='completed' or not root['ended_at']
                    or str(evidence_calendar_date(root['ended_at']))>=row['plan_date']):
                raise ValueError('ORAL_SUPPLY_CARRYOVER_PLAN_CHANGED')
            result.append({**root,'carry_decision_id':row['decision_id'],'retained_at':row['created_at']})
            break
    return result


def append_to_plan(c, child_id, session_id, plan_id, *, now):
    """The plan transaction owns both decisions and their rollback/replay boundary."""
    plan = c.execute('''SELECT dp.plan_date FROM daily_plan dp JOIN daily_session ds USING(session_id)
        WHERE dp.plan_id=? AND dp.session_id=? AND dp.child_id=? AND ds.child_id=dp.child_id
        AND dp.status='active' AND ds.status='active' ''',(plan_id,session_id,child_id)).fetchone()
    if not plan or plan[0]!=str(evidence_calendar_date(now)):
        raise ValueError('ORAL_SUPPLY_CARRYOVER_PLAN_CHANGED')
    for delivery in unfinished_deliveries(c,child_id):
        if (delivery['origin_session_id']==session_id or delivery['session_status']!='completed'
                or not delivery['ended_at'] or str(evidence_calendar_date(delivery['ended_at']))>=plan[0]):
            continue
        identity = str(uuid5(NAMESPACE_URL,'hsc-oral-delivered-carry:'+plan_id+':'+delivery['commitment_id']))
        metadata = json.dumps(dict(title='上次还没回答的口述题',subject=delivery['subject'],
            oral_delivered_continuation=delivery['binding']),ensure_ascii=False)
        prior = c.execute('SELECT metadata_json FROM plan_constraint_decision WHERE decision_id=?',(identity,)).fetchone()
        if prior:
            if prior[0]!=metadata:
                raise ValueError('ORAL_SUPPLY_CARRYOVER_ACTION_CONFLICT')
            continue
        c.execute('''INSERT INTO plan_constraint_decision(decision_id,plan_id,obligation_type,source_ref,
            decision,reason,requested_minutes,allocated_minutes,metadata_json,created_at)
            VALUES(?,?,'oral_delivered_continuation',?,'deferred',?,0,0,?,?)''',
            (identity,plan_id,delivery['commitment_id'],'原题还没回答，进度保留；继续前需重新核对今天时间和孩子意愿。',metadata,now))
    for root in pending(c,child_id):
        if (root['root_session_id']==session_id or root['session_status']!='completed'
                or not root['ended_at'] or str(evidence_calendar_date(root['ended_at']))>=plan[0]):
            continue
        binding = {key:root[key] for key in ('commitment_id','objective_id','root_session_id','source_hash')}
        binding.update(version=VERSION,**LOCAL)
        metadata = dict(title='上次请求的口述新题',subject=root['subject'],oral_supply_carryover=binding)
        encoded = json.dumps(metadata,ensure_ascii=False)
        identity = str(uuid5(NAMESPACE_URL,'hsc-oral-supply-carry:'+plan_id+':'+root['commitment_id']))
        existing = c.execute('SELECT metadata_json FROM plan_constraint_decision WHERE decision_id=?',(identity,)).fetchone()
        if existing:
            if existing[0]!=encoded:
                raise ValueError('ORAL_SUPPLY_CARRYOVER_ACTION_CONFLICT')
            continue
        c.execute('''INSERT INTO plan_constraint_decision(decision_id,plan_id,obligation_type,source_ref,
            decision,reason,requested_minutes,allocated_minutes,metadata_json,created_at)
            VALUES(?,?,'oral_question_supply',?,'deferred',?,0,0,?,?)''',
            (identity,plan_id,root['commitment_id'],
             '保留原目标和原回答；先核对合格新题、今天的时间与孩子意愿，未分配练习，不重做旧题代替。',encoded,now))


def current_plan(c, child_id, root_id, source_hash):
    """A current plan may retain the request, but that is not start permission."""
    rows = c.execute('''SELECT d.*,dp.session_id,ac.objective_id AS root_objective_id,
        o.session_id AS root_session_id FROM plan_constraint_decision d
        JOIN daily_plan dp USING(plan_id) JOIN daily_session ds USING(session_id)
        JOIN agent_commitment ac ON ac.commitment_id=d.source_ref AND ac.child_id=dp.child_id
        JOIN agent_objective o ON o.objective_id=ac.objective_id AND o.child_id=ac.child_id
        WHERE dp.child_id=? AND ds.child_id=dp.child_id AND dp.status='active' AND ds.status='active'
        AND d.obligation_type='oral_question_supply' AND d.source_ref=? AND d.decision='deferred'
        AND dp.plan_date=? ORDER BY d.created_at DESC,d.rowid DESC''',
        (child_id,root_id,str(evidence_calendar_date(datetime.now(timezone.utc).isoformat()))))
    for row in rows:
        metadata = json.loads(row['metadata_json'])
        if not isinstance(metadata,dict):
            raise ValueError('ORAL_SUPPLY_CARRYOVER_SOURCE_CHANGED')
        binding = metadata.get('oral_supply_carryover')
        if (not isinstance(binding,dict) or binding.get('version')!=VERSION
                or binding.get('commitment_id')!=root_id or binding.get('source_hash')!=source_hash
                or binding.get('objective_id')!=row['root_objective_id']
                or binding.get('root_session_id')!=row['root_session_id']
                or any(binding.get(key) is not value for key,value in LOCAL.items())):
            raise ValueError('ORAL_SUPPLY_CARRYOVER_SOURCE_CHANGED')
        return row
    return None


def workspace(c, child_id, session_id, plan_id):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return workspace(c,child_id,session_id,plan_id)
        finally:
            c.rollback()
    from .oral_delivery_resume import workspace as continuation_workspace
    result = continuation_workspace(c,child_id,session_id,plan_id)
    from .oral_daily_supply import workspace as daily_workspace
    result += daily_workspace(c,child_id,session_id,plan_id)
    for delivery in unfinished_deliveries(c,child_id,session_id):
        if delivery['decision_id'] and c.execute('SELECT 1 FROM plan_constraint_decision WHERE decision_id=? AND plan_id=?',
                (delivery['decision_id'],plan_id)).fetchone():
            result.append(dict(obligation_id=delivery['decision_id'],root_obligation_id=delivery['commitment_id'],
                title='上次还没回答的口述题',status=delivery['status'],
                message=('原题还没回答，但来源或交付记录需要重核；记录保留，未开始练习。' if delivery['status']=='source_changed'
                    else '原题还没回答，进度保留；继续前需要核对今天的时间和孩子意愿，尚未安排新的练习。'),
                plan_item_id=None,task_status=None,**LOCAL))
    for root in pending(c,child_id):
        row = current_plan(c,child_id,root['commitment_id'],root['source_hash'])
        if row and (row['plan_id'],row['session_id'])==(plan_id,session_id):
            state = projection(c,child_id,session_id,root['commitment_id'])
            result.append(dict(obligation_id=row['decision_id'],root_obligation_id=root['commitment_id'],
                title='上次请求的口述新题',status=state['status'],message=state['message'],
                plan_item_id=None,task_status=None,**LOCAL))
    delivered=c.execute('''SELECT ac.commitment_id,ac.payload_json,d.decision_id,pi.plan_item_id,pi.status
        FROM plan_constraint_decision d JOIN daily_plan dp USING(plan_id)
        JOIN agent_commitment ac ON ac.commitment_id=d.source_ref AND ac.child_id=dp.child_id
        JOIN plan_item pi ON pi.plan_id=dp.plan_id AND pi.source_ref=ac.commitment_id
            AND pi.source_type='oral_question_supply'
        WHERE dp.plan_id=? AND dp.session_id=? AND dp.child_id=? AND d.decision='deferred'
        AND d.obligation_type='oral_question_supply' AND ac.status='completed' ''',
        (plan_id,session_id,child_id)).fetchall()
    for row in delivered:
        from .oral_supply_execution import delivery_projection
        try:
            payload=json.loads(row['payload_json'])
            binding=current_plan(c,child_id,row['commitment_id'],payload['source_hash'])
            if not binding or binding['decision_id']!=row['decision_id']:
                raise ValueError('ORAL_SUPPLY_CARRYOVER_SOURCE_CHANGED')
            state=delivery_projection(c,child_id,row['commitment_id'],payload['resolution'])
            if state['plan_item_id']!=row['plan_item_id']:
                raise ValueError('ORAL_SUPPLY_DELIVERY_CHANGED')
        except (ValueError,KeyError,TypeError):
            state=dict(status='source_changed',next_action='原新题交付需要核对，已有题目和回答保留。')
        result.append(dict(obligation_id=row['decision_id'],root_obligation_id=row['commitment_id'],
            title='上次请求的口述新题',status='active' if state['status']=='ready' else state['status'],
            message=state['next_action'],plan_item_id=row['plan_item_id'],task_status=row['status'],**LOCAL))
    return result
