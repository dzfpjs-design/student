"""Publication admission against the production Database and lifecycle entry points."""
import asyncio
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent import native_privacy as native
from home_study_agent import privacy_operation_receipt as receipts
from home_study_agent import restore_publication as publication


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.initialize()
    return db


def test_original_schema_initialization_and_nested_sql_remain_admitted(database):
    database.initialize()
    with publication.publication_owner(database) as owner:
        with publication.publication_owner(database) as nested:
            assert nested is owner
            with database.connect() as first, database.connect() as second:
                assert first.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                assert second.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] > 0
    with database.connect() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_foreign_async_task_cannot_inherit_original_owner_or_execute_sql(database):
    async def run():
        with publication.publication_owner(database) as owner:
            async def foreign():
                with pytest.raises(publication.RestorePublicationError, match="BUSY"):
                    with database.connect():
                        pytest.fail("foreign task entered SQL")
                with pytest.raises(publication.RestorePublicationError, match="BUSY"):
                    with publication.publication_owner(database):
                        pytest.fail("foreign task inherited writer")
                with pytest.raises(publication.RestorePublicationError, match="OWNER_REQUIRED"):
                    publication.new_prepared_path(owner)
            await asyncio.create_task(foreign())
            with database.connect() as connection:
                assert connection.execute("SELECT 1").fetchone()[0] == 1
    asyncio.run(run())


@pytest.mark.parametrize("method,args", [
    ("restore_backup", ("synthetic-backup", "确认恢复")),
    ("restore_portable_backup", ("synthetic-path", "synthetic-passphrase", "确认迁移恢复")),
])
def test_real_lifecycle_entry_rejects_reader_upgrade_before_guarded_body(database, tmp_path, monkeypatch, method, args):
    service = PrivacyLifecycleService(database, tmp_path)
    entered = []
    monkeypatch.setattr(service, "_" + method + "_guarded", lambda *a, **kw: entered.append(True))
    with database.connect():
        with pytest.raises(publication.RestorePublicationError, match="READER_UPGRADE_FORBIDDEN"):
            getattr(service, method)(*args, operation_request_id="synthetic-request")
    assert entered == []
    with publication.publication_owner(database):
        assert getattr(service, method)(*args, operation_request_id="synthetic-request") is None
    assert entered == [True]


def test_unknown_journal_stops_before_schema_or_upgrade_sql(database, monkeypatch):
    path = database.path.with_name("." + database.path.name + ".restore-publication.json")
    path.write_bytes(b'{"schema":1,"unknown":true}')
    called = []
    monkeypatch.setattr(database, "_initialize_admitted", lambda: called.append(True))
    with pytest.raises(publication.RestorePublicationError, match="JOURNAL_INVALID"):
        database.initialize()
    assert called == []
    path.unlink()
    with database.connect() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_actual_schema_db_only_publication_keeps_original_receipt_and_native_tails(database):
    service = receipts.PrivacyOperationReceipts(database)
    with database.connect() as connection:
        receipts.initialize(connection)
        native.ensure_schema(connection)
        connection.execute("CREATE TABLE publication_test_value(value TEXT)")
        connection.execute("INSERT INTO publication_test_value VALUES('old')")
        connection.commit()
    request = "public-" + uuid4().hex
    operation = {"request_id": request, "expected_generation": service.current()["generation"],
                 "cleanup_manifest_sha256": "c" * 64}
    body = {"operation": operation, "backup_id": "public_synthetic", "confirmation": "public_test"}
    route = "/v1/parent/backups/restore"
    metadata = {"nonce": uuid4().hex, "request_id": request,
                "expected_generation": operation["expected_generation"], "kind": "restore",
                "child_id": None, "scope": None, "command_sha256": native.command_hash(route, body),
                "cleanup_manifest_sha256": "c" * 64}
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        native.claim(connection, metadata, route, body, "restore")
        connection.commit()
    service.reserve(operation, "restore", "public_synthetic")
    service.bind_source(request, "a" * 64)
    with publication.publication_owner(database) as owner:
        prepared = publication.new_prepared_path(owner)
        with database.connect() as live, closing(sqlite3.connect(prepared)) as staging:
            live.backup(staging)
            receipts.preserve_live_identities(live, staging)
            native.preserve_live_authority(live, staging)
            staging.execute("UPDATE publication_test_value SET value='new'")
            audit = uuid4().hex
            staging.execute("INSERT INTO audit_event VALUES(?,'parent','backup_restored',NULL,?,'2026-10-20T00:00:00Z')",
                            (audit, '{"privacy_operation_request_id":"' + request + '"}'))
            receipts.record_restore(staging, request, audit)
            staging.commit()
            staging.execute("PRAGMA journal_mode=DELETE")
        publication.prepare_publication(owner, prepared, request)
        publication.publish_prepared_database(owner)
        assert publication.reconcile_publication(owner)["state"] == "DATABASE_COMMITTED_PENDING_ORIGINAL_TAILS"
        service.finish(request)
        with database.connect() as connection:
            native.finish(connection, metadata, "finished")
            connection.commit()
    assert publication.recover_before_initialize(database)["state"] == "GATE_RELEASED_ORIGINAL_RECEIPT_CONFIRMED"
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT value FROM publication_test_value").fetchone()[0] == "new"
