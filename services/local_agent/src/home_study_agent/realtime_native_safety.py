"""Retain the existing local child-safety policy in native audio admission.

The original audio may already have reached the authorized provider. This is
not a claim that input was never uploaded, nor a verification of model output.
"""
import json
from uuid import NAMESPACE_URL, uuid5
from .child_safety import CHILD_SAFETY_CLASSIFIER_VERSION, classify_child_safety, persist_safety_event
from .privacy_operation_receipt import generation
from .realtime_capability_tools import _hash, _fail
from .repository import utc_now

SOURCE = 'native_child_safety_v1'
RISKS = frozenset(('R1', 'R2', 'R3'))
RESPONSES = {
    'R1': '听起来你今天不太好受。现在先停止后续云交互，你不用马上把事情说清楚。可以先喝水、安静一会儿，或者去找你信任的大人陪一会儿。本机家长中心已保存一条不含你原话的低压力关注提醒。',
    'R2': '这件事听起来让你不舒服，你不用一个人处理。现在先停止后续云交互，请去找身边可信任的大人；你可以只说愿意说的部分。本机家长中心已保存一条不含你原话的关注提醒，但它不能代替你现在去找大人。',
    'R3': '我很在意你现在说的这件事。现在先停止后续云交互，安全最重要：请不要独处，马上去找身边可信任的大人；如果危险正在发生，请让大人联系当地急救或警方。本机家长中心已保存一条不含你原话的紧急提醒，但它不能代替现场帮助。',
}


def _risk(voice):
    result = classify_child_safety(voice['transcript'])
    return result if result and result[2].get('risk_level') in RISKS else None


def native_input_blocked(c, voice):
    if c.execute('SELECT 1 FROM audit_event WHERE audit_id=?', ('native-safety-' + voice['voice_input_id'],)).fetchone():
        return True
    # Previously admitted native originals must not be exported/used by a new
    # call merely because they predate this flag. No normal intent classifier.
    return bool(voice['purpose'] in ('native_cloud_dialogue', 'household_local_transcript') and _risk(voice))


def protect_native_input(c, voice_input_id):
    """Called after the existing witness, within its trusted complete transaction."""
    if not c.in_transaction: _fail('REALTIME_NATIVE_SAFETY_TRANSACTION_REQUIRED')
    voice = c.execute('SELECT * FROM voice_input WHERE voice_input_id=?', (voice_input_id,)).fetchone()
    if not voice or voice['status'] != 'completed' or voice['purpose'] not in ('native_cloud_dialogue', 'household_local_transcript'):
        _fail('REALTIME_NATIVE_SAFETY_SOURCE_INVALID')
    if voice['purpose'] == 'household_local_transcript':
        from .realtime_household_input import verify_household_witness
        if not verify_household_witness(c, voice, allow_safety=True):
            _fail('REALTIME_NATIVE_SAFETY_SOURCE_INVALID')
    risk = _risk(voice)
    if not risk: return None  # R0 learning pressure remains natural native dialogue.
    intent, _, classified = risk
    risk_level = classified['risk_level']
    action_id = 'qwen-native-safety-' + voice_input_id
    turn_id = str(uuid5(NAMESPACE_URL, 'native-child-safety-v1:' + voice_input_id))
    action = {**classified, 'source': SOURCE, 'local_only': True, 'cloud_transmission_allowed': False,
              'voice_input_id': voice_input_id, 'mastery_update_allowed': False, 'independent_evidence': False}
    now = utc_now()
    prior = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (action_id,)).fetchone()
    if prior:
        if (prior['agent_turn_id'] != turn_id or prior['child_id'] != voice['child_id'] or prior['session_id'] != voice['session_id']
                or prior['transcript'] != voice['transcript'] or prior['intent'] != intent or json.loads(prior['action_json']) != action
                or prior['response_text'] != RESPONSES[risk_level]):
            _fail('REALTIME_NATIVE_SAFETY_RECORD_UNCONFIRMED')
    else:
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
            orchestrator_version,tool_status,completed_at,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (turn_id,voice['child_id'],voice['session_id'],voice['transcript'],intent,RESPONSES[risk_level],
             json.dumps(action,ensure_ascii=False,sort_keys=True),SOURCE,'completed',now,now,action_id))
    persist_safety_event(c,voice['child_id'],voice['session_id'],turn_id,action,now)
    event = c.execute('SELECT * FROM child_safety_event WHERE agent_turn_id=?', (turn_id,)).fetchone()
    if not event or (event['child_id'],event['session_id'],event['risk_level'],event['category']) != (
            voice['child_id'],voice['session_id'],risk_level,classified['category']):
        _fail('REALTIME_NATIVE_SAFETY_RECORD_UNCONFIRMED')
    summary = {'version':1,'voice_input_id':voice_input_id,'session_id':voice['session_id'],'child_id':voice['child_id'],
        'storage_generation':generation(c),'agent_turn_id':turn_id,'safety_event_id':event['safety_event_id'],
        'risk_level':risk_level,'category':classified['category'],'classifier_version':CHILD_SAFETY_CLASSIFIER_VERSION,
        'transcript_sha256':_hash(voice['transcript'])}
    aid = 'native-safety-' + voice_input_id
    prior_flag = c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?', (aid,)).fetchone()
    if prior_flag and (prior_flag['actor'] != SOURCE or json.loads(prior_flag['summary_json']) != summary):
        _fail('REALTIME_NATIVE_SAFETY_RECORD_UNCONFIRMED')
    c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING',
        (aid,SOURCE,'child_safety_blocked',voice['child_id'],json.dumps(summary,ensure_ascii=False,sort_keys=True),now))
    return native_safety_control(c,voice_input_id)


def native_safety_control(c, voice_input_id):
    row = c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?', ('native-safety-' + voice_input_id,)).fetchone()
    if not row: return None
    value = json.loads(row['summary_json'])
    turn = c.execute('''SELECT t.intent,t.response_text,t.action_json FROM agent_turn t JOIN child_safety_event e USING(agent_turn_id)
        WHERE t.agent_turn_id=? AND e.safety_event_id=? AND t.child_id=? AND t.session_id=?''',
        (value['agent_turn_id'],value['safety_event_id'],value['child_id'],value['session_id'])).fetchone()
    if (row['actor'] != SOURCE or value['voice_input_id'] != voice_input_id or not turn
            or json.loads(turn['action_json']).get('voice_input_id') != voice_input_id):
        _fail('REALTIME_NATIVE_SAFETY_RECORD_UNCONFIRMED')
    return {'kind':'child_safety','intent':turn['intent'],'response_text':turn['response_text'],
            'agent_turn_id':value['agent_turn_id'],'safety_event_id':value['safety_event_id']}
