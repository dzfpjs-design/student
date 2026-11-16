"""Read delivery facts, never replay planning or execute an old command."""

import json
import re
from contextlib import nullcontext


def read_agent_execution_recovery_index(database, session_id, child_id):
    """Canonical identities for buffered browser metadata lost during a crash.

    No transcripts, action bodies, replay permission, or database mutations.
    Conversation-only/internal evaluation turns are not executable operations.
    """
    with database.connect() as c:
        c.execute('BEGIN')
        owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not owner or owner[0] != child_id:
            raise KeyError(session_id)
        rows = c.execute('''SELECT agent_turn_id,client_action_id,created_at FROM agent_turn
            WHERE session_id=? AND child_id=? AND tool_status='pending'
            AND json_valid(action_json) AND json_type(action_json,'$.type')='text'
            AND json_extract(action_json,'$.type') NOT IN ('none','')
            ORDER BY created_at,client_action_id LIMIT 129''', (session_id, child_id)).fetchall()
    return {'session_id': session_id, 'child_id': child_id,
            'entries': [{'agent_turn_id': row[0], 'client_action_id': row[1], 'created_at': row[2]}
                        for row in rows[:128]], 'has_more': len(rows) > 128,
            'local_only': True, 'cloud_transmission_allowed': False, 'replay_allowed': False}


def _saved_original_diagnostic_answer(connection, action_id, action_json, session_id, child_id):
    """An answer reservation is a fact, not a completed command or replay grant."""
    try:
        action = json.loads(action_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(action, dict) or action.get('type') != 'submit_diagnostic_answer':
        return False
    fields = ('diagnostic_prompt_id', 'diagnostic_item_version', 'answer')
    if any(not isinstance(action.get(key), str) or not action[key].strip() for key in fields):
        return False
    columns = {row[1] for row in connection.execute('PRAGMA table_info(diagnostic_prompt)')}
    if 'authorized_turn_id' not in columns:
        return False
    prompt = connection.execute('''SELECT p.answer_json, p.status, p.attempt_id, p.diagnostic_item_id
        FROM diagnostic_prompt p JOIN agent_turn t ON t.agent_turn_id=p.authorized_turn_id
        WHERE p.prompt_id=? AND p.item_version=? AND t.child_id=? AND t.session_id=?
        AND t.client_action_id=? AND p.child_id=t.child_id AND p.origin_session_id=t.session_id''',
        (action['diagnostic_prompt_id'], action['diagnostic_item_version'],
         child_id, session_id, action_id)).fetchone()
    if not prompt or prompt[1] not in ('answer_saved', 'answered'):
        return False
    # Finalization deliberately removes the reservation's answer_json; the
    # owned diagnostic_attempt is then the durable system of record.
    if prompt[1] == 'answer_saved' or prompt[0] is not None:
        try:
            saved = json.loads(prompt[0])
        except (TypeError, ValueError):
            return False
        if not isinstance(saved, dict) or saved.get('answer') != action['answer']:
            return False
    if prompt[1] == 'answered':
        return connection.execute('''SELECT 1 FROM diagnostic_attempt WHERE attempt_id=?
            AND child_id=? AND session_id=? AND diagnostic_item_id=? AND submitted_answer=?''',
            (prompt[2], child_id, session_id, prompt[3], action['answer'])).fetchone() is not None
    return True


def read_agent_request_receipt(database, session_id, action_id, *, connection=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', action_id):
        raise ValueError('INVALID_ACTION_ID')
    with (database.connect() if connection is None else nullcontext(connection)) as connection:
        if not connection.in_transaction:
            connection.execute('BEGIN')
        owner = connection.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if owner is None:
            raise KeyError(session_id)
        # Do not reveal whether this key belongs to another child/session.
        turn = connection.execute('''SELECT tool_status, action_json FROM agent_turn
            WHERE session_id=? AND child_id=? AND client_action_id=?''',
            (session_id, owner[0], action_id)).fetchone()
        if turn is not None:
            status = {'completed': 'recorded_completed', 'failed': 'recorded_failed'}.get(turn[0], 'interpreted')
        else:
            arrival = connection.execute('''SELECT 1 FROM learning_input_arrival
                WHERE session_id=? AND child_id=? AND client_action_id=?''',
                (session_id, owner[0], action_id)).fetchone()
            status = 'received' if arrival else 'unconfirmed'
        saved_answer = status == 'interpreted' and turn is not None and _saved_original_diagnostic_answer(
            connection, action_id, turn[1], session_id, owner[0])
    messages = {
        'recorded_completed': '上次请求已记录为完成。我没有重新执行，也不会重播旧内容；这不代表知识已经掌握。',
        'recorded_failed': '上次请求已记录为未完成。我没有重新执行；原有学习进度需要按当前记录继续核对。',
        'interpreted': '原请求的理解结果已保存，但后续操作还没有完成回执。我没有重新执行，请先核对当前任务。',
        'received': '原请求已经到达，但还没有完整处理回执。这不代表它仍在运行，也不能确认已完成；请不要重复提交原答案。',
        'unconfirmed': '暂时查不到原请求的处理回执，不能据此断定没有执行，也不会自动重做；请不要重复提交原答案。',
    }
    if saved_answer:
        messages['interpreted'] = ('原答案已经保存，不用重复提交。原请求的后续操作还没有完成回执，'
                                   '我没有重新执行。可以说“继续任务”恢复学习；原回执仍保留。')
    if status == 'recorded_completed':
        try:
            action = json.loads(turn[1])
        except (TypeError, ValueError):
            action = None
        if isinstance(action, dict) and action.get('type') == 'none':
            messages[status] = ('上次只完成了对话回复，没有执行学习或设备操作。'
                                '我没有重新执行；你可以继续询问当前任务。')
        elif not isinstance(action, dict) or not isinstance(action.get('type'), str) or not action['type'].strip():
            messages[status] = ('上次处理回执已保存，但这本身不能证明学习或设备操作已完成。'
                                '可以继续核对当前任务；我没有重新执行。')
    return {'session_id': session_id, 'client_action_id': action_id, 'status': status,
            'response_text': messages[status], 'local_only': True, 'cloud_transmission_allowed': False,
            'replay_allowed': False, 'mastery_update_allowed': False}


def read_recent_request_receipts(database, child_id, session_id, *, exclude_turn_id=None):
    """One bounded local snapshot, not a retry queue or proof of non-execution."""
    from .voice_capture_binding import read_realtime_admission_receipt, resolve_voice_capture, VoiceCaptureBindingError
    with database.connect() as c:
        c.execute('BEGIN')
        owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not owner or owner['child_id'] != child_id:
            raise ValueError('LEARNING_SYSTEM_QUERY_OWNER_MISMATCH')
        exclude_key, exclude_voice = '', ''
        if exclude_turn_id is not None:
            current = c.execute('SELECT client_action_id,action_json FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                                (exclude_turn_id, child_id, session_id)).fetchone()
            if not current:
                raise ValueError('LEARNING_SYSTEM_QUERY_OWNER_MISMATCH')
            exclude_key = current['client_action_id']
            try:
                action = json.loads(current['action_json'])
                exclude_voice = action.get('voice_input_id') or '' if isinstance(action, dict) else ''
            except (TypeError, ValueError):
                raise ValueError('LEARNING_SYSTEM_QUERY_SOURCE_INVALID')
        # UNION retains arrivals without turns, while canonical realtime inputs
        # own their original key even when the Runtime uses a different key.
        rows = c.execute('''WITH captures AS (
                SELECT client_action_id,voice_input_id,created_at FROM voice_capture_binding
                WHERE child_id=:child AND session_id=:session AND source_kind='realtime_transcript'
                AND local_recovery_only=0 AND (:voice='' OR COALESCE(voice_input_id,'')!=:voice)
                AND client_action_id!=:key
                AND NOT EXISTS (SELECT 1 FROM agent_turn t WHERE t.child_id=:child AND t.session_id=:session
                    AND json_extract(CASE WHEN json_valid(t.action_json) THEN t.action_json ELSE '{}' END,'$.voice_input_id')=voice_capture_binding.voice_input_id
                    AND t.intent='system.inspect_learning'
                    AND json_extract(CASE WHEN json_valid(t.action_json) THEN t.action_json ELSE '{}' END,'$.scope')='requests')
            ), turns AS (
                SELECT client_action_id,created_at FROM agent_turn t WHERE child_id=:child AND session_id=:session
                AND client_action_id!=:key
                AND NOT (intent='system.inspect_learning'
                    AND COALESCE(json_extract(CASE WHEN json_valid(action_json) THEN action_json ELSE '{}' END,'$.scope'),'')='requests')
                AND NOT EXISTS (SELECT 1 FROM captures b WHERE b.voice_input_id=
                    json_extract(CASE WHEN json_valid(t.action_json) THEN t.action_json ELSE '{}' END,'$.voice_input_id'))
            ), arrivals AS (
                SELECT client_action_id,created_at FROM learning_input_arrival a
                WHERE child_id=:child AND session_id=:session AND client_action_id!=:key
                AND NOT EXISTS (SELECT 1 FROM agent_turn t WHERE t.child_id=:child AND t.session_id=:session
                    AND t.client_action_id=a.client_action_id)
                AND NOT EXISTS (SELECT 1 FROM captures b WHERE 'realtime-'||b.voice_input_id=a.client_action_id)
            ) SELECT 'admission' AS kind,client_action_id,created_at,voice_input_id FROM captures
            UNION ALL SELECT 'request',client_action_id,created_at,NULL FROM turns
            UNION ALL SELECT 'request',client_action_id,created_at,NULL FROM arrivals
            ORDER BY created_at DESC,client_action_id DESC LIMIT 9''',
            {'child': child_id, 'session': session_id, 'key': exclude_key, 'voice': exclude_voice}).fetchall()
        result = []
        for row in rows[:8]:
            key = row['client_action_id']
            status, message = 'unconfirmed', '原记录尚未核对成功，不能确认完成，也不会自动重做。'
            if row['kind'] == 'admission':
                try:
                    admission = read_realtime_admission_receipt(database, session_id, key, connection=c)
                    status = admission['status']
                    if status == 'completed':
                        turn = c.execute('''SELECT client_action_id,transcript FROM agent_turn WHERE child_id=? AND session_id=?
                            AND json_extract(CASE WHEN json_valid(action_json) THEN action_json ELSE '{}' END,'$.voice_input_id')=?
                            ORDER BY created_at DESC,agent_turn_id DESC LIMIT 1''',
                            (child_id, session_id, admission['voice_input_id'])).fetchone()
                        if turn:
                            resolve_voice_capture(c, session_id=session_id, child_id=child_id,
                                voice_input_id=admission['voice_input_id'], transcript=turn['transcript'])
                        elif c.execute('SELECT 1 FROM agent_turn WHERE child_id=? AND session_id=? AND client_action_id=?',
                                       (child_id, session_id, 'realtime-' + admission['voice_input_id'])).fetchone():
                            raise ValueError('REQUEST_RECEIPT_SOURCE_MISMATCH')
                        receipt = read_agent_request_receipt(database, session_id,
                            turn['client_action_id'] if turn else 'realtime-' + admission['voice_input_id'], connection=c)
                        status = 'saved_unconfirmed' if receipt['status'] == 'unconfirmed' else receipt['status']
                        message = ('原话已保存在本机，向学习任务的交接尚未确认；不能断言没有执行，也不会自动重做。'
                                   if status == 'saved_unconfirmed' else receipt['response_text'])
                    elif status == 'cancelled':
                        message = '原话的后台撤回已确认；这不是已执行学习任务的撤销，也没有重新执行。'
                    elif status == 'prepared':
                        message = '原话起始记录存在，但完整转写尚未确认保存；没有自动重发。'
                except (VoiceCaptureBindingError, ValueError, TypeError, KeyError):
                    status = 'unconfirmed'
            else:
                try:
                    receipt = read_agent_request_receipt(database, session_id, key, connection=c)
                    status, message = receipt['status'], receipt['response_text']
                except (ValueError, TypeError, KeyError):
                    pass
            result.append({'kind': row['kind'], 'client_action_id': key, 'status': status, 'response_text': message})
    return {'requests': result, 'has_more': len(rows) > 8, 'local_only': True,
            'cloud_transmission_allowed': False, 'replay_allowed': False, 'mastery_update_allowed': False}
