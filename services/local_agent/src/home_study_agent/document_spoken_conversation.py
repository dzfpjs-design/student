"""Local, source-checked explanation history; never an aggregate mastery score."""
import json
from copy import deepcopy

from . import document_goal_handoff as goal
from .document_spoken_retry import parent_packet


def read(c, packet, *, before_turn_id=None, through_turn_id=None, applying_turn_id=None, recovering_turn_id=None,include_undoable=False):
    from .document_open_feedback import read_scope
    with read_scope(c):
        return _read(c, packet, include_undoable=include_undoable,before_turn_id=before_turn_id, through_turn_id=through_turn_id,
            applying_turn_id=applying_turn_id,recovering_turn_id=recovering_turn_id)


def _read(c, packet, *, include_undoable=False, **limits):
    from .document_check_followup import current
    from .document_open_feedback import read as feedback
    from .document_spoken_supplement import KEY
    prompt_id = packet[1]['practice']['independent_prompt_id']
    rows, seen = [], set()
    upper = None
    # Establish a finite same-owner, same-item ancestry before evaluating it.
    while prompt_id:
        row = c.execute('SELECT rowid AS sequence,* FROM diagnostic_prompt WHERE prompt_id=?', (prompt_id,)).fetchone()
        goal.require(row and prompt_id not in seen and (upper is None or row['sequence'] < upper)
            and row['child_id'] == packet[0]['child_id']
            and row['origin_session_id'] == packet[1]['practice']['session_id'], 'DOCUMENT_CONVERSATION_ANCESTRY_CHANGED')
        if rows:
            goal.require(all(row[k] == rows[0][0][k] for k in ('diagnostic_item_id','item_version')),
                'DOCUMENT_CONVERSATION_TASK_CHANGED')
        seen.add(prompt_id)
        upper = row['sequence']
        ref = json.loads(row['request_json'])['document_independent']
        retry = ref['binding'].get('spoken_retry')
        turn = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (ref['turn_id'],)).fetchone()
        goal.require(turn, 'DOCUMENT_CONVERSATION_ORIGIN_MISSING')
        action = json.loads(turn[0])
        relation = ('continuation' if action.get(KEY) else 'restart') if retry else 'initial'
        rows.append((row, relation, (action.get(KEY) or {}).get('revision')))
        prompt_id = retry['prompt_id'] if retry else None
    from .document_spoken_location import events, apply as apply_location
    from .document_spoken_undo import remember,apply as apply_undo
    prompt_ids={row[0]['prompt_id'] for row in rows}
    controls=[(seq,'location',e) for seq,e in events(c,packet,prompt_ids=prompt_ids,**limits)]
    controls+= [(seq,'undo',e) for seq,e in events(c,packet,prompt_ids=prompt_ids,kind='undo',**limits)]
    controls.sort(key=lambda item:item[0])
    applied,undone,operations=[],[],[]
    segments, active, effective, pending = [], [], [], []
    for row, relation, revision in reversed(rows):
        historical = parent_packet(packet, row['prompt_id'])
        _, _, attempt = current(c, historical)
        assessed = feedback(c, historical)
        if relation in {'initial','restart'}:
            active = []
            effective, pending = [], []
            operations=[]
        active.append(row['prompt_id'])
        segments.append(dict(prompt_id=row['prompt_id'], attempt_id=attempt['attempt_id'],
            evidence_id=attempt['evidence_id'], relation=relation, transcript=attempt['submitted_answer'],
            assessment_id=assessed['assessment_id'], feedback_version=assessed['feedback_version'],
            dimensions=assessed['dimensions'], assessment_scope='this_segment_only'))
        from .document_spoken_revision import append
        before=deepcopy((effective,pending)) if revision else None
        append(effective,pending,segments[-1],revision)
        if revision:
            remember(operations,row['prompt_id'],before,effective,pending)
        for _,kind,event in controls:
            if event['source_prompt_id']==row['prompt_id']:
                view=dict(prompt_id=row['prompt_id'],effective_segments=effective,pending_revisions=pending,
                    fingerprint='local-control-projection',undoable_revisions=operations)
                if kind=='location':
                    before=deepcopy((effective,pending))
                    apply_location(view,event)
                    remember(operations,event['pending_prompt_id'],before,effective,pending)
                    applied.append(event)
                else:
                    apply_undo(view,event)
                    undone.append(event)
    value = dict(version='document_spoken_conversation_v2', commitment_id=packet[0]['commitment_id'],
        child_id=packet[0]['child_id'], session_id=packet[1]['practice']['session_id'],
        prompt_id=rows[0][0]['prompt_id'], segments=segments, active_prompt_ids=active,
        effective_segments=effective, pending_revisions=pending,
        combined_assessment_status='not_evaluated', **goal.LOCAL)
    if applied:
        value.update(version='document_spoken_conversation_v3',location_events=applied)
    if undone:
        value.update(version='document_spoken_conversation_v4',undo_events=undone)
    value['fingerprint'] = goal.digest(value)
    if include_undoable:
        value['undoable_revisions']=operations
    return value


def response(chain, latest_feedback):
    active = [s for s in chain['segments'] if s['prompt_id'] in chain['active_prompt_ids']]
    current_sources={s['source_prompt_id'] for s in chain['effective_segments']}
    current_sources.update(s['prompt_id'] for s in chain['pending_revisions'])
    if chain.get('undo_events') and chain['segments'][-1]['prompt_id'] not in current_sources:
        latest_feedback='已撤回部分的旧评价仅留在历史中，尚未对当前完整说法重新评价。'
    if len(chain['segments']) == 1:
        return latest_feedback
    if chain['pending_revisions'] or chain.get('undo_events') or any(not s['original_text'] for s in chain['effective_segments']):
        readback='按你确认的更正和撤回，目前采用的说法是：'
        readback+='；'.join('第'+str(i)+'段：“'+s['text']+'”' for i,s in enumerate(chain['effective_segments'],1))+'。'
        if chain['pending_revisions']:
            readback+='还有未定位的更正：'+'；'.join('“'+s['transcript']+'”' for s in chain['pending_revisions'])+'。你要改第几段？只有一条待定位更正时，说第几段就可以，改后的内容不用重说。原话都还在。'
        return readback+'旧回答另行保留。这是当前说法，不是整段解释的总评，也不表示答案正确或已经掌握。'+latest_feedback
    readback = '这一轮你先说：“'+active[0]['transcript']+'”。'
    for segment in active[1:]:
        readback += '随后说：“'+segment['transcript']+'”。'
    if len(active) > 1:
        readback += '这些原话都保留着。下面只针对最后一段，不是整段解释的总评；前后有更正的部分也不会直接拼成结论。'
    else:
        readback += '这是重新完整作答的一轮，前一轮另外保留，没有拼入这次评价。'
    return readback+latest_feedback


def validate_receipt(c, turn_id, saved):
    """A cached spoken readback has the same revocation boundary as fresh speech."""
    chain = (saved.get('result') or {}).get('spoken_conversation')
    from .document_revisit import retained
    turn = c.execute('SELECT child_id,session_id,action_json,response_text,tool_status FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    action = json.loads(turn['action_json']) if turn else {}
    journal = c.execute('SELECT execution_receipt_json FROM voice_control_intent_request WHERE request_key=?',
        (action.get('document_revisit_request'),)).fetchone()
    receipt = json.loads(journal[0]) if journal and journal[0] else {}
    recorded = (receipt.get('result') or {}).get('spoken_conversation')
    if recorded is None and chain is None:
        return
    goal.require(recorded is not None and chain == recorded and saved.get('response_text') == receipt.get('response_text')
        and (turn['tool_status'] != 'completed' or turn['response_text'] == saved['response_text'][:4000]),
        'DOCUMENT_CONVERSATION_RECEIPT_CHANGED')
    goal.require(turn and chain['child_id'] == turn['child_id'] and chain['session_id'] == turn['session_id']
        and chain['commitment_id'] == action.get('document_revisit_commitment'), 'DOCUMENT_CONVERSATION_OWNER_CHANGED')
    packet = retained(c, chain['commitment_id'], turn['child_id'])
    historical = parent_packet(packet, chain['prompt_id'])
    current = read(c, historical, through_turn_id=turn_id,recovering_turn_id=turn_id)
    if (chain.get('version')=='document_spoken_conversation_v1' and 'effective_segments' not in chain
            and 'pending_revisions' not in chain and not current['pending_revisions']
            and all(s['original_text'] for s in current['effective_segments'])):
        current={k:v for k,v in current.items() if k not in {'effective_segments','pending_revisions','fingerprint'}}
        current['version']='document_spoken_conversation_v1'
        current['fingerprint']=goal.digest(current)
    goal.require(current == chain, 'DOCUMENT_CONVERSATION_RECEIPT_CHANGED')
