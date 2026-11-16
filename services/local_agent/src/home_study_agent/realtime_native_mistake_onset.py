"""Independent host-only visible-question witness; never formal question authority."""
import json
import re
from datetime import datetime,timezone

ACTOR='native_mistake_onset_v1'
FIELDS={'version','child_id','session_id','mistake_id','learning_session_id','source_revision','focus_generation','practice_item_id','question_visible'}
BOUND=('child_id','session_id','mistake_id','learning_session_id','source_revision','focus_generation','practice_item_id')


def fail(code):
    from .agent_runtime import InvalidAgentPlanError
    raise InvalidAgentPlanError('NATIVE_MISTAKE_ONSET_'+code)


def _hash(value):
    from .realtime_capability_tools import _hash as digest
    return digest(value)


def normalize(value,sid,child):
    if value is None:return None
    if (type(value) is not dict or set(value)!=FIELDS or type(value.get('version')) is not int or value['version']!=1
        or value.get('question_visible') is not True or value.get('session_id')!=sid or value.get('child_id')!=child
        or type(value.get('focus_generation')) is not int or value['focus_generation']<1
        or type(value.get('source_revision')) is not str or not re.fullmatch('[0-9a-f]{64}',value['source_revision'])):
        fail('INVALID')
    for key in ('child_id','session_id','mistake_id','learning_session_id','practice_item_id'):
        item=value[key]
        if key=='practice_item_id' and item is None:continue
        if type(item) is not str or not 1<=len(item)<=180 or item.strip()!=item or any(ord(char)<32 for char in item):fail('INVALID')
    return dict(value)


def _focus(c,sid,*,prefer_coach_variant=True,current_graph_version=None):
    from .mistake_learning import native_focus,MistakeLearningError
    try:return native_focus(c,sid,prefer_coach_variant=prefer_coach_variant,current_graph_version=current_graph_version)
    except MistakeLearningError as error:
        if error.code in {'MISTAKE_LEARNING_SOURCE_CHANGED','MISTAKE_LEARNING_FOCUS_CHANGED','MISTAKE_LEARNING_PRACTICE_CHANGED'}:return None
        raise


def _current(carrier,focus,*,allow_own_progress=False,allow_original=False):
    return bool(carrier and focus and all(carrier.get(key)==focus.get(key) for key in BOUND
        if not (key=='practice_item_id' and (allow_own_progress or allow_original and carrier.get(key) is None))))


def record(c,captured,carrier,*,allow_new,current_graph_version=None):
    """Original native-input post_persist transaction only, not a public port."""
    from .privacy_operation_receipt import generation
    if not c.in_transaction:fail('TRANSACTION_REQUIRED')
    voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=?',(captured['voice_input_id'],)).fetchone()
    if not voice or voice['purpose']!='native_cloud_dialogue' or voice['status']!='completed':fail('VOICE_REQUIRED')
    carrier=normalize(carrier,voice['session_id'],voice['child_id'])
    immutable={'version':1,'voice_input_id':voice['voice_input_id'],'child_id':voice['child_id'],'session_id':voice['session_id'],
        'storage_generation':generation(c),'transcript_sha256':_hash(voice['transcript']),'carrier':carrier}
    aid='native-mistake-onset-'+voice['voice_input_id']
    prior=c.execute('SELECT * FROM audit_event WHERE audit_id=?',(aid,)).fetchone()
    if prior:
        try:saved=json.loads(prior['summary_json'])
        except (TypeError,ValueError):fail('CHANGED')
        if (prior['actor']!=ACTOR or prior['action']!='native_mistake_onset' or prior['child_id']!=voice['child_id']
            or saved.get('validation') not in {'current','stale'} or {key:saved.get(key) for key in immutable}!=immutable
            or set(saved)!=(set(immutable)|{'validation'})):fail('CHANGED')
        return saved
    if not allow_new and carrier is not None:fail('LATE_UPGRADE_FORBIDDEN')
    sid=voice['session_id']
    current = _current(carrier,_focus(c,sid,current_graph_version=current_graph_version),allow_original=True) or _current(carrier,_focus(c,sid,prefer_coach_variant=False,current_graph_version=current_graph_version))
    value={**immutable,'validation':'current' if current else 'stale'}
    c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?)',
        (aid,ACTOR,'native_mistake_onset',voice['child_id'],json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')),datetime.now(timezone.utc).isoformat()))
    return value


def require_current(c,voice,snapshot,*,allow_own_progress=False,allow_original=False,select_scope=False,current_graph_version=None):
    """Caller first verifies the unchanged original native-input witness."""
    from .privacy_operation_receipt import generation
    row=c.execute('SELECT * FROM audit_event WHERE audit_id=?',('native-mistake-onset-'+voice['voice_input_id'],)).fetchone()
    if not row or row['actor']!=ACTOR or row['action']!='native_mistake_onset' or row['child_id']!=voice['child_id']:fail('REQUIRED')
    try:value=json.loads(row['summary_json'])
    except (TypeError,ValueError):fail('CHANGED')
    expected={'version':1,'voice_input_id':voice['voice_input_id'],'child_id':voice['child_id'],'session_id':voice['session_id'],
              'storage_generation':generation(c),'transcript_sha256':_hash(voice['transcript'])}
    if (type(value) is not dict or set(value)!=(set(expected)|{'carrier','validation'})
        or any(value.get(key)!=item for key,item in expected.items()) or value.get('validation')!='current'):fail('STALE')
    carrier=normalize(value.get('carrier'),voice['session_id'],voice['child_id'])
    sid=voice['session_id']
    live=_focus(c,sid,current_graph_version=current_graph_version)
    if not _current(carrier,live,allow_own_progress=allow_own_progress,allow_original=allow_original):
        live=_focus(c,sid,prefer_coach_variant=False,current_graph_version=current_graph_version)
        if not _current(carrier,live,allow_original=allow_original):fail('STALE')
    if select_scope and live:
        if allow_original and carrier['practice_item_id'] is None:
            return {**live,'practice_item_id':None,'practice_created_at':None}
        return live
    if not _current(carrier,snapshot,allow_original=allow_original):fail('STALE')
    return carrier
