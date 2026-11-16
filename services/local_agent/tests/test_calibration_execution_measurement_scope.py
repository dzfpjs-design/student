"""Synthetic measurement is transparent to real admitted SQLite ownership."""
from datetime import datetime, timezone

import pytest

from calibration_execution_measurement import ExecutionMeasurement
from home_study_agent.database import Database
from home_study_agent.original_connection_read import original_connection
from home_study_agent.repository import StudyRepository
from home_study_agent.rubric_calibration import RubricCalibrationService


@pytest.fixture
def databases(tmp_path):
    first = Database(tmp_path / 'measurement.db')
    first.initialize()
    StudyRepository(first).seed_family()
    RubricCalibrationService(first).initialize()
    second = Database(first.path)
    now = datetime.now(timezone.utc)
    with first.connect() as c:
        child = c.execute('SELECT child_id FROM child_profile ORDER BY child_id LIMIT 1').fetchone()[0]
        # Explicit nonqualifying synthetic journal. It cannot confer score authority.
        c.execute('''INSERT INTO rubric_calibration_protocol
            (protocol_id,child_id,version,scorer_json,scope_json,manifest_json,
             protocol_hash,created_at,expires_at) VALUES (?,?,?,'{}','{}','{}',?,?,?)''',
            ('measurement-revoked', child, 'synthetic', 'a' * 64, now.isoformat(), now.isoformat()))
        c.execute('''INSERT INTO rubric_calibration_event
            (event_id,protocol_id,child_id,revision,operation,reference_json,
             request_hash,client_action_id,created_at) VALUES (?,?,?,1,'revoke','{}',?,?,?)''',
            ('measurement-event', 'measurement-revoked', child, 'b' * 64,
             'measurement-revoke', now.isoformat()))
        c.commit()
        row = dict(c.execute('SELECT * FROM rubric_calibration_protocol WHERE protocol_id=?',
                             ('measurement-revoked',)).fetchone())
    return first, second, row, now


def test_explicit_databases_and_existing_new_none_services_keep_real_class_identity(databases, monkeypatch):
    first, second, row, now = databases
    factory, read = Database.connect, RubricCalibrationService._read
    existing = RubricCalibrationService(first)
    measurement = ExecutionMeasurement(monkeypatch, first, RubricCalibrationService,
        additional_databases=(second, second), additional_services=(existing,))

    def operation():
        services = (existing, RubricCalibrationService(second), RubricCalibrationService(None))
        for owner, service in zip((first, second, second), services):
            with owner.connect() as c:
                assert original_connection(c) is c
                value = service._read(c, row, now)
                assert value['status'] == 'revoked' and not value['score_qualified']
                assert not c.in_transaction
        with first.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE child_profile SET nickname=? WHERE child_id=?',
                      ('synthetic-not-committed', row['child_id']))
            c.rollback()
        with second.connect() as c:
            assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?',
                             (row['child_id'],)).fetchone()[0] != 'synthetic-not-committed'
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE child_profile SET nickname=? WHERE child_id=?',
                      ('synthetic-committed', row['child_id']))
            c.commit()
        return 'observed'

    value, metrics = measurement.run(operation, lock_probe=True)
    assert value == 'observed'
    assert Database.connect is factory and Database.connect.__wrapped__ is factory.__wrapped__
    assert RubricCalibrationService._read is read
    assert metrics['lock_probe'] and metrics['probe_interval_ms'] == 10
    assert len(metrics['calibration_read_ms']) == len(metrics['calibration_read_callers']) == 3
    assert metrics['sql_operations']['SELECT'] >= 4
    assert metrics['sql_operations']['UPDATE'] == 2
    assert metrics['sql_operations']['COMMIT'] >= 1 and metrics['sql_operations']['ROLLBACK'] >= 1
    assert len(metrics['begin_to_commit_ms']) == 2
    with first.connect() as c:
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?',
                         (row['child_id'],)).fetchone()[0] == 'synthetic-committed'


def test_unlisted_database_is_not_wrapped_or_counted(databases, monkeypatch):
    first, second, row, now = databases
    unlisted = Database(first.path)
    factory = Database.connect
    measurement = ExecutionMeasurement(monkeypatch, first, RubricCalibrationService,
        additional_databases=(second,))
    def operation():
        with unlisted.connect() as c:
            assert original_connection(c) is c
            return c.execute('SELECT 7').fetchone()[0]
    value, metrics = measurement.run(operation)
    assert value == 7 and metrics['sql_count'] == 0
    assert 'connect' not in unlisted.__dict__ and Database.connect is factory


def test_original_exception_and_rollback_survive_measurement_and_probe(databases, monkeypatch):
    first, _, row, _ = databases
    measurement = ExecutionMeasurement(monkeypatch, first, RubricCalibrationService)
    error = RuntimeError('synthetic original failure')
    def operation():
        with first.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE child_profile SET nickname=? WHERE child_id=?',
                      ('must-not-persist', row['child_id']))
            raise error
    with pytest.raises(RuntimeError) as caught:
        measurement.run(operation, lock_probe=True)
    assert caught.value is error and not measurement.enabled and measurement.stop.is_set()
    with first.connect() as c:
        assert original_connection(c) is c
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?',
                         (row['child_id'],)).fetchone()[0] != 'must-not-persist'


def test_measurement_constructor_retirement_preserves_original_service_arguments(databases):
    first, second, _, now = databases
    factory, read, initialize = Database.connect, RubricCalibrationService._read, RubricCalibrationService.__init__
    resolver = lambda *args, **kwargs: None
    clock = lambda: now
    with pytest.MonkeyPatch.context() as patch:
        measurement = ExecutionMeasurement(patch, first, RubricCalibrationService,
            additional_databases=(second,))
        service = RubricCalibrationService(None, authority_resolver=resolver, clock=clock)
        assert service.database is None and service.authority_resolver is resolver and service.clock is clock
        assert service._read.__func__ is not read
        assert Database.connect is factory and RubricCalibrationService._read is read
        assert not measurement.enabled
    assert Database.connect is factory and RubricCalibrationService._read is read
    assert RubricCalibrationService.__init__ is initialize
    assert service._read.__func__ is read
    with first.connect() as c:
        assert original_connection(c) is c and c.execute('SELECT 1').fetchone()[0] == 1
