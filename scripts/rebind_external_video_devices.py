from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from home_study_agent.database import Database
from home_study_agent.device_registry import DeviceRegistry


def _one(items: list[dict], label_prefix: str) -> dict:
    matches = [item for item in items if item.get("label", "").lower().startswith(label_prefix.lower())]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {label_prefix} device, found {len(matches)}")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--inspection", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()

    inspection = json.loads(args.inspection.read_text(encoding="utf-8"))
    if inspection.get("raw_media_saved") is not False or inspection.get("media_uploaded") is not False:
        raise RuntimeError("inspection privacy boundary is not explicit")
    observation = _one(inspection["devices"], "L523")
    document = _one(inspection["devices"], "Usb HDCamera")
    if observation["system_id_hash"] == document["system_id_hash"]:
        raise RuntimeError("external device identities must differ")

    database = Database(args.database)
    registry = DeviceRegistry(database)
    before = registry.bindings()
    by_role = {item["device_type"]: item for item in before}
    current_camera = by_role.get("camera")
    current_document = by_role.get("document_camera")
    if not current_camera or "integrated" not in current_camera["label"].lower():
        raise RuntimeError("refusing migration because the current observation binding is not integrated")
    if not current_document or not current_document["label"].lower().startswith("usb hdcamera"):
        raise RuntimeError("refusing migration because the current document binding is unexpected")

    backup_dir = args.database.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"pre-external-camera-rebind-{stamp}.db"
    with sqlite3.connect(args.database) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")

    verified_at = datetime.now(timezone.utc).isoformat()
    bindings = [
        {
            "device_type": "camera",
            "system_id_hash": observation["system_id_hash"],
            "label": observation["label"],
            "verified_at": verified_at,
            "capabilities": {
                "system_enumeration_name": observation["label"],
                "identity_version": "browser_device_id_v2",
                "transport": "browser-media-device",
                "external_only": True,
                "role_verified": "observation_camera",
                "hardware_probe": "directshow_dual_concurrency_passed",
                "raw_device_id_stored": False,
            },
        },
        {
            "device_type": "document_camera",
            "system_id_hash": document["system_id_hash"],
            "label": document["label"],
            "verified_at": verified_at,
            "capabilities": {
                **current_document["capabilities"],
                "system_enumeration_name": document["label"],
                "identity_version": "browser_device_id_v2",
                "external_only": True,
                "role_verified": "document_capture",
                "hardware_probe": "directshow_dual_concurrency_passed",
                "raw_device_id_stored": False,
            },
        },
    ]
    after = registry.save_video_bindings(bindings, f"external-video-role-correction:{uuid4()}")
    result = {
        "status": "passed",
        "before": [{"device_type": item["device_type"], "label": item["label"]} for item in before],
        "after": [{"device_type": item["device_type"], "label": item["label"]} for item in after],
        "backup": str(backup_path),
        "raw_media_saved": False,
        "media_uploaded": False,
    }
    args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
