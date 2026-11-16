"""Correct the most recent scored spoken answer without rewriting history."""
import json
import re

from .agent_runtime import InvalidAgentPlanError
from .local_supply import digest
from .mistake_pending_answer import explicit_replacement


def explicit_withdrawal(text):
    value = re.sub(r'[，。！？、\s]', '', text or '')
    if re.search(r'(?:不要|别|先不|暂不|不能|不想|无需|不用)(?:再)?(?:撤回|撤销|取消|作废)', value):
        return False
    return bool(re.fullmatch(
        r'(?:请|帮我|麻烦)?(?:把)?(?:我)?刚才(?:那份|那次|的)?(?:已评分的)?'
        r'(?:回答|答案|作答)(?:给我|帮我)?(?:撤回|撤销|取消|作废|删掉|撤掉)'
        r'|(?:请|帮我|麻烦)?(?:撤回|撤销|取消|作废|删掉|撤掉)(?:我)?刚才(?:那份|那次|的)?(?:已评分的)?(?:回答|答案|作答)'
        r'|(?:我)?刚才(?:那份|那次|的)?(?:回答|答案|作答)(?:不算|不算数|作废)',
        value))


def withdrawal_grounded(text):
    value = re.sub(r'[，。！？、\s]', '', text or '')
    if not value or re.search(r'(?:不要|别|先不|暂不|不能|不想|无需|不用).{0,10}(?:撤|取消|作废|不算)', value):
        return False
    if re.search(r'(?:如果|假如|老师说|同学说|是不是|为什么|怎么会|吗)$', value):
        return False
    recent = re.search(r'(?:刚才|方才|上一句|那份|那次).{0,12}(?:回答|答案|作答)', value)
    control = re.search(r'(?:撤回|撤销|取消|作废|撤掉|删掉|不算数|不作数)', value)
    return bool(recent and control)


def scored_binding(c, focus, operation):
    if operation['kind'] == 'mastery_answer':
        row = c.execute('''SELECT ma.*,le.session_id,le.knowledge_node_id
            FROM mistake_attempt ma JOIN learning_evidence le USING(evidence_id)
            WHERE ma.client_action_id=? AND ma.child_id=? AND ma.mistake_id=?''',
            (operation['action_id'] + ':attempt', focus['child_id'], focus['mistake_id'])).fetchone()
        answer_event_id = None
    else:
        answer = c.execute('''SELECT event_id FROM mistake_diagnosis_event
            WHERE client_action_id=? AND child_id=? AND mistake_id=? AND kind='answer' ''',
            (operation['action_id'] + ':answer', focus['child_id'], focus['mistake_id'])).fetchone()
        answer_event_id = operation.get('answer_event_id') or (answer[0] if answer else None)
        row = c.execute('''SELECT da.*,le.knowledge_node_id FROM diagnostic_attempt da
            JOIN learning_evidence le USING(evidence_id)
            WHERE da.client_action_id=? AND da.child_id=? AND da.session_id=?''',
            ('mistake-diagnosis-score:' + str(answer_event_id), focus['child_id'],
             focus['session_id'])).fetchone() if answer_event_id else None
    if not row:
        return None
    item = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                     (row['diagnostic_item_id'],)).fetchone()
    evidence = c.execute('SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=?',
                         (row['evidence_id'], focus['child_id'])).fetchone()
    if (not item or not evidence or evidence['session_id'] != focus['session_id']
            or evidence['knowledge_node_id'] != row['knowledge_node_id']):
        return None
    value = {
        'version': 1,
        'kind': operation['kind'],
        'mistake_id': focus['mistake_id'],
        'child_id': focus['child_id'],
        'session_id': focus['session_id'],
        'action_id': operation['action_id'],
        'attempt_id': row['attempt_id'],
        'evidence_id': row['evidence_id'],
        'diagnostic_item_id': row['diagnostic_item_id'],
        'answer': row['submitted_answer'],
        'hint_level': int(evidence['hint_level']),
        'item_version': digest(dict(item)),
        'answer_event_id': answer_event_id,
    }
    if operation['kind'] == 'mastery_answer':
        value['stage'] = row['stage']
    else:
        value['probe_event_id'] = operation['probe_event_id']
    value['binding_hash'] = digest(value)
    return value


def validate_binding(c, focus, binding):
    if not isinstance(binding, dict) or binding.get('version') != 1:
        raise InvalidAgentPlanError('SCORED_ANSWER_BINDING_MISSING')
    if any(binding.get(key) != focus.get(key) for key in
           ('mistake_id', 'child_id', 'session_id')):
        raise InvalidAgentPlanError('SCORED_ANSWER_OWNER_CHANGED')
    if binding.get('binding_hash') != digest({k: v for k, v in binding.items()
                                              if k != 'binding_hash'}):
        raise InvalidAgentPlanError('SCORED_ANSWER_BINDING_CHANGED')
    item = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                     (binding['diagnostic_item_id'],)).fetchone()
    if not item or digest(dict(item)) != binding['item_version']:
        raise InvalidAgentPlanError('SCORED_ANSWER_SOURCE_CHANGED')
    table = 'mistake_attempt' if binding['kind'] == 'mastery_answer' else 'diagnostic_attempt'
    row = c.execute(f'''SELECT * FROM {table} WHERE attempt_id=? AND evidence_id=?
        AND child_id=? AND diagnostic_item_id=? AND submitted_answer=?''',
        (binding['attempt_id'], binding['evidence_id'], binding['child_id'],
         binding['diagnostic_item_id'], binding['answer'])).fetchone()
    if not row:
        raise InvalidAgentPlanError('SCORED_ANSWER_RECEIPT_CHANGED')
    withdrawn = c.execute('SELECT 1 FROM learning_evidence_withdrawal WHERE evidence_id=?',
                          (binding['evidence_id'],)).fetchone()
    if withdrawn:
        raise InvalidAgentPlanError('SCORED_ANSWER_ALREADY_WITHDRAWN')
    return row


def record_model_authorization(c, call_id, binding, text, replacement_answer=None,
                               control='replace'):
    from .mistake_pending_answer import replacement_grounded
    if control not in {'replace', 'withdraw'}:
        raise ValueError('scored answer control is invalid')
    if (control == 'replace' and not replacement_grounded(text, replacement_answer)):
        raise ValueError('scored answer replacement is not grounded')
    if control == 'withdraw' and (replacement_answer is not None or not withdrawal_grounded(text)):
        raise ValueError('scored answer withdrawal is not grounded')
    receipt = {'version': 1, 'binding_hash': digest(binding),
        'control': control, 'request_hash': digest([text, control, replacement_answer])}
    changed = c.execute('''UPDATE model_call SET context_manifest_json=json_set(
        context_manifest_json,'$.mistake_scored_control',json(?))
        WHERE call_id=? AND purpose='agent_plan' AND status='completed'
          AND verification_status='verified' AND safety_status='passed'
          AND json_extract(context_manifest_json,'$.mistake_scored_control') IS NULL''',
        (json.dumps(receipt), call_id)).rowcount
    if changed != 1:
        raise ValueError('scored answer model receipt unavailable')


def model_authorized_control(c, focus, text, turn_id, control):
    if not turn_id:
        return None
    row = c.execute('''SELECT t.transcript,t.action_json,p.context_manifest_json,s.arguments_json
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?
          AND p.status='running' AND s.status='running'
          AND s.capability_name='mistake.dialogue' ''',
        (turn_id, focus['child_id'], focus['session_id'])).fetchall()
    if len(row) != 1 or row[0]['transcript'] != text:
        return None
    try:
        action = json.loads(row[0]['action_json'])
        manifest = json.loads(row[0]['context_manifest_json'])
        arguments = json.loads(row[0]['arguments_json'])
    except (TypeError, ValueError):
        return None
    answer = arguments.get('replacement_answer')
    from .mistake_pending_answer import replacement_grounded
    binding = focus['state'].get('last_scored_answer')
    expected_keys = {'utterance', 'scored_answer_control'}
    if control == 'replace':
        expected_keys.add('replacement_answer')
    if (control not in {'replace', 'withdraw'}
            or set(arguments) != expected_keys
            or arguments.get('utterance') != text
            or arguments.get('scored_answer_control') != control
            or control == 'replace' and not replacement_grounded(text, answer)
            or control == 'withdraw' and (answer is not None or not withdrawal_grounded(text))
            or action.get('scored_answer_operation') != binding
            or manifest.get('scored_answer_operation') != binding):
        return None
    model = c.execute('''SELECT context_manifest_json FROM model_call
        WHERE call_id=? AND child_id=? AND session_id=? AND purpose='agent_plan'
          AND status='completed' AND verification_status='verified' AND safety_status='passed' ''',
        (manifest.get('scored_answer_control_model_call_id'), focus['child_id'],
         focus['session_id'])).fetchone()
    try:
        receipt = json.loads(model[0]).get('mistake_scored_control') if model else None
    except (TypeError, ValueError, AttributeError):
        return None
    expected = {'version': 1, 'binding_hash': digest(binding), 'control': control,
        'request_hash': digest([text, control, answer])}
    return answer if receipt == expected and control == 'replace' else receipt == expected


def model_authorized_replacement(c, focus, text, turn_id):
    return model_authorized_control(c, focus, text, turn_id, 'replace')


def model_authorized_withdrawal(c, focus, text, turn_id):
    return model_authorized_control(c, focus, text, turn_id, 'withdraw') is True


def replace_scored(service, focus, text, action_id, authorized_turn_id, replacement_answer):
    from .diagnostic_prompt import _TransactionDatabase
    from .mistake_dialogue import MistakeDialogueService

    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        nested = MistakeDialogueService(_TransactionDatabase(c))
        replay = nested._replay(focus['session_id'], action_id, text)
        if replay:
            return replay
        live = nested.state(focus['session_id'])
        binding = (live or {}).get('state', {}).get('last_scored_answer')
        if not live or binding != focus['state'].get('last_scored_answer'):
            raise InvalidAgentPlanError('SCORED_ANSWER_TARGET_CHANGED')
        if (explicit_replacement(text) != replacement_answer
                and model_authorized_replacement(c, live, text, authorized_turn_id) != replacement_answer):
            raise InvalidAgentPlanError('SCORED_ANSWER_REPLACEMENT_UNGROUNDED')
        validate_binding(c, live, binding)
        last = c.execute('''SELECT client_action_id FROM mistake_dialogue_event
            WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1''',
            (live['child_id'], live['session_id'])).fetchone()
        if not last or last[0] != binding['action_id']:
            raise InvalidAgentPlanError('SCORED_ANSWER_NOT_IMMEDIATE')
        operation = {
            'kind': binding['kind'], 'action_id': action_id, 'utterance': text,
            'answer': replacement_answer, 'hint_level': binding['hint_level'],
            'correction_of': binding,
        }
        if authorized_turn_id is not None:
            operation['authorized_turn_id'] = authorized_turn_id
        if binding['kind'] == 'mastery_answer':
            operation.update(stage=binding['stage'], item_id=binding['diagnostic_item_id'])
        else:
            operation.update(probe_event_id=binding['probe_event_id'],
                             supersedes_event_id=binding['answer_event_id'])
        live['state']['pending_operation'] = operation
        live['state'].pop('pending_error', None)
        nested._write_focus(c, live)
        c.commit()
    return service._resume_operation(live, text, action_id)


def withdraw_scored(service, focus, text, action_id, authorized_turn_id):
    from .diagnostic_prompt import _TransactionDatabase
    from .mistake_dialogue import MistakeDialogueService

    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        nested = MistakeDialogueService(_TransactionDatabase(c))
        replay = nested._replay(focus['session_id'], action_id, text)
        if replay:
            return replay
        live = nested.state(focus['session_id'])
        binding = (live or {}).get('state', {}).get('last_scored_answer')
        if not live or binding != focus['state'].get('last_scored_answer'):
            raise InvalidAgentPlanError('SCORED_ANSWER_TARGET_CHANGED')
        if (not explicit_withdrawal(text)
                and not model_authorized_withdrawal(c, live, text, authorized_turn_id)):
            raise InvalidAgentPlanError('SCORED_ANSWER_WITHDRAWAL_UNGROUNDED')
        validate_binding(c, live, binding)
        last = c.execute('''SELECT client_action_id FROM mistake_dialogue_event
            WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1''',
            (live['child_id'], live['session_id'])).fetchone()
        if not last or last[0] != binding['action_id']:
            raise InvalidAgentPlanError('SCORED_ANSWER_NOT_IMMEDIATE')
        operation = {'kind': 'scored_withdrawal', 'action_id': action_id,
            'utterance': text, 'withdrawal_of': binding}
        if authorized_turn_id is not None:
            operation['authorized_turn_id'] = authorized_turn_id
        live['state']['pending_operation'] = operation
        live['state'].pop('pending_error', None)
        nested._write_focus(c, live)
        c.commit()
    return service._resume_operation(live, text, action_id)


def _reopen_plan_items(c, focus, binding, action_id, replacement_evidence_id):
    reopened = [row[0] for row in c.execute('''SELECT plan_item_id FROM plan_item_event
        WHERE event_type='source_withdrawn' AND client_action_id LIKE ?
          AND json_extract(payload_json,'$.correction_action_id')=?''',
        (action_id + ':reopen-plan:%', action_id))]
    for row in c.execute('''SELECT pi.plan_item_id,pe.payload_json FROM plan_item_event pe
        JOIN plan_item pi USING(plan_item_id) JOIN daily_plan dp USING(plan_id)
        WHERE pe.event_type='completed' AND pi.status='completed'
          AND dp.child_id=? AND dp.session_id=? AND pi.source_type='mistake_review'
          AND pi.source_ref=?
          AND json_extract(pe.payload_json,'$.mistake_completion.evidence_id')=?''',
        (focus['child_id'], focus['session_id'], focus['mistake_id'],
         binding['evidence_id'])).fetchall():
        event_key = action_id + ':reopen-plan:' + row['plan_item_id']
        prior = c.execute('SELECT payload_json FROM plan_item_event WHERE client_action_id=?',
                          (event_key,)).fetchone()
        payload = {'reason': 'answer_score_withdrawn',
            'old_evidence_id': binding['evidence_id'],
            'replacement_evidence_id': replacement_evidence_id,
            'correction_action_id': action_id}
        if prior:
            if json.loads(prior[0]) != payload:
                raise InvalidAgentPlanError('SCORED_ANSWER_PLAN_REOPEN_CONFLICT')
        else:
            c.execute('''UPDATE plan_item SET status='rescheduled',completed_at=NULL,
                completion_evidence_type=NULL,completion_note=NULL
                WHERE plan_item_id=? AND status='completed' ''', (row['plan_item_id'],))
            c.execute('''INSERT INTO plan_item_event(event_id,plan_item_id,event_type,
                occurred_at,payload_json,client_action_id)
                VALUES (lower(hex(randomblob(16))),?,'source_withdrawn',datetime('now'),?,?)''',
                (row['plan_item_id'], json.dumps(payload, ensure_ascii=False), event_key))
        if row['plan_item_id'] not in reopened:
            reopened.append(row['plan_item_id'])
    return sorted(reopened)


def finalize_withdrawal(c, focus, operation, book):
    from .learning_evidence_withdrawal import record_withdrawal
    from .learning_state import LearningStateService
    from .repository import utc_now

    binding = operation.get('withdrawal_of')
    historical = operation.get('historical_scored_control')
    origin_focus = focus
    existing = c.execute('''SELECT * FROM learning_evidence_withdrawal
        WHERE evidence_id=? OR client_action_id=?''',
        (binding['evidence_id'], operation['action_id'] + ':withdraw-score')).fetchall()
    if historical:
        from .mistake_cross_session_control import validate_operation
        validate_operation(c, focus, operation, allow_withdrawn=bool(existing))
        origin_focus = {**focus, 'session_id': binding['session_id']}
    if existing:
        if (len(existing) != 1 or existing[0]['child_id'] != focus['child_id']
                or existing[0]['evidence_id'] != binding['evidence_id']
                or existing[0]['replacement_evidence_id'] is not None
                or existing[0]['reason'] != 'answer_withdrawn'):
            raise InvalidAgentPlanError('SCORED_ANSWER_WITHDRAWAL_CONFLICT')
    else:
        validate_binding(c, origin_focus, binding)
    receipt = record_withdrawal(c, child_id=focus['child_id'],
        session_id=origin_focus['session_id'], evidence_id=binding['evidence_id'],
        client_action_id=operation['action_id'] + ':withdraw-score',
        request={'utterance': operation['utterance'], 'binding': binding,
                 **({'control_session_id': focus['session_id']} if historical else {})})
    now = utc_now()
    evidence = c.execute('SELECT knowledge_node_id FROM learning_evidence WHERE evidence_id=?',
                         (binding['evidence_id'],)).fetchone()
    LearningStateService(None).replay_mastery(focus['child_id'], evidence[0], as_of=now,
        connection=c, reason='answer_withdrawn')
    if binding['kind'] == 'mastery_answer':
        book.reconcile_after_answer_correction(focus['mistake_id'], focus['child_id'],
            operation['action_id'], now, connection=c)
    else:
        diagnosis = book.diagnosis_state(focus['mistake_id'], focus['child_id'])
        answer = next((event for event in diagnosis['history']
            if event['event_id'] == binding['answer_event_id']), None)
        if answer and answer['active']:
            book.retract_diagnosis_event(focus['mistake_id'], focus['child_id'],
                binding['answer_event_id'], operation['utterance'], diagnosis['version'],
                operation['action_id'] + ':retract-diagnosis-answer')
    reopened = _reopen_plan_items(c, origin_focus, binding, operation['action_id'], None)
    summary = {'version': 1, 'mistake_id': focus['mistake_id'],
        'old_attempt_id': binding['attempt_id'], 'old_evidence_id': binding['evidence_id'],
        'withdrawal_id': receipt['withdrawal_id'], 'score_authority_replayed': True,
        'replacement_evidence_id': None, 'reopened_plan_item_ids': reopened,
        'withdrawal_action_id': operation['action_id']}
    if historical:
        summary.update(origin_session_id=origin_focus['session_id'],
                       control_session_id=focus['session_id'])
    prior = c.execute('''SELECT summary_json FROM audit_event
        WHERE child_id=? AND action='learning_answer_withdrawn'
          AND json_extract(summary_json,'$.withdrawal_action_id')=?''',
        (focus['child_id'], operation['action_id'])).fetchall()
    if prior:
        if len(prior) != 1 or json.loads(prior[0][0]) != summary:
            raise InvalidAgentPlanError('SCORED_ANSWER_WITHDRAWAL_AUDIT_CONFLICT')
    else:
        c.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
            VALUES (lower(hex(randomblob(16))),'child','learning_answer_withdrawn',?,?,?)''',
            (focus['child_id'], json.dumps(summary, ensure_ascii=False), now))
    return summary


def finalize(c, focus, operation, replacement_evidence_id, book):
    from .learning_evidence_withdrawal import record_correction
    from .learning_state import LearningStateService
    from .repository import utc_now

    binding = operation.get('correction_of')
    historical = operation.get('historical_scored_control')
    origin_focus = focus
    existing = c.execute('''SELECT * FROM learning_evidence_withdrawal
        WHERE evidence_id=? OR client_action_id=?''',
        (binding['evidence_id'], operation['action_id'] + ':withdraw-old-score')).fetchall()
    if historical:
        from .mistake_cross_session_control import validate_operation
        validate_operation(c, focus, operation, allow_withdrawn=bool(existing))
        origin_focus = {**focus, 'session_id': binding['session_id']}
    if existing:
        if (len(existing) != 1 or existing[0]['child_id'] != focus['child_id']
                or existing[0]['evidence_id'] != binding['evidence_id']
                or existing[0]['replacement_evidence_id'] != replacement_evidence_id
                or existing[0]['reason'] != 'answer_corrected'):
            raise InvalidAgentPlanError('SCORED_ANSWER_CORRECTION_CONFLICT')
    else:
        validate_binding(c, origin_focus, binding)
    receipt = record_correction(c, child_id=focus['child_id'],
        session_id=origin_focus['session_id'], evidence_id=binding['evidence_id'],
        replacement_evidence_id=replacement_evidence_id,
        replacement_session_id=focus['session_id'],
        client_action_id=operation['action_id'] + ':withdraw-old-score',
        request={'utterance': operation['utterance'], 'replacement_answer': operation['answer'],
                 'binding': binding,
                 **({'control_session_id': focus['session_id']} if historical else {})})
    node = c.execute('SELECT knowledge_node_id FROM learning_evidence WHERE evidence_id=?',
                     (replacement_evidence_id,)).fetchone()[0]
    now = utc_now()
    LearningStateService(None).replay_mastery(focus['child_id'], node, as_of=now,
        connection=c, reason='answer_corrected')
    if binding['kind'] == 'mastery_answer':
        book.reconcile_after_answer_correction(focus['mistake_id'], focus['child_id'],
            operation['action_id'], now, connection=c)
    reopened = [row[0] for row in c.execute('''SELECT plan_item_id FROM plan_item_event
        WHERE event_type='source_withdrawn' AND client_action_id LIKE ?
          AND json_extract(payload_json,'$.correction_action_id')=?''',
        (operation['action_id'] + ':reopen-plan:%', operation['action_id']))]
    for row in c.execute('''SELECT pi.plan_item_id,pe.payload_json FROM plan_item_event pe
        JOIN plan_item pi USING(plan_item_id) JOIN daily_plan dp USING(plan_id)
        WHERE pe.event_type='completed' AND pi.status='completed'
          AND dp.child_id=? AND dp.session_id=? AND pi.source_type='mistake_review'
          AND pi.source_ref=?
          AND json_extract(pe.payload_json,'$.mistake_completion.evidence_id')=?''',
        (focus['child_id'], origin_focus['session_id'], focus['mistake_id'],
         binding['evidence_id'])).fetchall():
        event_key = operation['action_id'] + ':reopen-plan:' + row['plan_item_id']
        prior = c.execute('SELECT payload_json FROM plan_item_event WHERE client_action_id=?',
                          (event_key,)).fetchone()
        payload = {'reason': 'answer_score_withdrawn',
            'old_evidence_id': binding['evidence_id'],
            'replacement_evidence_id': replacement_evidence_id,
            'correction_action_id': operation['action_id']}
        if prior:
            if json.loads(prior[0]) != payload:
                raise InvalidAgentPlanError('SCORED_ANSWER_PLAN_REOPEN_CONFLICT')
        else:
            c.execute('''UPDATE plan_item SET status='rescheduled',completed_at=NULL,
                completion_evidence_type=NULL,completion_note=NULL
                WHERE plan_item_id=? AND status='completed' ''', (row['plan_item_id'],))
            c.execute('''INSERT INTO plan_item_event(event_id,plan_item_id,event_type,
                occurred_at,payload_json,client_action_id)
                VALUES (lower(hex(randomblob(16))),?,'source_withdrawn',?,?,?)''',
                (row['plan_item_id'], now, json.dumps(payload, ensure_ascii=False), event_key))
        if row['plan_item_id'] not in reopened:
            reopened.append(row['plan_item_id'])
    summary = {'version': 1, 'mistake_id': focus['mistake_id'],
        'old_attempt_id': binding['attempt_id'], 'old_evidence_id': binding['evidence_id'],
        'replacement_evidence_id': replacement_evidence_id,
        'withdrawal_id': receipt['withdrawal_id'], 'score_authority_replayed': True,
        'independence_claim_allowed': False,
        'reopened_plan_item_ids': sorted(reopened)}
    summary['correction_action_id'] = operation['action_id']
    if historical:
        summary.update(origin_session_id=origin_focus['session_id'],
                       control_session_id=focus['session_id'])
    prior_audit = c.execute('''SELECT summary_json FROM audit_event
        WHERE child_id=? AND action='learning_answer_corrected'
          AND json_extract(summary_json,'$.correction_action_id')=?''',
        (focus['child_id'], operation['action_id'])).fetchall()
    if prior_audit:
        if len(prior_audit) != 1 or json.loads(prior_audit[0][0]) != summary:
            raise InvalidAgentPlanError('SCORED_ANSWER_CORRECTION_AUDIT_CONFLICT')
    else:
        c.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
            VALUES (lower(hex(randomblob(16))),'child','learning_answer_corrected',?,?,?)''',
            (focus['child_id'], json.dumps(summary, ensure_ascii=False), now))
    return summary
