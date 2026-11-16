from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def test_voice_acceptance_record_dry_run_is_private_and_lists_external_gates(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    output = tmp_path / "voice-acceptance.json"
    completed = subprocess.run(
        [sys.executable, "services/local_agent/scripts/record_voice_acceptance.py", "--output", str(output), "--dry-run"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    record = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 0
    assert record["privacy"]["microphone_audio_recorded_by_script"] is False
    assert record["privacy"]["credentials_read_or_printed"] is False
    assert record["electron"]["status"] == "external_required"
    assert record["electron_voice_child_recovery"]["status"] == "external_required"
    assert "natural-TTS credential" in record["external_acceptance"][1]
    assert all(gate["status"] == "not_run" for gate in record["automated_gates"].values())


def test_voice_acceptance_record_accepts_only_truthful_synthetic_electron_child_recovery_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    evidence = tmp_path / "electron-onboarding.json"
    evidence.write_text(json.dumps({
        "record_type": "electron_voice_child_recovery_e2e_v1", "synthetic_profile": True,
        "raw_audio_stored": False, "natural_voice_tested": False,
        "checks": {
            "runtime": {"ready": True, "local_api_loopback": True},
            "parent_save": {"via_real_http": True, "raw_audio_stored": False, "visible_parent_ui": False},
            "parent_ui_delete_recalibration": {"visible_delete": True, "visible_recalibration_save": True, "raw_audio_stored": False},
            "child_read_only_recovery": {"visible": True, "service_profile_hydrated": True},
            "bound_microphone": {"manual_and_hands_free": True, "device_id": "synthetic-bound-mic"},
            "stop_release": {"active_leases": 0, "released_leases": 2},
            "restart_recovery": {"renderer_visible": True, "sqlite_profile_reused": True},
        },
    }), encoding="utf-8")
    output = tmp_path / "voice-acceptance.json"
    completed = subprocess.run(
        [sys.executable, "services/local_agent/scripts/record_voice_acceptance.py", "--output", str(output), "--dry-run", "--electron-child-recovery-evidence", str(evidence)],
        cwd=root, text=True, capture_output=True, check=False,
    )
    record = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 0
    assert record["electron_voice_child_recovery"]["status"] == "passed"
    assert record["electron_voice_child_recovery"]["checks"]["bound_microphone"]["device_id"] == "synthetic-bound-mic"


def test_voice_acceptance_record_rejects_false_or_incomplete_child_recovery_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    evidence = tmp_path / "incomplete-electron.json"
    evidence.write_text(json.dumps({
        "record_type": "electron_voice_child_recovery_e2e_v1", "synthetic_profile": True,
        "raw_audio_stored": False, "natural_voice_tested": False,
        "checks": {
            "runtime": {"ready": False, "local_api_loopback": True},
            "parent_ui_delete_recalibration": {"visible_delete": True, "visible_recalibration_save": False, "raw_audio_stored": False},
        },
    }), encoding="utf-8")
    output = tmp_path / "voice-acceptance.json"
    completed = subprocess.run(
        [sys.executable, "services/local_agent/scripts/record_voice_acceptance.py", "--output", str(output), "--dry-run", "--electron-child-recovery-evidence", str(evidence)],
        cwd=root, text=True, capture_output=True, check=False,
    )
    record = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 2
    assert record["electron_voice_child_recovery"]["status"] == "incomplete"
    assert "runtime" in record["electron_voice_child_recovery"]["missing"]
    assert "parent_ui_delete_recalibration" in record["electron_voice_child_recovery"]["missing"]
