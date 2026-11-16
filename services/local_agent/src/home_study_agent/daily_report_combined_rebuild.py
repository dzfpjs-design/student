"""Atomic replacement of a provably unsubmitted combined candidate."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from uuid import uuid4
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .daily_report_combined_snapshot import _aad as combined_aad, _collect, _key, _schema, MAX_CANDIDATE_BYTES
from .daily_report_delivery_state import identity_key
from .daily_report_frozen_snapshot import _aad as visual_aad, _consent_generation, _detailed_content
from .daily_report_observation import _authority, _projection, _source
from .daily_report_visual_submission import _check as check_visual


def _sent_snapshot_matches(key,history,child,day,batch_id):
    try:
        blob=bytes(history['encrypted_payload'])
        if not blob.startswith(b'HSC1') or len(blob)>MAX_CANDIDATE_BYTES+32: return False
        raw=AESGCM(key).decrypt(blob[4:16],blob[16:],combined_aad(history['snapshot_id'],batch_id,child,day,history['recipient_identity']))
        body=json.loads(raw)
        return (sha256(raw).hexdigest()==history['payload_sha256'] and body['generated_at']==history['generated_at']
                and body['child_id']==child and body['day']==day and body['batch_id']==batch_id
                and body['visual_id']==history['ledger_snapshot'])
    except Exception:
        return False


def rebuild(service,telemetry,snapshot_id):
    """Replace a ready candidate, preserving independently verified sent siblings.

    Source, visual snapshot, ready ledger and combined payload change together.
    The caller never receives, retries, or alters an attempted delivery.
    """
    if service.database.path.resolve()!=telemetry.database.path.resolve():
        raise ValueError('COMBINED_DATABASE_MISMATCH')
    key=_key(service.encryption_key)
    with service.database.external_effect_guard(timeout=10):
        with service.database.connect() as c:
            old=c.execute('SELECT batch_id FROM combined_daily_snapshot WHERE snapshot_id=?',(snapshot_id,)).fetchone()
            if not old: raise ValueError('COMBINED_CANDIDATE_UNAVAILABLE')
            batch_id=old['batch_id']
        telemetry.inspect_combined_batch(batch_id,key)
        with service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                _schema(c)
                row=c.execute('SELECT * FROM combined_daily_snapshot WHERE snapshot_id=?',(snapshot_id,)).fetchone()
                if not row or row['batch_id']!=batch_id or row['status']!='local_only':
                    raise ValueError('COMBINED_CANDIDATE_UNAVAILABLE')
                batch=c.execute('SELECT status,sealed,expected_count FROM combined_diagnostic_batch WHERE batch_id=?',(batch_id,)).fetchone()
                members=c.execute('SELECT child_id,status,claim_token FROM combined_diagnostic_member WHERE batch_id=?',(batch_id,)).fetchall()
                if not batch or batch['status']!='active' or batch['sealed']!=1 or len(members)!=batch['expected_count']:
                    raise ValueError('COMBINED_MEMBERS_UNCONFIRMED')
                own=next((m for m in members if m['child_id']==row['child_id']),None)
                if not own or own['status']!='waiting' or own['claim_token'] is not None:
                    raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
                if any(m['status'] not in ('waiting','sent') or (m['status']=='waiting' and m['claim_token'] is not None)
                       for m in members):
                    raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
                pilot=c.execute('SELECT recipient,attachment_sha256 FROM pilot_test_digest_outbox WHERE combined_batch_id=? AND status=\'processing\'',(batch_id,)).fetchone()
                if not pilot or not isinstance(pilot['recipient'],str): raise ValueError('COMBINED_MEMBERS_UNCONFIRMED')
                recipient_address=pilot['recipient'].strip().casefold()
                for member in members:
                    history=c.execute('''SELECT d.identity,d.status,d.claim_token,s.snapshot_id,s.child_id AS snapshot_child,s.batch_id AS snapshot_batch,s.day AS snapshot_day,
                            s.recipient_identity,s.encrypted_payload,s.payload_sha256,s.generated_at,
                            l.child_id AS ledger_child,l.status AS ledger_status,l.snapshot_id AS ledger_snapshot
                        FROM combined_daily_delivery d LEFT JOIN combined_daily_snapshot s ON s.snapshot_id=d.snapshot_id
                        LEFT JOIN daily_observation_delivery_ledger l ON l.identity=d.identity
                        WHERE d.batch_id=? AND d.child_id=?''',(batch_id,member['child_id'])).fetchall()
                    if member['status']=='waiting':
                        if history: raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
                    elif (len(history)!=1 or history[0]['status']!='sent' or not member['claim_token']
                          or history[0]['claim_token']!=member['claim_token'] or history[0]['snapshot_child']!=member['child_id']
                          or history[0]['snapshot_batch']!=batch_id or history[0]['snapshot_day']!=row['day']
                          or history[0]['recipient_identity']!=row['recipient_identity'] or history[0]['ledger_child']!=member['child_id']
                          or history[0]['ledger_status']!='sent'
                          or history[0]['identity']!=identity_key(key,member['child_id'],row['day'],recipient_address)
                          or not _sent_snapshot_matches(key,history[0],member['child_id'],row['day'],batch_id)):
                        raise ValueError('COMBINED_SENT_SIBLING_REVIEW_REQUIRED')
                try:
                    blob=bytes(row['encrypted_payload'])
                    if not blob.startswith(b'HSC1') or len(blob)>MAX_CANDIDATE_BYTES+32: raise ValueError()
                    raw=AESGCM(key).decrypt(blob[4:16],blob[16:],combined_aad(snapshot_id,batch_id,row['child_id'],row['day'],row['recipient_identity']))
                    if sha256(raw).hexdigest()!=row['payload_sha256']: raise ValueError()
                    old_body=json.loads(raw)
                    if (old_body['batch_id']!=batch_id or old_body['child_id']!=row['child_id'] or old_body['day']!=row['day']
                            or old_body['generated_at']!=row['generated_at']): raise ValueError()
                    child,day,recipient,old_visual=row['child_id'],row['day'],old_body['recipient_id'],old_body['visual_id']
                except Exception:
                    raise ValueError('COMBINED_CANDIDATE_UNREADABLE') from None
                address=_authority(c,child,day,recipient,key)
                token=identity_key(key,child,day,address)
                ledger=c.execute('SELECT child_id,status,snapshot_id FROM daily_observation_delivery_ledger WHERE identity=?',(token,)).fetchone()
                if not ledger or (ledger['child_id'],ledger['status'],ledger['snapshot_id'])!=(child,'ready',old_visual):
                    raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
                if c.execute('SELECT 1 FROM combined_daily_delivery WHERE identity=? LIMIT 1',(token,)).fetchone():
                    raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
                source=c.execute('''SELECT o.source_id FROM daily_observation_report_outbox o
                    JOIN daily_observation_report_snapshot v ON v.source_id=o.source_id AND v.child_id=o.child_id AND v.day=o.day AND v.recipient_id=o.recipient_id
                    WHERE o.child_id=? AND o.day=? AND o.recipient_id=? AND o.address_identity=? AND v.snapshot_id=?''',
                    (child,day,recipient,address,old_visual)).fetchone()
                if not source: raise ValueError('COMBINED_VISUAL_UNAVAILABLE')
                # A completely unrelated reference to the old visual snapshot
                # would make replacement ambiguous, even if this ledger is ready.
                if c.execute('SELECT 1 FROM daily_observation_delivery_ledger WHERE snapshot_id=? AND identity!=? LIMIT 1',(old_visual,token)).fetchone():
                    raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
                if c.execute('DELETE FROM combined_daily_snapshot WHERE snapshot_id=? AND status=\'local_only\'',(snapshot_id,)).rowcount!=1:
                    raise ValueError('COMBINED_CANDIDATE_UNAVAILABLE')
                if c.execute('DELETE FROM daily_observation_report_source WHERE source_id=? AND child_id=? AND day=?',(source['source_id'],child,day)).rowcount!=1:
                    raise ValueError('COMBINED_VISUAL_UNAVAILABLE')
                fingerprint,ids=_source(c,child,day)
                source_id=str(uuid4());visual_id=str(uuid4());now=datetime.now(timezone.utc).isoformat()
                c.execute('INSERT INTO daily_observation_report_source(source_id,child_id,day,fingerprint,source_ids_json) VALUES(?,?,?,?,?)',(source_id,child,day,fingerprint,ids))
                c.execute("INSERT INTO daily_observation_report_outbox VALUES(?,?,?,?,?,'prepared')",(child,day,recipient,address,source_id))
                from .daily_report_frozen_dependencies import build
                projection=_projection(c,child,day)
                dependencies=build(c,child,projection)
                visual_body=json.dumps({'version':1,'generated_at':now,'address_identity':address,'consent_generation':_consent_generation(c,child,recipient),
                    'projection':projection,'details':_detailed_content(c,child,projection),'dependencies':dependencies},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
                if len(visual_body)>1024*1024: raise ValueError('FROZEN_SNAPSHOT_TOO_LARGE')
                nonce=os.urandom(12);cipher=b'HSR1'+nonce+AESGCM(key).encrypt(nonce,visual_body,visual_aad(visual_id,source_id,child,day,recipient))
                c.execute("INSERT INTO daily_observation_report_snapshot VALUES(?,?,?,?,?,?,?,?, 'frozen_local')",(visual_id,source_id,child,day,recipient,now,sha256(visual_body).hexdigest(),cipher))
                if c.execute("UPDATE daily_observation_delivery_ledger SET snapshot_id=?,updated_at=? WHERE identity=? AND child_id=? AND status='ready' AND snapshot_id=?",(visual_id,now,token,child,old_visual)).rowcount!=1:
                    raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
                check_visual(c,service,child,day,recipient,visual_id)
                payload,identity=_collect(c,telemetry,batch_id,child,day,recipient,visual_id,key,allow_sent_members=True)
                new_id=uuid4().hex;payload['generated_at']=now
                raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
                if len(raw)>MAX_CANDIDATE_BYTES: raise ValueError('COMBINED_CANDIDATE_TOO_LARGE')
                nonce=os.urandom(12);cipher=b'HSC1'+nonce+AESGCM(key).encrypt(nonce,raw,combined_aad(new_id,batch_id,child,day,identity))
                c.execute("INSERT INTO combined_daily_snapshot VALUES(?,?,?,?,?,?,?,?, 'local_only')",(new_id,batch_id,child,day,identity,now,sha256(raw).hexdigest(),cipher))
                c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,'system','combined_candidate_rebuilt',?,?,?)",
                          (str(uuid4()),child,json.dumps({'previous_snapshot_id':snapshot_id,'snapshot_id':new_id,'day':day}),now))
                c.commit()
                return {'snapshot_id':new_id,'status':'ready_local_only'}
            except BaseException:
                c.rollback();raise
