from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

from .database import Database
from .document_monitoring import DocumentMonitoringService


REPLAY_FORMAT = "hsc_document_artifact_replay_v1"


def _file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_fingerprint(path: Path) -> dict[str, str | None]:
    # SQLite may update the shared-memory coordination file for a read-only
    # connection without changing database content. The database and WAL are
    # the durable records whose byte identity proves that replay did not write.
    return {
        suffix or "database": _file_sha256(path.with_name(path.name + suffix))
        for suffix in ("", "-wal")
    }


def _snapshot_database(source: Path, destination: Path) -> None:
    uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)
    # A copied database may retain WAL as its default journal mode. Convert the
    # disposable copy before the application migrator opens it.
    with sqlite3.connect(destination) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@dataclass(frozen=True)
class ReplayResult:
    report: dict[str, Any]
    isolated_database: Path


def replay_document_artifacts(
    source_database: Path,
    *,
    session_id: str | None = None,
    output_directory: Path | None = None,
) -> ReplayResult:
    """Replay persisted OCR artifacts in a writable, disposable database copy.

    The source is opened read-only through SQLite backup. The compact report
    contains aggregate counts and hashes only; it never includes OCR text,
    child names, question text, local media paths, or source identifiers.
    """
    source = source_database.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    before = _source_fingerprint(source)
    if output_directory is None:
        output = Path(tempfile.mkdtemp(prefix="hsc-document-replay-"))
    else:
        output = output_directory.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
    destination = output / "replay.db"
    if destination.exists():
        raise FileExistsError(destination)
    _snapshot_database(source, destination)

    from .runtime_data_scope import read_scope
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as snapshot:
        scope = read_scope(snapshot)
    database = Database(destination, runtime_mode=scope)
    database.initialize()
    monitor = DocumentMonitoringService(database)
    with database.connect() as connection:
        if session_id is None:
            row = connection.execute(
                """SELECT session_id FROM daily_session
                   WHERE status='active' AND state='study' AND ended_at IS NULL
                   ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
            if not row:
                raise ValueError("no active study session available for isolated replay")
            selected_session = str(row["session_id"])
        else:
            selected_session = session_id
        artifacts = connection.execute(
            """SELECT artifact_id,extracted_text,details_json
               FROM capture_artifact
               WHERE session_id=? AND artifact_type='document_ocr'
                 AND status IN ('completed','quality_warning')
               ORDER BY created_at,artifact_id""",
            (selected_session,),
        ).fetchall()

    monitor.start(selected_session, "isolated-document-replay:monitor")
    revisions: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        signature_payload = json.dumps(
            {
                "artifact_id": artifact["artifact_id"],
                "extracted_text": artifact["extracted_text"],
                "details_json": artifact["details_json"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        signature = hashlib.sha256(signature_payload.encode("utf-8")).hexdigest()
        revisions.append(
            monitor.record_revision(
                selected_session,
                str(artifact["artifact_id"]),
                f"isolated-replay:{signature}",
                f"isolated-document-replay:revision:{index}:{signature[:16]}",
            )
        )

    with database.connect() as connection:
        counts = {
            table: _table_count(connection, table)
            for table in (
                "document_page_revision",
                "observed_question",
                "observed_question_occurrence",
                "observed_answer_evidence",
                "mistake_capture_candidate",
            )
        }
        candidate_statuses = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                """SELECT status,COUNT(*) AS count FROM mistake_capture_candidate
                   GROUP BY status ORDER BY status"""
            ).fetchall()
        }
        raw_media_available = bool(connection.execute(
            """SELECT 1 FROM capture_artifact
               WHERE session_id=? AND artifact_type='document_ocr'
                 AND local_path IS NOT NULL AND TRIM(local_path)<>'' LIMIT 1""",
            (selected_session,),
        ).fetchone())
    after = _source_fingerprint(source)
    report = {
        "format": REPLAY_FORMAT,
        "source_unchanged": before == after,
        "artifacts": len(artifacts),
        "revisions_processed": len(revisions),
        "counts": counts,
        "candidate_statuses": candidate_statuses,
        "raw_media_available": raw_media_available,
        "cloud_requests": 0,
        "contains_child_content": False,
    }
    return ReplayResult(report=report, isolated_database=destination)


def delete_isolated_replay(result: ReplayResult) -> None:
    shutil.rmtree(result.isolated_database.parent, ignore_errors=True)
