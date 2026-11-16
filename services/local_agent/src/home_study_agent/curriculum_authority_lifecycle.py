"""Lifecycle for local curriculum transport grants; audit history is append-only."""
import json
from uuid import uuid4

from .repository import utc_now

AUTHORITY_ACTION = 'curriculum_internalization_authority_v1'
QUARANTINE_ACTION = 'curriculum_internalization_authority_quarantined_v1'


def authority_rows(connection, child_id):
    return [dict(row) for row in connection.execute(
        'SELECT * FROM audit_event WHERE child_id=? AND action IN (?,?) ORDER BY rowid',
        (child_id, AUTHORITY_ACTION, QUARANTINE_ACTION))]


def authority_count(connection, child_id):
    return connection.execute('SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action IN (?,?)',
        (child_id, AUTHORITY_ACTION, QUARANTINE_ACTION)).fetchone()[0]


def delete_authorities(connection, child_id):
    return connection.execute('DELETE FROM audit_event WHERE child_id=? AND action IN (?,?)',
        (child_id, AUTHORITY_ACTION, QUARANTINE_ACTION)).rowcount


def quarantine_after_restore(connection):
    """Caller owns the isolated restore transaction, before publishing the DB."""
    rows = connection.execute('''SELECT a.audit_id,a.child_id FROM audit_event a WHERE a.action=?
        AND NOT EXISTS (SELECT 1 FROM audit_event q WHERE q.child_id=a.child_id AND q.action=?
            AND json_extract(q.summary_json,'$.authority_audit_id')=a.audit_id)''',
        (AUTHORITY_ACTION, QUARANTINE_ACTION)).fetchall()
    now = utc_now()
    for row in rows:
        connection.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
            VALUES (?,'system',?,?,?,?)''', (str(uuid4()), QUARANTINE_ACTION, row['child_id'],
            json.dumps({'authority_audit_id':row['audit_id'],'reason':'backup_restore'}), now))
    return len(rows)
