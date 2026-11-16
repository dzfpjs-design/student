"""Native tool selection -> local frozen targets at original mutation boundary.

This is no intent classifier and makes no model call. Original nonnative controls
continue through their existing inspectors. No client/model-supplied IDs become
local target authority, and no native dialogue is a scoring/learning receipt.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from .agent_runtime import InvalidAgentPlanError
from .break_control import BreakControl
from .privacy_operation_receipt import generation

KEY='native_runtime_authority_v1'
CAPABILITIES=frozenset(('task.pause','break.begin','break.resume'))
_execution_authorizer=ContextVar('native_control_execution_authorizer',default=None)


@contextmanager
def native_execution_scope(authorize):
    """Trusted adapter dispatch only; no ambient grant survives its completion."""
    token=_execution_authorizer.set(authorize)
    try:yield
    finally:_execution_authorizer.reset(token)


def fail():raise InvalidAgentPlanError('NATIVE_RUNTIME_TARGET_CHANGED')


def _task(c,sid,child):
    rows=c.execute('''SELECT i.plan_item_id,i.plan_id,p.version AS version,i.status,
        i.source_type,i.source_ref,i.started_at FROM plan_item i JOIN daily_plan p USING(plan_id)
        WHERE p.session_id=? AND p.child_id=? AND p.status='active' AND i.status='active' ''',(sid,child)).fetchall()
    if len(rows)>1:fail()
    return dict(rows[0]) if rows else None


def compose_native_control(c,session,capability,args):
    if capability not in CAPABILITIES:return None
    if args:fail()
    if capability=='task.pause':
        targets={'task':_task(c,session['session_id'],session['child_id'])}
    else:
        target=BreakControl.snapshot(c,session['session_id'],session['child_id'])
        if target['status']!='ready' or target['state']!=('study' if capability=='break.begin' else 'break'):fail()
        targets={'break':target}
    return {'version':1,'capability':capability,'targets':targets,'storage_generation':generation(c)}


def inspect_native_control(c,turn_id,*,allow_planned=False):
    row=c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,s.status AS step_status,
        s.capability_name,s.arguments_json,s.idempotency_key,d.status AS session_status,cp.status AS child_status
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        JOIN daily_session d ON d.session_id=t.session_id AND d.child_id=t.child_id
        JOIN child_profile cp ON cp.child_id=t.child_id WHERE t.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
    if not row:return None
    manifest=json.loads(row['context_manifest_json']);authority=manifest.get(KEY)
    if authority is None:
        if row['capability_name'] in CAPABILITIES and json.loads(row['action_json']).get('source')=='qwen_realtime_capability':fail()
        return None
    from .realtime_capability_tools import KEY as SOURCE_KEY,SOURCE,_hash,_immutable_capture
    from .agent_runtime import AgentRuntimeStore
    packet=manifest.get(SOURCE_KEY);action=json.loads(row['action_json'])
    if (type(authority) is not dict or authority.get('version')!=1 or authority.get('capability') not in CAPABILITIES
        or authority.get('capability')!=row['capability_name'] or row['session_status']!='active' or row['child_status']!='active'
        or not packet or action.get('source')!=SOURCE or packet.get('native_authority_sha256')!=_hash(authority)
        or authority.get('storage_generation')!=generation(c) or json.loads(row['arguments_json'])!={}):fail()
    consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    if not consent or any(type(consent[k]) is not int or consent[k]!=1 for k in ('cloud_audio_allowed','cloud_model_text_allowed')):fail()
    voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND session_id=? AND child_id=?',
        (packet['identity']['voice_input_id'],row['session_id'],row['child_id'])).fetchone()
    if not voice or voice['status']!='completed' or voice['transcript']!=row['transcript'] or _hash(voice['transcript'])!=packet['original_transcript_sha256']:fail()
    capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=row['session_id'],child_id=row['child_id'],
        voice_input_id=voice['voice_input_id'],transcript=voice['transcript'])
    if capture.get('source_kind')!='realtime_transcript' or _hash(_immutable_capture(capture))!=packet['capture_hash']:fail()
    try:
        authorize=_execution_authorizer.get()
        owned=authorize is not None and authorize(c,row['session_id'],row['child_id'],
            {'version':1,**packet['identity']},dict(voice)) is True
    except Exception:owned=False
    if not owned:fail()
    capability=authority['capability'];targets=authority['targets'];recorded=None
    if capability=='task.pause':
        event=c.execute('SELECT plan_item_id,event_type,payload_json FROM plan_item_event WHERE client_action_id=?',
            (row['idempotency_key'],)).fetchone()
        if event:
            if event['event_type']!='paused' or event['plan_item_id']!=(targets.get('task') or {}).get('plan_item_id'):fail()
            recorded={'plan_item_id':event['plan_item_id'],'event_type':'paused'}
    else:
        recorded=BreakControl.recorded(c,row['session_id'],'begin' if capability=='break.begin' else 'resume',row['idempotency_key'])
    if recorded:
        return {'control':capability,'targets':targets,'recorded_receipt':recorded,'request_key':row['client_action_id']}
    allowed={'planned','running','waiting_child'} if allow_planned else {'running'}
    if row['step_status'] not in allowed or row['plan_status'] not in allowed:fail()
    if capability=='task.pause':
        if _task(c,row['session_id'],row['child_id'])!=targets.get('task'):fail()
        targets={'task':targets['task'] or {'plan_item_id':None}}
    elif BreakControl.snapshot(c,row['session_id'],row['child_id'])!=targets.get('break'):fail()
    return {'control':capability,'targets':targets,'request_key':row['client_action_id']}
