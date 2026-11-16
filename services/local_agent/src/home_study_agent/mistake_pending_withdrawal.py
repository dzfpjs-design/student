"""Append a cancellation receipt before releasing a saved answer's reservation."""
import json

from .diagnostic_prompt import _TransactionDatabase
from .mistake_pending_answer import authorized, binding
from .repository import utc_now


def assert_not_withdrawn(c, focus, operation):
    from .agent_runtime import InvalidAgentPlanError
    row = c.execute('''SELECT 1 FROM mistake_dialogue_event WHERE child_id=? AND session_id=?
        AND json_extract(result_json,'$.withdrawn_pending_answer.version')=1
        AND (client_action_id=? OR (? IS NOT NULL AND
          json_extract(result_json,'$.withdrawn_pending_answer.operation.authorized_turn_id')=?))''',
        (focus['child_id'], focus['session_id'], operation['action_id'],
         operation.get('authorized_turn_id'), operation.get('authorized_turn_id'))).fetchone()
    if row:
        raise InvalidAgentPlanError('PENDING_MISTAKE_ANSWER_WITHDRAWN')


def withdraw(service, focus, text, action_id, turn_id):
    from .agent_runtime import InvalidAgentPlanError
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
                or not authorized(_TransactionDatabase(c), live, text, turn_id, control='withdraw')):
            raise InvalidAgentPlanError('PENDING_MISTAKE_WITHDRAWAL_CHANGED')
        operation = live['state']['pending_operation']
        if (c.execute('SELECT 1 FROM mistake_attempt WHERE client_action_id=?',
                      (operation['action_id'] + ':attempt',)).fetchone()
                or c.execute('SELECT 1 FROM mistake_diagnosis_event WHERE client_action_id=?',
                             (operation['action_id'] + ':answer',)).fetchone()
                or operation.get('answer_event_id')):
            # A score may have committed before the dialogue receipt failed.
            # Do not erase that fact or present a completed withdrawal to the child.
            result = nested._save({**live, 'status': 'blocked', 'reason': 'answer_already_recorded'},
                '这份回答已经有作答或评分记录，我还没有撤回它，也没有删除原记录。'
                '你可以先暂停，或让我继续恢复刚才的处理进度，再核对这份回答。', action_id, text)
            c.commit()
            return result
        previous = c.execute('SELECT 1 FROM mistake_dialogue_event WHERE client_action_id=?',
                             (operation['action_id'],)).fetchone()
        if previous or action_id == operation['action_id']:
            raise InvalidAgentPlanError('PENDING_MISTAKE_WITHDRAWAL_CONFLICT')
        original_binding = binding(live)
        stamp = utc_now()
        for key in ('pending_operation', 'pending_error', 'pending_response'):
            live['state'].pop(key, None)
        live['state']['last_pending_withdrawal'] = {
            'binding': original_binding, 'withdrawal_action_id': action_id, 'withdrawn_at': stamp}
        # Keep the same question and hint exposure. A fresh answer needs fresh authority.
        nested._write_focus(c, live)
        result = nested._save(live, '已撤回刚才未处理完的回答，原话保留在记录中，不会继续评分。'
            '你可以重新回答这道题，也可以先暂停。', action_id, text)
        audit = {**result, 'withdrawn_pending_answer': {
            'version': 1, 'operation': operation, 'binding': original_binding,
            'withdrawal_action_id': action_id, 'withdrawn_at': stamp}}
        c.execute('INSERT INTO mistake_dialogue_event VALUES (?,?,?,?,?,?)',
                  (operation['action_id'], live['session_id'], live['child_id'],
                   nested._hash(operation['utterance']), json.dumps(audit, ensure_ascii=False), stamp))
        c.commit()
        return result
