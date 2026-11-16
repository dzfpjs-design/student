"""New writer ownership only; no relaxation of borrowed adapters or SQLite."""
from contextlib import contextmanager
import json
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent import rubric_calibration_transaction as tx
from home_study_agent.rubric_calibration import RubricCalibrationService


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'owned.db')
    with database.connect() as c:
        c.executescript('''CREATE TABLE child_profile(child_id TEXT);
            INSERT INTO child_profile VALUES('child');
            CREATE TABLE effects(value TEXT);
            CREATE TRIGGER child_event AFTER UPDATE ON child_profile
              BEGIN INSERT INTO effects VALUES('child-event'); END;''')
        c.commit()
    return database


def test_warm_is_read_only_upgrade_owns_writer_and_uncommitted_work_rolls_back(db, monkeypatch):
    with sqlite3.connect(db.path, timeout=0) as other:
        def warm(c, database, child):
            assert database is db and child == 'child'
            assert isinstance(c, tx._OwnedConnection)
            c.execute('SELECT * FROM child_profile').fetchall()
            other.execute('BEGIN IMMEDIATE')
            other.rollback()
            return True
        monkeypatch.setattr(tx, '_warm_score_protocols', warm)
        with tx.calibrated_write(db, 'child') as c:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
            assert c.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0
            c.execute("INSERT INTO effects VALUES('uncommitted')")
        assert other.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0


def test_concurrent_commit_retries_new_snapshot_without_replaying_caller(db, monkeypatch):
    with sqlite3.connect(db.path, timeout=0) as other:
        seen = []
        def warm(c, *_):
            c.execute('SELECT * FROM child_profile').fetchall()
            seen.append(c)
            if len(seen) == 1:
                other.execute("INSERT INTO effects VALUES('revoked elsewhere')")
                other.commit()
            return True
        monkeypatch.setattr(tx, '_warm_score_protocols', warm)
        calls = []
        with tx.calibrated_write(db, 'child') as c:
            calls.append('body')
            assert c.execute('SELECT value FROM effects').fetchone()[0] == 'revoked elsewhere'
            c.rollback()
        assert calls == ['body'] and len(seen) == 2 and seen[0] is not seen[1]
        assert other.execute('SELECT value FROM effects').fetchall() == [('revoked elsewhere',)]


def test_two_upgrade_conflicts_are_bounded_and_never_enter_caller(db, monkeypatch):
    seen = []
    with sqlite3.connect(db.path, timeout=0) as other:
        def warm(c, *_):
            c.execute('SELECT * FROM child_profile').fetchall()
            seen.append(c)
            other.execute('INSERT INTO effects VALUES(?)', (str(len(seen)),))
            other.commit()
            return True
        monkeypatch.setattr(tx, '_warm_score_protocols', warm)
        with pytest.raises(tx.CalibrationWriterBusyError, match='CALIBRATION_WRITER_BUSY'):
            with tx.calibrated_write(db, 'child'):
                pytest.fail('caller cannot run on either obsolete snapshot')
    assert len(seen) == 2


def test_caller_busy_after_yield_is_not_retried_and_writes_roll_back(db, monkeypatch):
    calls = []
    monkeypatch.setattr(tx, '_warm_score_protocols', lambda *_: False)
    with pytest.raises(sqlite3.OperationalError, match='database is locked'):
        with tx.calibrated_write(db, 'child') as c:
            calls.append('body')
            c.execute("INSERT INTO effects VALUES('never committed')")
            raise sqlite3.OperationalError('database is locked')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0
    assert calls == ['body']


@pytest.mark.parametrize('message', ['disk I/O error', 'database disk image is malformed', 'database table is locked'])
def test_upgrade_nonbusy_sql_errors_propagate_without_retry(db, monkeypatch, message):
    execute = tx._OwnedConnection.execute
    calls = []
    def fail(c, sql, parameters=()):
        if sql == 'UPDATE child_profile SET child_id=child_id WHERE 0':
            calls.append('upgrade')
            raise sqlite3.OperationalError(message)
        return execute(c, sql, parameters)
    monkeypatch.setattr(tx, '_warm_score_protocols', lambda *_: True)
    monkeypatch.setattr(tx._OwnedConnection, 'execute', fail)
    with pytest.raises(sqlite3.OperationalError, match=message):
        with tx.calibrated_write(db, 'child'):
            pytest.fail('nonbusy storage failure')
    assert calls == ['upgrade']


def test_explicit_commit_persists_and_sql_errors_are_not_swallowed(db, monkeypatch):
    monkeypatch.setattr(tx, '_warm_score_protocols', lambda *_: None)
    with tx.calibrated_write(db, 'child') as c:
        c.execute("INSERT INTO effects VALUES('committed')")
        c.commit()
    with db.connect() as c:
        assert c.execute('SELECT value FROM effects').fetchone()[0] == 'committed'
    def broken(*_):
        raise sqlite3.OperationalError('synthetic IO error')
    monkeypatch.setattr(tx, '_warm_score_protocols', broken)
    with pytest.raises(sqlite3.OperationalError, match='synthetic IO'):
        with tx.calibrated_write(db, 'child'):
            pytest.fail('SQL error must propagate')


def test_no_prepared_proof_discards_hint_snapshot_and_keeps_original_writer_semantics(db, monkeypatch):
    with sqlite3.connect(db.path, timeout=0) as other:
        def no_proof(c, *_):
            c.execute('SELECT * FROM child_profile').fetchall()
            other.execute("INSERT INTO effects VALUES('concurrent prior revision')")
            other.commit()
            return False
        monkeypatch.setattr(tx, '_warm_score_protocols', no_proof)
        with tx.calibrated_write(db, 'child') as c:
            assert c.execute('SELECT value FROM effects').fetchone()[0] == 'concurrent prior revision'
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
            assert tx._SESSION.get() == {}
            c.rollback()


def test_unknown_database_adapter_remains_unwrapped_and_owns_its_begin(db, monkeypatch):
    class Adapter:
        def __init__(self, c):
            self.execute = c.execute
            self.commit = c.commit
    class OtherDatabase:
        @contextmanager
        def connect(self):
            with db.connect() as c:
                yield Adapter(c)
    monkeypatch.setattr(tx, '_warm_score_protocols', lambda *_: pytest.fail('unknown adapter prewarm'))
    with tx.calibrated_write(OtherDatabase(), 'child') as c:
        assert type(c) is Adapter
        c.execute("INSERT INTO effects VALUES('adapter')")
        c.commit()


@pytest.mark.parametrize('failure', [ValueError('bad hint'), sqlite3.OperationalError('storage')])
def test_warm_hint_scope_deduplicates_and_never_swallows_database_errors(db, monkeypatch, failure):
    with db.connect() as c:
        c.executescript('''CREATE TABLE rubric_assessment(assessment_id,child_id,payload_json);
            CREATE TABLE rubric_review_event(assessment_id);
            CREATE TABLE rubric_calibration_protocol(protocol_id,child_id);
            CREATE TABLE rubric_calibration_event(protocol_id,operation);
            INSERT INTO rubric_calibration_event VALUES('p','qualify'),('other','qualify');
            INSERT INTO rubric_calibration_protocol VALUES('p','child'),('other','other');''')
        for aid, child, protocol in [('a', 'child', 'p'), ('b', 'child', 'p'), ('c', 'other', 'other')]:
            c.execute('INSERT INTO rubric_assessment VALUES(?,?,?)',
                      (aid, child, json.dumps({'calibration_binding': {'protocol_id': protocol}})))
        c.execute("INSERT INTO rubric_assessment VALUES('bad','child','{')")
        c.commit()
    seen = []
    def read(service, c, row, now):
        seen.append(row['protocol_id'])
        raise failure
    monkeypatch.setattr(RubricCalibrationService, '_read', read)
    if isinstance(failure, sqlite3.OperationalError):
        with pytest.raises(sqlite3.OperationalError, match='storage'):
            with tx.calibrated_write(db, 'child'):
                pytest.fail('unreadable storage')
    else:
        with tx.calibrated_write(db, 'child') as c:
            assert tx._SESSION.get() == {}, 'a failed hint is not an authorization ticket'
            with pytest.raises(ValueError, match='bad hint'):
                RubricCalibrationService(db)._read(c, {'protocol_id': 'p'}, None)
    assert seen == (['p'] if isinstance(failure, sqlite3.OperationalError) else ['p', 'p'])
