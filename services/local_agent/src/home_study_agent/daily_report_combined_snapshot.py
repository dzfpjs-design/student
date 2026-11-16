"""Isolated encrypted combined snapshot candidate; never authority to send.

The table is deliberately not installed by production migration. The complete
source-withdrawal and joint-log delivery protocols are not yet implemented.
"""
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from uuid import uuid4
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .daily_report_frozen_snapshot import _decrypt as decrypt_visual, _key
from .daily_report_frozen_dependencies import valid as visual_dependencies_valid
from .daily_report_observation import _authority
from .parent_reports import ParentReportService


CANDIDATE_SCHEMA = """CREATE TABLE combined_daily_snapshot (
 snapshot_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, child_id TEXT NOT NULL,
 day TEXT NOT NULL, recipient_identity TEXT NOT NULL, generated_at TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, encrypted_payload BLOB NOT NULL,
 status TEXT NOT NULL CHECK(status='local_only'), UNIQUE(batch_id,child_id))"""
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


def _schema(c):
    row=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='combined_daily_snapshot'").fetchone()
    if not row or ' '.join(row[0].split())!=' '.join(CANDIDATE_SCHEMA.split()):
        raise ValueError('COMBINED_CANDIDATE_SCHEMA_UNAVAILABLE')


def _aad(snapshot,batch,child,day,identity):
    return json.dumps(['combined-candidate-v1',snapshot,batch,child,day,identity],ensure_ascii=False,separators=(',',':')).encode()


def _pending_source_state(c, child, report):
    """Bind pending reports to current source state without exposing its text."""
    from .learning_feedback import session_report_feedback
    from .daily_behavior_source import daily_behavior_fingerprint
    from .document_revisit_memory import report_memory
    from .specialized_program import report_reference_fingerprint
    from .parent_supply_followup import report_supply_matches
    from .agent_runtime import _RuntimeTransactionDatabase
    from .repository import StudyRepository

    sid = report['session_id']
    session = c.execute('SELECT * FROM daily_session WHERE session_id=?', (sid,)).fetchone()
    reflections = c.execute('SELECT * FROM daily_reflection WHERE session_id=? ORDER BY reflection_id', (sid,)).fetchall()
    problem = ParentReportService.daily_source_problem(c, child, report['report_id'])
    state = {'problem': problem, 'session': dict(session) if session else None,
             'reflections': [dict(row) for row in reflections]}
    if problem:
        # Invalid/incomplete source is withheld; its raw report/session rows are
        # still bound through the outer hash, never included in the email.
        return state
    try:
        evidence = json.loads(report['evidence_json'])
        summary = json.loads(report['summary_json'])
        feedback = session_report_feedback(c, sid)
        dependencies = evidence.get('reliable_learning_dependencies')
        dependency_states = []
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if not isinstance(dependency, dict) or not isinstance(dependency.get('session_id'), str):
                    raise ValueError('COMBINED_PENDING_DEPENDENCY_INVALID')
                owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (dependency['session_id'],)).fetchone()
                dependency_states.append({'session_id': dependency['session_id'],
                    'owner_matches': bool(owner and owner['child_id'] == child),
                    'fingerprint': session_report_feedback(c, dependency['session_id'])['evidence_fingerprint'] if owner else None})
        state.update(feedback=feedback['evidence_fingerprint'], dependencies=dependency_states,
            behavior=daily_behavior_fingerprint(c, child, sid),
            mistake=StudyRepository(_RuntimeTransactionDatabase(c))._report_mistake_projection(c, sid, feedback)['fingerprint'],
            specialized=report_reference_fingerprint(c, child), revisit=report_memory(c, child, sid)['fingerprint'],
            supply_matches=report_supply_matches(c, child, sid, summary))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        # Do not replace an uninspectable source with an apparently stable hash.
        raise ValueError('COMBINED_PENDING_SOURCE_UNCONFIRMED') from error
    return state


def _collect(c,telemetry,batch_id,child,day,recipient_id,visual_id,key,formal_ids=None,*,for_inspection=False,processing_identity=None,allow_sent_members=False):
    telemetry._require_combined_schema(c); telemetry._joint_members_schema(c); _schema(c)
    batch=c.execute('SELECT * FROM combined_diagnostic_batch WHERE batch_id=?',(batch_id,)).fetchone()
    pilot=c.execute('SELECT * FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(batch_id,)).fetchone()
    allowed_batch_status = {'active','unknown'} if for_inspection else {'active'}
    if not batch or not pilot or batch['sealed']!=1 or batch['delivery_date']!=day or batch['status'] not in allowed_batch_status:
        raise ValueError('COMBINED_BATCH_UNAVAILABLE')
    if pilot['status']!='processing' or pilot['claim_token'] is not None or pilot['submission_token'] is not None:
        raise ValueError('COMBINED_PILOT_OWNERSHIP_CHANGED')
    members=c.execute('SELECT * FROM combined_diagnostic_member WHERE batch_id=? ORDER BY child_id',(batch_id,)).fetchall()
    if len(members)!=batch['expected_count'] or child not in [r['child_id'] for r in members]:
        raise ValueError('COMBINED_MEMBERS_UNCONFIRMED')
    if c.execute('''SELECT 1 FROM habit_experiment h JOIN habit_experiment_event e
        ON e.child_id=h.child_id AND e.experiment_id=h.experiment_id
        WHERE h.child_id=? AND h.status='active' AND e.event_type='auto_stop_pending' LIMIT 1''', (child,)).fetchone():
        raise ValueError('COMBINED_HABIT_STOP_PENDING')
    if c.execute('''SELECT 1 FROM intervention WHERE child_id=? AND status='active'
        AND withdrawal_reason='habit_support_source_changed_pending' LIMIT 1''', (child,)).fetchone():
        raise ValueError('COMBINED_INTERVENTION_WITHDRAWAL_PENDING')
    own=next(row for row in members if row['child_id']==child)
    own_expected='processing' if processing_identity is not None else 'waiting'
    allowed_other=({'waiting','sent','unknown'} if for_inspection else
                   {'waiting','sent'} if allow_sent_members else {'waiting'})
    if own['status']!=own_expected or any(row['status'] not in allowed_other for row in members if row['child_id']!=child):
        raise ValueError('COMBINED_BATCH_NOT_FRESH')
    claim=telemetry._frozen_claim(pilot)
    recipient=pilot['recipient'].strip().casefold()
    identity=telemetry._joint_hmac(key,['joint-log-recipient-v1',day,recipient])
    generation,authorities=telemetry._joint_authorities(c,recipient,[r['child_id'] for r in members])
    binding=telemetry._joint_hmac(key,['joint-log-members-v1',batch_id,day,identity,pilot['attachment_sha256'],generation,authorities])
    if not hmac.compare_digest(identity,batch['recipient_identity']) or not hmac.compare_digest(binding,batch['membership_sha256']):
        raise ValueError('COMBINED_BATCH_AUTHORITY_CHANGED')
    if _authority(c,child,day,recipient_id,key,processing_identity=processing_identity)!=recipient:
        raise ValueError('COMBINED_RECIPIENT_MISMATCH')
    visual=c.execute('''SELECT f.* FROM daily_observation_report_snapshot f JOIN daily_observation_report_outbox o
        ON o.source_id=f.source_id AND o.child_id=f.child_id AND o.day=f.day AND o.recipient_id=f.recipient_id
        WHERE f.snapshot_id=? AND f.child_id=? AND f.day=? AND f.recipient_id=? AND f.status='frozen_local'
        AND o.status='prepared' AND o.address_identity=?''',(visual_id,child,day,recipient_id,recipient)).fetchone()
    if not visual: raise ValueError('COMBINED_VISUAL_UNAVAILABLE')
    visual_body=decrypt_visual(visual,key)
    from .daily_report_frozen_snapshot import _consent_generation
    if (visual_body.get('address_identity')!=recipient
            or visual_body.get('consent_generation')!=_consent_generation(c,child,recipient_id)
            or not visual_dependencies_valid(c,child,visual_body.get('dependencies'))):
        raise ValueError('COMBINED_VISUAL_STALE')
    # Re-enumerate on every inspection: checking only the IDs in the frozen
    # payload would miss a report created after the snapshot was made. A day
    # with no completed/approved formal report may still contain observations.
    reports=c.execute('SELECT * FROM daily_report WHERE child_id=? AND report_date=? ORDER BY report_id',(child,day)).fetchall()
    current_formal_ids=[]; formal=[]; pending=[]
    for report in reports:
        rows=c.execute("SELECT * FROM parent_report_snapshot WHERE report_id=? AND child_id=? AND report_type='daily' AND source_kind='daily_report' ORDER BY snapshot_id",(report['report_id'],child)).fetchall()
        approved=[row for row in rows if row['status']=='approved']
        if len(approved)>1: raise ValueError('COMBINED_FORMAL_APPROVAL_REQUIRED')
        if approved and ParentReportService.snapshot_source_current(c,approved[0]):
            current_formal_ids.append(approved[0]['snapshot_id'])
            formal.append(dict(approved[0]))
        else:
            # The message contains only a count. The encrypted local candidate
            # binds a digest of unapproved or stale source state, not plaintext.
            # A further source change must invalidate even if it remains stale.
            pending.append({'report':dict(report),'snapshots':[dict(row) for row in rows],
                            'source_state':_pending_source_state(c,child,report)})
    if formal_ids is not None and (not isinstance(formal_ids,list) or formal_ids != current_formal_ids):
        raise ValueError('COMBINED_FORMAL_CHANGED')
    formal_ids=current_formal_ids
    return {'version':1,'batch_id':batch_id,'child_id':child,'day':day,'recipient_id':recipient_id,
            'formal':formal,'formal_pending_count':len(pending),
            'formal_pending_fingerprint':sha256(json.dumps(pending,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'visual_id':visual_id,'visual':visual_body,'log':{'digest':pilot['digest_sha256'],
            'sha256':pilot['attachment_sha256'],'manifest':claim.attachment_manifest,'window_start':claim.window_start_utc,
            'window_end':claim.window_end_utc,'policy':claim.window_policy},'membership_sha256':batch['membership_sha256']},identity


def freeze(telemetry,batch_id,child,day,recipient_id,visual_id,key):
    key=_key(key)
    with telemetry.database.external_effect_guard(timeout=10),telemetry.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        payload,identity=_collect(c,telemetry,batch_id,child,day,recipient_id,visual_id,key)
        if c.execute('SELECT 1 FROM combined_daily_snapshot WHERE batch_id=? AND child_id=?',(batch_id,child)).fetchone():
            raise ValueError('COMBINED_CANDIDATE_EXISTS')
        snapshot=uuid4().hex; generated=datetime.now(timezone.utc).isoformat()
        payload['generated_at']=generated
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>MAX_CANDIDATE_BYTES: raise ValueError('COMBINED_CANDIDATE_TOO_LARGE')
        nonce=os.urandom(12); ciphertext=b'HSC1'+nonce+AESGCM(key).encrypt(nonce,raw,_aad(snapshot,batch_id,child,day,identity))
        c.execute("INSERT INTO combined_daily_snapshot VALUES(?,?,?,?,?,?,?,?, 'local_only')",(snapshot,batch_id,child,day,identity,generated,sha256(raw).hexdigest(),ciphertext))
        c.commit()
        return {'snapshot_id':snapshot,'external_delivery_permitted':False}


def _inspect_in_connection(c,telemetry,snapshot_id,key,*,processing_identity=None):
    """Return verified plaintext only to an already transaction-owning caller."""
    key=_key(key)
    if not c.in_transaction: raise ValueError('COMBINED_TRANSACTION_REQUIRED')
    _schema(c)
    length=c.execute('SELECT length(encrypted_payload) FROM combined_daily_snapshot WHERE snapshot_id=?',(snapshot_id,)).fetchone()
    if length and (not isinstance(length[0],int) or length[0]>MAX_CANDIDATE_BYTES+32):
        raise ValueError('COMBINED_CANDIDATE_UNREADABLE')
    row=c.execute('SELECT * FROM combined_daily_snapshot WHERE snapshot_id=?',(snapshot_id,)).fetchone()
    if not row or row['status']!='local_only': raise ValueError('COMBINED_CANDIDATE_UNAVAILABLE')
    try:
        blob=bytes(row['encrypted_payload'])
        if not blob.startswith(b'HSC1') or len(blob)>MAX_CANDIDATE_BYTES+32: raise ValueError()
        raw=AESGCM(key).decrypt(blob[4:16],blob[16:],_aad(snapshot_id,row['batch_id'],row['child_id'],row['day'],row['recipient_identity']))
        if sha256(raw).hexdigest()!=row['payload_sha256']: raise ValueError()
        old=json.loads(raw)
        if old['generated_at']!=row['generated_at']: raise ValueError()
    except Exception: raise ValueError('COMBINED_CANDIDATE_UNREADABLE') from None
    current,identity=_collect(c,telemetry,row['batch_id'],row['child_id'],row['day'],old['recipient_id'],old['visual_id'],key,[x['snapshot_id'] for x in old['formal']],for_inspection=True,processing_identity=processing_identity)
    if (current.get('formal_pending_count') != old.get('formal_pending_count') or
            current.get('formal_pending_fingerprint') != old.get('formal_pending_fingerprint')):
        raise ValueError('COMBINED_FORMAL_CHANGED')
    current['generated_at']=row['generated_at']
    if identity!=row['recipient_identity'] or current!=old:
        raise ValueError('COMBINED_CANDIDATE_STALE')
    return row,old


def inspect(telemetry,snapshot_id,key):
    key=_key(key)
    with telemetry.database.external_effect_guard(timeout=10),telemetry.database.connect() as c:
        c.execute('BEGIN')
        _inspect_in_connection(c,telemetry,snapshot_id,key)
        return {'snapshot_id':snapshot_id,'current':True,'external_delivery_permitted':False,'source_withdrawal_checked':False}
