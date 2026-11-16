"""Trace unfinished PET obligations through existing append-only plan decisions.

Planning carries a promise, never starts it. No parallel learning/event store is
created; existing plan/privacy ownership continues to own all persisted rows.
"""
import json
from datetime import datetime, timezone
from uuid import uuid5, NAMESPACE_URL

from .local_supply import digest


VERSION = 'supply_carryover_v1'
GAPS = ('pet-writing-gap:', 'pet-skill-gap:')


def is_obligation(row):
    return (row['obligation_type'] == 'pet_daily' and row['decision'] == 'deferred'
        and (str(row['source_ref']).startswith(GAPS)
             or isinstance(json.loads(row['metadata_json']).get('supply_carryover'), dict)))


def _decision(row):
    return {k: row[k] for k in ('decision_id', 'plan_id', 'obligation_type', 'source_ref',
        'decision', 'reason', 'requested_minutes', 'allocated_minutes', 'metadata_json', 'created_at')}


def _records(c, child_id):
    return [dict(r) for r in c.execute('''SELECT d.*, ds.session_id,ds.status AS session_status,
        p.plan_date FROM plan_constraint_decision d JOIN daily_plan p USING(plan_id)
        JOIN daily_session ds ON ds.session_id=p.session_id AND ds.child_id=p.child_id
        WHERE p.child_id=? AND d.obligation_type='pet_daily' ORDER BY d.created_at,d.rowid''', (child_id,))]


def _fixed_item(c, row, metadata):
    previous = metadata.get('supply_carryover') or {}
    if previous.get('fixed_item'):
        return previous['fixed_item']
    supplied = metadata.get('supply_resume') or {}
    if supplied.get('supply_item'):
        return supplied['supply_item']
    if str(row['source_ref']).startswith(GAPS):
        return None
    evidence = metadata.get('planning_evidence') or {}
    if evidence.get('review_task_id'):
        return None
    if not (evidence.get('pet_question_binding') or evidence.get('stage_constraint')):
        return None
    task = c.execute('SELECT * FROM plan_item WHERE plan_item_id=? AND plan_id=?',
        (metadata.get('plan_item_id'), row['plan_id'])).fetchone()
    kind = 'writing' if evidence.get('stage_constraint') else (
        'reading' if evidence.get('pet_question_binding', {}).get('skill') == 'reading' else 'speaking')
    return dict(source_type='pet_daily', source_ref=row['source_ref'],
        subject=metadata.get('subject') or '英语', title=metadata.get('title') or '原PET练习',
        minutes=row['requested_minutes'], task_type=task['task_type'] if task else kind,
        completion_standard=task['completion_standard'] if task else '完成原题练习并保存实际回执',
        cognitive_load=task['cognitive_load'] if task else 'medium',
        planning_evidence=evidence)


def pending_history(c, child_id, target_session_id, *, as_of=None):
    if not c.in_transaction:
        raise ValueError('SUPPLY_HISTORY_TRANSACTION_REQUIRED')
    from .learning_input_replan import ZONE
    now = as_of or datetime.now(timezone.utc)
    today = now.astimezone(ZONE).date().isoformat()
    records = _records(c, child_id)
    by_id = {r['decision_id']: r for r in records}
    roots, root_for = {}, {}
    for row in records:
        m = json.loads(row['metadata_json'])
        carried = m.get('supply_carryover')
        placed = m.get('supply_resume')
        if carried:
            root_id = carried['root_decision_id']
            root = by_id.get(root_id)
            if not root or digest(_decision(root)) != carried['root_version']:
                raise ValueError('SUPPLY_HISTORY_ROOT_CHANGED')
        elif placed:
            root_id = root_for.get(placed['obligation_id'])
            if not root_id:
                continue
        elif str(row['source_ref']).startswith(GAPS) and row['decision'] == 'deferred':
            root_id = row['decision_id']
        elif row['decision'] in {'scheduled', 'deferred'} and _fixed_item(c, row, m):
            root_id = row['decision_id']
        else:
            continue
        root_for[row['decision_id']] = root_id
        entry = roots.setdefault(root_id, {'root': by_id[root_id], 'latest': row,
            'fixed_item': None, 'task': None, 'task_origin': None})
        entry['latest'] = row
        fixed = _fixed_item(c, row, m)
        if fixed:
            entry['fixed_item'] = fixed
        task_id = m.get('plan_item_id')
        if task_id:
            task = c.execute('SELECT * FROM plan_item WHERE plan_item_id=? AND plan_id=?',
                (task_id, row['plan_id'])).fetchone()
            if task:
                entry['task'] = dict(task)
                entry['task_origin'] = dict(plan_item_id=task_id, plan_id=row['plan_id'], session_id=row['session_id'])
    results = []
    for entry in roots.values():
        latest, root, task = entry['latest'], entry['root'], entry['task']
        if latest['session_id'] == target_session_id or latest['session_status'] != 'completed' or latest['plan_date'] >= today:
            continue
        if task and task['status'] in {'completed', 'cancelled'}:
            continue
        entry['kind'] = ('explicitly_deferred' if task and task['status'] == 'rescheduled'
            else 'placed_original' if entry['fixed_item'] else 'unselected_gap')
        results.append(entry)
    # The generic writing shortage and its detailed node shortages were two
    # views of one planning gap, not two independent promises to do more work.
    detailed_plans = {e['root']['plan_id'] for e in results if str(e['root']['source_ref']).startswith('pet-writing-gap:')}
    return [e for e in results if not (e['root']['source_ref'] == 'pet-skill-gap:writing'
                                      and e['root']['plan_id'] in detailed_plans)]


def skill(entry):
    fixed = entry['fixed_item']
    if fixed:
        evidence = fixed['planning_evidence']
        return 'writing' if evidence.get('stage_constraint') else evidence.get('pet_question_binding', {}).get('skill')
    ref = entry['root']['source_ref']
    return 'writing' if str(ref).startswith('pet-writing-gap:') else str(ref).split(':')[-1]


def append_to_plan(c, child_id, session_id, plan_id, entries, *, now):
    """Caller owns the plan write transaction; zero tasks/prompts are created."""
    if not c.in_transaction:
        raise ValueError('SUPPLY_HISTORY_TRANSACTION_REQUIRED')
    plan = c.execute('''SELECT p.plan_id FROM daily_plan p JOIN daily_session s USING(session_id)
        WHERE p.plan_id=? AND p.session_id=? AND p.child_id=? AND s.child_id=p.child_id
        AND p.status='active' AND s.status='active' ''', (plan_id, session_id, child_id)).fetchone()
    if not plan:
        raise ValueError('SUPPLY_HISTORY_SCOPE_CHANGED')
    for entry in entries:
        root, latest = entry['root'], entry['latest']
        for frozen in (root, latest):
            actual = c.execute('''SELECT d.* FROM plan_constraint_decision d JOIN daily_plan p USING(plan_id)
                JOIN daily_session s ON s.session_id=p.session_id AND s.child_id=p.child_id
                WHERE d.decision_id=? AND p.child_id=? AND s.status='completed' ''',
                (frozen['decision_id'], child_id)).fetchone()
            if not actual or digest(dict(actual)) != digest(_decision(frozen)):
                raise ValueError('SUPPLY_HISTORY_SCOPE_CHANGED')
        root_metadata = json.loads(root['metadata_json'])
        fixed = entry['fixed_item']
        metadata = {k: root_metadata[k] for k in ('title', 'subject', 'planning_evidence') if k in root_metadata}
        metadata['supply_carryover'] = dict(version=VERSION, root_decision_id=root['decision_id'],
            root_version=digest(_decision(root)), root_plan_id=root['plan_id'],
            root_session_id=root['session_id'], previous_decision_id=latest['decision_id'],
            previous_version=digest(_decision(latest)), kind=entry['kind'],
            fixed_item=fixed, task_origin=entry['task_origin'])
        if fixed:
            metadata.update(title=fixed['title'], subject=fixed['subject'])
        identity = str(uuid5(NAMESPACE_URL, 'hsc-supply-carry:' + plan_id + ':' + root['decision_id']))
        encoded = json.dumps(metadata, ensure_ascii=False)
        values = (identity, plan_id, root['source_ref'], root['requested_minutes'], encoded)
        existing = c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?', (identity,)).fetchone()
        if existing:
            if (existing['plan_id'], existing['metadata_json']) != (plan_id, encoded):
                raise ValueError('SUPPLY_HISTORY_ACTION_CONFLICT')
            continue
        c.execute('''INSERT INTO plan_constraint_decision
            (decision_id,plan_id,obligation_type,source_ref,decision,reason,requested_minutes,allocated_minutes,metadata_json,created_at)
            VALUES(?,?,'pet_daily',?,'deferred','原供给义务跨日保留；等待今天的新语音确认和预算核对，不自动开始。',?,0,?,?)''', (*values, now))


def validate_inherited(c, child_id, gap):
    """Deletion/cancellation of a historical source must also veto a late start."""
    carried = json.loads(gap['metadata_json']).get('supply_carryover')
    if not carried:
        return True
    if carried.get('version') != VERSION:
        return False
    for id_key, version_key in (('root_decision_id', 'root_version'), ('previous_decision_id', 'previous_version')):
        row = c.execute('''SELECT d.* FROM plan_constraint_decision d JOIN daily_plan p USING(plan_id)
            WHERE d.decision_id=? AND p.child_id=?''', (carried[id_key], child_id)).fetchone()
        if not row or digest(dict(row)) != carried[version_key]:
            return False
    origin = carried.get('task_origin')
    if origin:
        task = c.execute('''SELECT pi.status,pi.source_ref FROM plan_item pi JOIN daily_plan p USING(plan_id)
            WHERE pi.plan_item_id=? AND pi.plan_id=? AND p.child_id=? AND p.session_id=?''',
            (origin['plan_item_id'], origin['plan_id'], child_id, origin['session_id'])).fetchone()
        if not task or task['status'] in {'completed', 'cancelled'}:
            return False
        # The original prepared prompt is a separate cancellation boundary;
        # postponing the plan item must not discard that child's decision.
        bindings = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
            AND json_extract(context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?''',
            (child_id, origin['session_id'], origin['plan_item_id'])).fetchall()
        for row in bindings:
            binding = json.loads(row[0])['goal_prompt_binding']
            prompt = c.execute('''SELECT status FROM diagnostic_prompt WHERE prompt_id=? AND item_version=?
                AND child_id=? AND origin_session_id=? AND diagnostic_item_id=?''',
                (binding.get('prompt_id'), binding.get('item_version'), child_id, origin['session_id'], task['source_ref'])).fetchone()
            if not prompt or prompt['status'] in {'cancelled', 'invalidated'}:
                return False
    return True
