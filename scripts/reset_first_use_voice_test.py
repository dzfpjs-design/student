#!/usr/bin/env python3
"""Reset only first-use voice state for an explicitly requested family test."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def reset(profile_root: Path, backup_root: Path) -> dict:
    database_path = profile_root / "family-data" / "home-study.db"
    if not database_path.is_file():
        raise FileNotFoundError(f"Family database missing: {database_path}")

    reset_dir = backup_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    reset_dir.mkdir(parents=True, exist_ok=False)
    database_backup = reset_dir / "home-study-before-first-use-reset.db"

    with sqlite3.connect(database_path) as source, sqlite3.connect(database_backup) as target:
        source.backup(target)
        check = target.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"Backup integrity check failed: {check}")

    moved_files = 0
    wake_words = profile_root / "companion-wake-words.json"
    if wake_words.is_file():
        os.replace(wake_words, reset_dir / wake_words.name)
        moved_files += 1
    onboarding = profile_root / "voice-onboarding"
    if onboarding.is_dir():
        destination = reset_dir / onboarding.name
        shutil.move(str(onboarding), str(destination))
        moved_files += sum(1 for path in destination.rglob("*") if path.is_file())

    now = utc_now()
    cancelled_sessions = 0
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT session_id, child_id, state FROM daily_session WHERE status = 'active'"
        ).fetchall()
        for row in active:
            connection.execute(
                """
                UPDATE plan_item SET status = 'paused', active_segment_started_at = NULL
                WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id = ?)
                  AND status = 'active'
                """,
                (row["session_id"],),
            )
            connection.execute(
                """
                UPDATE document_monitor_session SET status = 'paused', updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (now, row["session_id"]),
            )
            connection.execute(
                """
                UPDATE daily_session
                SET status = 'cancelled', state = 'cancelled', ended_at = ?,
                    last_checkpoint_at = ?, observation_status = 'paused'
                WHERE session_id = ?
                """,
                (now, now, row["session_id"]),
            )
            connection.execute(
                """
                INSERT INTO session_transition(
                  transition_id, session_id, from_state, to_state, reason, occurred_at
                ) VALUES (?, ?, ?, 'cancelled', 'parent_requested_first_use_voice_retest', ?)
                """,
                (str(uuid4()), row["session_id"], row["state"], now),
            )
            cancelled_sessions += 1
        connection.execute(
            """
            INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
            VALUES (?, 'parent', 'first_use_voice_test_reset', NULL, ?, ?)
            """,
            (
                str(uuid4()),
                json.dumps(
                    {
                        "cancelled_active_sessions": cancelled_sessions,
                        "archived_voice_state_files": moved_files,
                        "learning_history_deleted": False,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
            ),
        )
        connection.commit()
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"Live database integrity check failed: {check}")

    result = {
        "status": "first_use_voice_test_ready",
        "cancelled_active_sessions": cancelled_sessions,
        "archived_voice_state_files": moved_files,
        "learning_history_deleted": False,
        "backup_dir": str(reset_dir),
        "database_quick_check": "ok",
    }
    (reset_dir / "reset-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--confirm-parent-requested", action="store_true")
    args = parser.parse_args()
    if not args.confirm_parent_requested:
        raise SystemExit("Explicit --confirm-parent-requested is required")
    print(json.dumps(reset(args.profile_root, args.backup_root), ensure_ascii=False))


if __name__ == "__main__":
    main()
