"""Local admission ledger for future daily report delivery. No network calls.

A `ready` row is a durable local plan, never permission to send. Processing,
unknown and sent are reserved for a future transport and cannot be created by
this module. Restore or erasure of unresolved history creates a global barrier.
"""
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import re

from .daily_report_observation import _authority
from .daily_report_frozen_snapshot import _decrypt, _key, _consent_generation
from .daily_report_frozen_dependencies import valid
from .family_clock import family_time

LEDGER_SCHEMA = """CREATE TABLE IF NOT EXISTS daily_observation_delivery_ledger (
 identity TEXT PRIMARY KEY, child_id TEXT NOT NULL, snapshot_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('ready','processing','unknown','sent')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)"""
BARRIER_SCHEMA = """CREATE TABLE IF NOT EXISTS daily_observation_delivery_barrier (
 singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
 blocked INTEGER NOT NULL CHECK(blocked IN (0,1)), reason TEXT NOT NULL
)"""


def schema_valid(c):
    def normalized(sql): return ' '.join(sql.replace('IF NOT EXISTS ', '').split())
    for name, ddl in (('daily_observation_delivery_ledger',LEDGER_SCHEMA),('daily_observation_delivery_barrier',BARRIER_SCHEMA)):
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if not row or normalized(row[0]) != normalized(ddl): return False
    barrier = c.execute('SELECT blocked,reason FROM daily_observation_delivery_barrier WHERE singleton_id=1').fetchone()
    return bool(barrier and ((barrier[0] == 0 and barrier[1] == '') or
        (barrier[0] == 1 and barrier[1] in ('RESTORE_HISTORY_UNRECONCILED','DELIVERY_HISTORY_ERASED'))))


def initialize_barrier(c):
    # Existing restore quarantine may predate this table; do not infer that
    # missing post-backup receipts mean no submission happened.
    restored = c.execute("SELECT 1 FROM audit_event WHERE action='report_delivery_restore_quarantined' LIMIT 1").fetchone()
    c.execute('INSERT OR IGNORE INTO daily_observation_delivery_barrier VALUES(1,?,?)',
              (int(bool(restored)), 'RESTORE_HISTORY_UNRECONCILED' if restored else ''))


def block_after_restore(c):
    c.execute("""INSERT INTO daily_observation_delivery_barrier VALUES(1,1,'RESTORE_HISTORY_UNRECONCILED')
        ON CONFLICT(singleton_id) DO UPDATE SET blocked=1,reason='RESTORE_HISTORY_UNRECONCILED'""")
    c.execute("UPDATE daily_observation_delivery_ledger SET status='unknown' WHERE status='processing'")
    c.execute("DELETE FROM daily_observation_delivery_ledger WHERE status='ready'")


def identity_key(key,child_id,day,address):
    payload = json.dumps(['daily_observation_delivery_v1',child_id,day,address],ensure_ascii=False,separators=(',', ':')).encode()
    key = _key(key)
    prefix = hmac.new(key,b'daily_observation_delivery_key_v1',sha256).hexdigest()[:32]
    return prefix + ':' + hmac.new(key,payload,sha256).hexdigest()


def assert_ledger_key(c, key):
    rows = c.execute('SELECT DISTINCT substr(identity,1,33) FROM daily_observation_delivery_ledger').fetchall()
    if not rows:
        return
    expected = identity_key(key,'','','').split(':',1)[0] + ':'
    if any(row[0] != expected for row in rows):
        raise ValueError('DELIVERY_KEY_HISTORY_UNRECONCILED')


def reserve_local(service, child_id, day, recipient_id, snapshot_id, *, expected_snapshot_id=None, now=None):
    """Persist local readiness; block uncertain prior outcomes and never send."""
    from .daily_report_email_verification import verified
    key = _key(service.encryption_key)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None: raise ValueError('INSTANT_INVALID')
    with service.database.external_effect_guard(), service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            barrier = c.execute('SELECT blocked,reason FROM daily_observation_delivery_barrier WHERE singleton_id=1').fetchone()
            if not barrier or barrier['blocked'] != 0: raise ValueError('RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED')
            assert_ledger_key(c,key)
            address = _authority(c,child_id,day,recipient_id,key)
            consent = _consent_generation(c,child_id,recipient_id)
            if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
                raise ValueError('DELETION_PENDING')
            row = c.execute('''SELECT f.* FROM daily_observation_report_snapshot f
                JOIN daily_observation_report_outbox o ON o.source_id=f.source_id AND o.child_id=f.child_id
                    AND o.day=f.day AND o.recipient_id=f.recipient_id
                WHERE f.snapshot_id=? AND f.child_id=? AND f.day=? AND f.recipient_id=?
                  AND o.address_identity=? AND o.status='prepared' AND f.status='frozen_local' ''',
                (snapshot_id,child_id,day,recipient_id,address)).fetchone()
            if not row: raise ValueError('FROZEN_SNAPSHOT_NOT_FOUND')
            body = _decrypt(row,key)
            if body.get('address_identity') != address or body.get('consent_generation') != consent:
                raise ValueError('FROZEN_RECIPIENT_CHANGED')
            if not valid(c,child_id,body.get('dependencies')): raise ValueError('FROZEN_SOURCE_CHANGED')
            preferences = c.execute('SELECT * FROM report_delivery_preference WHERE child_id=?', (child_id,)).fetchone()
            start,end,redaction = (preferences['quiet_start'],preferences['quiet_end'],preferences['redaction_level']) if preferences else ('21:00','07:00','standard')
            if (redaction not in ('standard','minimal') or not isinstance(start,str) or not isinstance(end,str)
                    or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',start)
                    or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',end)
                    or preferences and preferences['timezone'] != 'Asia/Shanghai'):
                raise ValueError('DISCLOSURE_PREFERENCE_INVALID')
            current = family_time(instant).strftime('%H:%M')
            if (start <= current < end if start < end else current >= start or current < end):
                raise ValueError('QUIET_HOURS')
            config = service._config('email')
            if config.get('allow_external_delivery') is not True: raise ValueError('EMAIL_EXTERNAL_DELIVERY_NOT_AUTHORIZED')
            test = c.execute("SELECT status FROM report_delivery_channel_test WHERE child_id=? AND channel='email'", (child_id,)).fetchone()
            if not test or test['status'] != 'passed': raise ValueError('EMAIL_CHANNEL_TEST_REQUIRED')
            if not verified(c,child_id,recipient_id,key,config.get('password') or ''):
                raise ValueError('RECIPIENT_EMAIL_TEST_REQUIRED')
            token = identity_key(key,child_id,day,address)
            old = c.execute('SELECT * FROM daily_observation_delivery_ledger WHERE identity=?', (token,)).fetchone()
            if old and old['status'] != 'ready': raise ValueError('PRIOR_DELIVERY_UNRESOLVED_OR_SENT')
            timestamp = datetime.now(timezone.utc).isoformat()
            if old:
                if old['child_id'] != child_id: raise ValueError('DELIVERY_IDENTITY_CONFLICT')
                if old['snapshot_id'] != snapshot_id:
                    if expected_snapshot_id != old['snapshot_id']: raise ValueError('DELIVERY_PLAN_CONFLICT')
                    c.execute("UPDATE daily_observation_delivery_ledger SET snapshot_id=?,updated_at=? WHERE identity=? AND status='ready'", (snapshot_id,timestamp,token))
            else:
                if expected_snapshot_id is not None: raise ValueError('DELIVERY_PLAN_CONFLICT')
                c.execute("INSERT INTO daily_observation_delivery_ledger VALUES(?,?,?,'ready',?,?)", (token,child_id,snapshot_id,timestamp,timestamp))
            c.commit()
            return {'status':'ready_local_only','external_delivery_permitted':False,'redaction_level':redaction}
        except BaseException:
            c.rollback()
            raise
