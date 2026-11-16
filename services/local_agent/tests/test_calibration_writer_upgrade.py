import sqlite3

import pytest

from home_study_agent import rubric_calibration_host as host
from home_study_agent.rubric_calibration_transaction import _OwnedConnection, begin_validated_write


@pytest.fixture
def pair(tmp_path):
    path = tmp_path / 'upgrade.db'
    raw = sqlite3.connect(path, timeout=0)
    raw.row_factory = sqlite3.Row
    raw.execute('PRAGMA journal_mode=WAL')
    raw.execute('CREATE TABLE learning_evaluation_execution (state)')
    raw.execute("INSERT INTO learning_evaluation_execution VALUES ('response_ready')")
    raw.execute('CREATE TABLE side_effect (value)')
    raw.execute("CREATE TRIGGER events AFTER UPDATE ON learning_evaluation_execution BEGIN INSERT INTO side_effect VALUES ('fired'); END")
    raw.commit()
    other = sqlite3.connect(path, timeout=0)
    try:
        yield _OwnedConnection(raw), other
    finally:
        raw.close()
        other.close()


def test_proof_precedes_writer_but_upgrade_holds_writer_without_row_events(pair, monkeypatch):
    c, other = pair
    def validate(connection, binding, **kwargs):
        assert connection is c and c.in_transaction
        c.execute('SELECT * FROM learning_evaluation_execution').fetchall()
        other.execute('BEGIN IMMEDIATE')
        other.rollback()
    monkeypatch.setattr(host, 'revalidate', validate)
    begin_validated_write(c, {'status': 'bound'})
    with pytest.raises(sqlite3.OperationalError, match='locked'):
        other.execute('BEGIN IMMEDIATE')
    assert not c.execute('SELECT * FROM side_effect').fetchall()
    assert c.execute('SELECT state FROM learning_evaluation_execution').fetchone()[0] == 'response_ready'
    c.rollback()
    other.execute('BEGIN IMMEDIATE')
    other.rollback()


def test_concurrent_commit_rejects_snapshot_upgrade_and_preserves_response(pair, monkeypatch):
    c, other = pair
    def validate(*args, **kwargs):
        c.execute('SELECT * FROM learning_evaluation_execution').fetchall()
        other.execute("INSERT INTO side_effect VALUES ('concurrent revoke')")
        other.commit()
    monkeypatch.setattr(host, 'revalidate', validate)
    with pytest.raises(sqlite3.OperationalError, match='locked'):
        begin_validated_write(c, {'status': 'bound'})
    c.rollback()
    assert c.execute('SELECT state FROM learning_evaluation_execution').fetchone()[0] == 'response_ready'


def test_invalid_proof_never_obtains_writer(pair, monkeypatch):
    c, other = pair
    def invalid(*args, **kwargs):
        raise ValueError('revoked')
    monkeypatch.setattr(host, 'revalidate', invalid)
    with pytest.raises(ValueError, match='revoked'):
        begin_validated_write(c, {'status': 'bound'})
    other.execute('BEGIN IMMEDIATE')
    other.rollback()
    c.rollback()


def test_unbound_path_keeps_immediate_writer(pair, monkeypatch):
    c, other = pair
    monkeypatch.setattr(host, 'revalidate', lambda *a, **k: pytest.fail('unbound proof'))
    begin_validated_write(c, {})
    with pytest.raises(sqlite3.OperationalError, match='locked'):
        other.execute('BEGIN IMMEDIATE')
    c.rollback()
