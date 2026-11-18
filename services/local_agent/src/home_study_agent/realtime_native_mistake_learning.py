"""Native formative mistake learning: concrete tool, original voice, saved effect.

The model chooses a bounded operation. The host alone resolves local identities
and injects the actual saved utterance. This grants no formal scoring authority.
"""
import json
from datetime import datetime
from types import SimpleNamespace

CAPABILITY='mistake.learn'
KEY='native_mistake_learning_v1'
OPERATIONS=('capture','analyze','practice','coach','answer','review_today','select')
EFFECT_ACTOR='native_mistake_learning_effect_v1'
SERVICE_OPERATIONS={'capture':'analyze','analyze':'analyze','practice':'practice','coach':'coach','answer':'answer','select':'focus'}
EFFECT_FIELDS=('action_id','mistake_id','source_revision','learning_session_id','revision')


def fail(code):
    from .agent_runtime import InvalidAgentPlanError
    raise InvalidAgentPlanError('NATIVE_MISTAKE_'+code)


def digest(value):
    from .realtime_capability_tools import _hash
    return _hash(value)


def public_schema():
    return {'type':'object','properties':{
        'operation':{'type':'string','enum':list(OPERATIONS)},
        'selector':{'anyOf':[{'type':'string','minLength':1,'maxLength':160},
                             {'type':'integer','minimum':1,'maximum':1000}]},
        'count':{'type':'integer','minimum':1,'maximum':3},
        'style':{'type':'string','enum':['direct','socratic']}},
        'required':['operation'],'additionalProperties':False}


def _owner(c,sid,child):
    row=c.execute('''SELECT d.status,p.status FROM daily_session d JOIN child_profile p USING(child_id)
        WHERE d.session_id=? AND d.child_id=?''',(sid,child)).fetchone()
    permit=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    if not row or any(value!='active' for value in row):fail('OWNER_CHANGED')
    if not permit or any(type(value) is not int or value!=1 for value in permit):fail('CONSENT_REQUIRED')


def _time(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError('timezone')
        return parsed
    except (ValueError,TypeError,AttributeError):fail('ONSET_REQUIRED')


def _onset(capture,snapshot,operation):
    # Selection/analyze can deliberately open a new case. A response or request
    # for practice must originate after the already selected learning material.
    if operation not in {'practice','coach','answer'}:return
    if operation=='practice' and snapshot and snapshot.get('selection') is True:return
    if not snapshot or snapshot.get('selection') is True:fail('FOCUS_REQUIRED')
    onset=_time(capture.get('speech_started_at'))
    if onset<_time(snapshot.get('focus_created_at')):fail('FOCUS_AFTER_VOICE')
    if snapshot.get('practice_item_id'):
        if onset<_time(snapshot.get('practice_created_at')):fail('PRACTICE_AFTER_VOICE')
    elif operation=='answer':fail('PRACTICE_REQUIRED')


def compose(c,session,voice,capture,args,*,turn_id,current_graph_version=None):
    from .mistake_learning import native_focus,native_target,validate_source
    from .realtime_native_contracts import conforms
    if not conforms(public_schema(),args):fail('ARGUMENTS_INVALID')
    sid=session['session_id'];child=session['child_id'];_owner(c,sid,child)
    operation=args['operation']
    if args.get('selector') is not None and operation not in {'analyze','practice','select'}:fail('SELECTOR_NOT_ALLOWED')
    if operation in {'capture','review_today'}:snapshot=None
    elif operation in {'coach','answer'} or operation=='practice' and args.get('selector') is None:
        snapshot=native_focus(c,sid,current_graph_version=current_graph_version)
    else:snapshot=native_target(c,sid,args.get('selector'),operation=operation,current_graph_version=current_graph_version)
    if snapshot is None and operation not in {'capture','review_today'}:fail('FOCUS_REQUIRED')
    if snapshot is not None:
        if type(args.get('selector')) is str and args['selector']==snapshot.get('mistake_id'):fail('SELECTOR_MUST_BE_TITLE_OR_ORDINAL')
        if snapshot.get('child_id')!=child or snapshot.get('session_id')!=sid:fail('OWNER_CHANGED')
        if validate_source(c,sid,snapshot,operation=operation,current_graph_version=current_graph_version) is not True:fail('SOURCE_CHANGED')
    if operation in {'coach','answer'}:
        from .realtime_native_mistake_onset import require_current
        snapshot = require_current(c,voice,snapshot,allow_original=operation=='coach',select_scope=True,current_graph_version=current_graph_version)
    _onset(capture,snapshot,operation)
    normalized={**args,'count':args.get('count',3),'style':args.get('style','socratic')}
    action={**normalized,'utterance':voice['transcript'],'mistake_learning_source':snapshot,
            'mistake_learning_action_id':turn_id+':mistake:learn'}
    witness={'version':1,'operation':operation,'source':snapshot,'utterance_sha256':digest(voice['transcript']),
             'arguments_sha256':digest(normalized),'action_fields_sha256':digest(action),
             'speech_started_at':capture.get('speech_started_at')}
    return {'arguments':normalized,'action':action,'manifest':{KEY:witness}}


def _row(c,turn_id):
    row=c.execute('''SELECT t.*,p.context_manifest_json,s.capability_name,s.arguments_json FROM agent_turn t
        JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
    if not row or row['capability_name']!=CAPABILITY:fail('TURN_REQUIRED')
    return row


def validate(c,turn_id,*,require_claim=True,current_graph_version=None):
    """Pure current-source guard; caller retains its signed execution/write scope."""
    from .realtime_native_context import _native_witness
    from .mistake_learning import validate_source
    row=_row(c,turn_id);action=json.loads(row['action_json']);manifest=json.loads(row['context_manifest_json'])
    witness=manifest.get(KEY);packet=manifest.get('qwen_realtime_capability_v1');args=json.loads(row['arguments_json'])
    if not witness or not packet or packet.get('capability')!=CAPABILITY:fail('WITNESS_REQUIRED')
    if (packet.get('native_sources_sha256')!=digest({key:manifest.get(key) for key in packet.get('native_manifest_keys',[])})
        or KEY not in packet.get('native_manifest_keys',[]) or packet.get('arguments_sha256')!=digest(args)
        or action.get('source')!='qwen_realtime_capability' or action.get('type')!='learn_mistake'
        or action.get('mastery_update_allowed') is not False or action.get('independent_evidence') is not False):fail('WITNESS_CHANGED')
    voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
        (packet['identity']['voice_input_id'],row['child_id'],row['session_id'])).fetchone()
    if not voice or voice['transcript']!=row['transcript']:fail('VOICE_CHANGED')
    capture=_native_witness(SimpleNamespace(page_source_store=None),c,voice)
    if capture is None:fail('VOICE_CHANGED')
    _owner(c,row['session_id'],row['child_id'])
    original={**args,'utterance':voice['transcript'],'mistake_learning_source':witness['source'],
              'mistake_learning_action_id':turn_id+':mistake:learn'}
    if (witness.get('version')!=1 or witness.get('operation')!=args.get('operation')
        or witness.get('arguments_sha256')!=digest(args) or witness.get('utterance_sha256')!=digest(voice['transcript'])
        or witness.get('speech_started_at')!=capture.get('speech_started_at')
        or witness.get('action_fields_sha256')!=digest(original)
        or any(action.get(key)!=value for key,value in original.items())):fail('WITNESS_CHANGED')
    snapshot=witness['source']
    if snapshot is not None and validate_source(c,row['session_id'],snapshot,operation=args['operation'],current_graph_version=current_graph_version) is not True:fail('SOURCE_CHANGED')
    if args['operation'] in {'coach','answer'}:
        from .realtime_native_mistake_onset import require_current
        completed=_service_receipt(c,row['session_id'],turn_id+':mistake:learn')
        own_progress=bool(completed and completed.get('status')=='completed'
            and completed.get('operation')==SERVICE_OPERATIONS[args['operation']]
            and completed.get('mistake_id')==(snapshot or {}).get('mistake_id'))
        require_current(c,voice,snapshot,allow_own_progress=own_progress,
                        allow_original=args['operation']=='coach' and snapshot.get('practice_item_id') is None,
                        current_graph_version=current_graph_version)
    _onset(capture,snapshot,args['operation'])
    if require_claim:
        from .realtime_native_client import require_claim as claimed
        if claimed(c,turn_id) is not True:fail('CLAIM_REQUIRED')
    return {'turn_id':turn_id,'child_id':row['child_id'],'session_id':row['session_id'],**original}


def _effect(c,turn_id):
    row=c.execute('SELECT actor,action,child_id,summary_json FROM audit_event WHERE audit_id=?',
                  ('native-mistake-learning-effect-'+turn_id,)).fetchone()
    if not row or row['actor']!=EFFECT_ACTOR or row['action']!='mistake_learning_saved':fail('EFFECT_NOT_SAVED')
    try:value=json.loads(row['summary_json'])
    except (ValueError,TypeError):fail('EFFECT_CHANGED')
    turn=_row(c,turn_id)
    if row['child_id']!=turn['child_id'] or value.get('turn_id')!=turn_id or value.get('child_id')!=turn['child_id'] or value.get('session_id')!=turn['session_id']:fail('EFFECT_CHANGED')
    return value


def _service_receipt(c,sid,action_id):
    from .mistake_learning import native_receipt
    value=native_receipt(c,sid,action_id)
    if value is None:return None
    return {**value,'action_id':value.get('client_action_id')}


def record_actual_effect(c,turn_id,result):
    """Called only after the original signed executor awaited a durable result."""
    from .mistake_learning import native_focus
    from .realtime_capability_tools import _now,_json
    if not c.in_transaction:fail('TRANSACTION_REQUIRED')
    source=validate(c,turn_id)
    receipt=None if source['operation']=='review_today' else _service_receipt(c,source['session_id'],source['mistake_learning_action_id'])
    if source['operation']=='review_today':
        # Root's signed executor performed the actual library read. It is not a
        # request to create practice or a fabricated model-learning action.
        if type(result) is not dict or result.get('child_id')!=source['child_id'] or type(result.get('items')) is not list:
            fail('LIBRARY_READ_REQUIRED')
        today=result.get('today') or {};due=today.get('due_count')
        if type(due) is not int or due<0 or due>len(result['items']):fail('LIBRARY_READ_REQUIRED')
        receipt={'operation':'review_today','status':'completed','action_id':source['mistake_learning_action_id'],
                 'mistake_id':None,'source_revision':None,'learning_session_id':None,'revision':None,'focus_generation':None,'practice_item_id':None}
    else:
        if (not receipt or receipt.get('status')!='completed' or receipt.get('action_id')!=source['mistake_learning_action_id']
            or receipt.get('operation')!=SERVICE_OPERATIONS[source['operation']]):fail('EFFECT_NOT_COMPLETED')
    if source['mistake_learning_source'] is not None and receipt.get('mistake_id')!=source['mistake_learning_source']['mistake_id']:fail('EFFECT_TARGET_CHANGED')
    if type(result) is not dict:fail('EFFECT_NOT_COMPLETED')
    if source['operation']!='review_today' and (result.get('mistake_id')!=receipt['mistake_id'] or result.get('revision')!=receipt['revision']
            or result.get('learning_session_id')!=receipt['learning_session_id'] or result.get('action_status')!='completed'):
        fail('EFFECT_RESULT_CHANGED')
    # The provider may return formative feedback; never save it as a grade or
    # mirror the question/answer into an operational audit record.
    value={'version':1,'turn_id':turn_id,'child_id':source['child_id'],'session_id':source['session_id'],
           'operation':source['operation'],'service_operation':receipt['operation'],
           **{key:receipt.get(key) for key in EFFECT_FIELDS}}
    if source['operation']=='review_today':value['due_count']=due
    else:
        focus=native_focus(c,source['session_id'])
        if not focus or focus['mistake_id']!=receipt['mistake_id'] or focus['source_revision']!=receipt['source_revision']:fail('EFFECT_TARGET_CHANGED')
        value['focus_generation']=focus['focus_generation']
        value['practice_item_id']=(source['mistake_learning_source'] or {}).get('practice_item_id')
    aid='native-mistake-learning-effect-'+turn_id
    prior=c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?',(aid,)).fetchone()
    if prior and (prior['actor']!=EFFECT_ACTOR or json.loads(prior['summary_json'])!=value):fail('EFFECT_CHANGED')
    c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING',
              (aid,EFFECT_ACTOR,'mistake_learning_saved',source['child_id'],_json(value),_now()))
    return {'mistake_learning_effect_id':aid,'view_rendered':True}


def verify_actual_effect(c,turn_id,output):
    if type(output) is not dict or output!={'mistake_learning_effect_id':'native-mistake-learning-effect-'+turn_id,'view_rendered':True}:fail('RENDER_RECEIPT_REQUIRED')
    value=_effect(c,turn_id)
    actual=None if value['operation']=='review_today' else _service_receipt(c,value['session_id'],value['action_id'])
    if value['operation']=='review_today':
        if value['service_operation']!='review_today' or value['mistake_id'] is not None or type(value.get('due_count')) is not int:fail('EFFECT_CHANGED')
    elif (not actual or actual.get('status')!='completed' or actual.get('operation')!=SERVICE_OPERATIONS.get(value['operation'])
          or value['service_operation']!=actual.get('operation')
          or any(actual.get(key)!=value.get(key) for key in EFFECT_FIELDS)):fail('EFFECT_CHANGED')
    return value


def validate_completion(c,turn,action,output):
    if action.get('type')!='learn_mistake':fail('TURN_REQUIRED')
    verify_actual_effect(c,turn['agent_turn_id'],output)


def completion_facts(c,row,output):
    try:value=verify_actual_effect(c,row['agent_turn_id'],output)
    except ValueError:return {'mistake_learning_saved':False}
    facts={'mistake_learning_saved':True,'mistake_learning_operation':value['operation'],
           'formative_only':True,'formal_mastery_updated':False}
    if value['operation']=='review_today':facts['due_count']=value['due_count']
    return facts


def context_projection(c,sid,*,names=(),current_graph_version=None,route_identity=None,graph_cloud_authorizer=None):
    """Current authorized lesson content only; no IDs, practice key or answer."""
    from .mistake_learning import native_context,MistakeLearningError
    from .cloud_privacy import sanitize_cloud_text,sensitive_categories
    try:raw=native_context(c,sid,current_graph_version=current_graph_version,route_identity=route_identity,graph_cloud_authorizer=graph_cloud_authorizer)
    except MistakeLearningError as error:
        if error.code in {'MISTAKE_LEARNING_SOURCE_CHANGED','MISTAKE_LEARNING_FOCUS_CHANGED','MISTAKE_LEARNING_PRACTICE_CHANGED'}:return None
        raise
    if not raw:return None
    focus=raw.get('focus') or {}
    _owner(c,sid,focus.get('child_id'))
    budget=10000
    truncated=False
    def text(value,limit=2000):
        nonlocal budget,truncated
        if type(value) is not str or sensitive_categories(value):return None
        clean=sanitize_cloud_text(value,names).text
        value=clean[:min(limit,budget)]
        truncated=truncated or len(value)<len(clean)
        budget-=len(value)
        return value or None
    source=raw.get('source') or {}
    public={'formative_only':True,'formal_mastery_updated':False}
    for key,limit in (('subject',80),('title',160),('question',5000),('original_answer',2000)):
        value=text(source.get(key),limit)
        if value:public[key]=value
    analysis=raw.get('analysis') or {};safe={}
    if type(analysis) is dict:
        summary=text(analysis.get('summary'))
        if summary:safe['summary']=summary
        for key,fields in (('wrong_causes',('label','explanation')),('knowledge_points',('label','description'))):
            items=analysis.get(key)
            if type(items) is list:
                allowed=[]
                for item in items[:6]:
                    if type(item) is not dict:continue
                    entry={field:value for field in fields if (value:=text(item.get(field),500))}
                    if entry:allowed.append(entry)
                if allowed:safe[key]=allowed
        strategies=analysis.get('correction_strategy')
        if type(strategies) is list:
            allowed=[value for item in strategies[:5] if (value:=text(item,500))]
            if allowed:safe['correction_strategy']=allowed
    if safe:public['analysis']=safe
    practice=raw.get('practice') or {}
    if type(practice) is dict:
        question=text(practice.get('question'),3000)
        if question:public['practice']={'question':question}
    if truncated:public['content_truncated']=True
    return _fit_projection('mistake_learning',public,3000) if public.get('question') else None


def review_context_projection(c,sid,*,names=(),current_graph_version=None,route_identity=None):
    """Today's finite due reminder also works without opening/focusing the book."""
    import re
    from .mistake_learning import native_due_context
    from .cloud_privacy import sanitize_cloud_text,sensitive_categories
    owner=c.execute('SELECT child_id FROM daily_session WHERE session_id=?',(sid,)).fetchone()
    if not owner:fail('OWNER_CHANGED')
    _owner(c,sid,owner['child_id'])
    value=native_due_context(c,sid,current_graph_version=current_graph_version,route_identity=route_identity)
    if value is None:return None
    if (type(value) is not dict or type(value.get('date')) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value['date'])
        or type(value.get('due_count')) is not int or not 0<=value['due_count']<=10000
        or type(value.get('remaining_questions')) is not int or not 0<=value['remaining_questions']<=30000
        or type(value.get('items')) is not list):return None
    items=[]
    for item in value['items'][:3]:
        if type(item) is not dict or type(item.get('remaining_count')) is not int or not 0<=item['remaining_count']<=3:continue
        safe={'remaining_count':item['remaining_count']}
        for key,limit in (('title',160),('subject',40)):
            content=item.get(key)
            if type(content) is str and not sensitive_categories(content):
                safe[key]=sanitize_cloud_text(content,names).text[:limit]
        if safe.get('title'):items.append(safe)
    return _fit_projection('mistake_review_today',{'date':value['date'],'due_count':value['due_count'],'remaining_questions':value['remaining_questions'],'items':items},900)


def _projection_wire_bytes(key,value):
    # The serializer embeds a JSON facts string inside session instructions.
    # Count that second escaping as well as ordinary UTF-8 JSON, conservatively.
    encoded=json.dumps({key:value},ensure_ascii=False,separators=(',',':'))
    return len(json.dumps(encoded,ensure_ascii=False,separators=(',',':')).encode('utf-8'))


def _fit_projection(key,value,maximum):
    """Bound content summaries only. Full original stays in the local service."""
    import copy
    value=copy.deepcopy(value)
    if _projection_wire_bytes(key,value)<=maximum:return value
    value['content_truncated']=True
    def strings(node,path=()):
        if type(node) is dict:
            for field,item in node.items():yield from strings(item,(*path,field))
        elif type(node) is list:
            for index,item in enumerate(node):yield from strings(item,(*path,index))
        elif type(node) is str:yield path,node
    while _projection_wire_bytes(key,value)>maximum:
        path,content=max(strings(value),key=lambda item:len(item[1].encode('utf-8')))
        raw=content.encode('utf-8')
        excess=_projection_wire_bytes(key,value)-maximum
        kept=raw[:max(0,len(raw)-max(excess,32))].decode('utf-8',errors='ignore')
        target=value
        for field in path[:-1]:target=target[field]
        target[path[-1]]=kept
    return value
