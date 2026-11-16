"""Local, revocable references from a spoken recall to its original questions."""
import json

from . import document_goal_handoff as goal

SOURCE_FIELDS = ('source_id', 'child_id', 'session_id', 'action_id', 'request_digest',
                 'kind', 'occurred_at', 'result_code', 'tombstoned')


def source_hash(source):
    return goal.digest({key: source[key] for key in SOURCE_FIELDS})


def capture_context(store, child, session, key, response):
    from .companion_memory import _now
    with store.database.connect() as c:
        c.execute('BEGIN')
        source = c.execute('SELECT * FROM companion_memory_source WHERE action_id=?', (key,)).fetchone()
        if not source or source['tombstoned'] or (source['child_id'], source['session_id']) != (child, session):
            return None
        code = source['result_code'] or ''
        if code != 'study_review_last' and not code.startswith('study_review_day:'):
            return None
        selected = []
        text = store._study_review_response(c, child, code.partition(':')[2] or None, _now(), selected)
        if text != response:
            return None
        refs = [dict(memory_id=row['memory_id'], observation=json.loads(row['observation_json']))
                for row in selected if row['observation_kind'] == 'document_revisit']
        return dict(source_id=source['source_id'], source_hash=source_hash(source),
                    response_hash=goal.digest(response), refs=refs, **goal.LOCAL) if refs else None


def turn_hash(turn):
    return goal.digest({key: turn[key] for key in ('agent_turn_id', 'child_id', 'session_id',
        'client_action_id', 'transcript', 'response_text', 'action_json', 'tool_status')})


def validate(c, child, session, reference):
    from .companion_memory import _now
    from .document_revisit import retained
    from .document_revisit_memory import report_memory, memory_current
    from .agent_runtime import AgentRuntimeStore
    recall = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (reference['recall_turn_id'],)).fetchone()
    goal.require(recall and (recall['child_id'], recall['session_id']) == (child, session)
        and recall['intent'] == 'memory_command' and recall['tool_status'] == 'completed'
        and turn_hash(recall) == reference['recall_hash'], 'REVISIT_RECALL_CHANGED')
    action = json.loads(recall['action_json'])
    context = action.get('document_memory_resume')
    goal.require(context and all(context.get(k) is v for k, v in goal.LOCAL.items())
        and context['response_hash'] == goal.digest(recall['response_text']), 'REVISIT_RECALL_CHANGED')
    voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=session, child_id=child,
        voice_input_id=action.get('voice_input_id'), transcript=recall['transcript'])
    goal.require(AgentRuntimeStore.voice_capture_snapshot(voice) == action.get('voice_input_binding'), 'REVISIT_RECALL_VOICE_CHANGED')
    source = c.execute('SELECT * FROM companion_memory_source WHERE source_id=?', (context['source_id'],)).fetchone()
    goal.require(source and not source['tombstoned'] and source_hash(source) == context['source_hash']
        and (source['child_id'], source['session_id'], source['action_id']) ==
            (child, session, recall['client_action_id']), 'REVISIT_RECALL_SOURCE_CHANGED')
    items = report_memory(c, child)['items']
    by_ref = {item['ref']: item for item in items}
    choices = []
    for ref in context['refs']:
        row = c.execute('SELECT * FROM companion_memory WHERE memory_id=? AND child_id=?', (ref['memory_id'], child)).fetchone()
        goal.require(row and row['status'] == 'active' and goal.instant(row['expires_at']) > _now()
            and json.loads(row['observation_json']) == ref['observation'] and memory_current(c, row, items),
            'REVISIT_RECALL_MEMORY_CHANGED')
        cid = by_ref[ref['observation']['ref']]['commitment_id']
        item = retained(c, cid, child)[0]
        goal.require(item['status'] in {'pending', 'due'}, 'REVISIT_RECALL_CANCELLED')
        choice = dict(commitment_id=cid, source_hash=goal.digest(item))
        if choice not in choices:
            choices.append(choice)
    goal.require(0 < len(choices) <= 3, 'REVISIT_RECALL_EMPTY')
    return choices


def focus(c, session, child, latest):
    """No proactive offer or old practice may guess a recalled question."""
    try:
        return _focus(c, session, child, latest)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def _focus(c, session, child, latest):
    from .voice_control_intent import VoiceControlIntent
    from .agent_runtime import AgentRuntimeStore
    if not latest:
        return None
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (latest[0],)).fetchone()
    if not turn or (turn['child_id'], turn['session_id']) != (child, session) or turn['tool_status'] != 'completed':
        return None
    action = json.loads(turn['action_json'])
    if turn['intent'] == 'memory_command' and action.get('document_memory_resume'):
        reference = dict(recall_turn_id=turn['agent_turn_id'], recall_hash=turn_hash(turn))
        phase = 'memory_offer'
    elif action.get('type') == 'control_document_revisit':
        journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
            (action.get('document_revisit_request'),)).fetchone()
        if not journal or not journal['execution_receipt_json'] or turn['tool_status'] != 'completed':
            return None
        receipt = json.loads(journal['execution_receipt_json'])
        status = receipt['result'].get('status')
        if status not in {'memory_choice','memory_stale'} or receipt['response_text'] != turn['response_text']:
            return None
        reference = json.loads(journal['binding_json'])['document_revisit']['memory_resume']
        phase = 'memory_choice' if status == 'memory_choice' else 'memory_offer'
    else:
        return None
    voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=session, child_id=child,
        voice_input_id=action.get('voice_input_id'), transcript=turn['transcript'])
    goal.require(AgentRuntimeStore.voice_capture_snapshot(voice) == action.get('voice_input_binding'),
        'REVISIT_RECALL_ANCHOR_VOICE_CHANGED')
    try:
        choices = validate(c, child, session, reference)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        # An invalid source retains clarification authority, never question,
        # generation, scheduling or assessment authority.
        choices, phase = [], 'memory_stale'
    consent = VoiceControlIntent.text_consent_version(c)
    return dict(memory_resume=reference, choices=choices, phase=phase, child_id=child,
        session_id=session, consent=list(consent) if consent else None,
        anchor_turn_id=turn['agent_turn_id'], anchor_hash=turn_hash(turn))


def resolve(c, turn, target, meaning):
    """Only an explicit choice after readback can pick from multiple questions."""
    from .document_revisit import retained
    if target['phase'] == 'memory_stale':
        goal.require(focus(c, turn['session_id'], turn['child_id'], (target['anchor_turn_id'],)) == target,
            'REVISIT_RECALL_FOCUS_CHANGED')
        declined = meaning == 'resume_decline'
        text = ('先不继续。这次没有修改复习安排，也没有删除其它学习记录。' if declined else
            '刚才回顾的原题或记忆已经失效，不能直接接着做。需要重新核对有效记录，或者重新拍题确认；我不会猜是哪道题。')
        return None, dict(agent_turn_id=turn['agent_turn_id'], response_text=text,
            result=dict(status='memory_declined' if declined else 'memory_stale', **goal.LOCAL))
    choices = validate(c, turn['child_id'], turn['session_id'], target['memory_resume'])
    goal.require(choices == target['choices'], 'REVISIT_RECALL_CHOICES_CHANGED')
    anchor = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (target['anchor_turn_id'],)).fetchone()
    goal.require(anchor and turn_hash(anchor) == target['anchor_hash'], 'REVISIT_RECALL_ANCHOR_CHANGED')
    from .agent_runtime import AgentRuntimeStore
    anchor_action = json.loads(anchor['action_json'])
    anchor_voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=turn['session_id'], child_id=turn['child_id'],
        voice_input_id=anchor_action.get('voice_input_id'), transcript=anchor['transcript'])
    goal.require(AgentRuntimeStore.voice_capture_snapshot(anchor_voice) == anchor_action.get('voice_input_binding'),
        'REVISIT_RECALL_ANCHOR_VOICE_CHANGED')
    index = 0 if len(choices) == 1 and meaning == 'start' else None
    if target['phase'] == 'memory_choice' and meaning in {'select_1', 'select_2', 'select_3'}:
        selected = int(meaning[-1]) - 1
        if selected < len(choices):
            index = selected
    if index is not None:
        return choices[index], None
    status = 'memory_declined' if meaning == 'resume_decline' else 'memory_choice'
    if status == 'memory_declined':
        text = '先不继续。原题、作答和原来的复习安排都保留着。'
    elif len(choices) == 1:
        text = '刚回顾的是一道有原题记录的题。你愿意现在自己再试一次吗？'
    else:
        labels = []
        for i, choice in enumerate(choices, 1):
            packet = retained(c, choice['commitment_id'], turn['child_id'])
            labels.append('第'+str(i)+'题：'+packet[4]['confirmed_text'])
        text = '刚回顾的记录涉及多道题：'+'；'.join(labels)+'。你想继续第几题？我先不替你选，也不开始讲解。'
    return None, dict(agent_turn_id=turn['agent_turn_id'], response_text=text,
        result=dict(status=status, **goal.LOCAL))
