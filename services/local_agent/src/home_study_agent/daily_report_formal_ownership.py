"""Transactional ownership handoff for a daily report and diagnostic ZIP.

All functions take a caller-owned SQLite transaction. A handoff only retires
queue rows with durable evidence that no delivery attempt ever occurred.
"""
from datetime import date
import json
from uuid import uuid4
from .repository import utc_now

RESERVATION = 'COMBINED_DELIVERY_RESERVED'
TAKEOVER = 'combined_daily_formal_takeover'
TOMBSTONE = 'COMBINED_PRIVACY_TOMBSTONE'


def _canonical(day):
    try:
        return isinstance(day,str) and date.fromisoformat(day).isoformat()==day
    except ValueError:
        return False


def owns(c, day, address):
    """Fail closed on ambiguity; only a persisted joint identity asserts ownership."""
    if not _canonical(day) or not isinstance(address,str) or not address.strip():
        return False
    address=address.strip().casefold()
    rows=[r for r in c.execute('SELECT recipient,error_code,combined_batch_id FROM pilot_test_digest_outbox WHERE delivery_date=?',(day,))
          if isinstance(r['recipient'],str) and r['recipient'].strip().casefold()==address]
    return any(r['error_code'] in (RESERVATION,TOMBSTONE) or r['combined_batch_id'] is not None for r in rows)


def snapshot_owned(c, snapshot_id, recipient_id):
    row=c.execute('''SELECT s.report_type,s.child_id,d.report_date,r.child_id AS recipient_child,r.channel,r.address
        FROM parent_report_snapshot s LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
        LEFT JOIN report_delivery_recipient r ON r.recipient_id=? WHERE s.snapshot_id=?''',(recipient_id,snapshot_id)).fetchone()
    if not row or row['report_type']!='daily' or row['channel']!='email': return False
    from .daily_report_combined_mode import blocks_legacy
    if blocks_legacy(c): return True
    if row['child_id']!=row['recipient_child']: return True
    if not _canonical(row['report_date']):
        # A malformed/ambiguous formal identity must not bypass any active joint owner.
        return bool(c.execute("SELECT 1 FROM pilot_test_digest_outbox WHERE error_code IN (?,?) OR combined_batch_id IS NOT NULL LIMIT 1",(RESERVATION,TOMBSTONE)).fetchone())
    return owns(c,row['report_date'],row['address'])


def take_over_unsent(c,day,address):
    """Retire safe legacy queue entries while retaining their reports and audit."""
    if not c.in_transaction or not _canonical(day): raise ValueError('COMBINED_FORMAL_IDENTITY_INVALID')
    address=address.strip().casefold()
    rows=c.execute('''SELECT o.*,s.child_id,d.report_date,r.child_id AS recipient_child,r.address
        FROM report_delivery_outbox o JOIN parent_report_snapshot s ON s.snapshot_id=o.snapshot_id
        LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
        LEFT JOIN report_delivery_recipient r ON r.recipient_id=o.recipient_id
        WHERE s.report_type='daily' AND o.channel='email' ''').fetchall()
    selected=[]
    for row in rows:
        if not isinstance(row['address'],str) or row['child_id']!=row['recipient_child']:
            raise ValueError('COMBINED_FORMAL_HISTORY_UNRESOLVED')
        if row['address'].strip().casefold()!=address: continue
        if not _canonical(row['report_date']): raise ValueError('COMBINED_FORMAL_HISTORY_UNRESOLVED')
        if row['report_date']!=day: continue
        attempt=c.execute('SELECT 1 FROM report_delivery_attempt WHERE delivery_id=? LIMIT 1',(row['delivery_id'],)).fetchone()
        if (row['status']!='scheduled' or row['attempt_count']!=0 or attempt or row['sent_at'] is not None
                or row['provider_message_id'] is not None or row['replacement_delivery_id'] is not None
                or row['reissue_action_id'] is not None):
            raise ValueError('COMBINED_FORMAL_HISTORY_UNRESOLVED')
        selected.append(row)
    now=utc_now()
    for row in selected:
        changed=c.execute("UPDATE report_delivery_outbox SET status='cancelled',cancel_reason=?,cancelled_at=?,updated_at=? WHERE delivery_id=? AND status='scheduled' AND attempt_count=0",(TAKEOVER,now,now,row['delivery_id'])).rowcount
        if changed!=1: raise ValueError('COMBINED_FORMAL_HISTORY_UNRESOLVED')
        c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,'system',?,?,?,?)",
                  (str(uuid4()),TAKEOVER,row['child_id'],json.dumps({'delivery_id':row['delivery_id'],'day':day}),now))


def is_taken_over(c,row,day,address):
    """Recognize only an auditable, never-attempted queue retirement."""
    if row['status']!='cancelled' or row['cancel_reason']!=TAKEOVER or row['attempt_count']!=0:
        return False
    if c.execute('SELECT 1 FROM report_delivery_attempt WHERE delivery_id=? LIMIT 1',(row['delivery_id'],)).fetchone(): return False
    if not owns(c,day,address): return False
    return bool(c.execute("SELECT 1 FROM audit_event WHERE action=? AND child_id=? AND json_extract(summary_json,'$.delivery_id')=? AND json_extract(summary_json,'$.day')=? LIMIT 1",
                          (TAKEOVER,row['recipient_child'],row['delivery_id'],day)).fetchone())
