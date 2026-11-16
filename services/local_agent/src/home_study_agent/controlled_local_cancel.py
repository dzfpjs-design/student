"""Explicit local failed-start cancellation; no cloud or native completion authority."""
import json
import re
from datetime import datetime, timezone


def direct_kind(transcript):
    for kind, verbs in (('content', r'停止视频|关闭视频|视频停下'),
                        ('games', r'停止游戏|关闭游戏|游戏停下')):
        if re.fullmatch(r'(?:请|请帮我|帮我)?(?:' + verbs + r')(?:吧)?[。！! ]*', transcript.strip()):
            return kind
    return None


def action_fields(target, kind):
    # Retain the existing renderer's exact cancel packet. Empty consent metadata
    # is deliberate: local stopping is not a new cloud-authorized operation.
    packet = {'child_id': target['child_id'], 'session_id': target['session_id'], 'kind': kind,
              'source_version': target['source_action_sha256'], 'text_consent_version': [],
              'launch_cancel': target, 'local_only': True}
    return {'controlled_launch_cancel_binding': target, 'controlled_voice_binding': packet}


def inspect_execution(c, row, manifest, *, allow_planned=False):
    from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
    from .voice_control_intent import VoiceControlIntent
    from .controlled_launch_cancel import failed_starts, validate

    def require(value):
        if not value:
            raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')

    target = manifest.get('local_controlled_launch_cancel')
    kind = direct_kind(row['transcript'])
    require(kind and isinstance(target, dict))
    action, arguments = json.loads(row['action_json']), json.loads(row['arguments_json'])
    control = 'controlled_content.stop' if kind == 'content' else 'controlled_game.stop'
    require(row['capability_name'] == control and action.get('type') == (
        'stop_controlled_content' if kind == 'content' else 'stop_controlled_game'))
    require(action.get('source') == 'local_controlled_stop_v1' and row['intent'] == action['type'])
    expected = action_fields(target, kind)
    require(all(action.get(k) == v and arguments.get(k) == v for k, v in expected.items()))
    states = {'planned', 'running', 'failed'} if allow_planned else {'running'}
    require(row['plan_status'] in states and row['step_status'] == row['plan_status']
            and row['tool_status'] in {'pending', 'failed'})
    require(VoiceControlIntent.scope_valid(c, row['session_id'], row['child_id'], row['client_action_id'], require_cloud=False))
    arrival = c.execute('''SELECT utterance FROM learning_input_arrival
        WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?''',
        (manifest.get('learning_input_arrival_id'), row['child_id'], row['session_id'], row['client_action_id'])).fetchone()
    require(arrival and arrival[0] == row['transcript'])
    capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=row['session_id'], child_id=row['child_id'],
        voice_input_id=action.get('voice_input_id'), transcript=row['transcript'])
    require(not capture.get('activity_reference') and AgentRuntimeStore.voice_capture_snapshot(capture)
            == action.get('voice_input_binding') == manifest.get('voice_input_binding'))
    from .voice_capture_binding import capture_started_at
    voice = capture_started_at(c, capture['voice_input_id'])
    def stamp(value):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    require(voice and stamp(target['created_at']) <= stamp(voice) and stamp(target['failed_at']) <= stamp(voice))
    try:
        validate(c, row['child_id'], row['session_id'], kind, target)
    except ValueError as error:
        raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED') from error
    current = failed_starts(c, row['child_id'], row['session_id'], kind)
    if current != [target]:
        require(not current and c.execute("SELECT 1 FROM sqlite_master WHERE name='controlled_launch_receipt'").fetchone())
        fenced = c.execute('''SELECT 1 FROM controlled_launch_receipt WHERE child_id=? AND session_id=?
            AND kind=? AND request_id=? AND cancelled_before_start=1''',
            (row['child_id'], row['session_id'], target['kind'], target['request_id'])).fetchone()
        require(fenced)
    return {'control': control, 'targets': {'controlled': expected['controlled_voice_binding']},
            'request_key': row['client_action_id'], 'local_only': True}
