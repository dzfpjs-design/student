"""Voice-backed location confirmations reference earlier speech without grading it."""
import hashlib
import json

from . import document_goal_handoff as goal
from .document_spoken_revision import parse as parse_revision, resolve, append

KEY = 'document_spoken_location'
EVENTS = 'spoken_location_events'


def parse(value, transcript):
    goal.require(isinstance(value, dict) and set(value)=={'target','target_quote'},
        'DOCUMENT_LOCATION_SCHEMA')
    parse_revision(dict(value,replacement_quote=transcript),transcript)
    return dict(value)


def basis(chain):
    return goal.digest({k:chain[k] for k in ('prompt_id','effective_segments','pending_revisions')})


def proposal(chain, location):
    pending=chain['pending_revisions']
    value=dict(version='document_spoken_location_v1',source_prompt_id=chain['prompt_id'],
        base_hash=basis(chain),location=location)
    if len(pending)!=1:
        return dict(value,status='no_pending' if not pending else 'ambiguous_pending')
    revision=resolve(chain,dict(location,replacement_quote=pending[0]['replacement_quote']))
    revision.pop('base_fingerprint')
    return dict(value,status=revision['status'],pending_prompt_id=pending[0]['prompt_id'],revision=revision)


def prepare(c, packet, location):
    from .document_spoken_conversation import read
    from .document_open_feedback import spoken_prompt
    try:
        goal.require(spoken_prompt(c,packet) is not None,'DOCUMENT_LOCATION_NOT_SPOKEN')
        return proposal(read(c,packet),location)
    except (ValueError,TypeError,KeyError):
        return dict(version='document_spoken_location_v1',status='source_unavailable',location=location)


def record(c, packet, turn, frozen, location):
    current=prepare(c,packet,location)
    goal.require(current==frozen,'DOCUMENT_LOCATION_SOURCE_CHANGED')
    if current['status']!='resolved':
        return None
    event=dict(current,turn_id=turn['agent_turn_id'])
    packet[1]['practice'].setdefault(EVENTS,[]).append(event)
    return event


def messages(status):
    return {'source_unavailable':'原回答还保留着，但当前口述题或更正依据暂时无法核对，没有改动你的解释。',
        'no_pending':'目前没有等待定位的更正。原来的解释没有改动。',
        'ambiguous_pending':'还有不止一条未定位的更正。请说清要处理哪条更正和对应的位置，我不会猜着覆盖。',
        'needs_clarification':'这个位置还不能对应到已保存的回答。请说第几段，已有更正内容不用重说。'}[status]


def events(c, packet, *, prompt_ids, before_turn_id=None, through_turn_id=None, applying_turn_id=None,recovering_turn_id=None,kind='location'):
    """Validate immutable controls and their voice; later events stay out of old views."""
    from .agent_runtime import AgentRuntimeStore
    from .document_revisit import parse_journal
    goal.require(kind in {'location','undo'},'DOCUMENT_CONTROL_KIND')
    event_key,action_key,meaning,receipt_key=EVENTS,KEY,'locate_revision','spoken_revision_event'
    if kind=='undo':
        from .document_spoken_undo import EVENTS as event_key,KEY as action_key
        meaning,receipt_key='undo_revision','spoken_undo_event'
    child,session=packet[0]['child_id'],packet[1]['practice']['session_id']
    limit=None
    anchor=before_turn_id or through_turn_id
    if anchor:
        cutoff=c.execute('SELECT rowid FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
            (anchor,child,session)).fetchone()
        goal.require(cutoff,'DOCUMENT_LOCATION_CUTOFF_MISSING')
        limit=cutoff[0]-(1 if before_turn_id else 0)
    result=[]
    prior=0
    for event in packet[1]['practice'].get(event_key,[]):
        if event.get('source_prompt_id') not in prompt_ids:
            continue
        turn=c.execute('''SELECT t.rowid AS sequence,t.*,p.status AS plan_status,s.status AS step_status,s.capability_name
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND s.step_index=0''',(event.get('turn_id'),)).fetchone()
        goal.require(turn and turn['sequence']>prior,'DOCUMENT_LOCATION_ORDER_CHANGED')
        prior=turn['sequence']
        if limit is not None and prior>limit:
            continue
        action=json.loads(turn['action_json'])
        goal.require((turn['child_id'],turn['session_id'])==(child,session)
            and turn['capability_name']=='learning.review_revisit'
            and action.get('document_revisit_commitment')==packet[0]['commitment_id']
            and event==dict(action.get(action_key) or {},turn_id=turn['agent_turn_id']), 'DOCUMENT_LOCATION_OWNER_CHANGED')
        journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
            (action.get('document_revisit_request'),)).fetchone()
        expected=hashlib.sha256(json.dumps([child,session,turn['transcript']],ensure_ascii=False).encode()).hexdigest()
        goal.require(journal and journal['status']=='completed' and journal['fingerprint']==expected
            and (journal['child_id'],journal['session_id'])==(child,session), 'DOCUMENT_LOCATION_JOURNAL_CHANGED')
        binding=json.loads(journal['binding_json'])
        decision=json.loads(journal['decision_json'])
        goal.require(binding['document_revisit']['commitment_id']==packet[0]['commitment_id']
            and binding['document_revisit']['phase']=='check_feedback'
            and not binding.get('document_feedback_projection')
            and parse_journal(decision,turn['transcript'],binding['review_clock'])==decision
            and decision.get('meaning')==meaning and (kind=='undo' or decision.get('location')==event['location']),
                'DOCUMENT_LOCATION_SEMANTICS_CHANGED')
        goal.require(kind!='undo' or decision.get('undo')==event.get('selection'),'DOCUMENT_UNDO_SELECTION_CHANGED')
        voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=session,child_id=child,
            voice_input_id=action.get('voice_input_id'),transcript=turn['transcript'])
        goal.require(action.get('voice_input_binding')==AgentRuntimeStore.voice_capture_snapshot(voice),
            'DOCUMENT_LOCATION_VOICE_CHANGED')
        parent=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(event['source_prompt_id'],)).fetchone()
        from .voice_capture_binding import capture_started_at
        capture = capture_started_at(c, action['voice_input_id'])
        goal.require(parent and parent['child_id']==child and parent['origin_session_id']==session
            and parent['status']=='answered' and capture
            and goal.instant(parent['updated_at'])<=goal.instant(capture)<=goal.instant(turn['created_at']),
            'DOCUMENT_LOCATION_CAPTURE_CHANGED')
        captured=AgentRuntimeStore.captured_question(voice)
        goal.require(not captured or captured==dict(kind='diagnostic',binding=dict(prompt_id=parent['prompt_id'],
            item_version=parent['item_version'])),'DOCUMENT_LOCATION_CAPTURE_TARGET_CHANGED')
        if applying_turn_id==turn['agent_turn_id']:
            goal.require(turn['step_status']=='running','DOCUMENT_LOCATION_NOT_EXECUTING')
        else:
            receipt=json.loads(journal['execution_receipt_json']) if journal['execution_receipt_json'] else {}
            terminal=turn['tool_status']==turn['plan_status']==turn['step_status']=='completed'
            recovering=(recovering_turn_id==turn['agent_turn_id'] and turn['tool_status'] in {'pending','failed','completed'}
                and turn['plan_status'] in {'running','failed','completed'} and turn['step_status'] in {'running','failed','completed'})
            goal.require((terminal or recovering)
                and receipt.get('agent_turn_id')==turn['agent_turn_id']
                and receipt.get('result',{}).get(receipt_key)==event,
                'DOCUMENT_LOCATION_RECEIPT_CHANGED')
        result.append((turn['sequence'],event))
    # Missing payload events must not silently resurrect an obsolete explanation.
    recorded=c.execute('''SELECT t.rowid,t.agent_turn_id,json_extract(j.execution_receipt_json,?) FROM agent_turn t
        JOIN voice_control_intent_request j ON j.request_key=json_extract(t.action_json,'$.document_revisit_request')
        WHERE t.child_id=? AND t.session_id=? AND json_extract(t.action_json,'$.document_revisit_commitment')=?
        AND json_extract(j.execution_receipt_json,?) IS NOT NULL''',
        ('$.result.'+receipt_key+'.source_prompt_id',child,session,packet[0]['commitment_id'],'$.result.'+receipt_key)).fetchall()
    ids={event['turn_id'] for _,event in result}
    goal.require(all(row[1] in ids for row in recorded if row[2] in prompt_ids and (limit is None or row[0]<=limit)),
        'DOCUMENT_LOCATION_EVENT_MISSING')
    return result


def apply(chain, event):
    expected=proposal(chain,event['location'])
    goal.require(event==dict(expected,turn_id=event['turn_id']) and event['status']=='resolved',
        'DOCUMENT_LOCATION_PROJECTION_CHANGED')
    pending=next(p for p in chain['pending_revisions'] if p['prompt_id']==event['pending_prompt_id'])
    append(chain['effective_segments'],chain['pending_revisions'],
        dict(prompt_id=pending['prompt_id']),event['revision'])
