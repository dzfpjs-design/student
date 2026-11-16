"""Exact local native-intent/start association. No execution or closure claim."""
import sqlite3
import json
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
import hmac
import re


class LaunchBindingConflict(ValueError):
    pass


def ensure_schema(c: sqlite3.Connection) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS controlled_launch_receipt(
        intent_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('video','game')),
        request_id TEXT NOT NULL,
        content_session_id TEXT REFERENCES controlled_content_session(content_session_id) ON DELETE CASCADE,
        game_session_id TEXT REFERENCES controlled_game_session(game_session_id) ON DELETE CASCADE,
        cancelled_before_start INTEGER NOT NULL DEFAULT 0 CHECK(cancelled_before_start IN (0,1)),
        native_epoch TEXT,
        UNIQUE(session_id,kind,request_id),
        CHECK((cancelled_before_start=1 AND content_session_id IS NULL AND game_session_id IS NULL)
           OR (cancelled_before_start=0 AND kind='video' AND content_session_id IS NOT NULL AND game_session_id IS NULL)
           OR (cancelled_before_start=0 AND kind='game' AND game_session_id IS NOT NULL AND content_session_id IS NULL)))''')
    if 'native_epoch' not in {row[1] for row in c.execute('PRAGMA table_info(controlled_launch_receipt)')}:
        c.execute('ALTER TABLE controlled_launch_receipt ADD COLUMN native_epoch TEXT')


def read(c, session_id: str, intent_id: str):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='controlled_launch_receipt'").fetchone():
        return None
    row=c.execute('''SELECT lr.*,cs.status AS content_status,gs.status AS game_status
        FROM controlled_launch_receipt lr
        JOIN daily_session ds ON ds.session_id=lr.session_id AND ds.child_id=lr.child_id
        LEFT JOIN controlled_content_session cs ON cs.content_session_id=lr.content_session_id
            AND cs.child_id=lr.child_id AND cs.session_id=lr.session_id
        LEFT JOIN controlled_game_session gs ON gs.game_session_id=lr.game_session_id
            AND gs.child_id=lr.child_id AND gs.session_id=lr.session_id
        WHERE lr.session_id=? AND lr.intent_id=?''',(session_id,intent_id)).fetchone()
    if row is None:
        return None
    status='cancelled_before_start' if row['cancelled_before_start'] else row['content_status'] if row['kind']=='video' else row['game_status']
    if status is None:
        return None
    return {'intentId':row['intent_id'],'childId':row['child_id'],'sessionId':row['session_id'],
            'kind':row['kind'],'requestId':row['request_id'],
            'activityId':row['content_session_id'] or row['game_session_id'],'status':status,
            **({'nativeEpoch':row['native_epoch']} if 'native_epoch' in row.keys() and row['native_epoch'] else {})}


def check_request(c, session_id, kind, request_id, intent_id):
    if not c.in_transaction:
        raise LaunchBindingConflict('LAUNCH_TRANSACTION_REQUIRED')
    if not intent_id and not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='controlled_launch_receipt'").fetchone():
        return
    ensure_schema(c)
    row=c.execute('SELECT * FROM controlled_launch_receipt WHERE intent_id=? OR (session_id=? AND kind=? AND request_id=?)',
                  (intent_id,session_id,kind,request_id)).fetchone()
    if row and (row['cancelled_before_start'] or row['session_id']!=session_id or row['kind']!=kind
                or row['request_id']!=request_id or (intent_id and row['intent_id']!=intent_id)):
        raise LaunchBindingConflict('LAUNCH_CANCELLED_OR_CONFLICT')


def cancel_unstarted(c,session_id,kind,request_id,intent_id):
    if not c.in_transaction:
        raise LaunchBindingConflict('LAUNCH_TRANSACTION_REQUIRED')
    ensure_schema(c)
    owner=c.execute('SELECT child_id FROM daily_session WHERE session_id=?',(session_id,)).fetchone()
    if owner is None:
        return None
    existing=read(c,session_id,intent_id)
    if existing:
        if existing['kind']!=kind or existing['requestId']!=request_id:
            raise LaunchBindingConflict('LAUNCH_BINDING_CONFLICT')
        return existing
    table='controlled_content_session' if kind=='video' else 'controlled_game_session'
    # Never attach a new intent to an old already-committed request.
    if c.execute(f'SELECT 1 FROM {table} WHERE client_action_id=?',(request_id,)).fetchone():
        raise LaunchBindingConflict('LAUNCH_LEGACY_RECEIPT_UNBOUND')
    if c.execute('SELECT 1 FROM controlled_launch_receipt WHERE intent_id=? OR (session_id=? AND kind=? AND request_id=?)',
                 (intent_id,session_id,kind,request_id)).fetchone():
        raise LaunchBindingConflict('LAUNCH_BINDING_CONFLICT')
    c.execute('''INSERT INTO controlled_launch_receipt(intent_id,child_id,session_id,kind,request_id,cancelled_before_start)
        VALUES(?,?,?,?,?,1)''',(intent_id,owner['child_id'],session_id,kind,request_id))
    return read(c,session_id,intent_id)


def confirm_not_dispatched(c,session_id,intent_id,activity_id,proof,*,nonce=None,revision=None,signature=None,key=None):
    """Consume a trusted local native journal receipt, not an inferred timeout."""
    if not c.in_transaction or proof not in {'cancelled_before_dispatch','owner_exit_before_registration'}:
        raise LaunchBindingConflict('LAUNCH_PROOF_REQUIRED')
    binding=read(c,session_id,intent_id)
    if binding is None or binding['activityId']!=activity_id:
        raise LaunchBindingConflict('LAUNCH_BINDING_CONFLICT')
    if (not isinstance(key,str) or not re.fullmatch(r'[a-f0-9]{64}',key)
            or not isinstance(signature,str) or not re.fullmatch(r'[a-f0-9]{64}',signature)
            or not isinstance(nonce,str) or not re.fullmatch(r'[a-f0-9-]{36}',nonce)
            or type(revision) is not int or revision<2):
        raise LaunchBindingConflict('LAUNCH_NATIVE_ATTESTATION_REQUIRED')
    values=['native_launch_absence_v1',binding['childId'],session_id,binding['kind'],binding['requestId'],
            intent_id,activity_id,proof,nonce,revision]
    expected=hmac.new(bytes.fromhex(key),json.dumps(values,ensure_ascii=False,separators=(',',':')).encode('utf-8'),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):
        raise LaunchBindingConflict('LAUNCH_NATIVE_ATTESTATION_INVALID')
    table,key=('controlled_content_session','content_session_id') if binding['kind']=='video' else ('controlled_game_session','game_session_id')
    row=c.execute(f'SELECT * FROM {table} WHERE {key}=? AND child_id=? AND session_id=?',
                  (activity_id,binding['childId'],session_id)).fetchone()
    if row is None or row['status']=='completed' or (binding['kind']=='game' and row['os_process_id'] is not None):
        raise LaunchBindingConflict('LAUNCH_EXECUTION_CONTRADICTION')
    if row['end_reason']=='native_not_dispatched':
        return binding
    now=datetime.now(timezone.utc).isoformat()
    c.execute(f"UPDATE {table} SET status='stopped',actual_seconds=0,ended_at=?,updated_at=?,end_reason='native_not_dispatched' WHERE {key}=?",
              (now,now,activity_id))
    # Preserve the earlier inferred duration/status as history; do not count
    # unexecuted startup latency or downtime as watched/played time.
    c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
              (str(uuid4()),'native','controlled_content_ended' if binding['kind']=='video' else 'controlled_game_ended',binding['childId'],
               json.dumps({key:activity_id,'native_intent_id':intent_id,'reason':'native_not_dispatched','proof':proof,
                           'native_nonce':nonce,'native_revision':revision,
                           'previous_status':row['status'],'previous_inferred_seconds':row['actual_seconds'],'actual_seconds':0}),now))
    return read(c,session_id,intent_id)


def attach(c, kind: str, request_id: str, intent_id, result: dict, *, replay: bool, native_epoch=None) -> dict:
    if not intent_id:
        return result
    if not c.in_transaction:
        raise LaunchBindingConflict('LAUNCH_TRANSACTION_REQUIRED')
    ensure_schema(c)
    activity_id=result['content_session_id' if kind=='video' else 'game_session_id']
    existing=read(c,result['session_id'],intent_id)
    expected={'intentId':intent_id,'childId':result['child_id'],'sessionId':result['session_id'],
              'kind':kind,'requestId':request_id,'activityId':activity_id,
              **({'nativeEpoch':native_epoch} if native_epoch else {})}
    if existing:
        if any(existing[key]!=value for key,value in expected.items()):
            raise LaunchBindingConflict('LAUNCH_BINDING_CONFLICT')
    elif replay:
        # An old start without a pre-existing binding cannot be retrofitted with
        # a newly prepared native intent to manufacture never-executed proof.
        raise LaunchBindingConflict('LAUNCH_LEGACY_RECEIPT_UNBOUND')
    else:
        if c.execute('SELECT 1 FROM controlled_launch_receipt WHERE intent_id=? OR (session_id=? AND kind=? AND request_id=?)',
                     (intent_id,result['session_id'],kind,request_id)).fetchone():
            raise LaunchBindingConflict('LAUNCH_BINDING_CONFLICT')
        c.execute('''INSERT INTO controlled_launch_receipt(intent_id,child_id,session_id,kind,request_id,content_session_id,game_session_id,native_epoch)
            VALUES(?,?,?,?,?,?,?,?)''',
                  (intent_id,result['child_id'],result['session_id'],kind,request_id,
                   activity_id if kind=='video' else None,activity_id if kind=='game' else None,native_epoch))
    return {**result,'native_intent_id':intent_id}
