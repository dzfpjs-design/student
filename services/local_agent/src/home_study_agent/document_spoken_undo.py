"""Undo correction effects, not original speech or independent later supplements."""
from copy import deepcopy

from . import document_goal_handoff as goal

KEY='document_spoken_undo'
EVENTS='spoken_undo_events'


def remember(operations, revision_id, before, effective, pending):
    """Track the latest application of a correction, including later location."""
    old_effective,old_pending=before
    removed={p['prompt_id'] for p in old_pending}-{p['prompt_id'] for p in pending}
    operations[:]=[op for op in operations if op['revision_prompt_id'] not in removed|{revision_id}]
    changed=[(a,b) for a,b in zip(old_effective,effective) if a!=b]
    goal.require(len(old_effective)==len(effective) and len(changed)<=1,'DOCUMENT_UNDO_UNSUPPORTED_CHANGE')
    if changed:
        old,new=changed[0]
        goal.require(old['slot_id']==new['slot_id'],'DOCUMENT_UNDO_SLOT_CHANGED')
        operations.append(dict(revision_prompt_id=revision_id,kind='applied',before=deepcopy(old),after=deepcopy(new)))
    else:
        added=[p for p in pending if p['prompt_id'] not in {q['prompt_id'] for q in old_pending}]
        goal.require(len(added)==1 and added[0]['prompt_id']==revision_id,'DOCUMENT_UNDO_PENDING_CHANGED')
        operations.append(dict(revision_prompt_id=revision_id,kind='pending',pending=deepcopy(added[0])))


def proposal(chain,selection=None):
    operations=chain.get('undoable_revisions',[])
    value=dict(version='document_spoken_undo_v1',source_prompt_id=chain['prompt_id'],
        base_hash=goal.digest({k:chain[k] for k in ('prompt_id','effective_segments','pending_revisions','undoable_revisions')}))
    if selection is not None:
        from .document_spoken_revision import resolve
        value['selection']=selection
        target=resolve(dict(chain,fingerprint='local-undo-target'),dict(selection,replacement_quote='local target only'))
        if target['status']!='resolved':
            return dict(value,status='needs_clarification')
        operations=[op for op in operations if op['kind']=='applied' and op['after']['slot_id']==target['target_slot_id']]
    if not operations:
        return dict(value,status='no_revision')
    return dict(value,status='resolved',operation=deepcopy(operations[-1]))


def prepare(c,packet,selection=None):
    from .document_spoken_conversation import read
    from .document_open_feedback import spoken_prompt
    try:
        goal.require(spoken_prompt(c,packet) is not None,'DOCUMENT_UNDO_NOT_SPOKEN')
        return proposal(read(c,packet,include_undoable=True),selection)
    except (ValueError,KeyError,TypeError):
        return dict(version='document_spoken_undo_v1',status='source_unavailable',**({'selection':selection} if selection else {}))


def record(c,packet,turn,frozen,selection=None):
    current=prepare(c,packet,selection)
    goal.require(current==frozen,'DOCUMENT_UNDO_SOURCE_CHANGED')
    if current['status']!='resolved':
        return None
    event=dict(current,turn_id=turn['agent_turn_id'])
    packet[1]['practice'].setdefault(EVENTS,[]).append(event)
    return event


def apply(chain,event):
    expected=proposal(chain,event.get('selection'))
    goal.require(event==dict(expected,turn_id=event['turn_id']) and event['status']=='resolved',
        'DOCUMENT_UNDO_PROJECTION_CHANGED')
    matches=[i for i,op in enumerate(chain['undoable_revisions']) if op==event['operation']]
    goal.require(len(matches)==1,'DOCUMENT_UNDO_OPERATION_CHANGED')
    operation=chain['undoable_revisions'].pop(matches[0])
    if operation['kind']=='pending':
        pending=chain['pending_revisions']
        goal.require(sum(p==operation['pending'] for p in pending)==1,'DOCUMENT_UNDO_PENDING_CHANGED')
        pending[:]=[p for p in pending if p['prompt_id']!=operation['revision_prompt_id']]
    else:
        effective=chain['effective_segments']
        matches=[i for i,s in enumerate(effective) if s['slot_id']==operation['after']['slot_id']]
        goal.require(len(matches)==1 and effective[matches[0]]==operation['after'],'DOCUMENT_UNDO_TARGET_CHANGED')
        effective[matches[0]]=deepcopy(operation['before'])


def message(status):
    return {'no_revision':'对应范围没有尚可撤回的更正。原回答和后来的补充都保留着。',
        'needs_clarification':'这个位置暂时不能对应到已保存的回答，没有撤回其他更正。请说明第几段，或明确说最后一次更正。',
        'source_unavailable':'原回答保留着，但更正依据暂时无法核对，没有按旧记录撤回。'}[status]
