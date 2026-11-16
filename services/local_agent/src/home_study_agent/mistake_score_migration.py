"""Version 73: preserve original mistake attempts with an unassessed score."""
import sqlite3

VERSION = 73
COLUMNS = ('attempt_id', 'mistake_id', 'child_id', 'diagnostic_item_id', 'evidence_id', 'stage',
           'submitted_answer', 'correctness', 'hint_level', 'independent', 'feedback', 'created_at', 'client_action_id')
CREATE_ATTEMPT = '''CREATE TABLE mistake_attempt_unknown_v73 (
  attempt_id TEXT PRIMARY KEY,
  mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
  diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
  evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id) ON DELETE CASCADE,
  stage TEXT NOT NULL CHECK(stage IN (
    'repair_check', 'near_transfer', 'representation_transfer',
    'discrimination', 'far_transfer', 'delayed_48h', 'delayed_7d'
  )),
  submitted_answer TEXT NOT NULL,
  correctness REAL CHECK(correctness >= 0 AND correctness <= 1),
  hint_level INTEGER NOT NULL CHECK(hint_level BETWEEN 0 AND 5),
  independent INTEGER NOT NULL CHECK(independent IN (0, 1)),
  feedback TEXT NOT NULL,
  created_at TEXT NOT NULL,
  client_action_id TEXT NOT NULL UNIQUE
)'''


def migrate_unknown_mistake_scores(connection):
    if connection.in_transaction:
        raise sqlite3.OperationalError('MISTAKE_MIGRATION_REQUIRES_IDLE_CONNECTION')
    foreign_keys = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    legacy_alter = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
    connection.execute('PRAGMA foreign_keys=OFF')
    connection.execute('PRAGMA legacy_alter_table=ON')
    try:
        connection.execute('BEGIN IMMEDIATE')
        columns = connection.execute('PRAGMA table_info(mistake_attempt)').fetchall()
        correctness = next((row for row in columns if row[1] == 'correctness'), None)
        if correctness is None:
            raise sqlite3.OperationalError('MISTAKE_MIGRATION_SCHEMA_CHANGED')
        already = connection.execute('SELECT 1 FROM schema_migration WHERE version=?', (VERSION,)).fetchone()
        if already and not correctness[3]:
            connection.commit()
            return
        violations = {tuple(row) for row in connection.execute('PRAGMA foreign_key_check')}
        if correctness[3]:
            if tuple(row[1] for row in columns) != COLUMNS:
                raise sqlite3.OperationalError('MISTAKE_MIGRATION_SCHEMA_CHANGED')
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='mistake_attempt'").fetchone()[0]
            expected = CREATE_ATTEMPT.replace('mistake_attempt_unknown_v73', 'mistake_attempt').replace('correctness REAL CHECK', 'correctness REAL NOT NULL CHECK')
            normalize = lambda value: ''.join(value.replace('IF NOT EXISTS ', '').replace('"', '').split()).lower()
            if normalize(sql) != normalize(expected):
                raise sqlite3.OperationalError('MISTAKE_MIGRATION_SCHEMA_CHANGED')
            objects = connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='mistake_attempt' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name").fetchall()
            connection.execute(CREATE_ATTEMPT)
            names = ','.join(COLUMNS)
            connection.execute('INSERT INTO mistake_attempt_unknown_v73(rowid,' + names + ') SELECT rowid,' + names + ' FROM mistake_attempt')
            connection.execute('DROP TABLE mistake_attempt')
            connection.execute('ALTER TABLE mistake_attempt_unknown_v73 RENAME TO mistake_attempt')
            for row in objects:
                connection.execute(row[0])
        _verify_foreign_keys(connection, violations)
        connection.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (?,datetime('now'))", (VERSION,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute('PRAGMA legacy_alter_table=' + str(int(legacy_alter)))
        connection.execute('PRAGMA foreign_keys=' + str(int(foreign_keys)))


def _verify_foreign_keys(connection, previous):
    if {tuple(row) for row in connection.execute('PRAGMA foreign_key_check')} - previous:
        raise sqlite3.IntegrityError('MISTAKE_MIGRATION_FOREIGN_KEY_CHECK_FAILED')
