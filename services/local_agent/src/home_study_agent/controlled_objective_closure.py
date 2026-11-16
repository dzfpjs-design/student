"""Close only an entertainment execution goal owned by the exact original start.

Borrow the outcome transaction. Device/activity receipts are execution facts,
never learning, mastery or automatic permission to resume another goal.
"""
import json
from datetime import datetime
from uuid import uuid4


KINDS = {
    'stop_controlled_content': ('content', 'video', 'content_session_id', 'controlled_content'),
    'stop_controlled_game': ('games', 'game', 'game_session_id', 'controlled_game'),
}


def _object(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except ValueError:
        return None


def _target(c, turn, action, fields):
    kind, native_kind, identity, table = fields
    cancel = action.get('controlled_launch_cancel_binding')
    if isinstance(cancel, dict):
        if (cancel.get('child_id'), cancel.get('session_id'), cancel.get('kind')) != (
                turn['child_id'], turn['session_id'], native_kind):
            return None, None
        request = cancel.get('request_id')
        return (request, None) if isinstance(request, str) and request == cancel.get('source_turn_id') else (None, None)
    bound = action.get('controlled_stop_binding')
    if not isinstance(bound, dict) or (bound.get('child_id'), bound.get('session_id'), bound.get('kind')) != (
            turn['child_id'], turn['session_id'], kind):
        return None, None
    if not isinstance(bound.get('activity_id'), str) or action.get(identity) != bound['activity_id']:
        return None, None
    row = c.execute(f'SELECT * FROM {table}_session WHERE {identity}=? AND child_id=? AND session_id=?',
                    (bound['activity_id'], turn['child_id'], turn['session_id'])).fetchone()
    return (row['client_action_id'], row) if row else (None, None)


def inspect(c, turn_id, result):
    """Read-only explanation. Missing legacy ownership is retained, not guessed."""
    retained = lambda reason: {'status': 'retained', 'reason': reason}
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    action = _object(turn['action_json']) if turn else {}
    fields = KINDS.get(action.get('type')) if isinstance(action.get('type'), str) else None
    if not fields:
        return {'status': 'not_applicable'}
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (fields[3]+'_session',)).fetchone():
        return retained('original_activity_storage_unavailable')
    source_id, activity = _target(c, turn, action, fields)
    if not source_id:
        return retained('original_start_binding_unavailable')
    kind, native_kind, identity, table = fields
    receipt = result.get('execution_receipt') if isinstance(result, dict) else None
    other = result.get('client_bridge_receipt') if isinstance(result, dict) else None
    output = receipt.get('output') if isinstance(receipt, dict) else None
    if (not isinstance(output, dict) or other is not None and other != receipt
            or receipt.get('capability') != table+'.stop' or receipt.get('action_type') != action['type']
            or receipt.get('execution_location') != 'client_device_bridge' or receipt.get('status') != 'completed'
            or output.get('device_closed') is not True or output.get('task_resumed') is not False):
        return retained('reliable_end_receipt_required')
    finished = _time(receipt.get('completed_at'))
    arrival = _time(turn['created_at'])
    if not finished or not arrival or arrival > finished:
        return retained('end_receipt_time_changed')
    if activity is None:
        if (output.get('request_id') != source_id or output.get('status') != 'cancelled_before_start'
                or output.get('no_activity') is not True or not isinstance(output.get('native_intent_id'), str)):
            return retained('exact_cancel_receipt_required')
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='controlled_launch_receipt'").fetchone()
        if not exists or not c.execute('''SELECT 1 FROM controlled_launch_receipt WHERE intent_id=?
                AND child_id=? AND session_id=? AND request_id=? AND kind=? AND cancelled_before_start=1
                AND content_session_id IS NULL AND game_session_id IS NULL''',
                (output['native_intent_id'], turn['child_id'], turn['session_id'], source_id, native_kind)).fetchone():
            return retained('cancel_not_persisted')
        if c.execute(f'SELECT 1 FROM {table}_session WHERE client_action_id=?', (source_id,)).fetchone():
            return retained('cancel_has_activity')
        from .controlled_launch_cancel import failed_starts
        if action['controlled_launch_cancel_binding'] not in failed_starts(
                c, turn['child_id'], turn['session_id'], kind, include_cancelled=True):
            return retained('original_failed_start_changed')
    else:
        ended = _time(activity['ended_at'])
        if (output.get(identity) != activity[identity] or output.get('child_id') != turn['child_id']
                or output.get('session_id') != turn['session_id'] or output.get('status') != activity['status']
                or activity['status'] not in {'stopped', 'completed', 'expired', 'interrupted', 'failed'}
                or not ended or ended > finished):
            return retained('exact_persisted_end_required')
    source = c.execute('''SELECT p.*,t.action_json,t.tool_status,t.created_at AS source_created,
        s.capability_name,s.arguments_json,s.status AS source_step_status FROM agent_plan p JOIN agent_turn t USING(agent_turn_id)
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE p.agent_turn_id=? AND p.child_id=? AND p.session_id=?
          AND t.child_id=p.child_id AND t.session_id=p.session_id''',
        (source_id, turn['child_id'], turn['session_id'])).fetchone()
    if not source:
        return retained('original_start_plan_unavailable')
    plan, original_action, arguments = (_object(source[k]) for k in ('plan_json', 'action_json', 'arguments_json'))
    draft = plan.get('objective')
    step = plan.get('next_step')
    if (source['capability_name'] != table+'.play' or original_action.get('type') != 'start_'+table
            or source['tool_status'] not in {'completed', 'failed'}
            or source['status'] != source['tool_status'] or source['source_step_status'] != source['tool_status']
            or not isinstance(original_action.get('query'), str) or not original_action['query'].strip()
            or original_action['query'] != arguments.get('query')
            or not isinstance(draft, dict) or draft.get('mode') != 'new' or draft.get('type') != 'execution'
            or plan.get('after_execution') != 'stop' or plan.get('continuation_scope') != []
            or plan.get('requires_child_reply') is not False
            or not isinstance(step, dict) or step.get('capability') != source['capability_name']
            or step.get('arguments') != arguments
            or not _time(source['source_created']) or _time(source['source_created']) > arrival):
        return retained('dedicated_execution_goal_required')
    goal = c.execute('''SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?''',
                     (source['objective_id'], turn['child_id'], turn['session_id'])).fetchone()
    if not goal or goal['objective_type'] != 'execution' or goal['last_agent_turn_id'] != source_id:
        return retained('original_objective_owner_changed')
    if (goal['summary'] != draft.get('summary') or goal['phase'] != draft.get('phase')
            or goal['success_criteria_json'] != json.dumps(draft.get('success_criteria'), ensure_ascii=False)):
        return retained('original_objective_revised')
    if goal['status'] not in {'active', 'paused'}:
        return retained('original_objective_already_terminal')
    # Any other use of this goal must itself target this exact original stop.
    # A shared learning step or a second launch cannot be cancelled with it.
    for row in c.execute('''SELECT t.*,s.capability_name FROM agent_plan p JOIN agent_turn t USING(agent_turn_id)
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            WHERE p.objective_id=? AND t.agent_turn_id NOT IN (?,?)''',
            (goal['objective_id'], source_id, turn_id)).fetchall():
        candidate = _object(row['action_json'])
        if (row['child_id'] != turn['child_id'] or row['session_id'] != turn['session_id']
                or row['capability_name'] != table+'.stop' or candidate.get('type') != action['type']
                or _target(c, row, candidate, fields)[0] != source_id):
            return retained('objective_shared_with_other_work')
    return {'status': 'ready', 'objective_id': goal['objective_id'], 'source_turn_id': source_id,
            'child_id': turn['child_id'], 'session_id': turn['session_id'], 'previous_status': goal['status']}


def apply(c, turn_id, result, now):
    if not c.in_transaction:
        raise ValueError('CONTROLLED_OBJECTIVE_TRANSACTION_REQUIRED')
    proof = inspect(c, turn_id, result)
    if proof['status'] != 'ready':
        return proof
    changed = c.execute('''UPDATE agent_objective SET status='cancelled',completed_at=?,updated_at=?,last_agent_turn_id=?
        WHERE objective_id=? AND child_id=? AND session_id=? AND objective_type='execution'
          AND status=? AND last_agent_turn_id=?''', (now, now, turn_id, proof['objective_id'],
            proof['child_id'], proof['session_id'], proof['previous_status'], proof['source_turn_id']))
    if changed.rowcount != 1:
        raise ValueError('CONTROLLED_OBJECTIVE_OWNER_CHANGED')
    c.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
        VALUES(?,?,?,?,?,?)''', (str(uuid4()), 'agent', 'controlled_execution_objective_cancelled', proof['child_id'],
            json.dumps({'objective_id': proof['objective_id'], 'source_turn_id': proof['source_turn_id'],
                        'completion_turn_id': turn_id, 'reason': 'verified_original_activity_end'}), now))
    return {**proof, 'status': 'cancelled'}
