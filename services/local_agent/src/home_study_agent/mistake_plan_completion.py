"""Settle only the daily review bound before a successful mistake answer."""
import json
import math

from .diagnostic_prompt import _TransactionDatabase
from .learning_obligations import project_child_mastery_evidence
from . import repository


def active_binding(connection, focus):
    rows = connection.execute('''SELECT pi.plan_item_id, pi.plan_id, pi.source_ref,
        pi.started_at, dp.child_id, dp.session_id
        FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
        JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
        WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active'
          AND ds.status='active' AND ds.state='study'
          AND pi.status='active' AND pi.source_type='mistake_review'
          AND pi.source_ref=? AND pi.started_at IS NOT NULL''',
        (focus['session_id'], focus['child_id'], focus['mistake_id'])).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def settle(connection, focus, operation, book):
    if not connection.in_transaction:
        raise ValueError('mistake settlement requires the answer transaction')
    binding = operation.get('plan_item_binding')
    if not binding:
        return None
    key = operation['action_id'] + ':plan-completion'
    event = connection.execute('''SELECT pe.plan_item_id, pe.payload_json FROM plan_item_event pe
        JOIN plan_item pi ON pi.plan_item_id=pe.plan_item_id JOIN daily_plan dp ON dp.plan_id=pi.plan_id
        WHERE pe.client_action_id=? AND pe.event_type='completed' AND pi.status='completed'
          AND dp.child_id=? AND dp.session_id=? AND pi.source_ref=?''',
        (key, focus['child_id'], focus['session_id'], focus['mistake_id'])).fetchone()
    if event:
        receipt = json.loads(event['payload_json']).get('mistake_completion')
        if receipt and receipt.get('binding') == binding:
            return receipt
        raise ValueError('mistake completion receipt conflicts with the original answer')
    if active_binding(connection, focus) != binding:
        return None
    attempt = connection.execute('''SELECT ma.*, mc.knowledge_node_id FROM mistake_attempt ma
        JOIN mistake_case mc ON mc.mistake_id=ma.mistake_id AND mc.child_id=ma.child_id
        WHERE ma.client_action_id=? AND ma.child_id=? AND ma.mistake_id=?
          AND ma.stage=? AND ma.diagnostic_item_id=? AND ma.submitted_answer=?
          AND julianday(ma.created_at)>=julianday(?)''',
        (operation['action_id'] + ':attempt', focus['child_id'], focus['mistake_id'],
         operation['stage'], operation['item_id'], operation['answer'], binding['started_at'])).fetchone()
    if not attempt:
        return None
    now = repository.utc_now()
    projected = project_child_mastery_evidence(connection, focus['child_id'], as_of=now)
    evidence = next((row for row in projected.get(attempt['knowledge_node_id'], {}).get('effective_evidence', [])
        if row['evidence_id'] == attempt['evidence_id'] and row.get('mistake_id') == focus['mistake_id']), None)
    if not evidence or not evidence['independent'] or evidence['hint_level'] or evidence['correctness'] != 1:
        return None
    activity = book.next_activity(focus['mistake_id'], focus['child_id'])
    if activity['kind'] not in {'scheduled', 'mastered'}:
        return None
    repo = repository.StudyRepository(_TransactionDatabase(connection))
    item = repo._get_plan_item_by_id(binding['plan_item_id'], connection=connection)
    seconds = int(item.get('active_seconds') or 0) + repo._segment_seconds(item['active_segment_started_at'], now)
    repo.complete_plan_item(binding['plan_item_id'], max(1, math.ceil(seconds / 60)), key,
        'review_evidence', '本次错题复习已有有效独立作答，后续复测按原门槛保留；不以任务完成替代长期掌握。',
        connection=connection)
    receipt = {'plan_item_id': binding['plan_item_id'], 'binding': binding,
        'answer_action_id': operation['action_id'], 'attempt_id': attempt['attempt_id'],
        'evidence_id': attempt['evidence_id'], 'stage': attempt['stage'],
        'next_activity': {k: activity.get(k) for k in ('kind', 'stage', 'due_at')}}
    event = connection.execute('SELECT payload_json FROM plan_item_event WHERE client_action_id=?', (key,)).fetchone()
    payload = json.loads(event['payload_json'])
    payload['mistake_completion'] = receipt
    connection.execute('UPDATE plan_item_event SET payload_json=? WHERE client_action_id=?',
        (json.dumps(payload, ensure_ascii=False), key))
    return receipt
