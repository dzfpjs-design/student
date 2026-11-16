"""Persist an unavailable original task as a local, revocable recovery offer."""
import json

from .task_start_binding import TaskStartBinding
from .task_start_choices import fingerprint, latest_turn


def original_state(c, child_id, item):
    row = c.execute('''SELECT status,prompt_summary,submitted_answer,source_type,source_ref
        FROM mistake_case WHERE mistake_id=? AND child_id=?''', (item.get('source_ref'), child_id)).fetchone()
    if not row:
        return None
    from .paper_formula_review import for_mistake
    review = for_mistake(c, item.get('source_ref'), child_id)
    return {**dict(row), 'question_review': review}


def source_gap(c, child_id, snapshot):
    item = snapshot.get('item') or {}
    if snapshot.get('status') != 'ready' or item.get('source_type') != 'mistake_review':
        return None
    row = original_state(c, child_id, item)
    reason = ('missing_original' if not row else 'inactive_original' if row['status'] in {'excluded','mastered'}
              else 'invalidated_review' if (row.get('question_review') or {}).get('status') == 'invalidated'
              else 'empty_original' if not (row['prompt_summary'] or '').strip() else None)
    if not reason:
        return None
    return {'version': 'task_source_gap_v1', 'snapshot': snapshot, 'reason': reason,
            'source_version': fingerprint(row), 'local_only': True, 'cloud_transmission_allowed': False}


def clarification(value):
    reason = {'missing_original': '这项复习的原错题记录暂时无法核对',
              'inactive_original': '这项旧错题记录目前不在可复习状态',
              'invalidated_review': '这项原题的纸面复核记录已经失效，需要重新核对',
              'empty_original': '这项复习的原题文字不完整'}[value['reason']]
    return (reason + '，没有开始计时，也没有换成别的题。原计划保留。'
            '原题核对恢复后可以重新开始；也可以告诉我把这项延期，先处理其他安排。'), {
        'type': 'none', 'source': 'task_navigation_clarification', 'task_source_gap': value,
        'local_only': True, 'cloud_transmission_allowed': False}


def capture_reference(target):
    gap = (target or {}).get('_source_gap')
    return {'offer_turn_id': gap['turn_id'], 'source_version': fingerprint(gap['value'])} if gap else None


def listening_gap(c, child_id, session_id, plan_item_id, prompt_id, item_version):
    """Freeze the actual unavailable original; no default pending-task authority."""
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return listening_gap(c, child_id, session_id, plan_item_id, prompt_id, item_version)
        finally:
            c.rollback()
    from .pet_task_execution import resolve_pet_task_execution
    from .pet_listening_admission import resolve_pet_listening_resource
    snapshot = TaskStartBinding.start_snapshot(c, session_id, child_id)
    selected = [item for item in snapshot['items'] if item['plan_item_id'] == plan_item_id]
    if snapshot['status'] != 'ready' or len(selected) != 1 or selected[0]['status'] != 'pending':
        return None
    source = resolve_pet_task_execution(c, session_id, plan_item_id)
    if (source['status'] != 'ready' or source['child_id'] != child_id
            or source['item_version'] != item_version):
        return None
    prompt = c.execute('SELECT diagnostic_item_id,status FROM diagnostic_prompt WHERE prompt_id=?', (prompt_id,)).fetchone()
    if not prompt or prompt[0] != source['diagnostic_item_id'] or prompt['status'] != 'waiting_answer':
        return None
    resource = resolve_pet_listening_resource(c, session_id, child_id, prompt_id, item_version)
    if resource['status'] != 'awaiting_resources' or resource['reason'] == 'listening_prompt_unavailable':
        return None
    media = [dict(row) for row in c.execute("""SELECT media_id,source_version,state_json FROM pet_media_binding
        WHERE child_id=? AND session_id=? AND prompt_id=? AND item_version=? AND kind='listening'
        ORDER BY media_id""", (child_id, session_id, prompt_id, item_version))] if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_media_binding'").fetchone() else []
    return {'version': 'task_listening_source_gap_v1', 'snapshot': {**snapshot, 'item': selected[0]},
            'prompt_id': prompt_id, 'item_version': item_version, 'reason': resource['reason'],
            'source_version': fingerprint({'binding': source['binding'], 'media': media}),
            'local_only': True, 'cloud_transmission_allowed': False}


def listening_deferral_target(c, child_id, session_id, turn_id):
    row = c.execute("""SELECT s.result_json FROM agent_turn t JOIN agent_plan p
        ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND t.tool_status='completed'
        AND p.status IN ('completed','waiting_child') AND s.status='completed'
        AND s.capability_name IN ('task.start','task.resume')""", (turn_id, child_id, session_id)).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row[0])['result']
        value = result.get('task_resource_gap')
        if result.get('status') != 'awaiting_resources' or not value:
            return None
        current = listening_gap(c, child_id, session_id, result['plan_item_id'],
                                value['prompt_id'], value['item_version'])
        if value != current or result.get('reason') != value['reason']:
            return None
        item = c.execute("""SELECT i.*,p.child_id,p.session_id,p.version AS plan_version
            FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE i.plan_item_id=?""",
            (result['plan_item_id'],)).fetchone()
        return {**dict(item), '_source_gap': {'turn_id': turn_id, 'value': value}}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def deferral_target(c, child_id, session_id, *, before_turn=None):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return deferral_target(c, child_id, session_id, before_turn=before_turn)
        finally:
            c.rollback()
    turn_id = latest_turn(c, session_id, child_id, before_turn)
    row = c.execute('''SELECT t.action_json,t.transcript,t.client_action_id FROM agent_turn t
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND t.tool_status='completed'
        AND p.status IN ('completed','waiting_child') AND s.status='completed'
        AND s.capability_name='conversation.respond' ''', (turn_id, child_id, session_id)).fetchone()
    if not row:
        return listening_deferral_target(c, child_id, session_id, turn_id)
    try:
        action = json.loads(row['action_json'])
        value = action.get('task_source_gap')
        if action.get('source') != 'task_navigation_clarification' or not value:
            return None
        snapshot = value['snapshot']
        arrival = c.execute('''SELECT snapshot_json FROM learning_input_arrival
            WHERE child_id=? AND session_id=? AND client_action_id=?''',
            (child_id, session_id, row['client_action_id'])).fetchone()
        origins = [part['snapshot'] for part in json.loads(arrival[0])
                   if part.get('kind') == 'task_navigation_arrival'] if arrival else []
        semantic = snapshot.get('semantic_start')
        if semantic:
            from .task_start_semantics import validate
            validate(semantic['selector'], row['transcript'])
            expected = TaskStartBinding.semantic_start_target(origins[0], semantic['selector']) if len(origins) == 1 else None
        else:
            expected = origins[0] if len(origins) == 1 else None
        if snapshot != expected or value != source_gap(c, child_id, snapshot):
            return None
        selected = TaskStartBinding.validate_start_snapshot(c, session_id, child_id, snapshot)
        if selected['status'] != 'pending':
            return None
        item = c.execute('''SELECT i.*,p.child_id,p.session_id,p.version AS plan_version
            FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE i.plan_item_id=?''',
            (selected['plan_item_id'],)).fetchone()
        return {**dict(item), '_source_gap': {'turn_id': turn_id, 'value': value}}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
