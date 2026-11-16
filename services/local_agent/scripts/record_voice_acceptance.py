"""Create a privacy-safe, repeatable local voice acceptance record.

The script never opens a microphone, reads credentials, or writes media.  It
records automated gates and optional Electron observations supplied as a small
non-sensitive JSON document.  Natural-voice and child-speech quality remain
explicit human acceptance items until new approved credentials and consented
evaluation material exist.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
AUTOMATED_GATES = {
    "renderer_device_enumeration_silence_gate_and_leases": [
        "npm", "test", "--", "--run", "LocalInputs", "mediaDevices", "voiceAdaptation", "VoiceCalibrationPanel", "voiceProfile", "companionVoice", "voiceCalibrationPending",
    ],
    "backend_asr_cancel_adapter_and_raw_audio": [
        sys.executable, "-m", "pytest",
        "services/local_agent/tests/test_app.py", "services/local_agent/tests/test_behavior_ocr_app_integration.py",
        "services/local_agent/tests/test_voice_calibration.py", "-q", "-k", "asr or voice_calibration or main_asr_flow",
    ],
    "tts_bounds_and_minimum_cpu_boundary": [
        sys.executable, "-m", "pytest", "services/local_agent/tests/test_voice_synthesis.py",
        "services/local_agent/tests/test_cpu_runtime_budget.py", "services/local_agent/tests/test_minimum_pc_environment.py", "-q",
    ],
}
REQUIRED_ELECTRON_OBSERVATIONS = {
    "device_enumerated", "microphone_opened", "silence_gated", "microphone_cancelled", "lease_released", "restart_recovered", "tts_fallback_label",
}
REQUIRED_ELECTRON_CHILD_RECOVERY_CHECKS = {
    "runtime", "parent_save", "parent_ui_delete_recalibration", "child_read_only_recovery", "bound_microphone", "stop_release", "restart_recovery",
}


def _run_gate(command: list[str], dry_run: bool) -> dict:
    if dry_run:
        return {"status": "not_run", "command": command}
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    # Keep the durable record useful without embedding a long test log.
    tail = (completed.stdout + completed.stderr).strip().splitlines()[-3:]
    return {"status": "passed" if completed.returncode == 0 else "failed", "exit_code": completed.returncode, "command": command, "summary_tail": tail}


def _electron_record(path: Path | None) -> dict:
    if path is None:
        return {"status": "external_required", "missing": sorted(REQUIRED_ELECTRON_OBSERVATIONS)}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Electron evidence must be a JSON object.")
    unknown = set(payload) - REQUIRED_ELECTRON_OBSERVATIONS
    if unknown:
        raise ValueError(f"Electron evidence has unsupported fields: {', '.join(sorted(unknown))}")
    missing = sorted(key for key in REQUIRED_ELECTRON_OBSERVATIONS if payload.get(key) is not True and not (key == "tts_fallback_label" and isinstance(payload.get(key), str)))
    return {"status": "passed" if not missing else "incomplete", "observations": payload, "missing": missing}


def _valid_child_recovery_check(name: str, value: object) -> bool:
    if not isinstance(value, dict): return False
    if name == "runtime": return value.get("ready") is True and value.get("local_api_loopback") is True
    if name == "parent_save": return value.get("via_real_http") is True and value.get("raw_audio_stored") is False and value.get("visible_parent_ui") is False
    if name == "parent_ui_delete_recalibration": return value.get("visible_delete") is True and value.get("visible_recalibration_save") is True and value.get("raw_audio_stored") is False
    if name in {"child_read_only_recovery", "restart_recovery"}: return value.get("renderer_visible", value.get("visible")) is True and value.get("service_profile_hydrated", value.get("sqlite_profile_reused")) is True
    if name == "bound_microphone": return value.get("manual_and_hands_free") is True and isinstance(value.get("device_id"), str) and bool(value["device_id"].strip())
    if name == "stop_release": return value.get("active_leases") == 0 and isinstance(value.get("released_leases"), int) and value["released_leases"] >= 2
    return False


def _electron_child_recovery_record(path: Path | None) -> dict:
    if path is None:
        return {"status": "external_required", "missing": sorted(REQUIRED_ELECTRON_CHILD_RECOVERY_CHECKS)}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("record_type") != "electron_voice_child_recovery_e2e_v1":
        raise ValueError("Electron child-recovery evidence must be an electron_voice_child_recovery_e2e_v1 record.")
    if payload.get("synthetic_profile") is not True or payload.get("raw_audio_stored") is not False or payload.get("natural_voice_tested") is not False:
        raise ValueError("Electron onboarding evidence must state synthetic profile, no raw audio, and no natural-voice claim.")
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        raise ValueError("Electron onboarding evidence checks are missing.")
    missing = sorted(name for name in REQUIRED_ELECTRON_CHILD_RECOVERY_CHECKS if not _valid_child_recovery_check(name, checks.get(name)))
    return {"status": "passed" if not missing else "incomplete", "checks": {name: checks[name] for name in REQUIRED_ELECTRON_CHILD_RECOVERY_CHECKS if name in checks}, "missing": missing}


def build_record(*, dry_run: bool, electron_evidence: Path | None, electron_child_recovery_evidence: Path | None = None) -> dict:
    gates = {name: _run_gate(command, dry_run) for name, command in AUTOMATED_GATES.items()}
    return {
        "record_type": "local_voice_acceptance_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "microphone_audio_recorded_by_script": False,
            "credentials_read_or_printed": False,
            "note": "Only test status, non-sensitive device lifecycle observations, and aggregate CPU evidence are recorded.",
        },
        "automated_gates": gates,
        "electron": _electron_record(electron_evidence),
        "electron_voice_child_recovery": _electron_child_recovery_record(electron_child_recovery_evidence),
        "minimum_pc": {
            "status": "external_required",
            "command": "node scripts/run-python.mjs services/local_agent/scripts/collect_minimum_pc_environment.py --runtime-metrics <aggregate.json> --output <windows-evidence.json> && node scripts/run-python.mjs services/local_agent/scripts/evaluate_minimum_pc_runtime.py <windows-evidence.json> --require-collected-environment",
            "boundary": "Windows x64, at least 8 logical CPUs with AVX2, 16GB RAM, 512GB NVMe; no discrete GPU requirement. This host must not be used as Windows evidence.",
        },
        "external_acceptance": [
            "Use consented representative child speech to measure ASR quality; do not retain original audio in this record.",
            "Provide a newly approved natural-TTS credential and complete parent/child listening evaluation. System voices and text-only remain explicitly non-natural fallbacks.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="Local JSON record path; do not place it in a shared credential directory")
    parser.add_argument("--electron-evidence", type=Path, help="Optional non-sensitive Electron lifecycle JSON")
    parser.add_argument("--electron-child-recovery-evidence", type=Path, help="Optional partial child-recovery record emitted by scripts/electron-voice-onboarding-e2e.mjs")
    parser.add_argument("--dry-run", action="store_true", help="Write the command plan without running tests")
    args = parser.parse_args()
    try:
        record = build_record(dry_run=args.dry_run, electron_evidence=args.electron_evidence, electron_child_recovery_evidence=args.electron_child_recovery_evidence)
    except (ValueError, json.JSONDecodeError) as error:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"record_type": "local_voice_acceptance_v1", "electron_voice_child_recovery": {"status": "invalid", "error": str(error)}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output), "electron_voice_child_recovery": "invalid"}, ensure_ascii=False))
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed = [name for name, gate in record["automated_gates"].items() if gate["status"] == "failed"]
    child_recovery_failed = args.electron_child_recovery_evidence is not None and record["electron_voice_child_recovery"]["status"] != "passed"
    print(json.dumps({"output": str(args.output), "failed_gates": failed, "electron": record["electron"]["status"], "electron_voice_child_recovery": record["electron_voice_child_recovery"]["status"]}, ensure_ascii=False))
    return 2 if failed or child_recovery_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
