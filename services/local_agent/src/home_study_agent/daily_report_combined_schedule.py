"""Combined report maintenance using persisted v96 cursors and ownership.

Nothing here opens the transport safety gate. Logs may contain family-wide
metadata, so each shared ZIP recipient must be authorized for every child.
"""
from datetime import date, datetime, time, timedelta, timezone
from .family_clock import family_time
from .daily_report_combined_mode import state, readiness_reason
from .daily_report_visual_schedule import _valid_schema
from .daily_report_observation import DailyReportObservationService
from .daily_report_delivery_state import reserve_local
from . import daily_report_combined_delivery as delivery, daily_report_combined_snapshot as snapshot

_ALLOWED = frozenset({'QUIET_HOURS','RECIPIENT_EMAIL_TEST_REQUIRED','EMAIL_CHANNEL_TEST_REQUIRED',
 'EMAIL_EXTERNAL_DELIVERY_NOT_AUTHORIZED','RECIPIENT_NOT_AUTHORIZED','COMBINED_FORMAL_APPROVAL_REQUIRED',
 'COMBINED_FORMAL_STALE','COMBINED_FORMAL_CHANGED','COMBINED_VISUAL_STALE','COMBINED_CANDIDATE_STALE','COMBINED_HABIT_STOP_PENDING','COMBINED_INTERVENTION_WITHDRAWAL_PENDING',
 'PILOT_JOINT_HISTORY_UNRESOLVED','PILOT_JOINT_AUTHORITY_CHANGED','COMBINED_FORMAL_HISTORY_UNRESOLVED',
 'RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED','DELETION_PENDING','OBSERVATION_STALE',
 'FROZEN_SOURCE_CHANGED','PREPARED_SOURCE_CHANGED','PRIOR_DELIVERY_UNRESOLVED_OR_SENT',
 'FORMAL_DELIVERY_EXISTS','COMBINED_BATCH_NOT_FRESH','COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE',
 'SMTP_WORKER_PACKAGE_INVALID'})


def _blocked(reason):
    return {'status':'blocked','reason':reason if reason in _ALLOWED else 'COMBINED_PREPARATION_FAILED'}


def _members(c):
    children=[r['child_id'] for r in c.execute('SELECT child_id FROM child_profile ORDER BY child_id')]
    if not children or not _valid_schema(c): raise ValueError('COMBINED_SCHEDULE_INVALID')
    schedules=c.execute('SELECT * FROM daily_visual_schedule WHERE enabled=1 ORDER BY child_id').fetchall()
    if [r['child_id'] for r in schedules]!=children: raise ValueError('COMBINED_SCHEDULE_INVALID')
    if any(not isinstance(r['next_day'],str) or date.fromisoformat(r['next_day']).isoformat()!=r['next_day']
           or r['next_day']<r['start_day'] for r in schedules): raise ValueError('COMBINED_SCHEDULE_INVALID')
    if len({r['next_day'] for r in schedules})!=1: raise ValueError('COMBINED_SCHEDULE_MISALIGNED')
    recipients={}
    for r in c.execute("SELECT child_id,recipient_id,address,consent_status,revoked_at FROM report_delivery_recipient WHERE channel='email'"):
        if r['child_id'] not in children or not isinstance(r['address'],str) or not r['address'].strip(): continue
        address=r['address'].strip().casefold()
        if r['consent_status']!='consented' or r['revoked_at'] is not None: continue
        members=recipients.setdefault(address,{})
        if r['child_id'] in members: raise ValueError('COMBINED_RECIPIENT_AMBIGUOUS')
        members[r['child_id']]=r['recipient_id']
    if not recipients or any(set(members)!=set(children) for members in recipients.values()):
        raise ValueError('COMBINED_FAMILY_AUTHORITY_INCOMPLETE')
    return children,schedules[0]['next_day'],recipients


def _submit(service,telemetry,snapshot_id):
    from .daily_report_combined_owned_delivery import submit
    return submit(service,telemetry,snapshot_id)


def _prepare_recipient(service,telemetry,children,day,address,members,now):
    # All local source/ledger changes remain serialized, but this guard must be
    # released before the process-owner thread acquires it for SMTP.
    with service.database.external_effect_guard(timeout=10):
        with service.database.connect() as c:
            if state(c)['mode']!='combined': raise ValueError('COMBINED_MODE_RESTORE_REVIEW_REQUIRED')
            actual_children,actual_day,actual_recipients=_members(c)
            if actual_children!=children or actual_day!=day or actual_recipients.get(address)!=members:
                raise ValueError('COMBINED_SCHEDULE_CHANGED')
            identity=telemetry._joint_hmac(service.encryption_key,['joint-log-recipient-v1',day,address])
            batch=c.execute('SELECT batch_id,status FROM combined_diagnostic_batch WHERE delivery_date=? AND recipient_identity=?',(day,identity)).fetchone()
        if not batch:
            telemetry.reserve_combined_day(day,address)
            batch_id=telemetry.create_combined_batch(day,address,children,service.encryption_key)['batch_id']
        else:
            batch_id=batch['batch_id']
            if batch['status']=='unknown': raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
            if batch['status']=='sent': return {}
        telemetry.inspect_combined_batch(batch_id,service.encryption_key)
        with service.database.connect() as c:
            rows=c.execute('SELECT child_id,status FROM combined_diagnostic_member WHERE batch_id=? ORDER BY child_id',(batch_id,)).fetchall()
            if [r['child_id'] for r in rows]!=children: raise ValueError('COMBINED_MEMBERS_CHANGED')
            member_states={r['child_id']:r['status'] for r in rows}
        if any(v in ('unknown','processing') for v in member_states.values()):
            raise ValueError('COMBINED_DELIVERY_RESULT_UNKNOWN')
        candidates={}
        for child in children:
            with service.database.connect() as c:
                existing=c.execute('SELECT snapshot_id FROM combined_daily_snapshot WHERE batch_id=? AND child_id=?',(batch_id,child)).fetchone()
            if member_states[child]=='sent':
                if not existing: raise ValueError('COMBINED_MEMBERS_CHANGED')
                continue
            if existing:
                previous=existing['snapshot_id']
                try:
                    snapshot.inspect(telemetry,previous,service.encryption_key)
                    candidates[child]=previous
                except ValueError as error:
                    if str(error) not in ('COMBINED_CANDIDATE_STALE','COMBINED_FORMAL_CHANGED','COMBINED_VISUAL_STALE','FROZEN_SOURCE_CHANGED'):
                        raise
                    from .daily_report_combined_rebuild import rebuild
                    candidates[child]=rebuild(service,telemetry,previous)['snapshot_id']
                continue
            recipient_id=members[child]
            source=DailyReportObservationService(service.database).prepare(child,day,recipient_id,key=service.encryption_key)['source_id']
            visual=service.freeze_daily_observation(child,day,recipient_id,source)['snapshot_id']
            reserve_local(service,child,day,recipient_id,visual,now=now)
            candidates[child]=snapshot.freeze(telemetry,batch_id,child,day,recipient_id,visual,service.encryption_key)['snapshot_id']
        return candidates


def run_due(service,telemetry,*,now=None):
    """Reserve today, then process at most one due day, without implicit retries."""
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None: raise ValueError('INSTANT_INVALID')
    if service.database.path.resolve()!=telemetry.database.path.resolve(): raise ValueError('COMBINED_DATABASE_MISMATCH')
    with service.database.external_effect_guard(timeout=10),service.database.connect() as c:
        mode=state(c)['mode']
        if mode=='legacy': return {'status':'legacy'}
        if mode!='combined': return {'status':'blocked','reason':'COMBINED_MODE_RESTORE_REVIEW_REQUIRED'}
        reason = readiness_reason()
        if reason:
            return {'status':'blocked','reason':reason}
        try: children,day,recipients=_members(c)
        except (ValueError,TypeError): return {'status':'blocked','reason':'COMBINED_FAMILY_OR_SCHEDULE_REVIEW_REQUIRED'}
    delivery.recover(service)
    today=family_time(now).date().isoformat()
    for address in sorted(recipients):
        try:
            identity=telemetry._joint_hmac(service.encryption_key,['joint-log-recipient-v1',today,address])
            with service.database.connect() as c:
                exists=c.execute('SELECT 1 FROM combined_diagnostic_batch WHERE delivery_date=? AND recipient_identity=?',(today,identity)).fetchone()
            if not exists: telemetry.reserve_combined_day(today,address)
        except (ValueError,LookupError) as error: return _blocked(str(error))
    local_now=family_time(now)
    if date.fromisoformat(day)>local_now.date() or (day==today and local_now.time()<time(22)):
        return {'status':'waiting_for_cutoff','day':day}
    for address,members in sorted(recipients.items()):
        try:
            candidates=_prepare_recipient(service,telemetry,children,day,address,members,now)
            for child in children:
                if child not in candidates:continue
                outcome=_submit(service,telemetry,candidates[child])
                if outcome['status']!='sent':
                    if outcome['status']=='cleanup_pending':return {'status':'blocked','reason':'SMTP_WORKER_CLEANUP_PENDING'}
                    if outcome['status']=='unknown':return {'status':'blocked','reason':'COMBINED_DELIVERY_RESULT_UNKNOWN'}
                    return _blocked(outcome.get('reason','COMBINED_PREPARATION_FAILED'))
        except (ValueError,LookupError) as error:
            if str(error) in ('COMBINED_SENT_SIBLING_REVIEW_REQUIRED','COMBINED_DELIVERY_RESULT_UNKNOWN'):
                return {'status':'blocked','reason':str(error)}
            return _blocked(str(error))
    next_day=(date.fromisoformat(day)+timedelta(days=1)).isoformat()
    with service.database.external_effect_guard(timeout=10),service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if state(c)['mode']!='combined' or _members(c)!=(children,day,recipients):
            raise ValueError('COMBINED_SCHEDULE_CHANGED')
        for address in recipients:
            identity=telemetry._joint_hmac(service.encryption_key,['joint-log-recipient-v1',day,address])
            batch=c.execute('SELECT batch_id,status FROM combined_diagnostic_batch WHERE delivery_date=? AND recipient_identity=?',(day,identity)).fetchone()
            if not batch or batch['status']!='sent':raise ValueError('COMBINED_DELIVERY_NOT_CONFIRMED')
            statuses=c.execute('SELECT child_id,status FROM combined_diagnostic_member WHERE batch_id=? ORDER BY child_id',(batch['batch_id'],)).fetchall()
            if [(r['child_id'],r['status']) for r in statuses]!=[(child,'sent') for child in children]:
                raise ValueError('COMBINED_DELIVERY_NOT_CONFIRMED')
        for child in children:
            if c.execute('UPDATE daily_visual_schedule SET next_day=?,updated_at=? WHERE child_id=? AND enabled=1 AND next_day=?',
                         (next_day,now.isoformat(),child,day)).rowcount!=1:
                raise ValueError('COMBINED_SCHEDULE_CHANGED')
        c.commit()
    return {'status':'sent','day':day,'next_day':next_day}
