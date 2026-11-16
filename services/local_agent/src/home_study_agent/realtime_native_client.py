"""Durable native host effects. Reads never dispatch and claims never retry.

Only the Main-signed client operation can enter this module. Existing device
receipt validators and the existing UI handlers remain the effect authority.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import inspect,json

KEY='native_client_dispatch_v1'
RETIRABLE=frozenset(('profile.switch','privacy.pause_all','mistake.open','reflection.complete'))
_completion=ContextVar('native_client_completion',default=None)
_claim_fences={}

def register_claim_fence(database,current):
    _claim_fences[str(database.path.resolve())]=current


def current_native_host(c):
    from .original_connection_read import original_connection
    callback = _claim_fences.get(getattr(original_connection(c), 'native_database_path', ''))
    return callback() if callback is not None else None


def required(location,capability):
    return capability!='learning.prepare_request'


def audit(c,key):
    row=c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?',(key,)).fetchone()
    return json.loads(row[0]) if row else None


def claim_key(turn):return 'native-client-claim-'+turn

def complete_key(turn):return 'native-client-complete-'+turn


@contextmanager
def completion_scope(check):
    token=_completion.set(check)
    try:yield
    finally:_completion.reset(token)


def check_completion(c,turn_id):
    row=c.execute('''SELECT t.action_json,s.execution_location,s.capability_name FROM agent_turn t
        JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
    if not row or json.loads(row['action_json']).get('source')!='qwen_realtime_capability':return
    if row['execution_location'] not in {'client_device_bridge','local_model'}:return
    if row['capability_name']=='learning.evaluate_response':
        from .realtime_native_write_scope import active
        from .realtime_native_authority import _execution_authorizer
        if active() and _execution_authorizer.get() is not None:
            # This local_model capability is actually owned by the existing
            # backend grader, not a renderer-provided evaluation result.
            require_claim(c,turn_id)
            return
    check=_completion.get()
    if check is None or check(c,turn_id) is not True:
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError('NATIVE_CLIENT_COMPLETION_NOT_AUTHORIZED')


def dispatch(service,c,row):
    from .agent_orchestrator import AgentOrchestrator
    packet=service._packet(c,row)
    return {'version':1,'dispatch_id':'native-dispatch-'+service.digest([row['agent_turn_id'],packet['fingerprint']]),
        'action_id':row['client_action_id'],'turn':AgentOrchestrator._get(c,row['agent_turn_id']),'status':'pending'}


def status(service,c,row,status):
    step=c.execute('SELECT s.execution_location,s.capability_name FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0',(row['agent_turn_id'],)).fetchone()
    if not required(step['execution_location'],step['capability_name']):return status
    done=audit(c,complete_key(row['agent_turn_id']))
    if done:return done['status']
    if status=='failed':return 'failed'
    if audit(c,claim_key(row['agent_turn_id'])):return 'unknown'
    return 'pending'


def _owned(service,c,sid,value,*,completion=False):
    from .realtime_capability_tools import _identity,_hash,_immutable_capture,_fail
    from .privacy_operation_receipt import generation
    identity=_identity(value)
    row=c.execute('SELECT * FROM agent_turn WHERE session_id=? AND client_action_id=?',(sid,'qwen-tool-'+_hash([sid,identity]))).fetchone()
    packet=service._packet(c,row) if row else None
    if not packet or packet['identity']!=identity:_fail('REALTIME_CLIENT_DISPATCH_NOT_FOUND')
    phase=value.get('phase');retired=value.get('completion_after_retirement') is True
    if retired and (not completion or phase!='complete' or packet['capability'] not in RETIRABLE):_fail('REALTIME_CLIENT_RETIREMENT_DENIED')
    session,voice,capture=service._scope(c,sid,value,require_permission=not retired,require_owner=not retired)
    if (packet['storage_generation']!=generation(c) or packet['capture_hash']!=_hash(_immutable_capture(capture))
        or packet['original_transcript_sha256']!=_hash(voice['transcript'])):_fail('REALTIME_CAPABILITY_INPUT_CHANGED')
    target=dispatch(service,c,row)
    if target['dispatch_id']!=value.get('dispatch_id'):_fail('REALTIME_CLIENT_DISPATCH_CHANGED')
    step=c.execute('SELECT * FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?) AND step_index=0',(row['agent_turn_id'],)).fetchone()
    if not required(step['execution_location'],step['capability_name']):_fail('REALTIME_CLIENT_DISPATCH_NOT_CLIENT')
    action=json.loads(row['action_json'])
    if (packet['capability']!=step['capability_name'] or _hash(json.loads(step['arguments_json']))!=packet['arguments_sha256']
        or _hash({k:action.get(k) for k in ('type','source','voice_input_id','voice_input_binding','capability','execution_location','mastery_update_allowed','independent_evidence')})!=packet['action_authority_sha256']):_fail('REALTIME_CAPABILITY_TURN_CHANGED')
    return row,packet,target,step,retired


def _save(service,c,key,kind,child,value):
    from .realtime_capability_tools import _json,_now
    c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?)',(key,KEY,kind,child,_json(value),_now()))


async def run(service,sid,value):
    from .realtime_capability_tools import IDENTITY,_json,_hash,_fail
    if type(value) is not dict or value.get('phase') not in {'claim','execute','complete'}:_fail('REALTIME_CLIENT_PHASE_INVALID')
    allowed=set(IDENTITY)|{'version','dispatch_id','phase','input_source','completion_after_retirement'}
    if value['phase']=='complete':allowed|={'final_response','tool_status','tool_error_code','result'}
    if value['phase']=='complete' and (value.get('result') is not None and type(value['result']) is not dict or value.get('tool_error_code') is not None and type(value['tool_error_code']) is not str):_fail('REALTIME_CLIENT_OUTCOME_INVALID')
    if set(value)-allowed or len(_json(value))>120000:_fail('REALTIME_CLIENT_REQUEST_INVALID')
    if 'completion_after_retirement' in value and value['completion_after_retirement'] is not True:_fail('REALTIME_CLIENT_RETIREMENT_DENIED')
    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row,packet,target,step,retired=_owned(service,c,sid,value,completion=value['phase']=='complete')
        turn=row['agent_turn_id'];claim=audit(c,claim_key(turn));done=audit(c,complete_key(turn))
        if value['phase']=='complete' and step['capability_name']=='reflection.complete' and (retired or value.get('tool_status')=='completed'):
            validate_reflection_completion(c,row,value)
        if value['phase']=='claim':
            if claim or done:_fail('REALTIME_CLIENT_ALREADY_CLAIMED')
            require_resolved_chain(service,c,sid,row['child_id'],packet['identity']['voice_input_id'],exclude=turn)
            # Full original source/target authority is checked before the first effect.
            service.validate_turn(c,turn)
            from .realtime_capability_http import native_client_host_owner
            _save(service,c,claim_key(turn),'client_claim',row['child_id'],{'dispatch_id':target['dispatch_id'],'identity_sha256':_hash(packet['identity']),'action_id':row['client_action_id'],'host_owner':native_client_host_owner()})
            c.commit();return {'version':1,'status':'claimed','client_dispatch':target,'completion_descriptor':completion_descriptor(service,c,row)}
        if not claim or claim['dispatch_id']!=target['dispatch_id'] or claim['identity_sha256']!=_hash(packet['identity']):_fail('REALTIME_CLIENT_CLAIM_REQUIRED')
        if value['phase']=='execute':
            from .realtime_capability_http import native_client_host_owner
            if claim.get('host_owner')!=native_client_host_owner():_fail('REALTIME_CLIENT_EXECUTION_OWNER_CHANGED')
            service.validate_turn(c,turn)
            if done:_fail('REALTIME_CLIENT_ALREADY_COMPLETED')
            key='native-client-execute-'+turn
            if audit(c,key):_fail('REALTIME_CLIENT_OUTCOME_UNKNOWN')
            _save(service,c,key,'client_execute',row['child_id'],{'dispatch_id':target['dispatch_id']});c.commit()
        else:
            fingerprint=_hash({k:value.get(k) for k in ('final_response','tool_status','tool_error_code','result')})
            if done:
                if done['fingerprint']!=fingerprint:_fail('REALTIME_CLIENT_COMPLETION_CONFLICT')
                answer=service._receipt(c,row,replayed=True,retired=retired);answer['host_turn']=target['turn'];c.commit();return answer
            if value.get('tool_status') not in {'completed','failed'} or type(value.get('final_response')) is not str or not value['final_response'].strip():_fail('REALTIME_CLIENT_OUTCOME_INVALID')
            if step['execution_location']=='server' or step['capability_name']=='learning.evaluate_response':
                # Server review and UI opening are separate facts. No server work is repeated.
                result=value.get('result') or {};output=(result.get('execution_receipt') or result.get('client_bridge_receipt') or {}).get('output') or {}
                bridge=result.get('execution_receipt') or result.get('client_bridge_receipt') or {}
                if (bridge.get('capability')!=step['capability_name'] or bridge.get('execution_location')!='client_device_bridge'
                    or bridge.get('action_type')!=target['turn']['action']['type'] or bridge.get('status')!=value['tool_status']
                    or type(bridge.get('completed_at')) is not str or type(output) is not dict):_fail('REALTIME_CLIENT_DELIVERY_RECEIPT_REQUIRED')
                from datetime import datetime,timezone
                try:
                    completed=datetime.fromisoformat(bridge['completed_at'].replace('Z','+00:00'))
                    if completed.tzinfo is None or abs((datetime.now(timezone.utc)-completed).total_seconds())>180:raise ValueError()
                except (ValueError,TypeError):_fail('REALTIME_CLIENT_DELIVERY_RECEIPT_REQUIRED')
                if step['capability_name']=='mistake.open' and value['tool_status']=='completed' and (output.get('workspace_opened') is not True or output.get('workspace')!='mistake_book'
                    or output.get('child_id')!=row['child_id'] or output.get('session_id')!=sid or output.get('content_status') not in {'ready','empty'}):_fail('REALTIME_CLIENT_WORKSPACE_RECEIPT_REQUIRED')
                if not (step['status'] in {'completed','waiting_child'} and step['result_json'] or step['status']=='failed' and value['tool_status']=='failed'):_fail('REALTIME_CLIENT_SERVER_RECEIPT_REQUIRED')
                _save(service,c,complete_key(turn),'client_complete',row['child_id'],{'fingerprint':fingerprint,'status':value['tool_status'],'output':output,'local_delivery':_outcome_delivery(value)})
                answer=service._receipt(c,row,retired=retired);answer['host_turn']=target['turn'];c.commit();return answer
            c.commit()
    if value['phase']=='execute':
        if service.execute_turn is None:_fail('REALTIME_CAPABILITY_EXECUTOR_NOT_CONNECTED')
        from .realtime_native_authority import native_execution_scope
        from .realtime_native_write_scope import native_write_scope
        with native_execution_scope(service.authorize), native_write_scope(lambda c:service.validate_execution_scope(c,turn)):
            result=service.execute_turn(turn)
            if inspect.isawaitable(result):result=await result
        answer=service.receipt(sid,row['client_action_id']);answer['host_execution']=result;return answer
    if service.complete_turn is None:_fail('REALTIME_CLIENT_COMPLETER_NOT_CONNECTED')
    def check(c,check_turn):
        actual,_,_,_,_=_owned(service,c,sid,value,completion=True)
        if actual['agent_turn_id']!=check_turn or audit(c,claim_key(check_turn)) is None:return False
        previous=audit(c,complete_key(check_turn))
        if previous and previous['fingerprint']!=fingerprint:_fail('REALTIME_CLIENT_COMPLETION_CONFLICT')
        if not previous:_save(service,c,complete_key(check_turn),'client_complete',row['child_id'],{'fingerprint':fingerprint,'status':value['tool_status'],'local_delivery':_outcome_delivery(value)})
        return True
    with completion_scope(check):
        host_turn=service.complete_turn(turn,value['final_response'],value['tool_status'],value.get('tool_error_code'),value.get('result'))
        if inspect.isawaitable(host_turn):host_turn=await host_turn
    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE');actual,_,_,_,_=_owned(service,c,sid,value,completion=True)
        previous=audit(c,complete_key(turn))
        if previous and previous['fingerprint']!=fingerprint:_fail('REALTIME_CLIENT_COMPLETION_CONFLICT')
        if not previous:_save(service,c,complete_key(turn),'client_complete',row['child_id'],{'fingerprint':fingerprint,'status':value['tool_status'],'local_delivery':_outcome_delivery(value)})
        answer=service._receipt(c,actual,retired=retired);answer['host_turn']=host_turn;c.commit();return answer


def require_claim(c,turn_id,*,allow_planned=False):
    from .realtime_capability_tools import KEY as PACKET,_hash,_fail,_immutable_capture
    from .agent_runtime import AgentRuntimeStore,InvalidAgentPlanError
    def _fail(code):raise InvalidAgentPlanError(code)
    from .privacy_operation_receipt import generation
    row=c.execute('''SELECT t.*,p.context_manifest_json,s.capability_name,s.arguments_json,s.execution_location
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
    if not row or json.loads(row['action_json']).get('source')!='qwen_realtime_capability':return None
    manifest=json.loads(row['context_manifest_json']);packet=manifest.get(PACKET);claim=audit(c,claim_key(turn_id))
    if not packet or not claim or claim.get('identity_sha256')!=_hash(packet['identity']) or audit(c,complete_key(turn_id)) and _completion.get() is None:
        _fail('REALTIME_CLIENT_CLAIM_REQUIRED')
    expected='native-dispatch-'+_hash([turn_id,packet['fingerprint']])
    if claim.get('dispatch_id')!=expected:_fail('REALTIME_CLIENT_DISPATCH_CHANGED')
    if _completion.get() is not None:
        if _completion.get()(c,turn_id) is not True:_fail('REALTIME_CLIENT_COMPLETION_NOT_AUTHORIZED')
        return True
    current=current_native_host(c)
    if not claim.get('host_owner') or current!=claim['host_owner']:_fail('REALTIME_CLIENT_EXECUTION_OWNER_CHANGED')
    owner=c.execute('SELECT d.status,p.status FROM daily_session d JOIN child_profile p USING(child_id) WHERE d.session_id=? AND d.child_id=?',(row['session_id'],row['child_id'])).fetchone()
    consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',(packet['identity']['voice_input_id'],row['child_id'],row['session_id'])).fetchone()
    if not owner or any(x!='active' for x in owner) or not consent or any(type(x) is not int or x!=1 for x in consent) or not voice:_fail('REALTIME_CLIENT_EXECUTION_SCOPE_CHANGED')
    capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=row['session_id'],child_id=row['child_id'],voice_input_id=voice['voice_input_id'],transcript=voice['transcript'])
    action=json.loads(row['action_json'])
    if (packet['storage_generation']!=generation(c) or packet['capability']!=row['capability_name']
        or packet['arguments_sha256']!=_hash(json.loads(row['arguments_json'])) or packet['original_transcript_sha256']!=_hash(voice['transcript'])
        or packet['capture_hash']!=_hash(_immutable_capture(capture)) or action.get('voice_input_id')!=voice['voice_input_id']
        or packet['action_authority_sha256']!=_hash({k:action.get(k) for k in ('type','source','voice_input_id','voice_input_binding','capability','execution_location','mastery_update_allowed','independent_evidence')})):
        _fail('REALTIME_CAPABILITY_INPUT_CHANGED')
    return True


def completion_descriptor(service,c,row):
    packet=service._packet(c,row)
    step=c.execute('SELECT capability_name FROM agent_step JOIN agent_plan USING(plan_id) WHERE agent_turn_id=? AND step_index=0',(row['agent_turn_id'],)).fetchone()
    identity={'version':1,**packet['identity']}
    if packet.get('input_source') is not None:identity['input_source']=packet['input_source']
    return {'version':1,'dispatch_id':'native-dispatch-'+service.digest([row['agent_turn_id'],packet['fingerprint']]),
        'action_id':row['client_action_id'],'agent_turn_id':row['agent_turn_id'],'capability_name':step['capability_name'],
        'action_type':json.loads(row['action_json'])['type'],'session_id':row['session_id'],'child_id':row['child_id'],
        'storage_generation':packet['storage_generation'],'identity':identity}


def pending_completions(service,c,session_id,child_id):
    consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    owner=c.execute('SELECT d.status,p.status FROM daily_session d JOIN child_profile p USING(child_id) WHERE d.session_id=? AND d.child_id=?',(session_id,child_id)).fetchone()
    if not owner or any(value!='active' for value in owner) or not consent or any(type(value) is not int or value!=1 for value in consent):return []
    rows=[]
    for terminal in (False,True):
        predicate='EXISTS' if terminal else 'NOT EXISTS'
        rows.extend(c.execute("SELECT t.* FROM agent_turn t WHERE t.session_id=? AND t.child_id=? AND EXISTS(SELECT 1 FROM audit_event a WHERE a.audit_id='native-client-claim-'||t.agent_turn_id) AND "+predicate+"(SELECT 1 FROM audit_event a WHERE a.audit_id='native-client-complete-'||t.agent_turn_id) ORDER BY t.rowid DESC LIMIT 32",(session_id,child_id)).fetchall())
    result=[]
    for row in rows:
        try:
            # Same original read/projection gate as native context. It grants
            # no new effect and never restores a client_dispatch packet.
            service._receipt(c,row,replayed=True)
            result.append(completion_descriptor(service,c,row))
        except (ValueError,TypeError,KeyError):continue
    return result


def _delivery(output):
    value=output.get('local_delivery') if type(output) is dict else None
    if type(value) is dict and value.get('kind')=='protected_runtime_speech' and type(value.get('status')) is str and value['status'] in {'ended','not_delivered','unconfirmed'}:
        return {'kind':'protected_runtime_speech','status':value['status']}
    return None


def _outcome_delivery(value):
    result=value.get('result') or {}
    receipt=result.get('execution_receipt') or result.get('client_bridge_receipt') or {}
    return _delivery(result) or _delivery(receipt.get('output') or {})


def _tutor_native_turn(c,action_id):
    """Only host-derived action IDs select native source authority."""
    turn_id=(action_id[len('native-tutor-'):] if action_id.startswith('native-tutor-') else
        action_id[:-5] if action_id.endswith('-hint') else action_id[:-9] if action_id.endswith('-feedback') else None)
    if not turn_id:return None
    row=c.execute("SELECT t.*,s.capability_name,p.context_manifest_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0",(turn_id,)).fetchone()
    if not row or json.loads(row['action_json']).get('source')!='qwen_realtime_capability':return None
    return row


def validate_tutor_request(c,session_id,payload,*,allow_saved=False):
    from .agent_runtime import InvalidAgentPlanError
    row=_tutor_native_turn(c,payload.client_action_id)
    if not row:
        if payload.client_action_id.startswith('native-tutor-'):raise InvalidAgentPlanError('REALTIME_TUTOR_SOURCE_REQUIRED')
        return None
    action=json.loads(row['action_json']);manifest=json.loads(row['context_manifest_json'])
    if row['capability_name']=='learning.explain':
        expected=('native-tutor-'+row['agent_turn_id'],action.get('question'),action.get('subject'),action.get('hint_level'),None,
            action.get('ocr_confirmation_id'),action.get('diagnostic_prompt_id'),action.get('diagnostic_item_version'))
    elif row['capability_name']=='tutor.more_hint':
        prior=manifest.get('native_tutor_source_v1') or {}
        expected=(row['agent_turn_id']+'-hint',prior.get('question'),prior.get('subject'),min(6,prior.get('hint_level',6)+1),prior.get('turn_id'),None,None,None)
    else:raise InvalidAgentPlanError('REALTIME_TUTOR_CAPABILITY_MISMATCH')
    actual=(payload.client_action_id,payload.question,payload.subject,payload.hint_level,payload.parent_turn_id,payload.ocr_confirmation_id,payload.prompt_id,payload.item_version)
    if row['session_id']!=session_id or actual!=expected:raise InvalidAgentPlanError('REALTIME_TUTOR_SOURCE_CHANGED')
    saved=c.execute('SELECT turn_id FROM tutor_turn WHERE client_action_id=? AND session_id=? AND child_id=?',(payload.client_action_id,session_id,row['child_id'])).fetchone()
    if allow_saved and saved:return True
    require_claim(c,row['agent_turn_id']);return True


def validate_tutor_feedback(c,tutor_turn_id,response_text,outcome,action_id):
    from .agent_runtime import InvalidAgentPlanError
    row=_tutor_native_turn(c,action_id)
    if not row:return None
    action=json.loads(row['action_json']);target=json.loads(row['context_manifest_json']).get('native_tutor_source_v1') or {}
    prior=c.execute('SELECT child_id,session_id FROM tutor_turn WHERE turn_id=?',(tutor_turn_id,)).fetchone()
    if (row['capability_name']!='tutor.record_feedback' or action_id!=row['agent_turn_id']+'-feedback'
        or not prior or (prior['child_id'],prior['session_id'])!=(row['child_id'],row['session_id'])
        or tutor_turn_id!=target.get('turn_id') or response_text!=action.get('feedback_text') or outcome!=action.get('outcome')):
        raise InvalidAgentPlanError('REALTIME_TUTOR_SOURCE_CHANGED')
    require_claim(c,row['agent_turn_id']);return True


def require_resolved_chain(service,c,session_id,child_id,voice_input_id,*,exclude=None):
    """A prepared proposal is not permission to bypass an unfinished effect."""
    from .realtime_capability_tools import KEY as PACKET,_fail
    rows=c.execute("SELECT t.*,s.status AS step_status,s.error_code AS step_error FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id) WHERE t.session_id=? AND t.child_id=? AND s.step_index=0 AND json_extract(p.context_manifest_json,?)=?",(session_id,child_id,'$.'+PACKET+'.identity.voice_input_id',voice_input_id)).fetchall()
    for previous in rows:
        if previous['agent_turn_id']==exclude:continue
        old_claim=audit(c,'qwen-execution-claim-'+previous['agent_turn_id'])
        if status(service,c,previous,service._state(previous['step_status'],previous['step_error'],bool(old_claim)))=='unknown':
            _fail('REALTIME_CAPABILITY_PRIOR_OUTCOME_UNKNOWN')


def validate_reflection_completion(c,row,value):
    """Reconcile only an actually saved reflection, never execute its action."""
    from datetime import datetime
    from .realtime_capability_tools import _fail
    result=value.get('result') or {}
    receipt=result.get('execution_receipt') or result.get('client_bridge_receipt') or {}
    output=receipt.get('output')
    expected_action='reflection-close:'+row['session_id']
    if (value.get('tool_status')!='completed' or type(output) is not dict
        or set(output)!={'session_id','child_id','session_status','daily_report_ready','client_action_id'}
        or output.get('session_id')!=row['session_id'] or output.get('child_id')!=row['child_id']
        or output.get('session_status')!='completed' or output.get('client_action_id')!=expected_action
        or type(output.get('daily_report_ready')) is not bool):
        _fail('REALTIME_REFLECTION_RECEIPT_REQUIRED')
    saved=c.execute("SELECT r.created_at FROM daily_reflection r JOIN daily_session d ON d.session_id=r.session_id AND d.child_id=r.child_id WHERE r.session_id=? AND r.child_id=? AND r.client_action_id=? AND d.status='completed' AND d.state='completed'",(row['session_id'],row['child_id'],expected_action)).fetchone()
    claim=c.execute('SELECT occurred_at,child_id FROM audit_event WHERE audit_id=?',(claim_key(row['agent_turn_id']),)).fetchone()
    if not saved or not claim or claim['child_id']!=row['child_id']:_fail('REALTIME_REFLECTION_NOT_SAVED')
    try:
        created=datetime.fromisoformat(saved['created_at'].replace('Z','+00:00'))
        claimed=datetime.fromisoformat(claim['occurred_at'].replace('Z','+00:00'))
        if created.tzinfo is None or claimed.tzinfo is None or created<claimed:raise ValueError()
    except (ValueError,TypeError,AttributeError):_fail('REALTIME_REFLECTION_NOT_SAVED')
    # A report may become ready after the original UI read. Underclaiming is
    # safe; claiming ready requires an actual same-owner durable report.
    if output['daily_report_ready'] and not c.execute('SELECT 1 FROM daily_report WHERE session_id=? AND child_id=?',(row['session_id'],row['child_id'])).fetchone():
        _fail('REALTIME_REFLECTION_REPORT_NOT_SAVED')
