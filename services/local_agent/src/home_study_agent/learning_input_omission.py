"""Explicit late-homework addition; retain the current plan and all prior evidence."""
from uuid import uuid4

from .learning_input_revision import InputRevisionError, LOCAL, _json, _version


def context(connection, child_id, session_id):
    session = connection.execute('SELECT child_id,status,state FROM daily_session WHERE session_id=? AND child_id=?',
                                 (session_id, child_id)).fetchone()
    plans = connection.execute("SELECT * FROM daily_plan WHERE child_id=? AND session_id=? AND status='active'",
                               (child_id, session_id)).fetchall()
    if not session or session['status'] != 'active' or session['state'] not in ('study', 'break', 'plan_review') or len(plans) != 1:
        raise InputRevisionError('INPUT_ADDITION_ACTIVE_PLAN_REQUIRED')
    plan = dict(plans[0])
    from .learning_input_replan import RULE_FIELDS
    rule = connection.execute('SELECT ' + RULE_FIELDS + ' FROM family_rule WHERE child_id=?', (child_id,)).fetchone()
    budget = connection.execute('SELECT * FROM session_planning_context WHERE session_id=?', (session_id,)).fetchone()
    if not rule or not budget:
        raise InputRevisionError('INPUT_ADDITION_BUDGET_REQUIRED')
    return {'version': 'late_homework_addition_v1', 'session': dict(session), 'plan': plan,
            'items': [dict(row) for row in connection.execute('SELECT * FROM plan_item WHERE plan_id=? ORDER BY plan_item_id', (plan['plan_id'],))],
            'homework': [dict(row) for row in connection.execute('SELECT * FROM homework_item WHERE child_id=? AND session_id=? ORDER BY homework_id', (child_id, session_id))],
            'rule': dict(rule), 'planning_context': dict(budget)}


def current(connection, child_id, session_id, proposal):
    try:
        return (context(connection, child_id, session_id) == proposal['addition_context']
                and not connection.execute('SELECT 1 FROM homework_item WHERE homework_id=?', (proposal['target']['target_id'],)).fetchone())
    except (InputRevisionError, KeyError, TypeError):
        return False


def propose(connection, arrival, arguments, turn_id):
    from .learning_input_voice import arguments as grounded_arguments
    revision = {key: value for key, value in arguments.items() if key != 'request_quote'}
    if grounded_arguments(revision, arrival['utterance']) != arguments:
        raise InputRevisionError('INPUT_ADDITION_QUOTE_CHANGED')
    frozen = context(connection, arrival['child_id'], arrival['session_id'])
    changes = arguments['changes']
    if any(row['status'] != 'cancelled' and row['subject'] == changes['subject']
           and row['title'] == changes['title'] and row['range_text'] == changes.get('range_text') for row in frozen['homework']):
        return {'status': 'blocked', 'reason': 'matching_homework_exists',
                'response_text': '这项作业已经在记录里，没有重复新增。是要修改原项，还是另一份不同范围的作业？', **LOCAL}
    # Reuse the existing intake parser only for the task kind, never its default
    # duration/deadline. All persisted content and minutes came from exact quotes.
    from .agent_orchestrator import AgentOrchestrator
    task_type = AgentOrchestrator._homework_action(changes['title'])['task_type']
    target = {'kind': 'homework', 'target_id': str(uuid4())}
    proposal = {'kind': 'homework', 'operation': 'add', 'changes': changes,
                'request_quote': arrival['utterance'], 'target': target, 'candidates': [],
                'addition_context': frozen, 'task_type': task_type}
    from .learning_input_intake import begin, offer
    begin(proposal, arrival, arguments)
    connection.execute("UPDATE learning_input_proposal SET state='cancelled' WHERE session_id=? AND state IN ('clarification_required','awaiting_confirmation')", (arrival['session_id'],))
    return offer(connection, arrival, proposal, turn_id)


def confirm(service, connection, arrival, pending, proposal, turn_id):
    if pending['state'] != 'awaiting_confirmation' or not current(connection, arrival['child_id'], arrival['session_id'], proposal):
        connection.execute("UPDATE learning_input_proposal SET state='conflict' WHERE request_id=?", (pending['request_id'],))
        return {'status': 'conflict', 'reason': 'addition_plan_or_source_changed',
                'response_text': '原计划、时间或作业记录已经变化，刚才的确认不能继续补录。请重新说一下遗漏的作业。'}
    from .learning_input_replan import utc_now
    now = utc_now().isoformat()
    hid, child, sid = proposal['target']['target_id'], arrival['child_id'], arrival['session_id']
    fields = proposal['changes']
    connection.execute('''INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,
        range_text,expected_minutes,special_requirements,confirmation_status,status,created_at,client_action_id)
        VALUES (?,?,?,?,?,?,?,?,?,'child_confirmed','pending',?,?)''',
        (hid, child, sid, fields['subject'], proposal['task_type'], fields['title'], fields.get('range_text'),
         fields['expected_minutes'], fields.get('special_requirements'), now, 'input-addition:' + turn_id))
    added = service._item(connection, child, sid, 'homework', hid)
    eid = str(uuid4())
    frame = proposal['addition_context']
    impact = {'cancelled_unstarted_plan_ids': [], 'preserved_execution_plan_ids': [row['plan_item_id'] for row in frame['items']],
              'learning_evidence_changed': False, 'requires_replan': True}
    if proposal.get('intake'):
        impact['input_provenance'] = proposal['intake']
    impact['replan_obligation'] = {
        'version': 'learning_input_replan_v1', 'obligation_id': 'input-revision:' + eid,
        'revision_event_id': eid, 'status': 'pending', 'child_id': child, 'session_id': sid,
        'target_kind': 'homework', 'target_id': hid, 'operation': 'add',
        'before_source_version': _version(frame), 'expected_source_version': _version([added['record'], added['plans'], eid]),
        'revised_source': added['record'], 'original_plan_items': [], 'cancelled_unstarted_plan_ids': [],
        'preserve_plan_item_ids': impact['preserved_execution_plan_ids'], 'budget_snapshot': frame['rule'],
        'planning_context_snapshot': frame['planning_context'], 'artifact_fingerprint': None,
        'addition_plan': frame['plan'], 'addition_items': frame['items'], 'learning_evidence_changed': False, **LOCAL}
    connection.execute('INSERT INTO learning_input_revision_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (eid, child, sid, 'homework', hid, 'add', '{}', _json(added['record']), pending['request_id'],
         arrival['arrival_id'], None, _json(impact), now, turn_id))
    connection.execute("UPDATE learning_input_proposal SET state='applied' WHERE request_id=?", (pending['request_id'],))
    return {'status': 'applied', 'event_id': eid, 'target_id': hid, 'impact': impact,
            'response_text': '补录已保存，原任务和复习义务都保留；接下来核对剩余预算，未排入的部分不会算作完成。'}
