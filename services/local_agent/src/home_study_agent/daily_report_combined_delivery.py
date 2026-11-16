"""Disabled combined-delivery candidate; never registered as an API or scheduler.

Until source withdrawal is fully serialized, ordinary calls fail before a claim.
Transport is restricted to synthetic loopback and reserved example.test mail.
"""
from datetime import datetime, timezone
from html import escape
import json
from uuid import uuid4

from .daily_report_combined_snapshot import _inspect_in_connection
from .daily_report_combined_schema import DELIVERY_SCHEMA
from .daily_report_visual_submission import _check as check_visual, _render as render_visual
from .notification_channels import SmtpChannel, ChannelSubmissionCancelled, submission_guard
from .report_delivery import render_message
_ORIGINAL_SMTP_SEND = SmtpChannel.send

def schema_valid(c):
    row=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='combined_daily_delivery'").fetchone()
    normalize=lambda sql:' '.join(sql.replace('IF NOT EXISTS ','').split())
    return bool(row and normalize(row[0])==normalize(DELIVERY_SCHEMA))


def _source_withdrawal_guard_proven():
    # This cannot be overridden by requests, configuration or environment.
    # The source inventory and deletion/restore serialisation are not complete.
    return False


def _now(): return datetime.now(timezone.utc).isoformat()


def _render(body,redaction):
    sections=[]
    for raw in body['formal']:
        formal=dict(raw)
        for key in ('facts','inferences','content','confidence','parent_edit'):
            formal[key]=json.loads(formal.pop(key+'_json'))
        _,text,_=render_message(formal,redaction); sections.append(text)
    if body.get('formal_pending_count',0):
        sections.append(f"另有 {body['formal_pending_count']} 份学习报告需要家长核对，未纳入邮件。")
    _,visual,_=render_visual(body['day'],body['visual'],redaction); sections.append(visual)
    text='\n\n'.join(sections)
    return f"学习综合日报｜{body['day']}",text,'<html><body><pre>'+escape(text)+'</pre></body></html>'


def _finish(c,identity,batch,child,snapshot,token,status):
    timestamp=_now()
    changes=[c.execute("UPDATE combined_daily_delivery SET status=?,updated_at=? WHERE identity=? AND batch_id=? AND child_id=? AND snapshot_id=? AND claim_token=? AND status='processing'",(status,timestamp,identity,batch,child,snapshot,token)).rowcount,
             c.execute("UPDATE daily_observation_delivery_ledger SET status=?,updated_at=? WHERE identity=? AND child_id=? AND status='processing'",(status,timestamp,identity,child)).rowcount,
             c.execute("UPDATE combined_diagnostic_member SET status=?,updated_at=? WHERE batch_id=? AND child_id=? AND claim_token=? AND status='processing'",(status,timestamp,batch,child,token)).rowcount]
    if changes!=[1,1,1]: raise ValueError('COMBINED_CLAIM_LOST')
    if status=='unknown':
        if c.execute("UPDATE combined_diagnostic_batch SET status='unknown',updated_at=? WHERE batch_id=? AND status IN ('active','unknown')",(timestamp,batch)).rowcount!=1:
            raise ValueError('COMBINED_BATCH_CHANGED')
    else:
        remaining=c.execute("SELECT count(*) FROM combined_diagnostic_member WHERE batch_id=? AND status!='sent'",(batch,)).fetchone()[0]
        if remaining==0:
            b=c.execute('SELECT expected_count,sealed FROM combined_diagnostic_batch WHERE batch_id=?',(batch,)).fetchone()
            count=c.execute('SELECT count(*) FROM combined_diagnostic_member WHERE batch_id=?',(batch,)).fetchone()[0]
            if not b or b['sealed']!=1 or count!=b['expected_count']: raise ValueError('COMBINED_MEMBERS_UNCONFIRMED')
            if c.execute("UPDATE pilot_test_digest_outbox SET status='sent',accepted_at=?,updated_at=? WHERE combined_batch_id=? AND status='processing'",(timestamp,timestamp,batch)).rowcount!=1:
                raise ValueError('PILOT_JOINT_CLAIM_LOST')
            if c.execute("UPDATE combined_diagnostic_batch SET status='sent',updated_at=? WHERE batch_id=? AND status IN ('active','unknown')",(timestamp,batch)).rowcount!=1:
                raise ValueError('COMBINED_BATCH_CHANGED')


def _defer(c,identity,batch,child,snapshot,token):
    timestamp=_now()
    changes=[c.execute("DELETE FROM combined_daily_delivery WHERE identity=? AND batch_id=? AND child_id=? AND snapshot_id=? AND claim_token=? AND status='processing'",(identity,batch,child,snapshot,token)).rowcount,
             c.execute("UPDATE daily_observation_delivery_ledger SET status='ready',updated_at=? WHERE identity=? AND child_id=? AND status='processing'",(timestamp,identity,child)).rowcount,
             c.execute("UPDATE combined_diagnostic_member SET status='waiting',claim_token=NULL,updated_at=? WHERE batch_id=? AND child_id=? AND claim_token=? AND status='processing'",(timestamp,batch,child,token)).rowcount]
    if changes!=[1,1,1]: raise ValueError('COMBINED_CLAIM_LOST')


def submit(service,telemetry,snapshot_id):
    if not _source_withdrawal_guard_proven(): raise ValueError('COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE')
    if service.database.path.resolve()!=telemetry.database.path.resolve(): raise ValueError('COMBINED_DATABASE_MISMATCH')
    key=service.encryption_key
    with service.database.external_effect_guard(timeout=10):
        with service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not schema_valid(c): raise ValueError('COMBINED_DELIVERY_SCHEMA_UNAVAILABLE')
            row,body=_inspect_in_connection(c,telemetry,snapshot_id,key)
            identity,address,config,redaction,_=check_visual(c,service,row['child_id'],row['day'],body['recipient_id'],body['visual_id'])
            if config.get('host')!='127.0.0.1' or not address.endswith('@example.test') or address.count('@')!=1:
                raise ValueError('COMBINED_LOOPBACK_ONLY')
            if type(service.adapters.get('email')) is not SmtpChannel:
                raise ValueError('COMBINED_NATIVE_SMTP_REQUIRED')
            pilot=c.execute('SELECT * FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(row['batch_id'],)).fetchone()
            log=telemetry._frozen_claim(pilot)
            subject,text,html=_render(body,redaction); token=uuid4().hex; timestamp=_now()
            if c.execute("UPDATE daily_observation_delivery_ledger SET status='processing',updated_at=? WHERE identity=? AND child_id=? AND snapshot_id=? AND status='ready'",(timestamp,identity,row['child_id'],body['visual_id'])).rowcount!=1: raise ValueError('COMBINED_CLAIM_LOST')
            if c.execute("UPDATE combined_diagnostic_member SET status='processing',claim_token=?,updated_at=? WHERE batch_id=? AND child_id=? AND status='waiting'",(token,timestamp,row['batch_id'],row['child_id'])).rowcount!=1: raise ValueError('COMBINED_CLAIM_LOST')
            c.execute("INSERT INTO combined_daily_delivery VALUES(?,?,?,?,'processing',?,?,?)",(identity,row['batch_id'],row['child_id'],snapshot_id,token,timestamp,timestamp))
            c.commit()
        quiet_denied=False
        def check():
            nonlocal quiet_denied
            try:
                with service.database.connect() as c:
                    c.execute('BEGIN')
                    _inspect_in_connection(c,telemetry,snapshot_id,key,processing_identity=identity)
                    _,current_address,_,current_redaction,_=check_visual(c,service,row['child_id'],row['day'],body['recipient_id'],body['visual_id'],processing=identity,expected_config=config)
                    current=c.execute("SELECT 1 FROM combined_daily_delivery d JOIN combined_diagnostic_member m ON m.batch_id=d.batch_id AND m.child_id=d.child_id WHERE d.identity=? AND d.snapshot_id=? AND d.status='processing' AND d.claim_token=? AND m.status='processing' AND m.claim_token=?",(identity,snapshot_id,token,token)).fetchone()
                    if not current or current_address!=address or current_redaction!=redaction: return 'combined_authority_changed'
                    c.rollback()
            except ValueError as error:
                if str(error)=='QUIET_HOURS':
                    quiet_denied=True
                    return 'combined_quiet_hours'
                return 'combined_authority_changed'
            except Exception: return 'combined_authority_changed'
            return None
        status='unknown'
        try:
            with submission_guard(check):
                service.adapters['email'].send(address,subject,text,html,config,attachments=log.attachments)
            status='sent'
        except ChannelSubmissionCancelled as error:
            if (quiet_denied and error.reason=='combined_quiet_hours'
                    and type(service.adapters.get('email')) is SmtpChannel and SmtpChannel.send is _ORIGINAL_SMTP_SEND):
                status='ready'
        except Exception:
            status='unknown'
        with service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if status=='ready': _defer(c,identity,row['batch_id'],row['child_id'],snapshot_id,token)
            else: _finish(c,identity,row['batch_id'],row['child_id'],snapshot_id,token,status)
            c.commit()
        return {'status':status}


def recover(service):
    """Conservatively reconcile persisted in-flight claims, never re-submit."""
    with service.database.external_effect_guard(timeout=10),service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not schema_valid(c): raise ValueError('COMBINED_DELIVERY_SCHEMA_UNAVAILABLE')
        rows=c.execute("SELECT * FROM combined_daily_delivery WHERE status='processing'").fetchall()
        for row in rows: _finish(c,row['identity'],row['batch_id'],row['child_id'],row['snapshot_id'],row['claim_token'],'unknown')
        c.commit(); return len(rows)
