"""Local retrieval proposes targets; a spoken choice authorizes only a probe."""
import json
import re

from . import document_goal_handoff as goal
from .diagnostic_prompt import _version
from .retrieval.hybrid import HashingVectorizer

KEY='independent_target_hypothesis'
VERSION='document_target_hypothesis_v3'


def _retrieval_text(text):
    text=re.sub(r'\s+','',text.lower())
    text=re.sub(r'[，,。；;]?(?:请)?只回答(?:一个)?(?:整数|数字|选项字母|字母)[。.!！]?$', '',text)
    # Compare question structure, not repeated answer-format wording or constants.
    return re.sub(r'\d+(?:\.\d+)?','#',text).replace('*','×')


def candidates(c,packet):
    return _scan(c,packet)[0]


def _scan(c,packet,*,executing=None):
    from .document_independent_check import _qualified,original_version
    item,payload,root,state,confirmed,teacher=packet
    if payload.get('independent_mapping') is not None:
        return None,None
    text=confirmed['confirmed_text']
    # A withdrawn exact reference is not an invitation to route around revocation.
    rows=c.execute('''SELECT d.diagnostic_item_id,d.prompt,n.subject FROM diagnostic_item d
        JOIN knowledge_node n USING(knowledge_node_id) JOIN child_profile cp ON cp.child_id=?
        WHERE n.subject=? AND cp.status='active'
          AND substr(n.grade_band,1,length(cp.grade))=cp.grade ORDER BY d.diagnostic_item_id''',(item['child_id'],teacher['subject'])).fetchall()
    if any(' '.join(r['prompt'].split())==' '.join(text.split()) for r in rows):
        return None,None
    from .document_target_clarification import query as clarified_query
    try:
        text=clarified_query(c,packet,executing=executing) or text
    except (ValueError,TypeError,KeyError,IndexError):
        return None,{'status':'clarification_changed'}
    vectorizer=HashingVectorizer(dimensions=2048)
    query_text=_retrieval_text(text)
    query=vectorizer.encode(query_text)
    grams=lambda s:{s[i:i+3] for i in range(max(0,len(s)-2))}
    query_grams=grams(query_text)
    entries=[]
    for row in rows:
        # Avoid hash collisions and generic short queries as the sole match evidence.
        source_text=_retrieval_text(row['prompt'])
        overlap=query_grams & grams(source_text)
        similarity=vectorizer.cosine(query,vectorizer.encode(source_text)) if len(overlap)>=3 else 0.0
        source=_qualified(c,item['child_id'],row['diagnostic_item_id'])
        if not source:
            continue
        option=dict(item_id=source['diagnostic_item_id'],item_version=_version(source),
            node_id=source['knowledge_node_id'],title=source['title'],similarity=round(similarity,6))
        # Administrative scope/qualification notes are not the learning content.
        context=source['title']+'\n'+source['prompt']
        entries.append((option,context))
    matcher=getattr(c,'local_semantic_matcher',None)
    semantic=matcher.scores(text,[context for _,context in entries]) if matcher and entries else {'status':'not_configured'}
    options=[]
    for i,(option,_) in enumerate(entries):
        score=semantic['scores'][i] if semantic['status']=='ready' else None
        if option['similarity']<.35 and (score is None or score<.60):
            continue
        option['semantic_similarity']=score
        option['similarity']=max(option['similarity'],score or 0)
        options.append(option)
    # Rank every qualified variant before choosing one representative per node.
    by_node={}
    for option in sorted(options,key=lambda r:(-r['similarity'],r['item_id'])):
        by_node.setdefault(option['node_id'],option)
    options=list(by_node.values())[:3]
    retrieval={**{k:v for k,v in semantic.items() if k!='scores'},
        'source_policy':'all_qualified_variants_v1','source_count':len(entries)}
    if not options:
        retrieval['status']=('no_qualified_sources' if not entries else
            'no_match' if semantic['status']=='ready' else semantic['status'])
        return None,retrieval
    proposal=dict(version=VERSION,original_version=original_version(state),options=options,
        retrieval=retrieval,status='proposed',alignment_verified=False,**goal.LOCAL)
    for field in ('clarifications','superseded_clarifications'):
        if packet[1].get(KEY,{}).get(field):
            proposal[field]=packet[1][KEY][field]
    return proposal,retrieval


def search_message(packet):
    from .document_independent_check import original_version
    search=packet[1].get(KEY,{})
    if not isinstance(search,dict) or search.get('status')!='unavailable':
        return None
    if search.get('original_version')!=original_version(packet[3]) or search.get('version')!=VERSION:
        return '原题依据发生了变化，需要重新核对。我没有采用上次查找的结论。'
    retrieval=search.get('retrieval')
    reason=retrieval.get('status') if isinstance(retrieval,dict) else None
    if not isinstance(reason,str):
        reason=None
    messages={
        'time_budget':'没能及时查完相关题型，没有把只查了一部分的结果当作结论。',
        'no_match':'查过现有的合格题源，还没有找到足够相近的题型。这不代表你不会做。',
        'no_qualified_sources':'当前年级和学科还没有经过核对、可用于这次独立检查的题源。',
        'input_limit':'题目内容暂时无法完整比较，我没有截掉一部分后猜测知识点。',
        'token_limit':'题目内容暂时无法完整比较，我没有截掉一部分后猜测知识点。',
        'clarification_changed':'前面的补充语音依据已变化，我没有继续使用。请重新完整说明这道题的查找目标，或先回原题。',
        'mapping_changed':'原题的题源对应关系已变化，需要重新核对，未用旧候选判断掌握。',
    }
    return ('上次查找时，'+messages.get(reason,'题型比较暂时不能使用，没有据此判断你的薄弱点。')+
        '原题和换题请求都已保留。你可以补充这道题要你求什么、给了什么条件；也可以让我再查一次，或先继续原题。')


def offer(c,packet,*,executing=None):
    from .document_independent_check import original_version
    history={k:packet[1][KEY][k] for k in ('clarifications','superseded_clarifications')
        if packet[1].get(KEY,{}).get(k)}
    proposal,retrieval=_scan(c,packet,executing=executing)
    if not proposal:
        if retrieval is None:
            if not history:
                packet[1].pop(KEY,None)
                return None
            retrieval={'status':'mapping_changed'}
        packet[1][KEY]=dict(version=VERSION,original_version=original_version(packet[3]),
            status='unavailable',retrieval=retrieval,alignment_verified=False,**history,**goal.LOCAL)
        return search_message(packet)
    packet[1][KEY]=proposal
    choices='；'.join(f'{i+1}，{r["title"]}' for i,r in enumerate(proposal['options']))
    return '我找到可能相关的学习目标：'+choices+'。这还不是确定的知识点判断。请说选第几项，或继续补充、纠正描述；也可以说都不对。选定后先用新题检查这一项。'


def readback(c,packet):
    """Revalidate the saved choices without reranking or issuing a question."""
    from .document_independent_check import _qualified,original_version
    from .document_target_clarification import query as clarified_query
    proposal=packet[1].get(KEY)
    try:
        goal.require(isinstance(proposal,dict) and proposal.get('status')=='proposed'
            and proposal.get('version')==VERSION and proposal.get('original_version')==original_version(packet[3])
            and proposal.get('alignment_verified') is False
            and all(proposal.get(k) is v for k,v in goal.LOCAL.items()),'DOCUMENT_TARGET_PROPOSAL_CHANGED')
        clarified_query(c,packet)
        options=proposal.get('options')
        goal.require(isinstance(options,list) and 1<=len(options)<=3,'DOCUMENT_TARGET_PROPOSAL_CHANGED')
        nodes=set()
        for option in options:
            goal.require(isinstance(option,dict),'DOCUMENT_TARGET_PROPOSAL_CHANGED')
            source=_qualified(c,packet[0]['child_id'],option['item_id'])
            goal.require(source and source['subject']==packet[5]['subject']
                and source['knowledge_node_id']==option['node_id'] and source['title']==option['title']
                and _version(source)==option['item_version'] and option['node_id'] not in nodes,
                'DOCUMENT_TARGET_SOURCE_CHANGED')
            nodes.add(option['node_id'])
        choices='；'.join(f'{i+1}，{r["title"]}' for i,r in enumerate(options))
        return True,('刚才保存的候选是：'+choices+'。这还不是确定的知识点判断。'
            '可以说选第几项，继续补充或纠正描述，也可以说都不对。没有选定之前不出题、不评分。')
    except (ValueError,TypeError,KeyError,IndexError):
        if not isinstance(proposal,dict):
            proposal={}
        packet[1][KEY]={**proposal,'version':VERSION,'original_version':original_version(packet[3]),
            'status':'unavailable','retrieval':{'status':'mapping_changed'},'alignment_verified':False,**goal.LOCAL}
        return False,'刚才候选的依据发生了变化，没有继续读回旧选项，也没有替你选题。原题和已有作答保留，可以重新查找目标或回到原题。'


def select(c,packet,turn,meaning):
    proposal=packet[1].get(KEY)
    goal.require(proposal and proposal['status']=='proposed','DOCUMENT_TARGET_PROPOSAL_CHANGED')
    try:
        current=candidates(c,packet)
    except (ValueError,TypeError,KeyError,IndexError):
        current=None
    if current!=proposal:
        proposal['status']='stale'
        return None
    index=0 if meaning=='start' and len(proposal['options'])==1 else {'select_1':0,'select_2':1,'select_3':2}.get(meaning)
    if index is None or index>=len(proposal['options']):
        return None
    proposal.update(status='selected',selected_index=index,confirmation_turn_id=turn['agent_turn_id'])
    return proposal['options'][index]


def resolved(c,packet):
    from .document_independent_check import _qualified,original_version
    from .agent_runtime import AgentRuntimeStore
    from .document_revisit import parse
    proposal=packet[1].get(KEY)
    if not proposal or proposal.get('status')!='selected':
        return None,None
    goal.require(proposal['version']==VERSION and proposal['original_version']==original_version(packet[3])
        and proposal['alignment_verified'] is False and all(proposal.get(k) is v for k,v in goal.LOCAL.items()),
        'DOCUMENT_TARGET_HYPOTHESIS_CHANGED')
    from .document_target_clarification import query as clarified_query
    clarified_query(c,packet)
    options,index=proposal.get('options'),proposal.get('selected_index')
    goal.require(isinstance(options,list) and type(index) is int and 0<=index<len(options),
        'DOCUMENT_TARGET_SELECTION_CHANGED')
    option=options[index]
    source=_qualified(c,packet[0]['child_id'],option['item_id'])
    goal.require(source and _version(source)==option['item_version'] and source['knowledge_node_id']==option['node_id']
        and source['subject']==packet[5]['subject'],'DOCUMENT_TARGET_SOURCE_CHANGED')
    turn=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=?',
        (proposal['confirmation_turn_id'],packet[0]['child_id'])).fetchone()
    goal.require(turn,'DOCUMENT_TARGET_CONFIRMATION_MISSING')
    action=json.loads(turn['action_json'])
    voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
        voice_input_id=action.get('voice_input_id'),transcript=turn['transcript'])
    goal.require(action.get('voice_input_binding')==AgentRuntimeStore.voice_capture_snapshot(voice),
        'DOCUMENT_TARGET_VOICE_CHANGED')
    journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
        (action.get('document_revisit_request'),)).fetchone()
    goal.require(journal and journal['status']=='completed' and journal['child_id']==turn['child_id']
        and journal['session_id']==turn['session_id'],'DOCUMENT_TARGET_CONFIRMATION_CHANGED')
    binding,decision=json.loads(journal['binding_json']),json.loads(journal['decision_json'])
    goal.require(binding['document_revisit']['commitment_id']==packet[0]['commitment_id']
        and binding['document_revisit']['phase']=='target_choice','DOCUMENT_TARGET_CONFIRMATION_CHANGED')
    from .document_revisit import parse_journal
    parsed=parse_journal(decision,turn['transcript'],binding['review_clock'])
    allowed={f'select_{proposal["selected_index"]+1}'} | ({'start'} if len(proposal['options'])==1 else set())
    goal.require(parsed==decision and decision['meaning'] in allowed,'DOCUMENT_TARGET_SELECTION_CHANGED')
    # The current dispatch may not have written its final receipt yet; its frozen
    # selected proposal is independently checked again on every later use.
    expected=action.get('document_target_selection')
    goal.require(expected==goal.digest(proposal),'DOCUMENT_TARGET_SELECTION_CHANGED')
    return source,dict(version=VERSION,proposal_hash=goal.digest(proposal),alignment_verified=False,
        confirmation_turn_id=turn['agent_turn_id'])
