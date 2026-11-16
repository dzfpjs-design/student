"""Occurrence migration compatibility and opt-in real-corpus startup measurements.

Set HSC_SCHEMA_SCALE_BUNDLE to a local content bundle to measure the real
catalog bootstrap. No application, model, extraction job or remote API runs.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Barrier
from time import perf_counter

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph import schema
from home_study_agent.knowledge_graph.catalog_bootstrap import CatalogGraphBootstrapper
from home_study_agent.knowledge_graph.content_bundle import import_content
from home_study_agent.knowledge_graph import service as graph_module
from home_study_agent.knowledge_graph import content_bundle
from test_curriculum_content_delivery import seed as seed_bundle_source, initialize as initialize_bundle_target, digest


PENDING = "kg_segment_occurrence_pending"
MIGRATION = 31


def connect(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def prepare(path, *, legacy=False):
    with closing(connect(path)) as db:
        schema.ensure_schema(db, "test-initial")
        if legacy:
            for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall():
                if PENDING in (sql or ""):
                    db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            db.execute(f"DROP TABLE IF EXISTS {PENDING}")
            db.execute("DELETE FROM kg_schema_migration WHERE version>=?", (MIGRATION,))
            db.execute("INSERT OR IGNORE INTO kg_schema_migration VALUES(30,'legacy')")
        db.execute("""INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,source_ref,
            license_boundary,review_status,version_label,created_at,updated_at)
            VALUES('c','fixture','fixture','local://fixture','fixture','local','restricted','1','now','now')""")
        for rid in ("r", "other"):
            db.execute("""INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,transcript_relative_path,
                transcript_sha256,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at)
                VALUES(?,'c',?,'fixture','fixture.txt','fixture','fixture','local',.5,'restricted','1','now','now')""", (rid, rid))
        db.commit()


def add_segment(db, segment_id="s", ordinal=0):
    db.execute("""INSERT INTO kg_segment(segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,
        source_ref,confidence,review_status,created_at) VALUES(?,'r',?,10,30,'synthetic segment',?,'fixture',.5,'restricted','now')""",
        (segment_id, ordinal, segment_id))


def full_backfills(statements):
    return [sql for sql in statements if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO\s+kg_segment_occurrence\b", sql, re.I)
            and re.search(r"\bFROM\s+kg_segment\b", sql, re.I)]


def test_current_schema_repeated_checks_do_not_scan_corpus(tmp_path):
    path = tmp_path / "graph.db"
    prepare(path)
    statements = []
    with closing(connect(path)) as db:
        db.set_trace_callback(statements.append)
        for _ in range(5):
            schema.ensure_schema(db, "warm")
            db.commit()
    assert not full_backfills(statements)


def test_v30_backfill_is_once_durable_and_preserves_existing_occurrences(tmp_path):
    path = tmp_path / "legacy.db"
    prepare(path, legacy=True)
    with closing(connect(path)) as db:
        add_segment(db, "missing", 0)
        add_segment(db, "owned", 1)
        db.execute("INSERT INTO kg_segment_occurrence VALUES('r','owned',7,101,120)")
        db.execute("INSERT INTO kg_segment_occurrence VALUES('other','owned',9,201,220)")
        db.commit()
    statements = []
    with closing(connect(path)) as db:
        db.set_trace_callback(statements.append)
        schema.ensure_schema(db, "upgraded")
        db.commit()
        assert len(full_backfills(statements)) == 1
        assert db.execute("SELECT ordinal,start_char,end_char FROM kg_segment_occurrence WHERE segment_id='owned' AND resource_id='r'").fetchone() == (7, 101, 120)
        assert db.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 3
        assert db.execute("SELECT 1 FROM kg_schema_migration WHERE version=?", (MIGRATION,)).fetchone()
    with closing(connect(path)) as db:
        statements.clear()
        db.set_trace_callback(statements.append)
        schema.ensure_schema(db, "restarted")
        db.commit()
        assert not full_backfills(statements)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_page_insert_after_upgrade_is_backfilled_without_scan(tmp_path):
    path = tmp_path / "legacy-writer.db"
    prepare(path)
    with closing(connect(path)) as db:
        add_segment(db)
        db.commit()
        assert db.execute(f"SELECT segment_id FROM {PENDING}").fetchall() == [("s",)]
        assert db.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 0
    with closing(connect(path)) as db:
        statements = []
        db.set_trace_callback(statements.append)
        schema.ensure_schema(db, "next-reader")
        db.commit()
        assert db.execute("SELECT * FROM kg_segment_occurrence").fetchall() == [("r", "s", 0, 10, 30)]
        assert db.execute(f"SELECT COUNT(*) FROM {PENDING}").fetchone()[0] == 0
        assert not full_backfills(statements)


def test_modern_plain_occurrence_insert_remains_compatible_and_authoritative(tmp_path):
    path = tmp_path / "modern-writer.db"
    prepare(path)
    with closing(connect(path)) as db:
        add_segment(db)
        # TextbookPageService uses plain INSERT, not INSERT OR IGNORE.
        db.execute("INSERT INTO kg_segment_occurrence VALUES('r','s',8,100,120)")
        db.execute("INSERT INTO kg_segment_occurrence VALUES('other','s',9,200,220)")
        db.commit()
        schema.ensure_schema(db, "next-reader")
        db.commit()
        assert db.execute("SELECT * FROM kg_segment_occurrence ORDER BY resource_id").fetchall() == [
            ("other", "s", 9, 200, 220), ("r", "s", 8, 100, 120)]
        # Removing a source occurrence must not be undone by unrelated reads.
        db.execute("DELETE FROM kg_segment_occurrence WHERE resource_id='r'")
        db.commit()
        schema.ensure_schema(db, "after-withdrawal")
        db.commit()
        assert db.execute("SELECT resource_id FROM kg_segment_occurrence").fetchall() == [("other",)]


def test_updates_and_cascading_deletes_keep_pending_queue_correct(tmp_path):
    path = tmp_path / "updated.db"
    prepare(path)
    with closing(connect(path)) as db:
        add_segment(db)
        db.commit()
        schema.ensure_schema(db, "first")
        db.commit()
        db.execute("DELETE FROM kg_segment_occurrence")
        db.execute("UPDATE kg_segment SET ordinal=2,start_char=40,end_char=60 WHERE segment_id='s'")
        db.commit()
        schema.ensure_schema(db, "updated")
        db.commit()
        assert db.execute("SELECT * FROM kg_segment_occurrence").fetchall() == [("r", "s", 2, 40, 60)]
        add_segment(db, "deleted", 3)
        db.execute("DELETE FROM kg_segment WHERE segment_id='deleted'")
        db.commit()
        assert db.execute(f"SELECT COUNT(*) FROM {PENDING}").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_interrupted_sync_does_not_commit_half_backfill_or_migration_marker(tmp_path):
    path = tmp_path / "interrupted.db"
    prepare(path, legacy=True)
    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "DELETE FROM kg_segment_occurrence_pending":
                raise sqlite3.OperationalError("injected queue-drain failure")
            return super().execute(sql, *args)

    with closing(sqlite3.connect(path, factory=InterruptedConnection)) as db:
        add_segment(db)
        db.commit()

        with pytest.raises(sqlite3.OperationalError, match="injected"):
            schema.ensure_schema(db, "interrupted")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM kg_schema_migration WHERE version=?", (MIGRATION,)).fetchone()
    with closing(connect(path)) as db:
        schema.ensure_schema(db, "recovery")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 1


def test_schema_upgrade_and_missing_index_are_not_hidden_by_process_cache(tmp_path, monkeypatch):
    path = tmp_path / "future.db"
    prepare(path)
    with closing(connect(path)) as db:
        db.execute("DROP INDEX idx_kg_segment_hash")
        db.commit()
        monkeypatch.setattr(schema, "KG_SCHEMA_VERSION", schema.KG_SCHEMA_VERSION + 1)
        monkeypatch.setattr(schema, "SCHEMA", schema.SCHEMA + "\nCREATE TABLE IF NOT EXISTS kg_future_probe(id TEXT PRIMARY KEY);")
        schema.ensure_schema(db, "next-version")
        db.commit()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_kg_segment_hash'").fetchone()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='kg_future_probe'").fetchone()
        assert db.execute("SELECT 1 FROM kg_schema_migration WHERE version=?", (schema.KG_SCHEMA_VERSION,)).fetchone()


def test_concurrent_upgrade_backfills_once(tmp_path):
    path = tmp_path / "concurrent.db"
    prepare(path, legacy=True)
    with closing(connect(path)) as db:
        add_segment(db)
        db.commit()
    barrier = Barrier(2)

    def upgrade():
        with closing(connect(path)) as db:
            statements = []
            db.set_trace_callback(statements.append)
            barrier.wait(timeout=10)
            schema.ensure_schema(db, "concurrent")
            db.commit()
            return len(full_backfills(statements))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upgrade) for _ in range(2)]
        assert sum(f.result(timeout=30) for f in futures) == 1


def make_bundle(tmp_path):
    source, target, bundle = tmp_path / "source.db", tmp_path / "target.db", tmp_path / "content.zip"
    seed_bundle_source(source)
    initialize_bundle_target(target)
    with closing(connect(source)) as db:
        db.execute("INSERT INTO kg_segment_occurrence VALUES('r1','sr1',7,100,120)")
        db.commit()
    content_bundle.export_content(source, bundle)
    return target, bundle


def test_bundle_backfills_missing_canonical_occurrences_before_commit_and_replays(tmp_path):
    target, bundle = make_bundle(tmp_path)
    result = import_content(bundle, target, expected_sha256=digest(bundle))
    assert result["tables"]["kg_segment"]["inserted"] == 2
    with closing(connect(target)) as db:
        assert db.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 2
        assert db.execute("SELECT ordinal,start_char,end_char FROM kg_segment_occurrence WHERE segment_id='sr1'").fetchone() == (7, 100, 120)
        assert db.execute(f"SELECT COUNT(*) FROM {PENDING}").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert import_content(bundle, target, expected_sha256=digest(bundle))["already_imported"]
    with closing(connect(target)) as db:
        assert db.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM kg_content_delivery").fetchone()[0] == 1


def test_bundle_failure_after_backfill_rolls_back_and_retries(tmp_path, monkeypatch):
    target, bundle = make_bundle(tmp_path)
    original_connect = sqlite3.connect

    class FailReceiptConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO kg_content_delivery VALUES"):
                assert self.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 2
                raise sqlite3.OperationalError("injected receipt failure")
            return super().execute(sql, *args)

    def failed_connection(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=FailReceiptConnection)

    with monkeypatch.context() as patch:
        patch.setattr(content_bundle.sqlite3, "connect", failed_connection)
        with pytest.raises(sqlite3.OperationalError, match="injected receipt failure"):
            import_content(bundle, target, expected_sha256=digest(bundle))
    with closing(connect(target)) as db:
        for table in ("kg_resource", "kg_segment", "kg_segment_occurrence", PENDING, "kg_content_delivery"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert import_content(bundle, target, expected_sha256=digest(bundle))["tables"]["kg_segment"]["inserted"] == 2


@pytest.mark.skipif(not os.environ.get("HSC_SCHEMA_SCALE_BUNDLE"), reason="requires explicit local 49k content bundle")
def test_real_49k_bundle_catalog_startup_has_no_repeated_corpus_backfill(tmp_path, monkeypatch):
    bundle = Path(os.environ["HSC_SCHEMA_SCALE_BUNDLE"])
    hasher = hashlib.sha256()
    with bundle.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    database = Database(tmp_path / "scale.db")
    database.initialize()
    with database.connect() as db:
        started = perf_counter()
        for _ in range(20):
            schema.ensure_schema(db, "empty")
            db.commit()
        empty_seconds = perf_counter() - started
    started = perf_counter()
    imported = import_content(bundle, database.path, expected_sha256=digest)
    report = {"bundle_sha256": digest, "import_seconds": round(perf_counter() - started, 3),
              "empty_20_seconds": round(empty_seconds, 3),
              "segments": imported["tables"]["kg_segment"]["inserted"],
              "resources": imported["tables"]["kg_resource"]["inserted"], "cloud_calls": 0}
    assert report["segments"] == 49619 and report["resources"] == 10091
    statements = []
    with database.connect() as db:
        started = perf_counter()
        schema.ensure_schema(db, "post-import")
        db.commit()
        report["first_ready_seconds"] = round(perf_counter() - started, 3)
        db.set_trace_callback(statements.append)
        started = perf_counter()
        for _ in range(20):
            schema.ensure_schema(db, "warm")
            db.commit()
        report["warm_20_seconds"] = round(perf_counter() - started, 3)
        report["warm_full_backfills"] = len(full_backfills(statements))
    original = graph_module.ensure_schema
    metrics = {"calls": 0, "full_backfills": 0, "seconds": 0.0}

    def measured(db, applied_at):
        seen = []
        db.set_trace_callback(seen.append)
        started = perf_counter()
        try:
            return original(db, applied_at)
        finally:
            metrics["calls"] += 1
            metrics["seconds"] += perf_counter() - started
            metrics["full_backfills"] += len(full_backfills(seen))
            db.set_trace_callback(None)

    monkeypatch.setattr(graph_module, "ensure_schema", measured)
    started = perf_counter()
    result = CatalogGraphBootstrapper(database).run(rebuild_local_candidates=False)
    report["catalog_seconds"] = round(perf_counter() - started, 3)
    report["catalog_schema"] = {**metrics, "seconds": round(metrics["seconds"], 3)}
    assert result["candidate_segment_mappings"]["skipped"] == "startup_does_not_rescan_local_corpus"
    with database.connect() as db:
        report["occurrences"] = db.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        started = perf_counter()
        assert [row[0] for row in db.execute("PRAGMA integrity_check")] == ["ok"]
        report["integrity_check_seconds"] = round(perf_counter() - started, 3)
    if os.environ.get("HSC_SCHEMA_SCALE_LIFESPAN") == "1":
        from fastapi.testclient import TestClient
        from home_study_agent.app import create_app
        from home_study_agent.config import Settings
        settings = Settings(data_dir=tmp_path, database_path=database.path, maintenance_enabled=False)
        report["lifespans"] = []
        for _ in range(2):
            started = perf_counter()
            app = create_app(settings)
            constructed = perf_counter()
            with TestClient(app) as client:
                ready = perf_counter()
                response = client.get("/v1/bootstrap")
                assert response.status_code == 200
                report["lifespans"].append({"create_app_seconds": round(constructed - started, 3),
                    "lifespan_seconds": round(ready - constructed, 3),
                    "bootstrap_seconds": round(perf_counter() - ready, 3)})
    evidence = tmp_path / "schema-scale-evidence.json"
    evidence.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"evidence": str(evidence), **report}))
    assert report["warm_full_backfills"] == 0
    assert metrics["full_backfills"] == 0
