"""Local, recorded offers for choosing among pending tasks by voice."""
import hashlib
import json
import re


def choice_index(text):
    match = re.fullmatch(r'(?:那|我要|我|请)?(?:就)?(?:选|做|开始)?第([一二三四五六七八九十]{1,3}|[1-9][0-9]?)(?:项|个(?:任务|作业|练习)?)(?:吧)?[。！!\s]*', text.strip())
    if not match:
        return None
    number = match[1]
    digits = {char: index + 1 for index, char in enumerate('一二三四五六七八九')}
    if number.isdigit():
        value = int(number)
    elif number in digits:
        value = digits[number]
    elif number.count('十') == 1:
        tens, units = number.split('十')
        if (tens and tens not in digits) or (units and units not in digits):
            return None
        value = digits.get(tens, 1) * 10 + digits.get(units, 0)
    else:
        return None
    return value - 1


def offer(snapshot):
    if (snapshot.get('reason') != 'requested_task_ambiguous'
            or (snapshot.get('requested_filter') or {}).get('conflict')
            or len(snapshot.get('matching_items', [])) < 2):
        return None
    return {'version': 'task_start_offer_v1', 'snapshot': snapshot,
            'choices': snapshot['matching_items']}


def offer_text(value):
    names = [f'第{index + 1}项，{item["subject"]}，{item["title"]}'
             for index, item in enumerate(value['choices'])]
    return '有几项符合你的意思：' + '；'.join(names) + '。你想先做哪一项？'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def read_offer(c, session_id, child_id, turn_id):
    row = c.execute('''SELECT t.action_json,t.client_action_id,t.transcript FROM agent_turn t
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.agent_turn_id=? AND t.session_id=? AND t.child_id=? AND t.tool_status='completed'
          AND p.status IN ('completed','waiting_child') AND s.status='completed'
          AND s.capability_name='conversation.respond' ''', (turn_id, session_id, child_id)).fetchone()
    if not row:
        return None
    try:
        action = json.loads(row['action_json'])
        if action.get('source') != 'task_navigation_clarification':
            return None
        value = action.get('task_start_offer')
        arrival = c.execute('''SELECT snapshot_json FROM learning_input_arrival
            WHERE session_id=? AND child_id=? AND client_action_id=?''',
            (session_id, child_id, row['client_action_id'])).fetchone()
        snapshots = [part['snapshot'] for part in json.loads(arrival[0])
                     if part.get('kind') == 'task_navigation_arrival'] if arrival else []
        if len(snapshots) != 1 or not value:
            return None
        snapshot = snapshots[0]
        semantic = value.get('snapshot', {}).get('semantic_start')
        if semantic:
            from .task_start_binding import TaskStartBinding
            from .task_start_semantics import validate
            validate(semantic['selector'], row['transcript'])
            snapshot = TaskStartBinding.semantic_start_target(snapshot, semantic['selector'])
        return value if offer(snapshot) == value else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def latest_turn(c, session_id, child_id, before_turn=None):
    row = c.execute('''SELECT agent_turn_id FROM agent_turn WHERE session_id=? AND child_id=?
        AND (? IS NULL OR rowid < (SELECT rowid FROM agent_turn WHERE agent_turn_id=?))
        ORDER BY rowid DESC LIMIT 1''', (session_id, child_id, before_turn, before_turn)).fetchone()
    return row[0] if row else None


def project(c, session_id, child_id, snapshot, selection, *, turn_id=None):
    result = {**snapshot, 'selection_offer': selection, 'status': 'blocked',
              'reason': 'task_start_choice_expired', 'item': None}
    reference = selection.get('reference') or {}
    source = reference.get('offer_turn_id')
    value = read_offer(c, session_id, child_id, source)
    if (not value or reference.get('source_version') != fingerprint(value)
            or latest_turn(c, session_id, child_id, turn_id) != source
            or snapshot['items'] != value['snapshot']['items']):
        return result
    if snapshot['status'] != 'ready':
        return {**result, 'reason': snapshot.get('reason')}
    index = selection.get('index')
    if type(index) is not int or not 0 <= index < len(value['choices']):
        return {**result, 'reason': 'task_start_choice_out_of_range'}
    item = value['choices'][index]
    if snapshot['items'][0]['source_type'] == 'planned_break' and item != snapshot['items'][0]:
        return {**result, 'reason': 'planned_break_first'}
    return {**result, 'status': 'ready', 'reason': None, 'item': item}


def capture_reply(c, session_id, child_id, snapshot, transcript, voice_capture):
    index = choice_index(transcript)
    reference = (voice_capture or {}).get('task_choice_reference')
    if index is None:
        return None
    if (voice_capture or {}).get('question_reference'):
        return None
    # A missing reference must never acquire a new list after ASR completes.
    selection = {'reference': reference, 'index': index}
    return project(c, session_id, child_id, snapshot, selection)


def current_offer(c, session_id, child_id):
    from .task_start_binding import TaskStartBinding
    source = latest_turn(c, session_id, child_id)
    value = read_offer(c, session_id, child_id, source)
    snapshot = TaskStartBinding.start_snapshot(c, session_id, child_id)
    session = c.execute('SELECT status,state FROM daily_session WHERE session_id=? AND child_id=?',
                        (session_id, child_id)).fetchone()
    reference = None
    if (session and tuple(session) == ('active', 'study') and value
            and snapshot['status'] == 'ready' and snapshot['items'] == value['snapshot']['items']):
        reference = {'offer_turn_id': source, 'source_version': fingerprint(value)}
    if session and tuple(session) == ('active', 'study') and reference is None:
        from .task_source_recovery import deferral_target, capture_reference
        reference = capture_reference(deferral_target(c, child_id, session_id))
    return {'session_id': session_id, 'child_id': child_id, 'reference': reference,
            'local_only': True, 'cloud_transmission_allowed': False}
