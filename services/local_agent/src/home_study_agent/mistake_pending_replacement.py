"""Atomically retire an unprocessed answer and reserve its spoken replacement."""
import json

from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from .diagnostic_prompt import _TransactionDatabase
from .local_supply import digest
from .mistake_pending_answer import authorized, binding
from .repository import utc_now


def _already_recorded(c, operation):
    return bool(
        c.execute('SELECT 1 FROM mistake_attempt WHERE client_action_id=?',
                  (operation['action_id'] + ':attempt',)).fetchone()
        or c.execute('SELECT 1 FROM mistake_diagnosis_event WHERE client_action_id=?',
                     (operation['action_id'] + ':answer',)).fetchone()
        or operation.get('answer_event_id'))


def replacement_authorized(c, focus, operation):
    row = c.execute('''SELECT result_json FROM mistake_dialogue_event
        WHERE child_id=? AND session_id=?
        AND json_extract(result_json,'$.pending_answer_replacement.version')=1
        AND json_extract(result_json,'$.pending_answer_replacement.replacement_action_id')=?
        ORDER BY created_at DESC LIMIT 1''',
        (focus['child_id'], focus['session_id'], operation['action_id'])).fetchone()
    if not row:
        return False
    try:
        receipt = json.loads(row[0])['pending_answer_replacement']
    except (TypeError, ValueError, KeyError):
        return False
    if (receipt.get('new_operation_hash') != digest(operation)
            or receipt.get('new_operation') != operation
            or focus.get('mistake_id') != receipt.get('mistake_id')):
        return False
    current = AgentRuntimeStore.local_mistake_answer_target(
        c, focus['session_id'], focus['child_id'],
        authorized_turn_id=operation.get('authorized_turn_id'))
    return bool(current and current.get('binding') == receipt.get('answer_binding'))


def replace_pending(service, focus, text, action_id, turn_id, replacement_answer):
    from .mistake_dialogue import MistakeDialogueService

    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        nested = MistakeDialogueService(_TransactionDatabase(c))
        replay = nested._replay(focus['session_id'], action_id, text)
        if replay:
            return replay
        live = nested.state(focus['session_id'])
        if (not live or not binding(live) or binding(live) != binding(focus)
                or live['child_id'] != focus['child_id']
                or not authorized(_TransactionDatabase(c), live, text, turn_id,
                                  control='replace', replacement_answer=replacement_answer)):
            raise InvalidAgentPlanError('PENDING_MISTAKE_REPLACEMENT_CHANGED')
        old_operation = live['state']['pending_operation']
        # The replacement is permission to change an answer, not permission to
        # follow a question/source that changed after the child spoke.
        nested._answer_write_gate(c, live, old_operation)
        if _already_recorded(c, old_operation):
            result = nested._save(
                {**live, 'status': 'blocked', 'reason': 'answer_already_recorded'},
                '这份回答已经有作答或评分记录，我还没有替换它，也没有删除原记录。'
                '你可以先暂停，或让我继续恢复刚才的处理进度，再核对如何更正。',
                action_id, text)
            c.commit()
            return result
        if (c.execute('SELECT 1 FROM mistake_dialogue_event WHERE client_action_id=?',
                      (old_operation['action_id'],)).fetchone()
                or action_id == old_operation['action_id']):
            raise InvalidAgentPlanError('PENDING_MISTAKE_REPLACEMENT_CONFLICT')

        old_binding = binding(live)
        stamp = utc_now()
        operation = {
            **old_operation,
            'action_id': action_id,
            'utterance': text,
            'answer': replacement_answer,
            'authorized_turn_id': turn_id,
        }
        operation.pop('answer_event_id', None)
        live['state']['pending_operation'] = operation
        live['state'].pop('pending_error', None)
        live['state'].pop('pending_response', None)
        live['state']['last_pending_withdrawal'] = {
            'binding': old_binding,
            'withdrawal_action_id': action_id,
            'replacement': True,
            'withdrawn_at': stamp,
        }
        nested._write_focus(c, live)
        target = AgentRuntimeStore.local_mistake_answer_target(
            c, live['session_id'], live['child_id'], authorized_turn_id=turn_id)
        if not target:
            raise InvalidAgentPlanError('PENDING_MISTAKE_REPLACEMENT_TARGET_CHANGED')
        replacement = {
            'version': 1,
            'mistake_id': live['mistake_id'],
            'old_binding': old_binding,
            'replacement_action_id': action_id,
            'replacement_turn_id': turn_id,
            'replacement_answer': replacement_answer,
            'new_operation': operation,
            'new_operation_hash': digest(operation),
            'answer_binding': target['binding'],
            'replaced_at': stamp,
        }
        audit = {
            **live,
            'response_text': '已停用刚才未处理完的旧答案，正在按你刚说的新答案继续核对。',
            'cloud_transmission_allowed': False,
            'withdrawn_pending_answer': {
                'version': 1,
                'operation': old_operation,
                'binding': old_binding,
                'withdrawal_action_id': action_id,
                'withdrawn_at': stamp,
            },
            'pending_answer_replacement': replacement,
        }
        c.execute('INSERT INTO mistake_dialogue_event VALUES (?,?,?,?,?,?)', (
            old_operation['action_id'], live['session_id'], live['child_id'],
            nested._hash(old_operation['utterance']),
            json.dumps(audit, ensure_ascii=False), stamp))
        c.commit()
    return service._resume_operation(live, text, action_id)
