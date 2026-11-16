"""Explicitly replace an unexecuted legacy review, never repair its history."""
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from .local_supply import digest
from .task_start_binding import TaskStartBindingError

VERSION = 'legacy_listening_review_replacement_v1'
KEY = 'legacy_listening_review_replacement'


def require(value, code='TASK_START_LEGACY_REVIEW_CHANGED'):
    if not value:
        raise TaskStartBindingError(code)


def _now():
    from .learning_input_replan import datetime, timezone
    return datetime.now(timezone.utc)


def _plan(c, sid, child):
    from .learning_input_replan import ZONE
    rows = c.execute('''SELECT dp.* FROM daily_plan dp JOIN daily_session s USING(session_id)
        JOIN child_profile cp ON cp.child_id=dp.child_id WHERE dp.session_id=? AND dp.child_id=?
        AND s.child_id=dp.child_id AND dp.status='active' AND s.status='active'
        AND s.state='study' AND cp.status='active' ''', (sid, child)).fetchall()
    return rows[0] if len(rows) == 1 and rows[0]['plan_date'] == _now().astimezone(ZONE).date().isoformat() else None


def _decisions(c, item):
    return [dict(r) for r in c.execute('''SELECT * FROM plan_constraint_decision WHERE plan_id=?
        AND obligation_type='due_review' AND source_ref=? AND decision='scheduled' ORDER BY decision_id''',
        (item['plan_id'], item['source_ref'])).fetchall()
        if json.loads(r['metadata_json']).get('plan_item_id') in (None, item['plan_item_id'])]


def candidate(c, sid, child, item):
    from .pet_task_execution import is_listening_review_task, freeze_listening_review
    if (item['source_type'] != 'due_review' or item['task_type'] != 'review'
            or item['status'] != 'pending' or item['started_at'] or item['completed_at']
            or item['active_seconds'] or item['actual_minutes'] is not None
            or not is_listening_review_task(c, item)):
        return None
    plan = _plan(c, sid, child)
    if not plan or plan['plan_id'] != item['plan_id']:
        return None
    if not c.execute("SELECT 1 FROM review_task WHERE review_task_id=? AND child_id=? AND status='pending'",
            (item['source_ref'], child)).fetchone():
        return None
    rows = _decisions(c, item)
    if (not rows or any('pet_question_binding' in json.loads(r['metadata_json']).get('planning_evidence', {})
            or KEY in json.loads(r['metadata_json']) for r in rows)
            or c.execute('SELECT 1 FROM plan_item_event WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()
            or c.execute('''SELECT 1 FROM agent_plan WHERE child_id=? AND session_id=?
                AND json_extract(context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?''',
                (child, sid, item['plan_item_id'])).fetchone()):
        return None
    # An altered current source cannot be used to upgrade an existing bad snapshot.
    try:
        binding = freeze_listening_review(c, child, item['source_ref'])
    except ValueError:
        return None
    from .voice_control_intent import VoiceControlIntent
    return {**{k: item[k] for k in ('plan_item_id','plan_id','source_ref','source_type','task_type',
        'subject','title','homework_id','expected_minutes','started_at','active_seconds')},
        'operation': 'start', 'version': VERSION, 'child_id': child, 'session_id': sid,
        'plan_version': plan['version'], 'old_item_version': digest(dict(item)),
        'old_decisions_version': digest(rows), 'review_binding': binding,
        'control_consent_version': list(VoiceControlIntent.text_consent_version(c) or ()),
        'pause_event_id': None, 'goal_prompt_binding': None, 'origin_turn_id': None}


def candidates(c, sid, child):
    rows = c.execute('''SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active' AND pi.source_type='due_review'
        AND pi.status='pending' ORDER BY pi.position,pi.plan_item_id''', (sid, child)).fetchall()
    return [value for row in rows if (value := candidate(c, sid, child, row)) is not None]


def arrival(c, turn_id, item_id):
    row = c.execute('''SELECT t.*,p.context_manifest_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id''', (turn_id,)).fetchone()
    require(row)
    from .agent_runtime import AgentRuntimeStore
    action = json.loads(row['action_json'])
    require(action.get('voice_input_id'), 'TASK_START_LEGACY_REVIEW_VOICE_REQUIRED')
    voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=row['session_id'], child_id=row['child_id'],
        voice_input_id=action['voice_input_id'], transcript=row['transcript'])
    require(AgentRuntimeStore.voice_capture_snapshot(voice) == action.get('voice_input_binding'),
        'TASK_START_LEGACY_REVIEW_VOICE_CHANGED')
    manifest = json.loads(row['context_manifest_json'])
    target = (manifest.get('voice_control_binding', {}).get('targets', {}).get('task_resume') or {}).get('item')
    if not target:
        # task.start's local action still uses the exact pre-classification snapshot.
        require(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_control_intent_request'").fetchone(),
            'TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED')
        key = hashlib.sha256(('voice-control:' + row['client_action_id']).encode()).hexdigest()
        journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([row['child_id'], row['session_id'], row['transcript']], ensure_ascii=False).encode()).hexdigest()
        require(journal and journal['child_id'] == row['child_id'] and journal['session_id'] == row['session_id']
            and journal['status'] == 'completed' and journal['fingerprint'] == fingerprint)
        decision = json.loads(journal['decision_json'] or '{}')
        require(decision.get('status') == 'control' and decision.get('control') == 'task.resume',
            'TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED')
        snapshot = json.loads(journal['binding_json']).get('task_resume', {})
        require(snapshot.get('status') == 'ready' and len(snapshot.get('items', [])) == 1)
        target = snapshot.get('item')
    require(target and target.get('version') == VERSION and target['plan_item_id'] == item_id)
    return target


def start_arrival(c, turn_id):
    """Only a captured legacy target can take precedence over the current queue."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_control_intent_request'").fetchone():
        return None
    row = c.execute('SELECT client_action_id,child_id,session_id FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    require(row)
    key = hashlib.sha256(('voice-control:' + row['client_action_id']).encode()).hexdigest()
    journal = c.execute('SELECT binding_json FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
    if not journal:
        return None
    snapshot = json.loads(journal[0]).get('task_resume', {})
    if not any(item.get('version') == VERSION for item in snapshot.get('items', [])):
        return None
    require(snapshot.get('status') == 'ready' and len(snapshot.get('items', [])) == 1
        and snapshot.get('item', {}).get('version') == VERSION, 'TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED')
    target = arrival(c, turn_id, snapshot['item']['plan_item_id'])
    from .voice_control_intent import VoiceControlIntent
    require(VoiceControlIntent.scope_valid(c, row['session_id'], row['child_id'], row['client_action_id']),
        'TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED')
    validate_target(c, row['session_id'], row['child_id'], target)
    return {key: target[key] for key in ('plan_item_id', 'source_type', 'source_ref', 'operation')}


def budget(c, target, *, replacement_id=None):
    from .learning_input_replan import LearningInputReplanService
    plan = _plan(c, target['session_id'], target['child_id'])
    require(plan, 'TASK_START_LEGACY_REVIEW_PLAN_CHANGED')
    rule = c.execute('SELECT * FROM family_rule WHERE child_id=?', (target['child_id'],)).fetchone()
    session = c.execute('SELECT s.*,cp.grade FROM daily_session s JOIN child_profile cp USING(child_id) WHERE session_id=?',
        (target['session_id'],)).fetchone()
    context = c.execute('SELECT * FROM session_planning_context WHERE session_id=?', (target['session_id'],)).fetchone()
    require(rule, 'TASK_START_LEGACY_REVIEW_BUDGET_UNKNOWN')
    context = dict(context) if context else {'energy_level': 'medium', 'fixed_commitment_minutes': 0}
    items = [dict(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=?', (plan['plan_id'],)).fetchall()
        if r['plan_item_id'] not in {target['plan_item_id'], replacement_id}]
    info, available, reason = LearningInputReplanService(None)._budget(
        c, target['child_id'], session, plan, items, rule, context, {}, _now())
    require(reason is None, 'TASK_START_LEGACY_REVIEW_BUDGET_UNKNOWN')
    require(0 < target['expected_minutes'] <= info['effective_task_segment_minutes_cap']
        and target['expected_minutes'] * 60 <= available, 'TASK_START_LEGACY_REVIEW_BUDGET_INSUFFICIENT')
    return {**info, 'remaining_seconds': available}


def receipt(c, turn_id):
    row = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()
    return json.loads(row[0]).get(KEY) if row else None


def validate_target(c, sid, child, target, *, mapped=None):
    from .pet_task_execution import freeze_listening_review
    from .voice_control_intent import VoiceControlIntent
    require(target.get('version') == VERSION and target['session_id'] == sid and target['child_id'] == child)
    plan = _plan(c, sid, child)
    require(plan and plan['plan_id'] == target['plan_id']
        and plan['version'] == target['plan_version'] + bool(mapped), 'TASK_START_LEGACY_REVIEW_PLAN_CHANGED')
    require(target['control_consent_version'] == list(VoiceControlIntent.text_consent_version(c) or ()),
        'TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED')
    try:
        current = freeze_listening_review(c, child, target['source_ref'])
    except ValueError as error:
        raise TaskStartBindingError('TASK_START_LEGACY_REVIEW_CHANGED') from error
    require(current == target['review_binding'])
    require(c.execute("SELECT 1 FROM review_task WHERE review_task_id=? AND child_id=? AND status='pending'",
        (target['source_ref'], child)).fetchone())
    old = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (target['plan_item_id'],)).fetchone()
    require(old)
    normalized = dict(old)
    if mapped:
        require(old['status'] == 'cancelled' and old['started_at'] is None)
        normalized['status'] = 'pending'
        require(mapped['origin_target'] == target)
        new = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (mapped['replacement_plan_item_id'],)).fetchone()
        require(new and all(new[key] == old[key] for key in ('plan_id','source_type','source_ref','task_type',
            'subject','title','homework_id','expected_minutes','position','priority','completion_standard','cognitive_load'))
            and new['status'] == 'pending' and new['started_at'] is None)
        event = c.execute('SELECT * FROM plan_item_event WHERE client_action_id=?',
            ('legacy-listening-replacement:' + mapped['requested_by_turn_id'],)).fetchone()
        require(event and event['plan_item_id'] == old['plan_item_id'] and event['event_type'] == 'cancelled'
            and json.loads(event['payload_json']) == {'reason': VERSION, 'replacement_plan_item_id': new['plan_item_id'],
                'authorized_turn_id': mapped['requested_by_turn_id']})
    require(digest(normalized) == target['old_item_version']
        and digest(_decisions(c, old)) == target['old_decisions_version'])
    require(not c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE dp.child_id=? AND dp.session_id=? AND dp.status='active' AND pi.status='active' ''', (child, sid)).fetchone(),
        'TASK_START_LEGACY_REVIEW_ACTIVE_TASK')
    budget(c, target, replacement_id=mapped['replacement_plan_item_id'] if mapped else None)


def materialize(c, turn_id, item):
    from .task_start_binding import TaskStartBinding
    require(c.in_transaction)
    target = arrival(c, turn_id, item['plan_item_id'])
    # This checks the real original command/capture before any replacement writes.
    row = c.execute('SELECT capability_name FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0', (turn_id,)).fetchone()
    if row[0] == 'task.resume':
        from .voice_control_intent import VoiceControlIntent
        control = VoiceControlIntent.inspect_execution(c, turn_id)
        require(control and not control.get('recorded_receipt'))
    else:
        TaskStartBinding.authorize(c, turn_id, item['plan_item_id'], 'start')
    require(receipt(c, turn_id) is None)
    validate_target(c, target['session_id'], target['child_id'], target)
    require(not c.execute("SELECT 1 FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=? AND status IN ('waiting_answer','answer_saved')",
        (target['child_id'], target['session_id'])).fetchone(), 'TASK_START_LEGACY_REVIEW_PENDING_QUESTION')
    rid = str(uuid5(NAMESPACE_URL, 'hsc-legacy-listening:' + turn_id + ':' + item['plan_item_id']))
    mapped = {'version': VERSION, 'origin_target': target, 'requested_by_turn_id': turn_id,
        'replacement_plan_item_id': rid, 'placement_plan_version': target['plan_version'] + 1,
        'budget_receipt': budget(c, target)}
    require(c.execute("UPDATE plan_item SET status='cancelled' WHERE plan_item_id=? AND status='pending' AND started_at IS NULL",
        (item['plan_item_id'],)).rowcount == 1)
    columns = ('plan_id','homework_id','subject','title','expected_minutes','priority','position','source_type',
        'source_ref','completion_standard','cognitive_load','task_type')
    c.execute('INSERT INTO plan_item(plan_item_id,status,' + ','.join(columns) + ') VALUES (?,?, ' + ','.join('?' for _ in columns) + ')',
        (rid, 'pending', *[item[k] for k in columns]))
    c.execute('INSERT INTO plan_item_event VALUES (?,?,?,?,?,?)', (str(uuid5(NAMESPACE_URL, rid + ':cancel')),
        item['plan_item_id'], 'cancelled', _now().isoformat(), json.dumps({'reason': VERSION, 'replacement_plan_item_id': rid,
        'authorized_turn_id': turn_id}), 'legacy-listening-replacement:' + turn_id))
    metadata = {'plan_item_id': rid, 'planning_evidence': {'pet_question_binding': target['review_binding']}, KEY: mapped}
    c.execute('INSERT INTO plan_constraint_decision VALUES (?,?,?,?,?,?,?,?,?,?)',
        (str(uuid5(NAMESPACE_URL, rid + ':decision')), item['plan_id'], 'due_review', item['source_ref'], 'scheduled',
        '孩子明确重新承接原复测；保留旧计划记录，以当前原题和预算新建绑定。', item['expected_minutes'], item['expected_minutes'],
        json.dumps(metadata, ensure_ascii=False), _now().isoformat()))
    require(c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id=? AND version=? AND status='active'",
        (item['plan_id'], target['plan_version'])).rowcount == 1, 'TASK_START_LEGACY_REVIEW_PLAN_CHANGED')
    manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()[0])
    manifest[KEY] = mapped
    manifest['task_start_binding'] = {'plan_item_id': rid, 'source_type': 'due_review', 'source_ref': item['source_ref'], 'operation': 'start'}
    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?', (json.dumps(manifest), turn_id))
    return {**dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (rid,)).fetchone()), '_start_operation': 'start'}


def mapped_target(c, turn_id, target):
    mapped = receipt(c, turn_id)
    if not mapped:
        return target
    require(mapped.get('version') == VERSION and mapped['requested_by_turn_id'] == turn_id and mapped['origin_target'] == target)
    rows = c.execute('''SELECT metadata_json FROM plan_constraint_decision WHERE plan_id=? AND decision='scheduled'
        AND json_extract(metadata_json,'$.plan_item_id')=?''', (target['plan_id'], mapped['replacement_plan_item_id'])).fetchall()
    require(len(rows) == 1 and json.loads(rows[0][0]).get(KEY) == mapped)
    return {**target, 'plan_item_id': mapped['replacement_plan_item_id']}


def validate_mapped_start(c, turn_id, item_id):
    mapped = receipt(c, turn_id)
    if not mapped:
        return False
    target = mapped['origin_target']
    require(mapped_target(c, turn_id, target)['plan_item_id'] == item_id)
    validate_target(c, target['session_id'], target['child_id'], target, mapped=mapped)
    return True


def validate_current_budget(c, sid, child, item_id):
    rows = c.execute('''SELECT metadata_json FROM plan_constraint_decision d JOIN daily_plan p USING(plan_id)
        WHERE p.child_id=? AND p.session_id=? AND d.decision='scheduled'
        AND json_extract(d.metadata_json,'$.plan_item_id')=? AND json_type(d.metadata_json,?)='object' ''',
        (child, sid, item_id, '$.' + KEY)).fetchall()
    if not rows:
        return
    require(len(rows) == 1)
    mapped = json.loads(rows[0][0])[KEY]
    target = mapped['origin_target']
    require(target['session_id'] == sid and target['child_id'] == child
        and mapped_target(c, mapped['requested_by_turn_id'], target)['plan_item_id'] == item_id)
    budget(c, target, replacement_id=item_id)
