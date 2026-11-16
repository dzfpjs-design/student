"""Raw row reuse is bounded by one synchronous read, never an authority receipt."""
import sqlite3

import pytest

from home_study_agent.rubric_calibration_rows import ReadRows


@pytest.fixture
def connection():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE child_profile(child_id TEXT, grade TEXT, status TEXT)')
    c.executemany('INSERT INTO child_profile VALUES (?,?,?)', [('a', '3', 'active'), ('b', '4', 'active')])
    c.commit()
    c.execute('BEGIN')
    yield c
    c.close()


QUERY = "SELECT grade FROM child_profile WHERE child_id=? AND status='active'"


def test_repeated_raw_rows_execute_once_and_cursors_are_independent(connection):
    calls = []
    connection.set_trace_callback(calls.append)
    with ReadRows(connection) as read:
        first = read.execute(QUERY, ('a',))
        assert first.fetchone()[0] == '3'
        assert first.fetchone() is None
        assert read.execute(QUERY, ('a',)).fetchall()[0][0] == '3'
        assert read.execute(QUERY, ('b',)).fetchone()[0] == '4'
    assert len(calls) == 2
    with pytest.raises(ValueError, match='READ_SCOPE_CHANGED'):
        read.execute(QUERY, ('a',))


def test_raw_reuse_rejects_external_transaction_write_and_does_not_commit(connection):
    with ReadRows(connection) as read:
        assert read.execute(QUERY, ('a',)).fetchone()[0] == '3'
        connection.execute("UPDATE child_profile SET grade='5' WHERE child_id='a'")
        with pytest.raises(ValueError, match='READ_SCOPE_CHANGED'):
            read.execute(QUERY, ('a',))
    assert connection.in_transaction
    connection.rollback()
    connection.execute('BEGIN')
    with ReadRows(connection) as read:
        assert read.execute(QUERY, ('a',)).fetchone()[0] == '3'


def test_non_whitelisted_functions_remain_live_and_scope_cannot_end_transaction(connection):
    values = iter([1, 2])
    connection.create_function('live_value', 0, lambda: next(values))
    with ReadRows(connection) as read:
        assert read.execute('SELECT live_value()').fetchone()[0] == 1
        assert read.execute('SELECT live_value()').fetchone()[0] == 2
        with pytest.raises(ValueError, match='READ_SCOPE_CHANGED'):
            read.commit()
    assert connection.in_transaction


def test_raw_cache_is_bounded_and_next_read_observes_mutation(connection):
    with ReadRows(connection, max_entries=1) as read:
        read.execute(QUERY, ('a',)).fetchone()
        read.execute(QUERY, ('b',)).fetchone()
        assert len(read.rows) == 1
    connection.execute("UPDATE child_profile SET grade='6' WHERE child_id='a'")
    with ReadRows(connection) as read:
        assert read.execute(QUERY, ('a',)).fetchone()[0] == '6'


def test_row_reuse_never_reuses_external_image_validation(tmp_path, monkeypatch):
    from home_study_agent.rubric_calibration_evidence import source_snapshot
    from test_visual_rubric_identity import published, assessment
    with published(tmp_path, monkeypatch) as case:
        rubrics, snap = assessment(case, monkeypatch)
        paths = list((case.settings.data_dir / 'media').rglob('*.enc'))
        assert len(paths) == 1
        path, original = paths[0], paths[0].read_bytes()
        with case.db.connect() as c:
            c.execute('BEGIN')
            with ReadRows(c) as read:
                before = source_snapshot(read, 'child', snap['source_evidence_id'])
                assert rubrics.get('child', snap['assessment_id'], connection=read)['source_status'] == 'current'
                changes = c.total_changes
                try:
                    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
                    with pytest.raises(ValueError):
                        source_snapshot(read, 'child', snap['source_evidence_id'])
                    assert rubrics.get('child', snap['assessment_id'], connection=read)['source_status'] == 'changed'
                    assert c.total_changes == changes
                finally:
                    path.write_bytes(original)
                assert source_snapshot(read, 'child', snap['source_evidence_id']) == before
            c.rollback()
