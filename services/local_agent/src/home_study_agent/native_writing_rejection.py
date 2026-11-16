"""Terminal, no-effect native request receipts in the existing audit journal."""
from datetime import datetime, timezone
import hashlib
import json

from .local_supply import digest
from .writing_response_origin import require


def rejection_id(stage, session_id, action_id):
    return 'native-writing-rejection:' + digest([stage, session_id, action_id])


def require_not_rejected(c, stage, session_id, action_id):
    require(not c.execute('SELECT 1 FROM audit_event WHERE audit_id=?',
                         (rejection_id(stage, session_id, action_id),)).fetchone(), 'NATIVE_REQUEST_REJECTED')


def owned_prompt(c, value):
    row = c.execute('''SELECT p.* FROM diagnostic_prompt p
        JOIN daily_session s ON s.session_id=p.origin_session_id AND s.child_id=p.child_id
        JOIN child_profile cp ON cp.child_id=p.child_id
        WHERE p.prompt_id=? AND p.origin_session_id=? AND p.child_id=?
          AND s.status='active' AND s.state='study' AND cp.status='active' ''',
        (value['prompt_id'], value['session_id'], value['child_id'])).fetchone()
    require(row is not None and row['diagnostic_item_id']==value['diagnostic_item_id'], 'NATIVE_SCOPE_CHANGED')
    return row


def _has_effect(c, stage, packet):
    if stage == 'capture':
        return c.execute('SELECT 1 FROM capture_artifact WHERE client_action_id=?',
                         ('writing-original:'+packet['capture_action_id'],)).fetchone()
    return c.execute('SELECT 1 FROM diagnostic_prompt WHERE answer_action_id=?',
                     (packet['answer_action_id'],)).fetchone()


def read_rejection(c, packet):
    owned_prompt(c, packet)
    stage = 'answer' if packet['answer_action_id'] else 'capture'
    action = packet[stage+'_action_id']
    row = c.execute("SELECT summary_json FROM audit_event WHERE audit_id=? AND action='native_writing_rejected' AND child_id=?",
                    (rejection_id(stage, packet['session_id'], action), packet['child_id'])).fetchone()
    if row is None:
        return None
    saved = json.loads(row['summary_json'])
    require(all(saved.get(k)==v for k,v in packet.items()) and not _has_effect(c, stage, saved),
            'NATIVE_REJECTION_SCOPE_CHANGED')
    return {**saved, 'state':stage+'_rejected', 'original':None, 'result':None,
            'local_only':True, 'cloud_transmission_allowed':False}


def record_rejection(database, stage, value):
    """Only called after verified native capture or a native-answer error.

    The same writer lock checks absence and installs the replay fence. A 409
    after capture/answer_saved committed must never become a rejected receipt.
    """
    try:
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if stage == 'capture':
                packet = {k:value[k] for k in ('child_id','session_id','prompt_id','item_version','diagnostic_item_id')}
                packet.update(text_sha256=hashlib.sha256(value['text'].encode()).hexdigest(),
                              capture_action_id=value['client_action_id'], answer_action_id='')
            else:
                artifact = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND artifact_type='writing_original' AND session_id=?",
                    (value['writing_original_artifact_id'],value['session_id'])).fetchone()
                require(artifact is not None, 'NATIVE_ORIGINAL_UNAVAILABLE')
                details = json.loads(artifact['details_json'])
                require(details.get('origin')=={'kind':'native_editor'} and details.get('prompt_id')==value['prompt_id']
                        and artifact['client_action_id'].startswith('writing-original:'), 'NATIVE_ORIGINAL_SCOPE_CHANGED')
                prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (value['prompt_id'],)).fetchone()
                require(prompt is not None and prompt['child_id']==artifact['child_id'], 'NATIVE_SCOPE_CHANGED')
                packet = dict(child_id=artifact['child_id'],session_id=value['session_id'],prompt_id=value['prompt_id'],
                    item_version=value['item_version'],diagnostic_item_id=prompt['diagnostic_item_id'],
                    text_sha256=hashlib.sha256(value['answer'].encode()).hexdigest(),
                    capture_action_id=artifact['client_action_id'][len('writing-original:'):],answer_action_id=value['client_action_id'])
            owned_prompt(c, packet)
            if _has_effect(c, stage, packet):
                return
            action = packet[stage+'_action_id']
            c.execute('''INSERT OR IGNORE INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES(?,'native_writing','native_writing_rejected',?,?,?)''',
                (rejection_id(stage,packet['session_id'],action),packet['child_id'],
                 json.dumps(packet,sort_keys=True),datetime.now(timezone.utc).isoformat()))
            c.commit()
    except (ValueError, KeyError, TypeError):
        # No proof is safer than claiming that an unbound operation never wrote.
        return
