#!/usr/bin/env python3
"""Rebind one local video role from a successful on-device capture probe."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rebind(database: Path, probe_path: Path, role: str, backup_dir: Path) -> dict[str, object]:
    probe = json.loads(probe_path.read_text(encoding="utf-8-sig"))
    if probe.get("status") != "captured":
        raise ValueError("PROBE_NOT_CAPTURED")
    device_hash = str(probe.get("systemIdHash", ""))
    label = str(probe.get("deviceLabel", "")).strip()
    if len(device_hash) != 64 or any(char not in "0123456789abcdef" for char in device_hash.lower()):
        raise ValueError("PROBE_IDENTITY_INVALID")
    if not label:
        raise ValueError("PROBE_LABEL_MISSING")
    if role not in {"camera", "document_camera"}:
        raise ValueError("ROLE_INVALID")

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{database.name}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(database, backup)
    now = utc_now()
    capabilities = {
        "system_enumeration_name": label,
        "identity_version": "browser_device_id_v2",
        "width": probe.get("width"),
        "height": probe.get("height"),
        "frame_rate": (probe.get("settings") or {}).get("frameRate"),
        "focus_mode": (probe.get("settings") or {}).get("focusMode"),
        "transport": "browser-media-device",
        "raw_device_id_stored": False,
        "calibration_source": "local_electron_capture_probe_v1",
        "calibrated_at": now,
    }
    with sqlite3.connect(database) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("DATABASE_QUICK_CHECK_FAILED")
        conflict = connection.execute(
            "SELECT device_type FROM device_binding WHERE system_id_hash=? AND device_type<>?",
            (device_hash, role),
        ).fetchone()
        if conflict:
            raise ValueError("DEVICE_ALREADY_BOUND_TO_OTHER_ROLE")
        previous = connection.execute(
            "SELECT system_id_hash,label FROM device_binding WHERE device_type=?", (role,)
        ).fetchone()
        connection.execute(
            """
            INSERT INTO device_binding(device_type,system_id_hash,label,capabilities_json,verified_at,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(device_type) DO UPDATE SET
              system_id_hash=excluded.system_id_hash,label=excluded.label,
              capabilities_json=excluded.capabilities_json,verified_at=excluded.verified_at,
              updated_at=excluded.updated_at
            """,
            (role, device_hash, label, json.dumps(capabilities, ensure_ascii=False), now, now),
        )
        connection.execute(
            "INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                "parent_local_deployment",
                "video_device_binding_calibrated",
                None,
                json.dumps(
                    {
                        "device_type": role,
                        "previous_label": previous[1] if previous else None,
                        "new_label": label,
                        "probe_status": "captured",
                        "probe_width": probe.get("width"),
                        "probe_height": probe.get("height"),
                        "raw_media_stored_in_database": False,
                        "cloud_transmission_allowed": False,
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("DATABASE_QUICK_CHECK_FAILED_AFTER_UPDATE")
    return {"status": "calibrated", "device_type": role, "label": label, "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--role", choices=("camera", "document_camera"), required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--confirm-parent-requested", action="store_true")
    args = parser.parse_args()
    if not args.confirm_parent_requested:
        raise SystemExit("PARENT_CONFIRMATION_REQUIRED")
    print(json.dumps(rebind(args.database, args.probe, args.role, args.backup_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
