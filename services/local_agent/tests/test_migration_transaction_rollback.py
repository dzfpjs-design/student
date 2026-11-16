"""Failed migrations must release their own SQL transaction before restoring."""
from contextlib import closing
import sqlite3

import pytest

from home_study_agent.database import Database, DatabaseUpgradeRecoveryError, LATEST_SCHEMA_VERSION, StudyConnection


def synthetic_v92_with_failed_statement(tmp_path):
    db = Database(tmp_path / 'synthetic.db')
    db.initialize()
    with db.connect() as connection:
        connection.execute('DELETE FROM schema_migration WHERE version > 92')
        connection.execute("INSERT INTO child_profile VALUES('synthetic-child','synthetic','grade-9','test','active','2026-01-01','2026-01-01')")
        connection.execute("CREATE TRIGGER synthetic_migration_failure BEFORE INSERT ON schema_migration WHEN NEW.version=93 BEGIN SELECT RAISE(ABORT,'SYNTHETIC_FAILURE'); END")
        connection.commit()
    return db


def test_migration_failure_rolls_back_active_transaction_before_restoring_snapshot(tmp_path):
    db = synthetic_v92_with_failed_statement(tmp_path)
    # The statement cursor remains reachable through the original traceback.
    with pytest.raises(sqlite3.IntegrityError, match='SYNTHETIC_FAILURE'):
        db.initialize()
    assert db.latest_upgrade_status()['status'] == 'restored_after_failure'
    with closing(sqlite3.connect(db.path)) as connection:
        assert connection.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 92
        assert connection.execute("SELECT COUNT(*) FROM child_profile WHERE child_id='synthetic-child'").fetchone()[0] == 1
        connection.execute('DROP TRIGGER synthetic_migration_failure')
        connection.commit()
    db.initialize()
    with closing(sqlite3.connect(db.path)) as connection:
        assert connection.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM child_profile WHERE child_id='synthetic-child'").fetchone()[0] == 1


def test_failed_original_rollback_fails_closed_and_retains_fault_chain(tmp_path, monkeypatch):
    db = synthetic_v92_with_failed_statement(tmp_path)
    original = StudyConnection.rollback
    def failed_rollback(connection):
        if connection.in_transaction:
            raise sqlite3.OperationalError('SYNTHETIC_ROLLBACK_FAILURE')
        return original(connection)
    monkeypatch.setattr(StudyConnection, 'rollback', failed_rollback)
    from home_study_agent.restore_publication import RestorePublicationError, _gate
    with pytest.raises((DatabaseUpgradeRecoveryError, RestorePublicationError)) as caught:
        db.initialize()
    assert _gate(db.path).poisoned
    errors = []
    pending = [caught.value]
    while pending:
        error = pending.pop()
        if error in errors:
            continue
        errors.append(error)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if error.__context__ is not None:
            pending.append(error.__context__)
    chain = [(type(error).__name__, str(error)) for error in errors]
    assert any('SYNTHETIC_FAILURE' in message for _, message in chain), chain
    assert any('SYNTHETIC_ROLLBACK_FAILURE' in message for _, message in chain), chain
    assert db.latest_upgrade_status()['status'] != 'restored_after_failure'


def test_migration_restore_does_not_override_an_independent_active_writer(tmp_path, monkeypatch):
    db = synthetic_v92_with_failed_statement(tmp_path)
    external = []

    def fail_with_external_writer(_database):
        connection = sqlite3.connect(db.path, timeout=0)
        external.append(connection)
        connection.execute('BEGIN IMMEDIATE')
        raise RuntimeError('SYNTHETIC_MIGRATION_FAILURE')

    monkeypatch.setattr(Database, '_initialize_schema', fail_with_external_writer)
    try:
        with pytest.raises(DatabaseUpgradeRecoveryError) as caught:
            db.initialize()
        assert 'upgrade rollback target busy' in str(caught.value.__cause__)
        assert external[0].in_transaction
        assert db.latest_upgrade_status()['status'] != 'restored_after_failure'
    finally:
        for connection in external:
            connection.rollback()
            connection.close()
