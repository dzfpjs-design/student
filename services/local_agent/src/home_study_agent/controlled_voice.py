"""Borrowed-transaction external-activity voice authority; never exports a catalog."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .controlled_content import ControlledContentError, _video_selection_choice, _select_approved_query
from .controlled_game import ControlledGameError, _game_selection_choice

CONTROLS = {
    'controlled_content.play': ('start_controlled_content', {}),
    'controlled_content.stop': ('stop_controlled_content', {}),
    'controlled_game.play': ('start_controlled_game', {}),
    'controlled_game.stop': ('stop_controlled_game', {}),
}


def media_literal_request_text(text, query=None):
    """Mask one named object's delimiters, never a quoted instruction.

    This permits classification only; source/privacy and execution checks remain
    separate. A returned value retains every word outside the exact literal.
    """
    matches = list(re.finditer(r'“([^“”\n]{1,80})”|「([^「」\n]{1,80})」|『([^『』\n]{1,80})』|"([^"\n]{1,80})"', text))
    if len(matches) != 1:
        return None
    match = matches[0]
    literal = next(value for value in match.groups() if value is not None)
    if query is not None and query != literal:
        return None
    outside = text[:match.start()] + '本轮题名' + text[match.end():]
    if (re.search(r'[“”「」『』"\']|(?:他说|她说|老师说|如果|假如|假设|什么意思|然后|同时|并且|而且|这句话|这个标题|标题.{0,8}(?:怎么|如何)|怎么读|看到[了过]|看见[了过]|文字|含义)', outside)
            or not re.search(r'(?:播放|看|打开|启动|玩)', text[:match.start()])
            or re.search(r'(?:别|不要|不用|不想|不需要|并非|不是要)', outside)):
        return None
    return outside


def kind_for(control):
    return 'content' if control.startswith('controlled_content.') else 'games'


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


class _InvalidSourceDate(ValueError):
    pass


def _source_timestamp(value):
    try:
        return timestamp(value)
    except (TypeError, ValueError) as error:
        raise _InvalidSourceDate('CONTROL_SOURCE_DATE_INVALID') from error


def snapshot(c, child_id, session_id):
    result = {}
    for kind, table, identity, choice in (
        ('content', 'controlled_content', 'content_session_id', _video_selection_choice),
        ('games', 'controlled_game', 'game_session_id', _game_selection_choice),
    ):
        rule = c.execute(f'SELECT * FROM {table}_rule WHERE child_id=?', (child_id,)).fetchone()
        if kind == 'content':
            rows = c.execute('''SELECT a.max_minutes,a.updated_at AS approval_updated_at,a.approval_id,
                cr.*,cc.video_root_path,cc.status AS corpus_status FROM controlled_content_approval a
                JOIN curriculum_resource cr ON cr.row_id=a.curriculum_row_id
                JOIN curriculum_corpus cc ON cc.corpus_id=cr.corpus_id
                WHERE a.child_id=? AND a.status='approved' AND cr.local_video_available=1 ORDER BY cr.row_id''',
                (child_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM controlled_game_approval WHERE child_id=? AND status='approved' ORDER BY approval_id",
                             (child_id,)).fetchall()
        sources = []
        for row in rows:
            try:
                # Validate optional metadata before hashing the complete row;
                # SQLite TEXT columns can contain malformed text or BLOB values.
                times = [_source_timestamp(row['approval_updated_at'] if kind == 'content' else row['updated_at'])]
                if kind == 'content':
                    corpus = c.execute('SELECT updated_at FROM curriculum_corpus WHERE corpus_id=?', (row['corpus_id'],)).fetchone()
                    times.append(_source_timestamp(corpus[0] if corpus else None))
                source = choice(row, child_id, session_id)
                path = ((Path(row['video_root_path']) / row['video_relative_path']).resolve()
                        if kind == 'content' else Path(row['executable_path']).expanduser().resolve())
                stat = path.stat()
                times.extend((stat.st_mtime_ns / 1e9, stat.st_ctime_ns / 1e9))
                source['available_after'] = max(times)
            except _InvalidSourceDate:
                source = {'unavailable': True, 'approval_id': row['approval_id'],
                          'error_code': 'CONTROL_SOURCE_DATE_INVALID'}
            except (ControlledContentError, ControlledGameError, OSError):
                source = {'unavailable': True, 'approval_id': row['approval_id']}
            sources.append({**source, **{k: row[k] for k in ('unit_path', 'subject_title') if k in row.keys()}})
        activities = [dict(row) for row in c.execute(f'''SELECT {identity} AS activity_id,status,started_at,ended_at
            FROM {table}_session WHERE child_id=? AND session_id=? ORDER BY rowid''', (child_id, session_id))]
        result[kind] = {'child_id': child_id, 'session_id': session_id, 'kind': kind,
                        'rule': dict(rule) if rule else None, 'sources': sources, 'activities': activities}
        from .controlled_launch_cancel import failed_starts
        result[kind]['failed_starts'] = failed_starts(c, child_id, session_id, kind)
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resolve(c, child_id, session_id, control, decision, before, voice_capture=None, context=None):
    kind = kind_for(control)
    frozen = (before or {}).get(kind)
    if not frozen or frozen != snapshot(c, child_id, session_id)[kind]:
        raise ValueError('CONTROL_EXTERNAL_SOURCE_CHANGED')
    from .voice_control_intent import VoiceControlIntent
    packet = {'child_id': child_id, 'session_id': session_id, 'kind': kind,
              'source_version': fingerprint(frozen),
              'text_consent_version': list(VoiceControlIntent.text_consent_version(c) or ())}
    if control.endswith('.play'):
        available = [s for s in frozen['sources'] if not s.get('unavailable')]
        if not frozen['rule'] or not frozen['rule']['enabled'] or not available:
            raise ValueError('CONTROL_EXTERNAL_PERMISSION_REQUIRED')
        try:
            selected = _select_approved_query(available, decision['query'],
                fields=('title', 'unit_path', 'subject_title') if kind == 'content' else ('title',),
                label='视频' if kind == 'content' else '游戏', error_type=ControlledContentError)
        except ControlledContentError as error:
            if not getattr(error, 'selection_status', None):
                raise
            selected = None  # Real start API returns the existing local selection offer.
        if voice_capture:
            from .voice_capture_binding import capture_started_at
            arrival = capture_started_at(c, voice_capture['voice_input_id'])
            relevant = [selected] if selected else available
            if (not arrival or timestamp(frozen['rule']['updated_at']) > timestamp(arrival)
                    or any(source['available_after'] > timestamp(arrival) for source in relevant)):
                raise ValueError('CONTROL_EXTERNAL_FOCUS_CHANGED')
        packet['query'] = decision['query']
        if selected:
            packet['selection_binding'] = {k: selected[k] for k in ('resource_id', 'source_version')}
    else:
        if (voice_capture or {}).get('activity_reference'):
            raise ValueError('CONTROL_EXTERNAL_FOCUS_CHANGED')
        rows = frozen['activities']
        active = [r for r in rows if r['status'] == 'active']
        # Vague semantic references require a currently active resource. Only
        # the separate explicit local stop can recover a historical exact ID.
        target = active[0] if len(active) == 1 else None
        explicit_kind = bool(re.search('视频' if kind == 'content' else '游戏', decision.get('quote', '')))
        other = 'games' if kind == 'content' else 'content'
        if not explicit_kind and ((context or {}).get('tutor') or (context or {}).get('pet_listening')
                or any(row['status'] == 'active' for row in before[other]['activities'])):
            raise ValueError('CONTROL_EXTERNAL_FOCUS_CHANGED')
        if not target:
            if active:
                raise ValueError('CONTROL_EXTERNAL_TARGET_REQUIRED')
            if not explicit_kind and before[other].get('failed_starts'):
                raise ValueError('CONTROL_EXTERNAL_FOCUS_CHANGED')
            from .controlled_launch_cancel import resolve as resolve_cancel
            packet['launch_cancel'] = resolve_cancel(c, child_id, session_id, kind,
                frozen.get('failed_starts', []), voice_capture)
            return packet
        if voice_capture:
            from .voice_capture_binding import capture_started_at
            arrival = capture_started_at(c, voice_capture['voice_input_id'])
            if not arrival or datetime.fromisoformat(target['started_at']) > datetime.fromisoformat(arrival):
                raise ValueError('CONTROL_EXTERNAL_FOCUS_CHANGED')
        packet['target'] = target
    return packet


def action_fields(control, packet):
    if control.endswith('.stop') and packet.get('launch_cancel'):
        return {'controlled_voice_binding': packet, 'controlled_launch_cancel_binding': packet['launch_cancel']}
    if control.endswith('.play'):
        return {'query': packet['query'], 'controlled_voice_binding': packet,
                **({'selection_binding': packet['selection_binding']} if packet.get('selection_binding') else {})}
    binding = {k: packet[k] for k in ('child_id', 'session_id', 'kind')}
    binding['activity_id'] = packet['target']['activity_id']
    identity = 'content_session_id' if packet['kind'] == 'content' else 'game_session_id'
    return {identity: binding['activity_id'], 'controlled_voice_binding': packet,
            'controlled_stop_binding': binding,
            'controlled_stop_readback': {**binding, **{k: packet['target'][k] for k in ('status', 'started_at')}}}


def validate(c, child_id, session_id, control, packet, *, turn_id=None):
    if not packet or (packet.get('child_id'), packet.get('session_id'), packet.get('kind')) != (child_id, session_id, kind_for(control)):
        raise ValueError('CONTROL_EXTERNAL_SCOPE_CHANGED')
    # A committed original start is historical, not authority to relaunch it.
    if turn_id and control.endswith('.play'):
        table = 'controlled_content_session' if packet['kind'] == 'content' else 'controlled_game_session'
        if c.execute(f'SELECT 1 FROM {table} WHERE child_id=? AND session_id=? AND client_action_id=?',
                     (child_id, session_id, turn_id)).fetchone():
            return
    current = snapshot(c, child_id, session_id)[packet['kind']]
    from .voice_control_intent import VoiceControlIntent
    if packet.get('text_consent_version') != list(VoiceControlIntent.text_consent_version(c) or ()):
        raise ValueError('CONTROL_EXTERNAL_CONSENT_CHANGED')
    if control.endswith('.stop') and packet.get('launch_cancel'):
        from .controlled_launch_cancel import validate as validate_cancel
        validate_cancel(c, child_id, session_id, packet['kind'], packet['launch_cancel'])
        return
    if control.endswith('.stop'):
        target = packet['target']
        for index, actual in enumerate(current['activities']):
            if (actual['activity_id'] == target['activity_id'] and actual['started_at'] == target['started_at']
                    and actual['status'] in {'stopped', 'completed', 'expired', 'interrupted', 'failed'} and actual['ended_at']):
                # Reconcile the original persisted end without treating its own
                # lifecycle update as permission to stop a different resource.
                current['activities'][index] = target
    if fingerprint(current) != packet['source_version']:
        raise ValueError('CONTROL_EXTERNAL_SOURCE_CHANGED')


def check_start(c, session_id, client_action_id, query, selection_binding, kind):
    row = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (client_action_id,)).fetchone()
    if not row:
        return  # Existing explicit local start/selection API compatibility.
    action = json.loads(row['action_json'])
    packet = action.get('controlled_voice_binding')
    if not packet:
        return
    from .voice_control_intent import VoiceControlIntent
    from .agent_runtime import InvalidAgentPlanError
    if (row['session_id'] != session_id or packet['kind'] != kind or packet.get('query') != query
            or packet.get('selection_binding') != selection_binding):
        raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
    VoiceControlIntent.inspect_execution(c, client_action_id)
