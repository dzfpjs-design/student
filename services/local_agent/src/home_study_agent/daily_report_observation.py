"""Local-only, persistent observation source for future daily reports.

This module cannot send, schedule, or authorize external transmission.
"""
from datetime import date
from hashlib import sha256
import json
from uuid import uuid4

from .agent_runtime import _RuntimeTransactionDatabase
from .daily_visual_evidence import project_daily_visual_evidence
from .daily_page_observation import daily_page_observation
from .daily_observation_projection import project_daily_observation
from .daily_study_duration import project_recorded_task_time

# Schema v94. Executed only from Database.initialize's protected migration.
SOURCE_SCHEMA = """CREATE TABLE IF NOT EXISTS daily_observation_report_source (
 source_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
 day TEXT NOT NULL, fingerprint TEXT NOT NULL, source_ids_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_id,child_id,day)
)"""
OUTBOX_SCHEMA = """CREATE TABLE IF NOT EXISTS daily_observation_report_outbox (
 child_id TEXT NOT NULL, day TEXT NOT NULL, recipient_id TEXT NOT NULL,
 address_identity TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status='prepared'),
 PRIMARY KEY(child_id,day,address_identity),
 FOREIGN KEY(source_id,child_id,day) REFERENCES daily_observation_report_source(source_id,child_id,day) ON DELETE CASCADE
)"""


def schema_valid(c):
    # Compare complete DDL so a damaged same-version DB is never silently repaired.
    def normalized(sql):
        # Keep literal case: SQLite considers 'PREPARED' different from
        # 'prepared' in the status CHECK even though SQL keywords ignore case.
        return ' '.join(sql.replace('IF NOT EXISTS ', '').split())
    for name, ddl in (('daily_observation_report_source', SOURCE_SCHEMA),
                      ('daily_observation_report_outbox', OUTBOX_SCHEMA)):
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if not row or normalized(row[0]) != normalized(ddl):
            return False
    return True



def _projection(connection, child_id, day):
    view = _RuntimeTransactionDatabase(connection)
    return {'version': 'daily_observation_v1',
            'visual': project_daily_visual_evidence(view, child_id, day),
            'timing': daily_page_observation(view, child_id, day),
            'task_time': project_recorded_task_time(connection, child_id, day),
            'samples': project_daily_observation(connection, child_id, day, include_all_source_ids=True)}


def _source_digest(source):
    ids = []
    def collect(value, path=''):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith('_id') and isinstance(item, str):
                    ids.append([path + key, item])
                elif key.endswith('_ids') and isinstance(item, list):
                    ids.extend([path + key, value] for value in item if isinstance(value, str))
                else:
                    collect(item, path + key + '.')
        elif isinstance(value, list):
            for item in value:
                collect(item, path)
    collect(source)
    raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    encoded_ids = json.dumps(sorted(ids), ensure_ascii=False, separators=(',', ':'))
    if len(raw.encode('utf-8')) > 1024 * 1024 or len(encoded_ids.encode('utf-8')) > 1024 * 1024:
        raise ValueError('OBSERVATION_TOO_LARGE')
    return sha256(raw.encode()).hexdigest(), encoded_ids


def _source(connection, child_id, day):
    return _source_digest(_projection(connection, child_id, day))


def _ledger_status(c,child_id,day,address,key=None):
    if not c.execute("SELECT 1 FROM daily_observation_delivery_ledger WHERE status IN ('processing','unknown','sent') LIMIT 1").fetchone():
        return None
    from .daily_report_delivery_state import assert_ledger_key, identity_key
    if key is None:
        from .database import local_media_scope
        key = local_media_scope(c).get('data_encryption_key')
    try:
        assert_ledger_key(c,key)
        token = identity_key(key,child_id,day,address)
    except ValueError:
        return 'blocked'
    row = c.execute('SELECT status FROM daily_observation_delivery_ledger WHERE identity=? AND child_id=?', (token,child_id)).fetchone()
    if not row or row['status'] == 'ready': return None
    return 'already_sent' if row['status'] == 'sent' else 'unknown'


def _authority(c, child_id, day, recipient_id, key=None, *, processing_identity=None):
    if not isinstance(child_id, str) or not child_id or not isinstance(recipient_id, str) or not recipient_id:
        raise ValueError('INVALID_ID')
    try:
        if date.fromisoformat(day).isoformat() != day: raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('INVALID_DAY') from None
    barrier = c.execute('SELECT blocked FROM daily_observation_delivery_barrier WHERE singleton_id=1').fetchone()
    if not barrier or barrier['blocked'] != 0:
        raise ValueError('RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED')
    recipient = c.execute('SELECT * FROM report_delivery_recipient WHERE recipient_id=? AND child_id=?', (recipient_id, child_id)).fetchone()
    if not recipient or recipient['channel'] != 'email' or recipient['consent_status'] != 'consented' or recipient['revoked_at']:
        raise ValueError('RECIPIENT_NOT_AUTHORIZED')
    address = recipient['address'].strip().casefold()
    if not address: raise ValueError('RECIPIENT_NOT_AUTHORIZED')
    if _ledger_status(c,child_id,day,address,key):
        # A sender may revalidate its own already-claimed row while holding the
        # external-effect guard. Unknown/sent or a different identity never pass.
        own = False
        if processing_identity is not None:
            from .daily_report_delivery_state import identity_key
            identity = identity_key(key,child_id,day,address)
            own = (processing_identity == identity and c.execute(
                "SELECT 1 FROM daily_observation_delivery_ledger WHERE identity=? AND child_id=? AND status='processing'",
                (identity,child_id)).fetchone())
        if not own:
            raise ValueError('PRIOR_DELIVERY_UNRESOLVED_OR_SENT')
    if _legacy_status(c, child_id, day, address):
        raise ValueError('FORMAL_DELIVERY_EXISTS')
    return address


def _legacy_status(c, child_id, day, address):
    # A legacy attempt has no immutable recipient-address receipt. Conservatively
    # preserve any old same-day attempt and all attempts with an unknown day.
    prior = c.execute('''SELECT d.report_date,r.address,r.child_id AS recipient_child,o.status,o.delivery_id,o.cancel_reason,o.attempt_count FROM report_delivery_outbox o
        JOIN parent_report_snapshot s ON s.snapshot_id=o.snapshot_id
        LEFT JOIN daily_report d ON d.report_id=s.report_id AND d.child_id=s.child_id
        LEFT JOIN report_delivery_recipient r ON r.recipient_id=o.recipient_id
        WHERE s.child_id=? AND s.report_type='daily' ''', (child_id,)).fetchall()
    matching = [row for row in prior if (not _is_canonical_day(row['report_date']) or row['report_date'] == day)
                and (row['recipient_child'] != child_id or not isinstance(row['address'], str)
                     or row['address'].strip().casefold() == address)]
    from .daily_report_formal_ownership import is_taken_over
    matching = [row for row in matching if not is_taken_over(c,row,day,address)]
    if any(not _is_canonical_day(row['report_date']) or row['status'] in ('processing', 'unknown', 'unknown_acknowledged', 'superseded') for row in matching):
        return 'unknown'
    if any(row['status'] == 'sent' for row in matching):
        return 'already_sent'
    return 'blocked' if matching else None


def _is_canonical_day(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def prepare(c, child_id, day, recipient_id, expected_source_id=None, admission_check=None, *, key=None):
    c.execute('BEGIN IMMEDIATE')
    try:
        address = _authority(c, child_id, day, recipient_id, key)
        existing = c.execute('SELECT * FROM daily_observation_report_outbox WHERE child_id=? AND day=? AND address_identity=?', (child_id, day, address)).fetchone()
        if existing:
            if existing['recipient_id'] != recipient_id: raise ValueError('RECIPIENT_ID_CHANGED')
            saved = c.execute('SELECT fingerprint,source_ids_json FROM daily_observation_report_source WHERE source_id=? AND child_id=? AND day=?', (existing['source_id'],child_id,day)).fetchone()
            current = _source(c, child_id, day)
            if not saved:
                raise ValueError('OBSERVATION_STALE')
            if current == (saved['fingerprint'],saved['source_ids_json']):
                if expected_source_id is not None and expected_source_id != existing['source_id']:
                    raise ValueError('OBSERVATION_CONFLICT')
                if admission_check: admission_check()
                c.commit()
                return existing['source_id']
            if expected_source_id != existing['source_id']:
                raise ValueError('OBSERVATION_STALE')
            # Only a human-requested exact replacement of this local-only draft;
            # it cannot represent, retry, or cancel an SMTP submission.
            c.execute('DELETE FROM daily_observation_report_source WHERE source_id=? AND child_id=? AND day=?',
                      (existing['source_id'], child_id, day))
        elif expected_source_id is not None:
            raise ValueError('OBSERVATION_CONFLICT')
        fingerprint, ids = _source(c, child_id, day)
        source_id = str(uuid4())
        c.execute('INSERT INTO daily_observation_report_source(source_id,child_id,day,fingerprint,source_ids_json) VALUES(?,?,?,?,?)', (source_id,child_id,day,fingerprint,ids))
        c.execute('INSERT INTO daily_observation_report_outbox(child_id,day,recipient_id,address_identity,source_id,status) VALUES(?,?,?,?,?,\'prepared\')', (child_id,day,recipient_id,address,source_id))
        if admission_check: admission_check()
        c.commit()
        return source_id
    except BaseException:
        c.rollback()
        raise


def inspect_current(c, child_id, day, recipient_id):
    c.execute('BEGIN')
    try:
        address = _authority(c, child_id, day, recipient_id)
        row = c.execute('''SELECT o.fingerprint,o.source_ids_json FROM daily_observation_report_source o
          JOIN daily_observation_report_outbox b ON b.source_id=o.source_id
          WHERE b.child_id=? AND b.day=? AND b.recipient_id=? AND b.address_identity=?
            AND o.child_id=b.child_id AND o.day=b.day''', (child_id,day,recipient_id,address)).fetchone()
        if not row: raise ValueError('PREPARED_NOT_FOUND')
        fingerprint, ids = _source(c, child_id, day)
        return {'current': fingerprint == row['fingerprint'] and ids == row['source_ids_json'],
                'external_delivery_permitted': False}
    finally:
        c.rollback()


class DailyReportObservationService:
    """Prepare and inspect a local source; it has no path to external delivery."""
    def __init__(self, database):
        self.database = database

    def prepare(self, child_id, day, recipient_id, expected_source_id=None, admission_check=None, *, key=None):
        with self.database.connect() as connection:
            source_id = prepare(connection, child_id, day, recipient_id, expected_source_id, admission_check, key=key)
        return {'source_id': source_id, 'status': 'prepared_local_only',
                'external_delivery_permitted': False}

    def inspect_current(self, child_id, day, recipient_id):
        with self.database.connect() as connection:
            return inspect_current(connection, child_id, day, recipient_id)

    def preparation_status(self, child_id, day):
        try:
            if not child_id or date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('INVALID_DAY_OR_CHILD') from None
        with self.database.connect() as c:
            c.execute('BEGIN')
            try:
                if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
                    raise ValueError('CHILD_UNKNOWN')
                if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child_id,)).fetchone():
                    raise ValueError('DELETION_PENDING')
                recipients = c.execute("SELECT * FROM report_delivery_recipient WHERE child_id=? AND channel='email' ORDER BY created_at,recipient_id", (child_id,)).fetchall()
                barrier = c.execute('SELECT blocked FROM daily_observation_delivery_barrier WHERE singleton_id=1').fetchone()
                source = None
                result = []
                for recipient in recipients:
                    identity = recipient['address'].strip().casefold()
                    status = 'blocked' if not barrier or barrier['blocked'] != 0 else (
                        _ledger_status(c,child_id,day,identity) or _legacy_status(c, child_id, day, identity))
                    item = {'recipient_id': recipient['recipient_id'], 'label': recipient['label'], 'address': recipient['address']}
                    if not status and (recipient['consent_status'] != 'consented' or recipient['revoked_at']):
                        status = 'needs_authorization'
                    if not status:
                        row = c.execute('''SELECT b.recipient_id,o.source_id,o.fingerprint,o.source_ids_json
                            FROM daily_observation_report_outbox b JOIN daily_observation_report_source o
                            ON o.source_id=b.source_id AND o.child_id=b.child_id AND o.day=b.day
                            WHERE b.child_id=? AND b.day=? AND b.address_identity=?''', (child_id,day,identity)).fetchone()
                        if row and row['recipient_id'] != recipient['recipient_id']:
                            status = 'blocked'
                        elif row:
                            if source is None: source = _source(c, child_id, day)
                            status = 'prepared' if source == (row['fingerprint'],row['source_ids_json']) else 'source_changed'
                            item['source_id'] = row['source_id']
                        else:
                            status = 'not_prepared'
                    item['status'] = status
                    result.append(item)
                return {'child_id': child_id, 'day': day, 'recipients': result, 'delivery_enabled': False}
            finally:
                c.rollback()
