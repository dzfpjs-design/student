import json
import sqlite3
from pathlib import Path

import pytest

from rebind_video_device_from_probe import rebind


def database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        CREATE TABLE device_binding(device_type TEXT PRIMARY KEY,system_id_hash TEXT UNIQUE,label TEXT,capabilities_json TEXT,verified_at TEXT,updated_at TEXT);
        CREATE TABLE audit_event(audit_id TEXT PRIMARY KEY,actor TEXT,action TEXT,child_id TEXT,summary_json TEXT,occurred_at TEXT);
        INSERT INTO device_binding VALUES('camera','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','old camera','{}','old','old');
        INSERT INTO device_binding VALUES('document_camera','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','document','{}','old','old');
        """)


def test_rebinds_from_captured_probe_with_backup_and_audit(tmp_path: Path) -> None:
    db = tmp_path / "home-study.db"
    database(db)
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"status": "captured", "deviceLabel": "Integrated Camera", "systemIdHash": "c" * 64,
                                 "width": 1280, "height": 720, "settings": {"frameRate": 30}}), encoding="utf-8")
    result = rebind(db, probe, "camera", tmp_path / "backups")
    assert result["status"] == "calibrated"
    assert Path(result["backup"]).exists()
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT label,system_id_hash FROM device_binding WHERE device_type='camera'").fetchone() == ("Integrated Camera", "c" * 64)
        action, summary = connection.execute("SELECT action,summary_json FROM audit_event").fetchone()
        assert action == "video_device_binding_calibrated"
        assert json.loads(summary)["raw_media_stored_in_database"] is False


def test_rejects_failed_probe_and_cross_role_identity(tmp_path: Path) -> None:
    db = tmp_path / "home-study.db"
    database(db)
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="PROBE_NOT_CAPTURED"):
        rebind(db, probe, "camera", tmp_path / "backups")
    probe.write_text(json.dumps({"status": "captured", "deviceLabel": "wrong", "systemIdHash": "b" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="DEVICE_ALREADY_BOUND_TO_OTHER_ROLE"):
        rebind(db, probe, "camera", tmp_path / "backups")
