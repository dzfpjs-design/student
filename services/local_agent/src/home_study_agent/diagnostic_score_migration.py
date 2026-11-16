"""Version 70: preserve attempt identity while admitting unassessed responses."""
import sqlite3

VERSION = 70
COLUMNS = ('attempt_id','diagnostic_item_id','evidence_id','child_id','session_id',
           'submitted_answer','correctness','feedback','review_task_id','created_at','client_action_id')
CREATE_ATTEMPT = '''CREATE TABLE diagnostic_attempt_unknown_v70 (
  attempt_id TEXT PRIMARY KEY,
  diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
  evidence_id TEXT NOT NULL REFERENCES learning_evidence(evidence_id),
  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
  submitted_answer TEXT NOT NULL,
  correctness REAL,
  feedback TEXT NOT NULL,
  review_task_id TEXT REFERENCES review_task(review_task_id),
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
)'''


def migrate_unknown_diagnostic_scores(connection):
    """Run outside a transaction, under Database's existing upgrade backup.

    Do not rename the old parent table: children, triggers and views must keep
    referencing diagnostic_attempt. No new FK violation may be introduced;
    unrelated pre-existing violations are not repaired by this migration.
    """
    if connection.in_transaction:
        raise sqlite3.OperationalError('DIAGNOSTIC_MIGRATION_REQUIRES_IDLE_CONNECTION')
    foreign_keys = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    legacy_alter = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
    connection.execute('PRAGMA foreign_keys=OFF')
    connection.execute('PRAGMA legacy_alter_table=ON')
    try:
        connection.execute('BEGIN IMMEDIATE')
        columns = connection.execute('PRAGMA table_info(diagnostic_attempt)').fetchall()
        correctness_column = next((row for row in columns if row[1]=='correctness'),None)
        if correctness_column is None:
            raise sqlite3.OperationalError('DIAGNOSTIC_MIGRATION_SCHEMA_CHANGED')
        already = connection.execute('SELECT 1 FROM schema_migration WHERE version=?',(VERSION,)).fetchone()
        if already and not correctness_column[3]:
            connection.commit()
            return
        previous_violations = {tuple(row) for row in connection.execute('PRAGMA foreign_key_check')}
        if correctness_column[3]:
            if tuple(row[1] for row in columns) != COLUMNS:
                raise sqlite3.OperationalError('DIAGNOSTIC_MIGRATION_SCHEMA_CHANGED')
            objects = connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='diagnostic_attempt' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name").fetchall()
            connection.execute(CREATE_ATTEMPT)
            names = ','.join(COLUMNS)
            connection.execute('INSERT INTO diagnostic_attempt_unknown_v70(rowid,'+names+') SELECT rowid,'+names+' FROM diagnostic_attempt')
            connection.execute('DROP TABLE diagnostic_attempt')
            connection.execute('ALTER TABLE diagnostic_attempt_unknown_v70 RENAME TO diagnostic_attempt')
            for row in objects:
                connection.execute(row[0])
        if not already:
            _clear_verified_open_placeholders(connection)
        if {tuple(row) for row in connection.execute('PRAGMA foreign_key_check')} - previous_violations:
            raise sqlite3.IntegrityError('DIAGNOSTIC_MIGRATION_FOREIGN_KEY_CHECK_FAILED')
        connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (?,datetime('now'))",(VERSION,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute('PRAGMA legacy_alter_table='+str(int(legacy_alter)))
        connection.execute('PRAGMA foreign_keys='+str(int(foreign_keys)))


def _clear_verified_open_placeholders(connection):
    from .local_supply import validate_item, LocalSupplyError
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'curriculum_supply_package','curriculum_supply_event','specialized_supply_candidate'} <= tables:
        return
    rows = connection.execute('''SELECT da.attempt_id,da.evidence_id,da.child_id,da.diagnostic_item_id
        FROM diagnostic_attempt da JOIN diagnostic_item di USING(diagnostic_item_id)
        JOIN learning_evidence le ON le.evidence_id=da.evidence_id AND le.child_id=da.child_id
        WHERE di.answer_type='open_response' AND di.assessment_mode='open_response_rubric'
          AND da.correctness=0 AND le.correctness=0 AND le.independent=0 AND le.confidence<=.35
          AND le.evidence_type='diagnostic_answer' ''').fetchall()
    for row in rows:
        try:
            validate_item(connection,row['child_id'],row['diagnostic_item_id'])
        except LocalSupplyError:
            continue
        connection.execute('UPDATE diagnostic_attempt SET correctness=NULL WHERE attempt_id=?',(row['attempt_id'],))
        connection.execute("""UPDATE learning_evidence SET correctness=NULL,confidence=0,
            evidence_summary=evidence_summary||';score_status=unassessed_open_response_v1'
            WHERE evidence_id=?""",(row['evidence_id'],))
