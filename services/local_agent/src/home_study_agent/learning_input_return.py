"""Explicit dialogue re-entry; all authority stays in existing journals."""
import json

from .learning_input_revision import InputRevisionError, _version


def snapshot(connection, child_id, session_id, *, turn_id=None):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_proposal'").fetchone():
        return None
    session = connection.execute("SELECT status,state FROM daily_session WHERE session_id=? AND child_id=?",
                                 (session_id, child_id)).fetchone()
    pending = connection.execute("""SELECT * FROM learning_input_proposal WHERE child_id=? AND session_id=?
        AND state IN ('clarification_required','awaiting_confirmation') ORDER BY rowid DESC LIMIT 1""",
        (child_id, session_id)).fetchone()
    if not session or session['status'] != 'active' or not pending:
        return None
    try:
        proposal = json.loads(pending['proposal_json'])
    except (ValueError, TypeError):
        return None
    if (not isinstance(proposal, dict)
            or not isinstance(proposal.get('target'), dict) or not proposal['target'].get('target_id')
            or proposal.get('operation') not in {'add', 'update', 'delete', 'undo'} or not proposal.get('kind')
            or proposal['operation'] == 'add' and not proposal.get('intake')):
        return None
    offered = connection.execute("""SELECT t.agent_turn_id,t.response_text,t.tool_status,p.status AS plan_status,
        s.status AS step_status FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id""",
        (pending['offered_by_turn_id'], child_id, session_id)).fetchone()
    previous = connection.execute("""SELECT t.agent_turn_id,t.response_text,t.tool_status,p.status AS plan_status,
        s.status AS step_status FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.child_id=? AND t.session_id=? AND (? IS NULL OR t.agent_turn_id!=?)
        ORDER BY t.rowid DESC LIMIT 1""", (child_id, session_id, turn_id, turn_id)).fetchone()
    if (not offered or offered['tool_status'] != 'completed' or offered['response_text'] != pending['response_text']
            or offered['plan_status'] == 'cancelled' or offered['step_status'] == 'cancelled'
            or not previous or previous['tool_status'] != 'completed'
            or previous['plan_status'] == 'cancelled' or previous['step_status'] == 'cancelled'):
        return None
    from .learning_input_revision import LearningInputRevisionService
    if proposal['operation'] == 'add':
        from .learning_input_omission import context
        try:
            source = context(connection, child_id, session_id)
        except ValueError:
            source = None
    else:
        source = LearningInputRevisionService(None)._item(connection, child_id, session_id,
            proposal['kind'], proposal['target']['target_id'])
    return {'request_id': pending['request_id'], 'readback_version': pending['readback_version'],
            'state': pending['state'], 'offered_by_turn_id': pending['offered_by_turn_id'],
            'proposal_version': _version(proposal), 'source_version': _version(source),
            'dialogue_version': _version(dict(previous)), 'dialogue_turn_id': previous['agent_turn_id'],
            'session_state': session['state']}


def validate(connection, child_id, session_id, packet, *, turn_id=None):
    if not packet or snapshot(connection, child_id, session_id, turn_id=turn_id) != packet:
        raise InputRevisionError('INPUT_RETURN_FOCUS_CHANGED')


def reoffer(service, connection, arrival, turn_id, binding):
    packet = binding.get('input_return')
    validate(connection, arrival['child_id'], arrival['session_id'], packet, turn_id=turn_id)
    return _offer(service, connection, arrival['child_id'], arrival['session_id'], turn_id, packet)


def _offer(service, connection, child_id, session_id, turn_id, packet):
    row = connection.execute('SELECT * FROM learning_input_proposal WHERE request_id=? AND child_id=? AND session_id=?',
        (packet['request_id'], child_id, session_id)).fetchone()
    proposal = json.loads(row['proposal_json'])
    from .learning_input_intake import valid, offer, missing
    if proposal.get('intake'):
        current = valid(service, connection, child_id, session_id, proposal)
    else:
        target = service._item(connection, child_id, session_id, proposal['kind'], proposal['target']['target_id'])
        current = bool(target and target['version'] == proposal['target']['version'])
    if not current:
        connection.execute("UPDATE learning_input_proposal SET state='conflict' WHERE request_id=?", (row['request_id'],))
        return {'status': 'conflict', 'response_text': '原草稿还留着，但作业来源或计划已经变化。需要重新核对后再确认，我不会用旧确认覆盖。'}
    original = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=?',
        (row['request_id'], child_id, session_id)).fetchone()
    # Re-offer only; neither this utterance nor an earlier duration supplies a field.
    if proposal.get('intake') and (proposal['operation'] == 'add' or missing(proposal)):
        result = offer(connection, original, proposal, turn_id)
    else:
        result = service._offer(connection, original, proposal, turn_id)
        if proposal.get('intake'):
            result['input_provenance'] = proposal['intake']
    return {**result, 'input_return_from': packet}


def after_break(connection, database, session_id, key, turn_id):
    """Called inside the real resume receipt transaction, before its commit."""
    if not turn_id:
        return None
    row = connection.execute('''SELECT t.child_id,t.client_action_id,p.context_manifest_json,s.idempotency_key
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.agent_turn_id=? AND t.session_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id''',
        (turn_id, session_id)).fetchone()
    if not row or row['idempotency_key'] != key:
        raise InputRevisionError('INPUT_RETURN_RESUME_BINDING_CHANGED')
    frozen = json.loads(row['context_manifest_json']).get('voice_control_binding') or {}
    packet = frozen.get('targets', {}).get('input_return')
    if frozen.get('control') != 'break.resume' or not packet:
        return None
    from .learning_input_revision import LearningInputRevisionService, _json, LOCAL
    service = LearningInputRevisionService(database)
    if connection.execute('SELECT 1 FROM learning_input_command WHERE turn_id=?', (turn_id,)).fetchone():
        return service.replay_result(connection, turn_id)
    from .break_control import BreakControl
    receipt = BreakControl.recorded(connection, session_id, 'resume', key)
    current = snapshot(connection, row['child_id'], session_id, turn_id=turn_id)
    latest = connection.execute('SELECT client_action_id FROM learning_input_arrival WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                                (session_id, row['child_id'])).fetchone()
    # The resume receipt alone permits the expected phase transition. The full
    # frozen source must still pass intake.valid in the resulting study phase.
    matches = bool(receipt and latest and latest[0] == row['client_action_id'] and current
                   and packet['session_state'] == 'break' and current['session_state'] == 'study'
                   and all(current[k] == v for k, v in packet.items() if k not in {'session_state', 'source_version'}))
    if matches:
        result = _offer(service, connection, row['child_id'], session_id, turn_id, packet)
    else:
        result = {'status': 'superseded', 'response_text': '休息已结束。原草稿的焦点或确认已经变化，需要重新核对，我不会沿用旧回答。'}
    result = {**result, **LOCAL}
    connection.execute('INSERT INTO learning_input_command VALUES (?,?,?)', (turn_id, row['child_id'], _json(result)))
    return result
