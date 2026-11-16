"""Isolated daily visual scheduler candidate; not registered with app maintenance.

Do not enable in the app until existing daily-report content is combined and
the v96 migration, delete and restore policies have been independently audited.
"""
from datetime import datetime, timedelta, timezone

from .daily_report_delivery_state import assert_ledger_key, identity_key, reserve_local, schema_valid as ledger_schema_valid
from .daily_report_observation import DailyReportObservationService, _is_canonical_day
from .daily_report_visual_submission import submit
from .family_clock import family_time

SCHEDULE_SCHEMA = """CREATE TABLE IF NOT EXISTS daily_visual_schedule (
 child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id) ON DELETE CASCADE,
 start_day TEXT NOT NULL, next_day TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), updated_at TEXT NOT NULL
)"""
_PUBLIC_BLOCK_REASONS = frozenset({
    'QUIET_HOURS', 'RECIPIENT_EMAIL_TEST_REQUIRED', 'EMAIL_CHANNEL_TEST_REQUIRED',
    'EMAIL_EXTERNAL_DELIVERY_NOT_AUTHORIZED', 'RECIPIENT_NOT_AUTHORIZED',
    'FROZEN_SOURCE_CHANGED', 'PREPARED_SOURCE_CHANGED', 'OBSERVATION_STALE',
    'DELETION_PENDING', 'RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED',
    'FORMAL_DELIVERY_EXISTS', 'PRIOR_DELIVERY_UNRESOLVED_OR_SENT',
    'DELIVERY_PLAN_CONFLICT', 'DELIVERY_IDENTITY_CONFLICT',
})


def _valid_schema(c):
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_visual_schedule'").fetchone()
    normalized = lambda s: ' '.join(s.replace('IF NOT EXISTS ', '').split())
    return bool(row and normalized(row[0]) == normalized(SCHEDULE_SCHEMA))


def _utc_now():
    return datetime.now(timezone.utc)


def _instant():
    now = _utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('INSTANT_INVALID')
    return now


def enable(service, child_id):
    """Explicitly activate from today's family date; never reset an old cursor."""
    now = _instant()
    day = family_time(now).date().isoformat()
    with service.database.external_effect_guard(), service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not _valid_schema(c): raise ValueError('VISUAL_SCHEDULE_SCHEMA_REQUIRED')
        row = c.execute('SELECT * FROM daily_visual_schedule WHERE child_id=?', (child_id,)).fetchone()
        if row:
            if not _is_canonical_day(row['start_day']) or not _is_canonical_day(row['next_day']) or row['next_day'] < row['start_day']:
                raise ValueError('VISUAL_SCHEDULE_INVALID')
            if not row['enabled']:
                raise ValueError('VISUAL_SCHEDULE_DISABLED_REQUIRES_REVIEW')
            return {'start_day':row['start_day'], 'next_day':row['next_day'], 'created':False}
        if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
            raise ValueError('VISUAL_SCHEDULE_CHILD_UNKNOWN')
        c.execute('INSERT INTO daily_visual_schedule VALUES(?,?,?,1,?)', (child_id,day,day,now.isoformat()))
        c.commit()
        return {'start_day':day, 'next_day':day, 'created':True}


def run_due(service, child_id, *, max_days=7):
    """Process completed family days in order; blocked and uncertain days stay put."""
    now = _instant()
    if not isinstance(max_days,int) or isinstance(max_days,bool) or not 1 <= max_days <= 31:
        raise ValueError('VISUAL_SCHEDULE_LIMIT_INVALID')
    from datetime import date
    today = family_time(now).date()
    results = []
    with service.database.external_effect_guard():
        for _ in range(max_days):
            with service.database.connect() as c:
                if not _valid_schema(c): raise ValueError('VISUAL_SCHEDULE_SCHEMA_REQUIRED')
                schedule = c.execute('SELECT * FROM daily_visual_schedule WHERE child_id=?', (child_id,)).fetchone()
                if not schedule or schedule['enabled'] != 1: raise ValueError('VISUAL_SCHEDULE_NOT_ENABLED')
                if not ledger_schema_valid(c): raise ValueError('DELIVERY_STORAGE_UNCONFIRMED')
                if c.execute('SELECT blocked FROM daily_observation_delivery_barrier WHERE singleton_id=1').fetchone()[0] != 0:
                    return {'status':'blocked','reason':'RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED','next_day':schedule['next_day'],'results':results}
                assert_ledger_key(c,service.encryption_key)
                start, day = schedule['start_day'], schedule['next_day']
                if not _is_canonical_day(start) or not _is_canonical_day(day) or day < start:
                    raise ValueError('VISUAL_SCHEDULE_INVALID')
                if date.fromisoformat(day) >= today:
                    return {'status':'waiting_for_day_end','next_day':day,'results':results}
                # A visual-only message cannot replace existing formal content.
                # Invalid dates are unresolved and therefore block as well.
                formal = c.execute('SELECT report_date FROM daily_report WHERE child_id=?', (child_id,)).fetchall()
                formal_snapshots = c.execute('''SELECT d.report_date FROM parent_report_snapshot s
                    LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
                    WHERE s.child_id=? AND s.report_type='daily' ''', (child_id,)).fetchall()
                if any(not _is_canonical_day(row[0]) or row[0] == day for row in [*formal, *formal_snapshots]):
                    return {'status':'blocked','reason':'FORMAL_REPORT_MERGE_REQUIRED','next_day':day,'results':results}
                if c.execute("SELECT 1 FROM report_delivery_recipient WHERE child_id=? AND channel='email' AND consent_status='pending' LIMIT 1", (child_id,)).fetchone():
                    return {'status':'blocked','reason':'RECIPIENT_AUTHORIZATION_REQUIRED','next_day':day,'results':results}
                recipients = c.execute("""SELECT recipient_id,address FROM report_delivery_recipient
                    WHERE child_id=? AND channel='email' AND consent_status='consented' AND revoked_at IS NULL
                    ORDER BY recipient_id""", (child_id,)).fetchall()
                if not recipients:
                    return {'status':'blocked','reason':'RECIPIENT_AUTHORIZATION_REQUIRED','next_day':day,'results':results}
                recipients = [dict(row) for row in recipients]
            for recipient in recipients:
                try:
                    token = identity_key(service.encryption_key,child_id,day,recipient['address'].strip().casefold())
                    with service.database.connect() as c:
                        prior = c.execute('SELECT child_id,status FROM daily_observation_delivery_ledger WHERE identity=?', (token,)).fetchone()
                    if prior:
                        if prior['child_id'] != child_id: raise ValueError('DELIVERY_IDENTITY_CONFLICT')
                        if prior['status'] == 'sent': continue
                        if prior['status'] in ('processing','unknown'):
                            return {'status':'blocked','reason':'DELIVERY_RESULT_UNKNOWN','next_day':day,'results':results}
                    source = DailyReportObservationService(service.database).prepare(
                        child_id,day,recipient['recipient_id'],key=service.encryption_key)['source_id']
                    snapshot = service.freeze_daily_observation(child_id,day,recipient['recipient_id'],source)['snapshot_id']
                    reserve_local(service,child_id,day,recipient['recipient_id'],snapshot,now=now)
                    outcome = submit(service,child_id,day,recipient['recipient_id'],snapshot)
                    if outcome.get('status') != 'sent':
                        return {'status':'blocked','reason':'DELIVERY_NOT_CONFIRMED','next_day':day,'results':results}
                except (ValueError, LookupError) as error:
                    # Only bounded fixed codes are exposed; no document content.
                    reason = str(error)
                    if reason not in _PUBLIC_BLOCK_REASONS:
                        reason = 'DELIVERY_PREPARATION_FAILED'
                    return {'status':'blocked','reason':reason,'next_day':day,'results':results}
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            with service.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                changed = c.execute('UPDATE daily_visual_schedule SET next_day=?,updated_at=? WHERE child_id=? AND next_day=? AND enabled=1',
                                    (next_day,now.isoformat(),child_id,day)).rowcount
                if changed != 1: raise ValueError('VISUAL_SCHEDULE_CHANGED')
                c.commit()
            results.append({'day':day,'status':'sent'})
        return {'status':'more_days_pending','next_day':next_day,'results':results}
