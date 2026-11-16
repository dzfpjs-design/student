"""Canonical capture schema; upgrade under Database's backup/restore contract."""

TABLE_SQL = """CREATE TABLE {table} (
    client_action_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
    wav_sha256 TEXT, purpose TEXT NOT NULL,
    prompt_id TEXT, item_version TEXT,
    voice_input_id TEXT UNIQUE REFERENCES voice_input(voice_input_id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','completed','cancelled')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    question_reference_json TEXT,
    question_validation TEXT NOT NULL DEFAULT 'valid' CHECK(question_validation IN ('valid','stale')),
    activity_reference_json TEXT,
    activity_validation TEXT NOT NULL DEFAULT 'valid' CHECK(activity_validation IN ('valid','stale')),
    pending_evaluation_turn_id TEXT,
    local_recovery_only INTEGER NOT NULL DEFAULT 0 CHECK(local_recovery_only IN (0,1)),
    task_choice_reference_json TEXT,
    source_kind TEXT NOT NULL DEFAULT 'local_wav' CHECK(source_kind IN ('local_wav','realtime_transcript')),
    source_model TEXT,
    speech_started_at TEXT,
    ordinary_response_binding_json TEXT,
    CHECK((source_kind='local_wav' AND wav_sha256 IS NOT NULL)
       OR (source_kind='realtime_transcript' AND wav_sha256 IS NULL AND source_model IS NOT NULL)))"""


OPTIONAL_COLUMNS = {
    'question_reference_json': 'TEXT',
    'question_validation': "TEXT NOT NULL DEFAULT 'valid' CHECK(question_validation IN ('valid','stale'))",
    'activity_reference_json': 'TEXT',
    'activity_validation': "TEXT NOT NULL DEFAULT 'valid' CHECK(activity_validation IN ('valid','stale'))",
    'pending_evaluation_turn_id': 'TEXT',
    'local_recovery_only': 'INTEGER NOT NULL DEFAULT 0 CHECK(local_recovery_only IN (0,1))',
    'task_choice_reference_json': 'TEXT',
    'speech_started_at': 'TEXT',
    'ordinary_response_binding_json': 'TEXT',
}


def needs_upgrade(connection):
    columns = {row[1]: row for row in connection.execute('PRAGMA table_info(voice_capture_binding)')}
    return (not connection.execute("SELECT 1 FROM sqlite_master WHERE name='tutor_voice_playback'").fetchone()
            or not (set(OPTIONAL_COLUMNS) | {'source_kind', 'source_model', 'wav_sha256'}) <= columns.keys()
            or bool(columns['wav_sha256'][3]))


def quarantine_missing_validation(connection, table, old):
    for reference, validation in [('question_reference_json', 'question_validation'),
                                  ('activity_reference_json', 'activity_validation')]:
        if reference in old and validation not in old:
            connection.execute('UPDATE ' + table + " SET " + validation + "='stale' WHERE " + reference + ' IS NOT NULL')


def ensure_schema(connection):
    from .tutor_voice_playback import TABLE_SQL as DELIVERY_TABLE_SQL
    connection.execute(DELIVERY_TABLE_SQL)
    existing = connection.execute("PRAGMA table_info(voice_capture_binding)").fetchall()
    if not existing:
        connection.execute(TABLE_SQL.format(table='voice_capture_binding'))
        return
    old = {row[1] for row in existing}
    if 'source_kind' in old:
        if 'source_model' not in old or next(row for row in existing if row[1] == 'wav_sha256')[3]:
            raise ValueError('VOICE_CAPTURE_SCHEMA_INCOMPATIBLE')
        # Historical optional references stay empty; upgrading cannot grant new
        # question/activity/recovery authority to a recording already captured.
        for name, definition in OPTIONAL_COLUMNS.items():
            if name not in old:
                connection.execute('ALTER TABLE voice_capture_binding ADD COLUMN ' + name + ' ' + definition)
        quarantine_missing_validation(connection, 'voice_capture_binding', old)
        return
    connection.execute(TABLE_SQL.format(table='voice_capture_binding_v80'))
    new = {row[1] for row in connection.execute('PRAGMA table_info(voice_capture_binding_v80)')}
    if old - new:
        raise ValueError('VOICE_CAPTURE_SCHEMA_UNKNOWN_COLUMNS')
    # No external reference or custom trigger/index may be silently destroyed.
    for table, in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if any(row[2] == 'voice_capture_binding' for row in connection.execute(
                'PRAGMA foreign_key_list("' + table.replace('"', '""') + '")')):
            raise ValueError('VOICE_CAPTURE_SCHEMA_EXTERNAL_REFERENCE')
    if connection.execute("""SELECT 1 FROM sqlite_master WHERE tbl_name='voice_capture_binding'
            AND (type='trigger' OR (type='index' AND sql IS NOT NULL))""").fetchone():
        raise ValueError('VOICE_CAPTURE_SCHEMA_CUSTOM_DEPENDENCY')
    columns = ','.join('"' + name + '"' for name in sorted(old))
    connection.execute('INSERT INTO voice_capture_binding_v80 (' + columns + ') SELECT ' + columns + ' FROM voice_capture_binding')
    connection.execute('DROP TABLE voice_capture_binding')
    connection.execute('ALTER TABLE voice_capture_binding_v80 RENAME TO voice_capture_binding')
    quarantine_missing_validation(connection, 'voice_capture_binding', old)
    if connection.execute('PRAGMA foreign_key_check(voice_capture_binding)').fetchall():
        raise ValueError('VOICE_CAPTURE_SCHEMA_FOREIGN_KEY_FAILURE')
