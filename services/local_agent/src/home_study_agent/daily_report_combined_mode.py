"""Durable combined-daily ownership. Legacy remains the migration default."""
from datetime import datetime, timezone
from uuid import uuid4

SCHEMA = """CREATE TABLE IF NOT EXISTS combined_daily_mode (
 singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
 mode TEXT NOT NULL CHECK(mode IN ('legacy','combined','blocked')),
 epoch TEXT NOT NULL,
 updated_at TEXT NOT NULL
)"""


def schema_valid(c):
    row=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='combined_daily_mode'").fetchone()
    normalize=lambda sql:' '.join(sql.replace('IF NOT EXISTS ','').split())
    if not row or normalize(row[0])!=normalize(SCHEMA): return False
    rows=c.execute('SELECT singleton_id,mode,epoch,updated_at FROM combined_daily_mode').fetchall()
    if len(rows)!=1: return False
    ident,mode,epoch,updated=tuple(rows[0])
    return (ident==1 and isinstance(epoch,str) and isinstance(updated,str) and bool(updated)
            and ((mode in ('legacy','blocked') and epoch=='') or (mode=='combined' and len(epoch)==32 and all(x in '0123456789abcdef' for x in epoch))))


def state(c):
    if not schema_valid(c): raise ValueError('COMBINED_MODE_SCHEMA_INVALID')
    return c.execute('SELECT mode,epoch FROM combined_daily_mode WHERE singleton_id=1').fetchone()


def blocks_legacy(c):
    return state(c)['mode']!='legacy'


def _worker_public_capability_proven(command):
    """Probe the actual validated worker without credentials or network work."""
    import os
    import subprocess
    import tempfile
    try:
        env = {k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR'}}
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([*command, '--public-readiness'], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL, timeout=5, check=False,
                close_fds=True, env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
            output.seek(0)
            return result.returncode == 0 and output.read(64) == b'HSC_SMTP_PUBLIC_READY_V1\n'
    except (OSError, subprocess.SubprocessError):
        return False


def readiness_reason():
    """Return the first unmet production requirement without changing state."""
    from .daily_report_combined_delivery import _source_withdrawal_guard_proven
    from .smtp_owned_worker import _public_transport_proven
    if not _source_withdrawal_guard_proven():
        return 'COMBINED_SOURCE_WITHDRAWAL_GUARD_UNAVAILABLE'
    if not _public_transport_proven():
        return 'SMTP_PUBLIC_TRANSPORT_UNAVAILABLE'
    from .smtp_owned_process import OwnedSmtpProcess
    try:
        command = OwnedSmtpProcess(lambda: None, lambda: True, lambda *args: None)._worker_command()
    except (ValueError, OSError):
        return 'SMTP_WORKER_PACKAGE_INVALID'
    if not _worker_public_capability_proven(command):
        return 'SMTP_WORKER_PUBLIC_CAPABILITY_UNCONFIRMED'
    return None


def enable(service):
    """Enable ownership only after the combined sender's hard safety gate opens."""
    from .family_clock import family_time
    reason = readiness_reason()
    if reason: raise ValueError(reason)
    with service.database.external_effect_guard(timeout=10),service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current=state(c)
        if current['mode']=='combined': return {'mode':'combined'}
        if current['mode']!='legacy': raise ValueError('COMBINED_MODE_RESTORE_REVIEW_REQUIRED')
        day=family_time(datetime.now(timezone.utc)).date().isoformat()
        if c.execute("SELECT 1 FROM pilot_test_digest_outbox WHERE status='processing' OR (delivery_date=? AND status IN ('sent','unknown')) LIMIT 1",(day,)).fetchone():
            raise ValueError('COMBINED_EXISTING_LOG_DELIVERY_UNRESOLVED')
        if c.execute('''SELECT 1 FROM report_delivery_outbox o JOIN parent_report_snapshot s ON s.snapshot_id=o.snapshot_id
            LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
            WHERE s.report_type='daily' AND o.channel='email' AND (d.report_date=? OR d.report_date IS NULL)
              AND (o.status!='scheduled' OR o.attempt_count!=0 OR o.sent_at IS NOT NULL OR o.provider_message_id IS NOT NULL
                   OR EXISTS(SELECT 1 FROM report_delivery_attempt a WHERE a.delivery_id=o.delivery_id)) LIMIT 1''',(day,)).fetchone():
            raise ValueError('COMBINED_EXISTING_FORMAL_DELIVERY_UNRESOLVED')
        children=[row['child_id'] for row in c.execute('SELECT child_id FROM child_profile ORDER BY child_id')]
        if not children: raise ValueError('COMBINED_CHILD_REQUIRED')
        for child in children:
            schedule=c.execute('SELECT enabled FROM daily_visual_schedule WHERE child_id=?',(child,)).fetchone()
            if schedule and schedule['enabled']!=1: raise ValueError('COMBINED_SCHEDULE_REVIEW_REQUIRED')
            if not schedule:
                c.execute("INSERT INTO daily_visual_schedule(child_id,start_day,next_day,enabled,updated_at) VALUES(?,?,?,1,?)",
                          (child,day,day,datetime.now(timezone.utc).isoformat()))
        c.execute("UPDATE combined_daily_mode SET mode='combined',epoch=?,updated_at=? WHERE singleton_id=1 AND mode='legacy'",(uuid4().hex,datetime.now(timezone.utc).isoformat()))
        c.commit()
        return {'mode':'combined'}


def preserve_on_restore(live,staged):
    """Never revive legacy ownership on a conflicting restored snapshot."""
    current=state(live); incoming=state(staged)
    if tuple(current)!=tuple(incoming):
        staged.execute("UPDATE combined_daily_mode SET mode='blocked',epoch='',updated_at=? WHERE singleton_id=1",
                       (datetime.now(timezone.utc).isoformat(),))
