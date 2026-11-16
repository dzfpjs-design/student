"""Isolated legacy schemas; no media, model calls or household records."""
import sqlite3

import pytest

from home_study_agent.voice_capture_schema import TABLE_SQL, ensure_schema
from home_study_agent.voice_capture_binding import capture_started_at


OPTIONAL = {
    'question_reference_json': None, 'question_validation': 'valid',
    'activity_reference_json': None, 'activity_validation': 'valid',
    'pending_evaluation_turn_id': None, 'local_recovery_only': 0,
    'task_choice_reference_json': None, 'speech_started_at': None,
}


@pytest.mark.parametrize('reference,validation', [
    ('question_reference_json', 'question_validation'), ('activity_reference_json', 'activity_validation'),
])
@pytest.mark.parametrize('legacy', [False, True])
def test_missing_validation_cannot_approve_an_existing_reference(reference, validation, legacy):
    with sqlite3.connect(':memory:') as c:
        c.executescript('''CREATE TABLE child_profile(child_id TEXT PRIMARY KEY);
            CREATE TABLE daily_session(session_id TEXT PRIMARY KEY);
            CREATE TABLE voice_input(voice_input_id TEXT PRIMARY KEY);
            INSERT INTO child_profile VALUES ('child'); INSERT INTO daily_session VALUES ('session');''')
        schema = TABLE_SQL.split('    source_kind ')[0].rstrip().rstrip(',') + ')' if legacy else TABLE_SQL
        c.execute(schema.format(table='voice_capture_binding'))
        c.execute('''INSERT INTO voice_capture_binding
            (client_action_id,session_id,child_id,wav_sha256,purpose,status,created_at,updated_at)
            VALUES ('original','session','child','digest','agent_command','completed','onset','onset')''')
        c.execute('UPDATE voice_capture_binding SET ' + reference + "=?", ('{"synthetic":"reference"}',))
        c.execute('ALTER TABLE voice_capture_binding DROP COLUMN ' + validation)
        ensure_schema(c)
        assert c.execute('SELECT ' + reference + ',' + validation + ' FROM voice_capture_binding').fetchone() == (
            '{"synthetic":"reference"}', 'stale')


@pytest.mark.parametrize('column,default', OPTIONAL.items())
def test_partial_realtime_schema_restores_only_empty_defaults(column, default):
    with sqlite3.connect(':memory:') as c:
        c.execute(TABLE_SQL.format(table='voice_capture_binding'))
        c.execute('''INSERT INTO voice_capture_binding
            (client_action_id,session_id,child_id,wav_sha256,purpose,status,created_at,updated_at)
            VALUES ('original','session','child','digest','agent_command','completed','onset','onset')''')
        c.execute('ALTER TABLE voice_capture_binding DROP COLUMN ' + column)
        ensure_schema(c)
        assert c.execute('SELECT ' + column + ' FROM voice_capture_binding').fetchone()[0] == default
        assert c.execute('SELECT status,wav_sha256 FROM voice_capture_binding').fetchone() == ('completed', 'digest')
        before = c.total_changes
        ensure_schema(c)
        assert c.total_changes == before


@pytest.mark.parametrize('row_factory', [None, sqlite3.Row])
@pytest.mark.parametrize('source,onset,expected', [
    ('legacy', None, 'original-onset'), ('local_wav', None, 'original-onset'),
    ('realtime_transcript', None, None), ('realtime_transcript', 'speech-onset', 'speech-onset'),
    ('unknown-source', None, None),
])
def test_onset_read_is_readonly_and_never_substitutes_realtime_admission(row_factory, source, onset, expected):
    with sqlite3.connect(':memory:') as c:
        c.row_factory = row_factory
        fields = '' if source == 'legacy' else ',source_kind TEXT,speech_started_at TEXT'
        c.execute('CREATE TABLE voice_capture_binding(voice_input_id TEXT,created_at TEXT' + fields + ')')
        values = ('voice', 'original-onset') if source == 'legacy' else ('voice', 'original-onset', source, onset)
        c.execute('INSERT INTO voice_capture_binding VALUES (' + ','.join('?' for _ in values) + ')', values)
        before = c.total_changes
        assert capture_started_at(c, 'voice') == expected
        assert capture_started_at(c, 'missing') is None
        assert c.total_changes == before


def test_realtime_schema_without_onset_column_does_not_borrow_created_at():
    with sqlite3.connect(':memory:') as c:
        c.execute('CREATE TABLE voice_capture_binding(voice_input_id TEXT,created_at TEXT,source_kind TEXT)')
        c.execute("INSERT INTO voice_capture_binding VALUES ('voice','admission','realtime_transcript')")
        assert capture_started_at(c, 'voice') is None
