"""Exact failed-start authority for cancellation, never evidence of a launch."""
import hashlib
import json
from datetime import datetime, timezone


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _completed(c, child_id, session_id, request, kind):
    """An end of the same cancel command, not mere existence of server fencing."""
    for row in c.execute('''SELECT t.action_json,s.result_json FROM agent_turn t
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.child_id=? AND t.session_id=? AND t.tool_status='completed'
        AND p.status='completed' AND s.status='completed' AND s.capability_name=?''',
        (child_id, session_id, 'controlled_content.stop' if kind=='content' else 'controlled_game.stop')).fetchall():
        try:
            action, result = json.loads(row[0]), json.loads(row[1])
            if not isinstance(action, dict) or not isinstance(result, dict):
                continue
            binding = action.get('controlled_launch_cancel_binding')
            receipt = result.get('execution_receipt')
            if not isinstance(binding, dict) or not isinstance(receipt, dict):
                continue
            output = receipt.get('output')
            if (binding.get('request_id') != request or binding.get('source_turn_id') != request
                    or binding.get('child_id') != child_id or binding.get('session_id') != session_id
                    or binding.get('kind') != ('video' if kind=='content' else 'game') or not isinstance(output, dict)):
                continue
            if (output.get('request_id') == request and output.get('status') == 'cancelled_before_start'
                    and output.get('device_closed') is True and output.get('no_activity') is True
                    and output.get('task_resumed') is False and receipt.get('status') == 'completed'):
                row = c.execute('''SELECT 1 FROM controlled_launch_receipt WHERE intent_id=?
                    AND child_id=? AND session_id=? AND request_id=? AND kind=? AND cancelled_before_start=1''',
                    (output.get('native_intent_id'),child_id,session_id,request,'video' if kind=='content' else 'game')).fetchone()
                if row:
                    return True
        except (ValueError, TypeError):
            continue
    return False


def failed_starts(c, child_id, session_id, kind, *, include_cancelled=False):
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'agent_turn', 'agent_plan', 'agent_step'} <= tables:
        return []
    table = 'controlled_content_session' if kind == 'content' else 'controlled_game_session'
    capability = 'controlled_content.play' if kind == 'content' else 'controlled_game.play'
    action_type = 'start_controlled_content' if kind == 'content' else 'start_controlled_game'
    has_receipts = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='controlled_launch_receipt'").fetchone()
    result = []
    for row in c.execute('''SELECT t.*,p.plan_id,p.context_manifest_json,s.arguments_json
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.child_id=? AND t.session_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id
        AND t.tool_status='failed' AND t.tool_error_code IN ('CLIENT_TOOL_ACTION_FAILED','CONTROLLED_START_NATIVE_UNCONFIRMED')
        AND p.status='failed' AND s.status='failed' AND s.capability_name=? ORDER BY t.agent_turn_id''',
        (child_id, session_id, capability)).fetchall():
        try:
            action = json.loads(row['action_json'])
            arguments = json.loads(row['arguments_json'])
            manifest = json.loads(row['context_manifest_json'])
            if not all(isinstance(v, dict) for v in (action, arguments, manifest)):
                continue
            if (action.get('type') != action_type or not isinstance(action.get('query'), str)
                    or not action['query'].strip() or action['query'] != arguments.get('query')
                    or not isinstance(row['response_text'], str)):
                continue
            _stamp(row['created_at'])
            _stamp(row['completed_at'])
        except (ValueError, TypeError):
            continue
        request = row['agent_turn_id']
        if c.execute(f'SELECT 1 FROM {table} WHERE client_action_id=?', (request,)).fetchone():
            continue
        cancelled = has_receipts and c.execute('''SELECT 1 FROM controlled_launch_receipt
            WHERE child_id=? AND session_id=? AND kind=? AND request_id=?''',
            (child_id, session_id, 'video' if kind == 'content' else 'game', request)).fetchone()
        if not include_cancelled and cancelled:
            legacy_unknown = row['response_text'] == '原启动是否执行仍待核对，没有重新打开或进入选择。请恢复连接后说停止原活动，核对原请求。'
            if (row['tool_error_code'] != 'CONTROLLED_START_NATIVE_UNCONFIRMED' and not legacy_unknown) or _completed(c,child_id,session_id,request,kind):
                continue
        digest = hashlib.sha256(json.dumps([action, arguments, manifest, row['tool_error_code'], row['response_text']], sort_keys=True,
            ensure_ascii=False).encode()).hexdigest()
        result.append({'child_id': child_id, 'session_id': session_id,
            'kind': 'video' if kind == 'content' else 'game', 'request_id': request,
            'source_turn_id': request, 'source_action_sha256': digest,
            'created_at': row['created_at'], 'failed_at': row['completed_at']})
    return result


def resolve(c, child_id, session_id, kind, frozen, capture):
    # No latest-row fallback. A voice request can only own a pre-arrival failure.
    current = failed_starts(c, child_id, session_id, kind)
    if current != frozen or len(current) != 1 or not capture or capture.get('activity_reference'):
        raise ValueError('CONTROL_LAUNCH_CANCEL_TARGET_REQUIRED')
    from .voice_capture_binding import capture_started_at
    owner = c.execute('SELECT child_id,session_id FROM voice_capture_binding WHERE voice_input_id=?',
        (capture.get('voice_input_id'),)).fetchone()
    voice = capture_started_at(c, capture.get('voice_input_id')) if owner and tuple(owner) == (child_id, session_id) else None
    if not voice or _stamp(current[0]['created_at']) > _stamp(voice) or _stamp(current[0]['failed_at']) > _stamp(voice):
        raise ValueError('CONTROL_LAUNCH_CANCEL_ARRIVAL_CHANGED')
    return current[0]


def validate(c, child_id, session_id, kind, target):
    table = 'controlled_content_session' if kind == 'content' else 'controlled_game_session'
    if not isinstance(target, dict) or target not in failed_starts(c, child_id, session_id, kind, include_cancelled=True):
        raise ValueError('CONTROL_LAUNCH_CANCEL_SOURCE_CHANGED')
    if c.execute(f"SELECT 1 FROM {table} WHERE child_id=? AND session_id=? AND status='active'", (child_id, session_id)).fetchone():
        raise ValueError('CONTROL_LAUNCH_CANCEL_ACTIVE_CHANGED')
