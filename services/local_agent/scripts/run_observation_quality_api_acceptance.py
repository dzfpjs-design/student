#!/usr/bin/env python3
"""Run a local-only, synthetic observation API acceptance record (not Electron UI evidence).

This prepares an isolated agent.db only. It never opens a physical device,
starts Electron, or inspects renderer text. The JSON result contains aggregate
states only and exits non-zero on a failed privacy, recovery, or lease gate.
It is an API preparation check, not visible Electron evidence.
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings


class _AlwaysVisibleFace:
    def detectMultiScale(self, *_args, **_kwargs):
        return ((20, 20, 64, 64),)


def _frame(value: int) -> str:
    image = np.full((240, 320, 3), value, dtype=np.uint8)
    if 25 < value < 245:
        cv2.rectangle(image, (65, 35), (255, 210), (40, 40, 40), 3)
        cv2.line(image, (80, 180), (240, 70), (80, 80, 80), 3)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise RuntimeError("synthetic frame encoding failed")
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def run(data_dir: Path) -> dict:
    settings = Settings(data_dir=data_dir, database_path=data_dir / "agent.db", maintenance_enabled=False)
    checks: dict[str, bool] = {}
    with TestClient(create_app(settings)) as client:
        session = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "api-quality-session-001"}).json()
        session_id = session["session_id"]
        client.app.state.observation_engine._face = _AlwaysVisibleFace()
        start_time = datetime.now(timezone.utc)
        client.app.state.observation_engine._now = iter([start_time, start_time + timedelta(seconds=1)]).__next__
        started = client.post(f"/v1/sessions/{session_id}/observation/qualification/start", json={"client_action_id": "api-quality-start-001"})
        stream_epoch = "public-api-acceptance-epoch"
        opened = client.post(f"/v1/sessions/{session_id}/observation/stream/start", json={
            "stream_epoch": stream_epoch, "client_action_id": "api-quality-stream-start"})
        opened.raise_for_status()
        def capture(sequence):
            return {"stream_epoch": stream_epoch, "sequence": sequence,
                    "captured_at": (start_time + timedelta(seconds=sequence - 1)).isoformat(),
                    "capture_monotonic_ms": (sequence - 1) * 1000}
        dark = client.post(f"/v1/sessions/{session_id}/observation-frame", json={"frame_base64": _frame(8), "capture_sample": capture(1)})
        qualified = client.post(f"/v1/sessions/{session_id}/observation-frame", json={"frame_base64": _frame(150), "capture_sample": capture(2)})
        lease = client.post(f"/v1/sessions/{session_id}/devices/register", json={"device_type": "camera", "system_id_hash": "api-synthetic-camera-0001", "capabilities": {"synthetic": True}})
        paused = client.post(f"/v1/sessions/{session_id}/observation/pause", json={"client_action_id": "api-quality-pause-001"})
        persisted_paused = client.get(f"/v1/sessions/{session_id}/observation/qualification")

    with TestClient(create_app(settings)) as restarted:
        restored_paused = restarted.get(f"/v1/sessions/{session_id}/observation/qualification")
        current = restarted.get(f"/v1/sessions/{session_id}")
        current.raise_for_status()
        known = current.json()["session"]
        resumed = restarted.post(f"/v1/sessions/{session_id}/observation/resume", json={
            "client_action_id": "api-quality-resume-001",
            "expected_revision": known["observation_revision"],
            "expected_generation": known["observation_generation"]})
        requalifying = restarted.get(f"/v1/sessions/{session_id}/observation/qualification")
        token = restarted.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        rule = restarted.put("/v1/parent/children/child-primary-4/family-rule", headers={"X-HSC-Parent": token}, json={"latest_end_time": None, "max_learning_minutes": 90, "task_segment_minutes": 25, "break_minutes": 5, "pet_min_minutes": 10, "competition_minutes": 15, "observation_mode": "gentle", "reminder_budget_per_10m": 1, "misclassification_shadow_threshold": 3})
        # Correcting the same local candidate three times is deliberately a
        # safety test: it must move the current rule to shadow, never diagnose.
        corrections = [restarted.post(f"/v1/sessions/{session_id}/observation/correct", json={"correction_text": "这是合成验收纠正，不作行为结论。", "client_action_id": f"api-quality-correction-{index:03d}"}) for index in range(3)]
        health = restarted.get("/v1/parent/system-health", headers={"X-HSC-Parent": token})

    with sqlite3.connect(settings.database_path) as connection:
        active_leases = connection.execute("SELECT COUNT(*) FROM sensor_lease WHERE status = 'active'").fetchone()[0]
        raw_paths = connection.execute("SELECT COUNT(*) FROM capture_artifact WHERE local_path IS NOT NULL").fetchone()[0]
    primary = next(item for item in health.json()["observation_quality"] if item["child_nickname"] == "四年级同学")
    checks = {
        "start_qualifying": started.status_code == 200 and started.json()["status"] == "qualifying",
        "dark_remains_unqualified": dark.status_code == 200 and not dark.json()["observation_ready"],
        "baseline_qualified": qualified.status_code == 200 and qualified.json()["observation_ready"],
        "pause_persisted": lease.status_code == 200 and paused.status_code == 200 and persisted_paused.json()["status"] == "paused" and restored_paused.json()["status"] == "paused",
        "resume_requalifies": resumed.status_code == 200 and requalifying.json()["status"] == "qualifying",
        "current_shadow_visible": primary["correction_count_7d"] >= 3 and primary["auto_shadowed"] is True and primary["observation_mode"] == "shadow",
        "no_raw_media": raw_paths == 0 and health.json()["raw_media_files_stored"] == 0,
        "leases_released": active_leases == 0,
    }
    return {
        "schema": "observation_api_acceptance_v1",
        "aggregate": {
            "child_surface": ["unqualified_after_dark", "qualified_after_baseline", "paused_after_privacy_pause", "qualifying_after_resume"],
            "parent_surface": {"quality_status": primary["qualification_status"], "current_shadow": primary["auto_shadowed"], "correction_count_7d": primary["correction_count_7d"]},
            "active_leases": active_leases,
            "raw_media_files_stored": health.json()["raw_media_files_stored"],
        },
        "checks": checks,
        "device_boundary": "Synthetic local frames only; L523 dark hardware remains unvalidated.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    temporary = args.data_dir is None
    with tempfile.TemporaryDirectory(prefix="hsc-observation-api-") if temporary else _null_context(args.data_dir) as raw_dir:
        data_dir = Path(raw_dir); data_dir.mkdir(parents=True, exist_ok=True)
        report = run(data_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": all(report["checks"].values())}, ensure_ascii=False))
    return 0 if all(report["checks"].values()) else 2


class _null_context:
    def __init__(self, value: Path): self.value = value
    def __enter__(self): return self.value
    def __exit__(self, *_args): return False


if __name__ == "__main__":
    raise SystemExit(main())
