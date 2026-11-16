"""Local, version-bound spoken hypotheses used only to retrieve candidate goals."""
import hashlib
import json

from . import document_goal_handoff as goal

MEANINGS={'describe_target','revise_target'}
PHASES={'target_search_pending','target_choice'}


def query(c,packet,*,executing=None):
    from .agent_runtime import AgentRuntimeStore
    from .document_independent_check import original_version
    from .document_revisit import parse_journal
    from .document_target_hypothesis import KEY
    proposal=packet[1].get(KEY,{})
    entries=proposal.get('clarifications',[])
    goal.require(isinstance(entries,list),'DOCUMENT_TARGET_CLARIFICATION_CHANGED')
    if not entries:
        return None
    original=original_version(packet[3])
    goal.require(proposal.get('original_version')==original,'DOCUMENT_TARGET_CLARIFICATION_CHANGED')
    active=[]
    seen=set()
    for i,entry in enumerate(entries):
        goal.require(isinstance(entry,dict) and set(entry)=={'turn_id','query','mode'}
            and entry['mode'] in MEANINGS and isinstance(entry['query'],str)
            and 0<len(entry['query'])<=80 and isinstance(entry['turn_id'],str)
            and entry['turn_id'] not in seen,'DOCUMENT_TARGET_CLARIFICATION_CHANGED')
        seen.add(entry['turn_id'])
        turn=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=?',
            (entry['turn_id'],packet[0]['child_id'])).fetchone()
        goal.require(turn,'DOCUMENT_TARGET_CLARIFICATION_MISSING')
        action=json.loads(turn['action_json'])
        expected=goal.digest({'original_version':original,'entries':entries[:i+1]})
        goal.require(action.get('document_target_clarification')==expected
            and action.get('document_revisit_commitment')==packet[0]['commitment_id'],
            'DOCUMENT_TARGET_CLARIFICATION_CHANGED')
        voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
            voice_input_id=action.get('voice_input_id'),transcript=turn['transcript'])
        goal.require(AgentRuntimeStore.voice_capture_snapshot(voice)==action.get('voice_input_binding'),
            'DOCUMENT_TARGET_CLARIFICATION_VOICE_CHANGED')
        journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
            (action.get('document_revisit_request'),)).fetchone()
        fingerprint=hashlib.sha256(json.dumps([turn['child_id'],turn['session_id'],turn['transcript']],ensure_ascii=False).encode()).hexdigest()
        goal.require(journal and journal['status']=='completed' and journal['fingerprint']==fingerprint
            and (journal['session_id'],journal['child_id'])==(turn['session_id'],turn['child_id']),
            'DOCUMENT_TARGET_CLARIFICATION_CHANGED')
        binding,decision=json.loads(journal['binding_json']),json.loads(journal['decision_json'])
        focus=binding['document_revisit']
        goal.require(focus['commitment_id']==packet[0]['commitment_id'] and focus['phase'] in PHASES,
            'DOCUMENT_TARGET_CLARIFICATION_CHANGED')
        parsed=parse_journal(decision,turn['transcript'],binding['review_clock'])
        goal.require(parsed==decision and parsed['meaning']==entry['mode'] and parsed.get('query')==entry['query'],
            'DOCUMENT_TARGET_CLARIFICATION_CHANGED')
        if entry['turn_id']!=executing:
            receipt=json.loads(journal['execution_receipt_json'] or '{}')
            goal.require(receipt.get('agent_turn_id')==entry['turn_id']
                and receipt.get('result',{}).get('commitment_id')==packet[0]['commitment_id'],
                'DOCUMENT_TARGET_CLARIFICATION_UNFINISHED')
        if entry['mode']=='revise_target':
            active=[]
        active.append(entry['query'])
    return '\n'.join(active)


def append(c,packet,turn,decision,action):
    from .document_independent_check import original_version
    from .document_target_hypothesis import KEY,offer
    previous=packet[1].get(KEY,{})
    try:
        query(c,packet)
    except (ValueError,TypeError,KeyError,IndexError):
        if decision['meaning']!='revise_target':
            packet[1][KEY]={**previous,'status':'unavailable','retrieval':{'status':'clarification_changed'}}
            return '前面的补充语音依据已经变化，不能把这次半句补充接在旧依据上。请重新完整说明要查找的目标，或先回原题。'
        previous={**previous,'clarifications':[],
            'superseded_clarifications':[*previous.get('superseded_clarifications',[]),previous.get('clarifications',[])]}
    entries=[*previous.get('clarifications',[]),
        dict(turn_id=turn['agent_turn_id'],query=decision['query'],mode=decision['meaning'])]
    packet[1][KEY]={**previous,'original_version':original_version(packet[3]),'clarifications':entries}
    action['document_target_clarification']=goal.digest({'original_version':original_version(packet[3]),'entries':entries})
    c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(action),turn['agent_turn_id']))
    response=offer(c,packet,executing=turn['agent_turn_id'])
    prefix=('已修订当前查找目标，前面的补充仍保留在历史中。' if decision['meaning']=='revise_target' else
        '这次补充已和前面的说明一起保存。')
    return prefix+'我理解你补充的目标是“'+decision['query']+'”，还需要核对。'+(response or
        '原题暂时不能据此匹配，没有改写原题或判断掌握。')
