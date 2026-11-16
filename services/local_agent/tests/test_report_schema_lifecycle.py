from contextlib import contextmanager
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.report_delivery import DeliveryStateError, ReportDeliveryService
from home_study_agent.repository import StudyRepository, utc_now


TABLE = "report_delivery_channel_test_action"


@pytest.fixture
def legacy_channel_ledger(tmp_path):
    database = Database(tmp_path / "report-schema.db")
    database.initialize()
    StudyRepository(database).seed_family()
    with database.connect() as connection:
        connection.execute(f"""CREATE TABLE {TABLE} (
            client_action_id TEXT PRIMARY KEY,
            child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
            channel TEXT NOT NULL CHECK(channel IN ('email','wechat_work_bot')),
            status TEXT NOT NULL CHECK(status IN ('processing','passed','failed','blocked')),
            message TEXT,tested_at TEXT,created_at TEXT NOT NULL)""")
        for child in ("child-primary-4", "child-junior-3"):
            connection.execute(f"INSERT INTO {TABLE} VALUES (?,?,'email','processing',NULL,NULL,?)",
                               (f"test-{child}", child, utc_now()))
        connection.commit()
    return database


def ledger(database):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {TABLE} ORDER BY client_action_id")]


@pytest.mark.parametrize("failure", ["create", "copy", "drop"])
def test_channel_ledger_upgrade_failure_is_atomic_and_restart_preserves_unknown(legacy_channel_ledger, monkeypatch, failure):
    database = legacy_channel_ledger
    before = ledger(database)
    original_connect = database.connect
    blocked = []

    @contextmanager
    def interrupted_connection():
        with original_connect() as connection:
            def authorize(operation, first, second, db_name, origin):
                target = {
                    "create": (sqlite3.SQLITE_CREATE_TABLE, TABLE),
                    "copy": (sqlite3.SQLITE_READ, TABLE + "_legacy"),
                    "drop": (sqlite3.SQLITE_DROP_TABLE, TABLE + "_legacy"),
                }[failure]
                # The initial CREATE IF NOT EXISTS is allowed; interrupt the actual rebuild.
                if (operation, first) == target and (
                    failure != "create" or not connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
                ):
                    blocked.append(failure)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            yield connection

    with monkeypatch.context() as patch:
        patch.setattr(database, "connect", interrupted_connection)
        with pytest.raises(sqlite3.DatabaseError):
            ReportDeliveryService(database, b"S" * 32).initialize()
    assert blocked
    assert ledger(database) == before
    with database.connect() as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE + "_legacy",)).fetchone()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    restarted = ReportDeliveryService(Database(database.path), b"S" * 32)
    restarted.initialize()
    assert [row["status"] for row in ledger(database)] == ["unknown", "unknown"]
    for child in ("child-primary-4", "child-junior-3"):
        outcome = restarted.test_channel(child, "email", f"test-{child}")
        assert outcome["status"] == "unknown"
    stable = ledger(database)
    restarted.initialize()
    assert ledger(database) == stable


def test_upgrade_recovers_an_empty_new_table_stranded_by_older_migrator(legacy_channel_ledger):
    database = legacy_channel_ledger
    with database.connect() as connection:
        connection.execute(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_legacy")
        connection.commit()
    # A previous crash after rename can leave no canonical table at all.
    restarted = ReportDeliveryService(Database(database.path), b"S" * 32)
    restarted.initialize()
    rows = ledger(database)
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"unknown"}
    with database.connect() as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE + "_legacy",)).fetchone()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_channel_ledger_upgrade_borrows_transaction_without_committing(legacy_channel_ledger):
    database = legacy_channel_ledger
    before = ledger(database)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        ReportDeliveryService._migrate_channel_test_actions(connection)
        assert connection.in_transaction
        assert "replacement_action_id" in {r["name"] for r in connection.execute(f"PRAGMA table_info({TABLE})")}
        connection.rollback()
    assert ledger(database) == before


def test_competing_stranded_ledgers_block_without_erasing_either(legacy_channel_ledger):
    database = legacy_channel_ledger
    before = ledger(database)
    with database.connect() as connection:
        connection.execute(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_legacy")
        connection.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM {TABLE}_legacy")
        connection.commit()
    with pytest.raises(DeliveryStateError, match="冲突的升级历史"):
        ReportDeliveryService(database, b"S" * 32).initialize()
    assert ledger(database) == before
    with database.connect() as connection:
        assert [dict(row) for row in connection.execute(f"SELECT * FROM {TABLE}_legacy ORDER BY client_action_id")] == before


def test_migrated_channel_ledger_child_delete_cascades_only_that_child(legacy_channel_ledger):
    database = legacy_channel_ledger
    ReportDeliveryService(database, b"S" * 32).initialize()
    with database.connect() as connection:
        connection.execute("DELETE FROM family_rule WHERE child_id='child-primary-4'")
        connection.execute("DELETE FROM child_profile WHERE child_id='child-primary-4'")
        connection.commit()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert {row["child_id"] for row in ledger(database)} == {"child-junior-3"}


def test_restore_hook_handles_legacy_check_without_ddl_and_upgrades_without_attempts(legacy_channel_ledger):
    database = legacy_channel_ledger
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        schemas = list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        result = ReportDeliveryService.quarantine_after_restore(connection, "legacy-restore")
        assert result["legacy_status_fallbacks"]["channel_tests"] == 2
        assert connection.in_transaction
        assert list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == schemas
        assert ReportDeliveryService.quarantine_after_restore(connection, "legacy-restore") == result
        connection.commit()
    assert {row["status"] for row in ledger(database)} == {"blocked"}
    ReportDeliveryService(database, b"S" * 32).initialize(recover_processing=False)
    assert {row["status"] for row in ledger(database)} == {"unknown"}
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM report_delivery_attempt").fetchone()[0] == 0


def test_restore_hook_old_snapshot_without_report_tables_is_idempotent_and_borrowed(tmp_path):
    database = Database(tmp_path / "pre-report.db")
    database.initialize()
    with database.connect() as connection:
        with pytest.raises(DeliveryStateError, match="TRANSACTION_REQUIRED"):
            ReportDeliveryService.quarantine_after_restore(connection, "no-transaction")
        connection.execute("BEGIN IMMEDIATE")
        schemas = list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        result = ReportDeliveryService.quarantine_after_restore(connection, "pre-report")
        assert result["outbox_unknown"] == result["snapshot_approvals_invalidated"] == 0
        assert list(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == schemas
        connection.rollback()
        assert not connection.execute("SELECT 1 FROM audit_event WHERE action='report_delivery_restore_quarantined'").fetchone()
