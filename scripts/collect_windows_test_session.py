from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BEHAVIOR_TABLES = (
    "audit_event",
    "voice_input",
    "agent_turn",
    "agent_request_receipt",
    "agent_execution_receipt",
    "capture_artifact",
    "mistake_case",
    "mistake_capture_candidate",
    "model_call",
    "observable_event",
    "intervention",
    "intervention_record",
    "document_monitor_session",
    "document_monitor_event",
    "document_page_revision",
    "document_monitor_request",
    "document_learning_analysis",
    "document_vision_request",
    "document_operation_receipt",
    "document_operation_delivery",
    "ocr_job",
    "ocr_review_item",
    "ocr_review_event",
    "ocr_review_batch",
    "companion_memory",
    "companion_memory_evidence",
    "companion_memory_audit",
    "companion_activity",
    "companion_activity_outcome",
    "companion_activity_outcome_event",
    "learning_session_closure",
)

SETTINGS_FILES = (
    "model-settings.json",
    "voice-settings.json",
    "asr-settings.json",
    "qwen-realtime-settings.json",
    "companion-wake-words.json",
)

INSTALLED_RESOURCE_ARTIFACTS = (
    "resources/app.asar",
    "resources/local-agent/home-study-local-agent.exe",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_inventory(paths: list[Path], root: Path | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        display = str(path.relative_to(root)) if root and path.is_relative_to(root) else path.name
        result.append({"path": display.replace("\\", "/"), "bytes": path.stat().st_size, "sha256": sha256(path)})
    return result


def settings_paths(profile_root: Path) -> list[Path]:
    paths = [profile_root / name for name in SETTINGS_FILES]
    onboarding_root = profile_root / "voice-onboarding"
    if onboarding_root.is_dir():
        paths.extend(sorted(path for path in onboarding_root.rglob("*") if path.is_file()))
    return paths


def installed_artifact_paths(install_root: Path | None) -> list[Path]:
    if not install_root:
        return []
    executables = sorted(
        path
        for path in install_root.glob("*.exe")
        if path.is_file() and not path.name.lower().startswith("uninstall")
    )
    resources = [install_root / name for name in INSTALLED_RESOURCE_ARTIFACTS]
    return executables + resources


def copy_settings_snapshot(profile_root: Path, destination: Path) -> list[dict[str, Any]]:
    copied: list[Path] = []
    for source in settings_paths(profile_root):
        if not source.is_file():
            continue
        relative = source.relative_to(profile_root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    return file_inventory(copied, destination)


def connect_readonly(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=30)


def database_summary(database: Path) -> dict[str, Any]:
    if not database.is_file():
        raise FileNotFoundError(f"Family database not found: {database}")
    with closing(connect_readonly(database)) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA quick_check")]
        existing = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        counts: dict[str, int] = {}
        for table in BEHAVIOR_TABLES:
            if table in existing:
                quoted = table.replace('"', '""')
                counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
    return {
        "quick_check": integrity,
        "behavior_row_counts": counts,
        "database_bytes": database.stat().st_size,
    }


def backup_database(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    with closing(connect_readonly(source)) as source_connection, closing(sqlite3.connect(temporary)) as target_connection:
        source_connection.backup(target_connection, pages=256)
        target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target_connection.commit()
    os.replace(temporary, destination)
    summary = database_summary(destination)
    summary.update({"sha256": sha256(destination), "snapshot_path": f"private/{destination.name}"})
    return summary


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def summarize_runtime_logs(log_files: list[Path], started_at: str) -> dict[str, Any]:
    lower_bound = parse_timestamp(started_at)
    events: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    http_statuses: Counter[str] = Counter()
    voice_modes: Counter[str] = Counter()
    invalid_lines = 0
    total_lines = 0
    for path in log_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                invalid_lines += 1
                continue
            timestamp = parse_timestamp(record.get("timestamp"))
            if lower_bound and timestamp and timestamp < lower_bound:
                continue
            total_lines += 1
            for key, counter in (("event", events), ("source", sources), ("level", levels), ("error_code", errors)):
                value = record.get(key)
                if isinstance(value, str):
                    counter[value] += 1
            details = record.get("details")
            if isinstance(details, dict):
                status_code = details.get("statusCode")
                if isinstance(status_code, int):
                    http_statuses[str(status_code)] += 1
                mode = details.get("mode")
                if record.get("event", "").startswith("voice.") and isinstance(mode, str):
                    voice_modes[mode] += 1
    return {
        "events_since_start": total_lines,
        "event_counts": dict(sorted(events.items())),
        "source_counts": dict(sorted(sources.items())),
        "level_counts": dict(sorted(levels.items())),
        "error_counts": dict(sorted(errors.items())),
        "http_status_counts": dict(sorted(http_statuses.items())),
        "voice_mode_counts": dict(sorted(voice_modes.items())),
        "invalid_json_lines": invalid_lines,
    }


def row_deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        table: after.get(table, 0) - before.get(table, 0)
        for table in sorted(set(before) | set(after))
        if after.get(table, 0) - before.get(table, 0) != 0
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def diagnostic_sources(profile_root: Path) -> list[Path]:
    return [
        source for source in (
            profile_root / "diagnostics",
            profile_root / "family-data" / "diagnostics",
        ) if source.is_dir()
    ]


def diagnostic_log_files(profile_root: Path) -> list[Path]:
    files: list[Path] = []
    for source in diagnostic_sources(profile_root):
        log_root = source / "logs"
        if log_root.is_dir():
            files.extend(log_root.glob("runtime.jsonl*"))
    return sorted(set(files))


def copy_diagnostics(profile_root: Path, private_root: Path) -> list[Path]:
    target = private_root / "diagnostics"
    for source in diagnostic_sources(profile_root):
        shutil.copytree(source, target, dirs_exist_ok=True)
    return sorted((target / "logs").glob("runtime.jsonl*")) if (target / "logs").is_dir() else []


def session_start(profile_root: Path, output_root: Path, install_root: Path | None) -> dict[str, Any]:
    database = profile_root / "family-data" / "home-study.db"
    started_at = utc_now()
    session_id = str(uuid.uuid4())
    session_dir = output_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{session_id[:8]}"
    session_dir.mkdir(parents=True, exist_ok=False)
    baseline = database_summary(database)
    settings = file_inventory(settings_paths(profile_root), profile_root)
    settings_snapshot = copy_settings_snapshot(profile_root, session_dir / "private" / "settings-before")
    installed = file_inventory(installed_artifact_paths(install_root), install_root)
    state = {
        "schema_version": 1,
        "session_id": session_id,
        "status": "recording",
        "started_at": started_at,
        "session_dir": str(session_dir),
        "profile_root": str(profile_root),
        "database_path": str(database),
        "baseline": baseline,
        "settings_inventory": settings,
        "settings_snapshot": settings_snapshot,
        "installed_artifacts": installed,
        "privacy": {
            "local_only": True,
            "automatic_upload": False,
            "raw_media_copied": False,
            "database_snapshot_contains_private_family_data": True,
            "settings_snapshot_contains_private_configuration": True,
        },
    }
    write_json(session_dir / "session-state.json", state)
    write_json(output_root / "active-test-session.json", state)
    return {
        "status": "recording",
        "session_id": session_id,
        "started_at": started_at,
        "session_dir": str(session_dir),
    }


def session_stop(profile_root: Path, output_root: Path, install_root: Path | None, session_id: str | None) -> dict[str, Any]:
    active_path = output_root / "active-test-session.json"
    if not active_path.is_file():
        raise RuntimeError("No active Windows test session was found.")
    state = json.loads(active_path.read_text(encoding="utf-8"))
    if session_id and state.get("session_id") != session_id:
        raise RuntimeError("Requested session id does not match the active test session.")
    session_dir = Path(state["session_dir"])
    if state.get("status") != "recording" or not session_dir.is_dir():
        raise RuntimeError("Active test session state is invalid or already closed.")
    database = profile_root / "family-data" / "home-study.db"
    private_root = session_dir / "private"
    snapshot_path = private_root / "home-study.db"
    if snapshot_path.exists():
        snapshot_path = private_root / f"home-study-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    snapshot = backup_database(database, snapshot_path)
    log_files = copy_diagnostics(profile_root, private_root)
    stopped_at = utc_now()
    before_counts = state["baseline"]["behavior_row_counts"]
    after_counts = snapshot["behavior_row_counts"]
    installed = file_inventory(installed_artifact_paths(install_root), install_root)
    manifest = {
        "schema_version": 1,
        "session_id": state["session_id"],
        "status": "captured_local_private",
        "started_at": state["started_at"],
        "stopped_at": stopped_at,
        "database": snapshot,
        "behavior_row_deltas": row_deltas(before_counts, after_counts),
        "runtime_diagnostics": summarize_runtime_logs(log_files, state["started_at"]),
        "settings_inventory_before": state.get("settings_inventory", []),
        "settings_inventory_after": file_inventory(settings_paths(profile_root), profile_root),
        "installed_artifacts_before": state.get("installed_artifacts", []),
        "installed_artifacts_after": installed,
        "private_files": file_inventory([path for path in private_root.rglob("*") if path.is_file()], session_dir),
        "privacy": {
            "local_only": True,
            "automatic_upload": False,
            "contains_private_family_database": True,
            "contains_raw_audio_or_video": False,
            "share_only_after_parent_review": True,
        },
    }
    write_json(session_dir / "manifest.json", manifest)
    state.update({"status": "captured_local_private", "stopped_at": stopped_at})
    write_json(session_dir / "session-state.json", state)
    active_path.unlink(missing_ok=True)
    archive_base = session_dir.parent / f"{session_dir.name}-PRIVATE"
    archive_path = Path(shutil.make_archive(str(archive_base), "zip", root_dir=session_dir))
    result = {
        "status": "captured_local_private",
        "session_id": state["session_id"],
        "session_dir": str(session_dir),
        "archive_path": str(archive_path),
        "archive_sha256": sha256(archive_path),
        "database_quick_check": snapshot["quick_check"],
        "behavior_row_deltas": manifest["behavior_row_deltas"],
        "runtime_events_since_start": manifest["runtime_diagnostics"]["events_since_start"],
        "automatic_upload": False,
    }
    write_json(session_dir / "collection-result.json", result)
    return result


def session_status(profile_root: Path, output_root: Path, session_id: str | None) -> dict[str, Any]:
    active_path = output_root / "active-test-session.json"
    if not active_path.is_file():
        raise RuntimeError("No active Windows test session was found.")
    state = json.loads(active_path.read_text(encoding="utf-8"))
    if session_id and state.get("session_id") != session_id:
        raise RuntimeError("Requested session id does not match the active test session.")
    if state.get("status") != "recording" or not Path(state["session_dir"]).is_dir():
        raise RuntimeError("Active test session state is invalid or already closed.")
    current = database_summary(profile_root / "family-data" / "home-study.db")
    return {
        "status": "recording",
        "session_id": state["session_id"],
        "started_at": state["started_at"],
        "checked_at": utc_now(),
        "database_quick_check": current["quick_check"],
        "behavior_row_deltas": row_deltas(
            state["baseline"]["behavior_row_counts"], current["behavior_row_counts"]
        ),
        "runtime_diagnostics": summarize_runtime_logs(
            diagnostic_log_files(profile_root), state["started_at"]
        ),
        "privacy": {
            "local_only": True,
            "automatic_upload": False,
            "raw_media_copied": False,
            "private_snapshot_created": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a local, private Windows test session.")
    parser.add_argument("--action", choices=("start", "status", "stop"), required=True)
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--session-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.output_root.mkdir(parents=True, exist_ok=True)
    try:
        if args.action == "start":
            result = session_start(args.profile_root, args.output_root, args.install_root)
        elif args.action == "status":
            result = session_status(args.profile_root, args.output_root, args.session_id)
        else:
            result = session_stop(args.profile_root, args.output_root, args.install_root, args.session_id)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
