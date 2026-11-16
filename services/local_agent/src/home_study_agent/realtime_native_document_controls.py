"""Typed native choices reuse durable document controls, never a classifier proof.

The existing request journal is a storage/receipt system. Native records use a
separate ID and contract, null confidence and no ModelGateway call. Source and
assessment evidence remain with the original document writers.
"""
import hashlib
import json
from types import SimpleNamespace

KEY='native_document_control_v1'
CAPABILITIES=frozenset(('learning.review_schedule','learning.review_revisit'))
SCHEDULE=('propose','confirm','cancel','status')
REVISIT=('start','answer','help','retry_answer','independent_check','finish_check','propose','confirm',
    'retry_confirm','retry_decline','cancel','status','select_1','select_2','select_3','resume_decline',
    'describe_target','revise_target','locate_revision','undo_revision','review_explanation')


def public_schema(capability):
    if capability not in CAPABILITIES:return None
    target={'anyOf':[{'type':'integer','minimum':1,'maximum':10000},{'type':'string','enum':['last']},{'type':'null'}]}
    location={'type':'object','properties':{'target':target,'target_quote':{'type':'string','minLength':1,'maxLength':2000}},
        'required':['target','target_quote'],'additionalProperties':False}
    time={'anyOf':[{'type':'null'}, {'type':'object','properties':{'kind':{'type':'string','enum':['relative']},
        'minutes':{'type':'integer','minimum':1,'maximum':129600}},'required':['kind','minutes'],'additionalProperties':False},
        {'type':'object','properties':{'kind':{'type':'string','enum':['calendar']},'date':{'type':'string','maxLength':10},
        'time':{'type':'string','maxLength':5}},'required':['kind','date','time'],'additionalProperties':False}]}
    props={'operation':{'type':'string','enum':list(SCHEDULE if capability.endswith('schedule') else REVISIT)},'time':time}
    if capability.endswith('revisit'):
        props.update(query={'type':'string','minLength':1,'maxLength':80},location=location,undo=location,
            revision={'type':'object','properties':{**location['properties'],'replacement_quote':{'type':'string','minLength':1,'maxLength':8000}},
                'required':['target','target_quote','replacement_quote'],'additionalProperties':False})
    return {'type':'object','properties':props,'required':['operation'],'additionalProperties':False}


def normalize(capability,args,transcript,anchor):
    from . import document_goal_handoff as goal
    from .document_review_schedule import parse as time_parse
    from .document_revisit import parse as revisit_parse
    allowed={'operation','time'} | ({'query','revision','location','undo'} if capability.endswith('revisit') else set())
    goal.require(type(args) is dict and not set(args)-allowed and args.get('operation') in
        (SCHEDULE if capability.endswith('schedule') else REVISIT),'NATIVE_DOCUMENT_ARGUMENTS_INVALID')
    raw={'meaning':args['operation'],'quote':transcript,'time':args.get('time'),'confidence':None}
    raw.update({k:args[k] for k in ('query','revision','location','undo') if k in args})
    parse=time_parse if capability.endswith('schedule') else revisit_parse
    value=parse(json.dumps(raw,ensure_ascii=False),transcript,anchor,native=True)
    return {**value,'request_kind':KEY,'contract':1,'native_arguments':args}


def parse_native_journal(capability,raw,transcript,anchor):
    from . import document_goal_handoff as goal
    goal.require(raw.get('request_kind')==KEY and type(raw.get('contract')) is int and raw['contract']==1
        and raw.get('confidence') is None,'NATIVE_DOCUMENT_CONTRACT_CHANGED')
    args=raw.get('native_arguments')
    goal.require(type(args) is dict,'NATIVE_DOCUMENT_CONTRACT_CHANGED')
    return normalize(capability,args,transcript,anchor)


def require_request(c,turn,capability,request,*,effect=False):
    """Current effect checks, or immutable readback of an already saved result."""
    from . import document_goal_handoff as goal
    from .realtime_capability_tools import KEY as PACKET,SOURCE,_hash,_immutable_capture
    from .realtime_native_context import _native_witness
    from .privacy_operation_receipt import generation
    action=json.loads(turn['action_json'])
    is_native=str(action.get('document_review_request') or action.get('document_revisit_request') or '').startswith('native-document-')
    if not is_native:
        goal.require(action.get('source')!=SOURCE,'NATIVE_DOCUMENT_CONTRACT_REQUIRED')
        return False
    goal.require(request and action.get('source')==SOURCE,'NATIVE_DOCUMENT_CONTRACT_REQUIRED')
    row=c.execute('''SELECT p.context_manifest_json,s.capability_name FROM agent_plan p JOIN agent_step s USING(plan_id)
        WHERE p.agent_turn_id=? AND s.step_index=0 AND p.child_id=? AND p.session_id=?''',
        (turn['agent_turn_id'],turn['child_id'],turn['session_id'])).fetchone()
    manifest=json.loads(row['context_manifest_json']) if row else {}
    witness=manifest.get(KEY);packet=manifest.get(PACKET)
    binding=json.loads(request['binding_json']);decision=json.loads(request['decision_json'])
    voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
        (action.get('voice_input_id'),turn['child_id'],turn['session_id'])).fetchone()
    capture=_native_witness(SimpleNamespace(page_source_store=None),c,voice) if voice else None
    expected=hashlib.sha256(json.dumps([turn['child_id'],turn['session_id'],turn['transcript']],ensure_ascii=False).encode()).hexdigest()
    goal.require(row and row['capability_name']==capability and type(witness) is dict and type(packet) is dict
        and witness.get('version')==1 and witness.get('capability')==packet.get('capability')==capability
        and witness.get('request_key')==request['request_key']=='native-document-'+turn['agent_turn_id']
        and request['fingerprint']==expected and request['status']=='completed'
        and (request['child_id'],request['session_id'])==(turn['child_id'],turn['session_id'])
        and decision.get('request_kind')==KEY and decision.get('contract')==1 and decision.get('confidence') is None
        and witness.get('decision_sha256')==_hash(decision) and witness.get('binding_sha256')==_hash(binding)
        and witness.get('storage_generation')==packet.get('storage_generation')==generation(c)
        and voice and voice['transcript']==turn['transcript'] and capture is not None
        and witness.get('voice_input_id')==packet['identity']['voice_input_id']==voice['voice_input_id']
        and witness.get('capture_sha256')==packet.get('capture_hash')==_hash(_immutable_capture(capture))
        and witness.get('transcript_sha256')==packet.get('original_transcript_sha256')==_hash(turn['transcript']),
        'NATIVE_DOCUMENT_SOURCE_CHANGED')
    goal.require(parse_native_journal(capability,decision,turn['transcript'],binding['review_clock'])==decision,
        'NATIVE_DOCUMENT_CHOICE_CHANGED')
    if effect or not request['execution_receipt_json']:
        from .realtime_native_client import require_claim
        require_claim(c,turn['agent_turn_id'])
    return True


def latest_voice(c,session,child):
    return c.execute("""SELECT voice_input_id FROM voice_input WHERE session_id=? AND child_id=?
        AND purpose IN ('agent_command','native_cloud_dialogue','household_local_transcript') AND status='completed'
        ORDER BY rowid DESC LIMIT 1""",(session,child)).fetchone()


def compose(c,session,voice,capture,capability,args,runtime,*,arrival_id=None,turn_id=None):
    if capability not in CAPABILITIES:return None
    from . import document_goal_handoff as goal
    from .realtime_native_special_sources import _capture
    from .realtime_capability_tools import _hash
    from .privacy_operation_receipt import generation
    from .repository import utc_now
    goal.require(c.in_transaction and turn_id and arrival_id,'NATIVE_DOCUMENT_WRITE_TRANSACTION_REQUIRED')
    actual,capture_hash=_capture(c,voice,runtime)
    from .realtime_capability_tools import _immutable_capture
    goal.require(_hash(_immutable_capture(capture))==capture_hash,'NATIVE_DOCUMENT_CAPTURE_CHANGED')
    goal.require((voice['child_id'],voice['session_id'])==(session['child_id'],session['session_id']),
        'NATIVE_DOCUMENT_OWNER_CHANGED')
    arrival=c.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=?',
        (arrival_id,session['child_id'],session['session_id'])).fetchone()
    goal.require(arrival and arrival['utterance']==voice['transcript'],'NATIVE_DOCUMENT_ARRIVAL_CHANGED')
    from . import document_review_schedule as schedule,document_revisit as revisit
    control=schedule if capability.endswith('schedule') else revisit
    target=control.focus(c,session['session_id'],session['child_id'],arrival['client_action_id'],native=True) if control is revisit else control.focus(c,session['session_id'],session['child_id'],arrival['client_action_id'])
    goal.require(target is not None,'NATIVE_DOCUMENT_FOCUS_REQUIRED')
    anchor=voice['created_at'];decision=normalize(capability,args,voice['transcript'],anchor)
    request_key='native-document-'+turn_id
    binding={'document_review' if control is schedule else 'document_revisit':target,'review_clock':anchor,
        'native_document_origin':{'kind':KEY,'contract':1,'voice_input_id':voice['voice_input_id']}}
    fingerprint=hashlib.sha256(json.dumps([session['child_id'],session['session_id'],voice['transcript']],ensure_ascii=False).encode()).hexdigest()
    now=utc_now()
    c.execute('''INSERT INTO voice_control_intent_request(request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
        VALUES(?,?,?,?,?,'completed',?,?,?)''',(request_key,session['child_id'],session['session_id'],fingerprint,
        json.dumps(binding,ensure_ascii=False),json.dumps(decision,ensure_ascii=False),now,now))
    action={'document_review_request' if control is schedule else 'document_revisit_request':request_key}
    manifest={KEY:{'version':1,'capability':capability,'request_key':request_key,'voice_input_id':voice['voice_input_id'],
        'storage_generation':generation(c),'transcript_sha256':_hash(voice['transcript']),'capture_sha256':capture_hash,
        'decision_sha256':_hash(decision),'binding_sha256':_hash(binding)}}
    if control is schedule:
        root,state=goal.validate(c,target['root_turn_id'],session['session_id'],incoming_action_id=arrival['client_action_id'])
        action['document_review_root']=root['agent_turn_id']
        manifest.update(document_review_request=request_key,document_review_focus=target)
        state['time_voice']={'turn_id':turn_id,'client_action_id':arrival['client_action_id'],'transcript':voice['transcript'],
            'voice_input_id':voice['voice_input_id'],'binding':runtime.voice_capture_snapshot(actual),'request_key':request_key}
        state['time_turn_id']=turn_id;goal.store(c,root,state)
        objective={'mode':'continue','type':'learning','summary':None,'success_criteria':[],'phase':'explain'}
    else:
        item={'commitment_id':None};prior={};packet=None
        if not target.get('memory_resume'):
            packet=revisit.retained(c,target['commitment_id'],session['child_id']);item=packet[0];prior=packet[1].get('practice',{})
        action['document_revisit_commitment']=item['commitment_id'];manifest['document_revisit']=target
        mode='continue' if prior.get('session_id')==session['session_id'] and prior.get('objective_id') else 'new'
        from .document_open_feedback import spoken_prompt
        preserve=mode=='continue' and packet is not None and spoken_prompt(c,packet) is not None
        objective={'mode':mode,'type':'learning','summary':'回顾已确认的原题',
            'success_criteria':[] if preserve else ['保留自己的解题过程并核对反馈'],'phase':'check'}
        meaning=decision['meaning']
        if packet and prior.get('phase')=='check_feedback':
            if meaning=='locate_revision':
                from .document_spoken_location import KEY as FIELD,prepare
                action[FIELD]=prepare(c,packet,decision['location'])
            elif meaning=='undo_revision':
                from .document_spoken_undo import KEY as FIELD,prepare
                action[FIELD]=prepare(c,packet,decision.get('undo'))
            elif meaning=='review_explanation':
                from .document_spoken_review import KEY as FIELD,prepare
                try:action[FIELD]=prepare(c,packet)
                except (ValueError,TypeError,KeyError):action[FIELD]={'status':'source_unavailable'}
        if packet and meaning=='independent_check':
            from .document_independent_check import plan
            action['document_independent_check']=plan(c,packet,session['session_id'])
        if packet and prior.get('phase')=='check_feedback' and meaning in ('retry_answer','answer'):
            from .document_spoken_retry import plan
            try:
                action['document_independent_check']=plan(c,packet,session['session_id'])
                if meaning=='answer':
                    from .document_spoken_supplement import KEY as FIELD,marker
                    from .document_spoken_revision import prepare as revision
                    action[FIELD]=marker(voice['transcript'],voice['voice_input_id'],action['document_independent_check'],
                        revision=revision(c,packet,decision.get('revision')))
                    action['source_voice_input_id']=voice['voice_input_id']
            except (ValueError,TypeError,KeyError):action['document_independent_check']={'status':'feedback_unavailable'}
    return {'arguments':{},'action':action,'manifest':manifest,'objective':objective}
