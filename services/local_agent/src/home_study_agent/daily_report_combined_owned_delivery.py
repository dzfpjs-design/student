"""Owned-process combined SMTP admission, limited to synthetic loopback.

The hard combined gate remains in force.  The owner's one effect-guard thread
performs both prior authorization and the atomic journal/claim transitions.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import make_msgid
from uuid import uuid4
from threading import Lock
import sqlite3

from . import daily_report_combined_delivery as combined, smtp_owned_journal as journal
from .daily_report_combined_snapshot import _inspect_in_connection
from .daily_report_visual_submission import _check as check_visual
from .notification_channels import validate_email_attachments
from .smtp_owned_process import OwnedSmtpProcess, _COMBINED_PUBLIC_TRANSPORT_AUTHORITY
from .smtp_owned_worker import _public_domain
from .database import ExternalEffectBusyError

_pending_lock = Lock()
_pending_owners = {}


def _pending_key(service, snapshot_id):
    return (str(service.database.path.resolve()), snapshot_id)


def cleanup_status_for_child(service, telemetry, child_id):
    """Expose retained owner cleanup; terminal states are checked against SQLite."""
    path = str(service.database.path.resolve())
    with _pending_lock:
        local = {key[1] for key, item in _pending_owners.items()
                 if key[0] == path and item[1] == child_id}
    try:
        with service.database.connect() as c:
            current = {row[0] for row in c.execute('''SELECT s.snapshot_id FROM combined_daily_snapshot s
                JOIN daily_visual_schedule d ON d.child_id=s.child_id AND d.next_day=s.day
                WHERE s.child_id=? AND d.enabled=1''', (child_id,))}
    except sqlite3.Error:
        return {'status':'unknown'}
    states = []
    for snapshot_id in local | current:
        item = status(service, telemetry, snapshot_id)
        # A never-attempted waiting candidate is normal; without a local owner,
        # do not mistake its ready ledger for a failed prior submission.
        if snapshot_id in local or item['status'] in ('cleanup_pending', 'unknown', 'sent'):
            states.append(item)
    for name in ('cleanup_pending', 'unknown', 'blocked', 'sent', 'not_submitted'):
        match = next((item for item in states if item['status'] == name), None)
        if match:
            return match
    return None


def status(service, telemetry, snapshot_id):
    """Inspect an owned attempt without retrying or trusting a live worker."""
    if service.database.path.resolve() != telemetry.database.path.resolve():
        raise ValueError('COMBINED_DATABASE_MISMATCH')
    key = _pending_key(service, snapshot_id)
    with _pending_lock:
        item = _pending_owners.get(key)
    if item is not None:
        owner = item[0]
        if owner.thread is None or owner.thread.is_alive():
            return {'status':'cleanup_pending'}
        if (not owner.confirmed or not owner.cleaned) and not (owner.process is None and not owner.intent):
            return {'status':'unknown'}
    try:
        with service.database.external_effect_guard(timeout=.05), service.database.connect() as c:
            c.execute('BEGIN')
            if not combined.schema_valid(c) or not journal.schema_valid_v99(c):
                return {'status':'unknown'}
            snap = c.execute('SELECT batch_id,child_id FROM combined_daily_snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            if not snap:
                return {'status':'unknown'}
            claims = c.execute('SELECT * FROM combined_daily_delivery WHERE batch_id=? AND child_id=?', (snap['batch_id'],snap['child_id'])).fetchall()
            bindings = c.execute('SELECT b.*,j.status AS journal_status,j.result FROM smtp_owned_worker_binding b JOIN smtp_owned_worker_journal j ON j.dispatch_id=b.dispatch_id WHERE b.batch_id=? AND b.child_id=?', (snap['batch_id'],snap['child_id'])).fetchall()
            if not claims and not bindings:
                try:
                    _view(c, service, telemetry, snapshot_id)
                except (ValueError, LookupError):
                    return {'status':'blocked','reason':'COMBINED_CANDIDATE_RECHECK_REQUIRED'}
                result = {'status':'not_submitted','reason':'SMTP_WORKER_ADMISSION_FAILED'}
            elif len(claims)==len(bindings)==1:
                claim, binding = claims[0], bindings[0]
                ledger = c.execute('SELECT status,snapshot_id,child_id FROM daily_observation_delivery_ledger WHERE identity=?', (claim['identity'],)).fetchone()
                member = c.execute('SELECT status,claim_token FROM combined_diagnostic_member WHERE batch_id=? AND child_id=?', (claim['batch_id'],claim['child_id'])).fetchone()
                matches = (claim['snapshot_id']==snapshot_id and binding['snapshot_id']==snapshot_id
                    and claim['claim_token']==binding['dispatch_id']==binding['claim_token']
                    and claim['identity']==binding['delivery_identity'] and ledger and member
                    and ledger['snapshot_id']==binding['visual_snapshot_id'] and ledger['child_id']==claim['child_id']
                    and member['claim_token']==claim['claim_token'])
                if matches and claim['status']==ledger['status']==member['status']=='sent' and binding['journal_status']=='stopped' and binding['result']=='accepted':
                    result = {'status':'sent'}
                else:
                    result = {'status':'unknown'}
            else:
                result = {'status':'unknown'}
    except ExternalEffectBusyError:
        return {'status':'cleanup_pending'}
    except sqlite3.Error:
        return {'status':'unknown'}
    return result


def _now():return datetime.now(timezone.utc).isoformat()


def _view(c,service,telemetry,snapshot_id,expected=None):
    if not combined.schema_valid(c) or not journal.schema_valid(c): raise ValueError('COMBINED_DELIVERY_SCHEMA_UNAVAILABLE')
    row,body=_inspect_in_connection(c,telemetry,snapshot_id,service.encryption_key)
    identity,address,config,redaction,_=check_visual(c,service,row['child_id'],row['day'],body['recipient_id'],body['visual_id'])
    synthetic = config.get('host')=='127.0.0.1' and address.endswith('@example.test') and address.count('@')==1
    if not synthetic:
        if not combined._source_withdrawal_guard_proven():
            raise ValueError('COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE')
        if not _public_domain(config.get('host')):
            raise ValueError('SMTP_PUBLIC_HOST_INVALID')
        use_ssl,starttls,_=_transport(config)
        if not (use_ssl or starttls):
            raise ValueError('SMTP_PUBLIC_TLS_REQUIRED')
    pilot=c.execute('SELECT * FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(row['batch_id'],)).fetchone()
    log=telemetry._frozen_claim(pilot)
    subject,text,html=combined._render(body,redaction)
    current={'identity':identity,'address':address,'config':config,'redaction':redaction,'body':body,
             'child_id':row['child_id'],'day':row['day'],'batch_id':row['batch_id'],
             'subject':subject,'text':text,'html':html,'attachments':log.attachments}
    if expected is not None and current!=expected:raise ValueError('COMBINED_AUTHORITY_CHANGED')
    return current


def _mime(view):
    attachments=validate_email_attachments(view['attachments'])
    config=view['config'];sender=str(config.get('from_address') or config.get('username') or '').strip()
    if not sender:raise ValueError('SMTP_CONFIG_INVALID')
    message=EmailMessage();message['Subject']=view['subject'];message['From']=sender;message['To']=view['address']
    message['Message-ID']=make_msgid(domain='home-study.local')
    message.set_content(view['text']);message.add_alternative(view['html'],subtype='html')
    message.add_attachment(view['html'].encode('utf-8'),maintype='text',subtype='html',filename='学习成长报告.html')
    for item in attachments:message.add_attachment(item.content,maintype='application',subtype='zip',filename=item.filename)
    return sender,message.as_bytes(policy=SMTP)


def _transport(config):
    """Preserve the saved transport choice; never guess away TLS."""
    use_ssl=config.get('use_ssl',False)
    starttls=config.get('starttls',False if use_ssl is True else True)
    if type(use_ssl) is not bool or type(starttls) is not bool or (use_ssl and starttls):
        raise ValueError('SMTP_TLS_MODE_INVALID')
    port=config.get('port')
    if port is None or port=='':port=465 if use_ssl else 587
    if isinstance(port,str):
        if not port.isascii() or not port.isdigit() or len(port)>5:raise ValueError('SMTP_PORT_INVALID')
        port=int(port)
    elif type(port) is not int:
        raise ValueError('SMTP_PORT_INVALID')
    if not 1<=port<=65535:raise ValueError('SMTP_PORT_INVALID')
    return use_ssl,starttls,port


def _claim(c,service,telemetry,snapshot_id,expected,dispatch_id,process_identity,members,*,job,process):
    if not c.in_transaction:raise ValueError('COMBINED_TRANSACTION_REQUIRED')
    view=_view(c,service,telemetry,snapshot_id,expected)
    if c.execute("SELECT 1 FROM smtp_owned_worker_journal WHERE status='pending' LIMIT 1").fetchone():
        raise ValueError('SMTP_WORKER_RECOVERY_PENDING')
    if c.execute('SELECT 1 FROM combined_daily_delivery WHERE claim_token=? LIMIT 1',(dispatch_id,)).fetchone():
        raise ValueError('COMBINED_CLAIM_LOST')
    timestamp=_now();identity=view['identity'];batch=view['batch_id'];child=view['child_id'];visual=view['body']['visual_id']
    if c.execute("UPDATE daily_observation_delivery_ledger SET status='processing',updated_at=? WHERE identity=? AND child_id=? AND snapshot_id=? AND status='ready'",(timestamp,identity,child,visual)).rowcount!=1:raise ValueError('COMBINED_CLAIM_LOST')
    if c.execute("UPDATE combined_diagnostic_member SET status='processing',claim_token=?,updated_at=? WHERE batch_id=? AND child_id=? AND status='waiting' AND claim_token IS NULL",(dispatch_id,timestamp,batch,child)).rowcount!=1:raise ValueError('COMBINED_CLAIM_LOST')
    c.execute("INSERT INTO combined_daily_delivery VALUES(?,?,?,?,'processing',?,?,?)",(identity,batch,child,snapshot_id,dispatch_id,timestamp,timestamp))
    binding={'kind':'combined','delivery_identity':identity,'batch_id':batch,'child_id':child,
             'snapshot_id':snapshot_id,'visual_snapshot_id':visual,'claim_token':dispatch_id}
    journal.reserve_in_transaction(service.database,c,dispatch_id,process_identity,members,binding,job=job,process=process)


def _finish(c,service,dispatch_id,result,*,job):
    if not c.in_transaction or result not in ('accepted','unknown'):raise ValueError('COMBINED_TRANSACTION_REQUIRED')
    matches=c.execute('SELECT * FROM combined_daily_delivery WHERE claim_token=?',(dispatch_id,)).fetchall()
    if len(matches)!=1 or matches[0]['status']!='processing':raise ValueError('SMTP_COMBINED_RECOVERY_UNCONFIRMED')
    item=matches[0];status='sent' if result=='accepted' else 'unknown'
    combined._finish(c,item['identity'],item['batch_id'],item['child_id'],item['snapshot_id'],dispatch_id,status)
    journal.finish_in_transaction(service.database,c,dispatch_id,result,job=job)


def submit(service,telemetry,snapshot_id,*,timeout_seconds=30):
    if not combined._source_withdrawal_guard_proven():raise ValueError('COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE')
    if not callable(getattr(journal,'reserve_in_transaction',None)) or not callable(getattr(journal,'finish_in_transaction',None)):
        raise ValueError('SMTP_COMBINED_JOURNAL_UNAVAILABLE')
    if service.database.path.resolve()!=telemetry.database.path.resolve():raise ValueError('COMBINED_DATABASE_MISMATCH')
    if service.database._external_effect_guard_owned():raise ValueError('SMTP_OWNER_LOCK_ORDER_INVALID')
    key = _pending_key(service, snapshot_id)
    with _pending_lock:
        previous = _pending_owners.get(key)
    if previous is not None:
        prior = status(service, telemetry, snapshot_id)
        if prior['status'] != 'not_submitted':
            return prior
        with _pending_lock:
            if _pending_owners.get(key) is previous:
                _pending_owners.pop(key, None)
    # This first read builds a candidate envelope. The owner's guard rechecks
    # it before spawning a worker, i.e. before MAIL/RCPT reveal the address.
    with service.database.external_effect_guard(timeout=10),service.database.connect() as c:
        c.execute('BEGIN');expected=_view(c,service,telemetry,snapshot_id);c.rollback()
    sender,mime=_mime(expected);config=expected['config']
    dispatch_id=uuid4().hex
    preflight_error=[]
    def preflight():
        try:
            with service.database.connect() as c:
                c.execute('BEGIN');_view(c,service,telemetry,snapshot_id,expected);c.rollback()
        except (ValueError,LookupError) as error:
            code=str(error)
            preflight_error.append(code if code and len(code)<=80 and all(x.isupper() or x.isdigit() or x=='_' for x in code) else 'COMBINED_AUTHORITY_CHANGED')
            raise
        return True
    @contextmanager
    def owner_guard():
        with service.database.external_effect_guard(timeout=10):
            preflight()
            yield
    def persist(identity,members,job,process):
        with service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                _claim(c,service,telemetry,snapshot_id,expected,dispatch_id,identity,members,job=job,process=process)
                c.commit()
            except BaseException:
                # Drop SQLite's writer lock before owner cleanup opens another
                # transaction, even while the original traceback holds a cursor.
                c.rollback()
                raise
    def finish(result,job):
        with service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE');_finish(c,service,dispatch_id,result,job=job);c.commit()
    use_ssl,starttls,port=_transport(config)
    owner=OwnedSmtpProcess(owner_guard,preflight,persist,finish)
    transport=dict(host=config['host'],port=port,username=str(config.get('username') or ''),
                   password=str(config.get('password') or ''),sender=sender,recipient=expected['address'],mime_bytes=mime,
                   timeout_seconds=timeout_seconds,use_ssl=use_ssl,starttls=starttls)
    if not (config['host']=='127.0.0.1' and expected['address'].endswith('@example.test')):
        transport['_public_authority']=_COMBINED_PUBLIC_TRANSPORT_AUTHORITY
    result=owner.submit(**transport)
    if result['status']=='accepted':return {'status':'sent'}
    if (result['status']=='not_submitted' or (owner.thread is not None and not owner.thread.is_alive()
            and owner.process is None and not owner.intent)):
        return {'status':'blocked','reason':preflight_error[-1] if preflight_error else 'SMTP_WORKER_NOT_READY'}
    if result['status']=='cleanup_pending':
        with _pending_lock:
            _pending_owners[key]=(owner,expected['child_id'],dispatch_id)
        return {'status':'cleanup_pending'}
    # If the owner has definitely ended and its atomic admission left neither
    # journal nor claim, GO was never written. Do not mislabel that safe
    # admission failure as an ambiguous delivery that must never be retried.
    if owner.thread is not None and not owner.thread.is_alive() and owner.confirmed and owner.cleaned:
        checked=status(service,telemetry,snapshot_id)
        if checked['status']=='not_submitted':return {'status':'blocked','reason':'SMTP_WORKER_ADMISSION_FAILED'}
        if checked['status']=='sent':return checked
    return {'status':'unknown'}
