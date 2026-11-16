"""Run a local, non-identifying acceptance check through the observation API.

The cases are synthetic frame patterns plus deterministic detector signals.  They
exercise the production HTTP route and its duration/privacy guards, but are not
a claim about face detection, child identity, attention, emotion, or household
camera accuracy.  Frames are generated in memory and never written to the
database or report.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import cv2
import numpy as np
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.behavior_model import ObservableEvent
from home_study_agent.config import Settings


class _Clock:
    def __init__(self) -> None:
        self.current = datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.current


class _SequencedFaceDetector:
    """Deterministic non-identity signal for state-machine acceptance only."""
    def __init__(self, visible: list[bool]) -> None:
        self.visible = list(visible)

    def detectMultiScale(self, *_args, **_kwargs):
        return ((20, 20, 50, 50),) if self.visible.pop(0) else ()


def _frame(moving: bool = False, dark: bool = False) -> bytes:
    image = np.full((240, 320, 3), 8 if dark else 150, dtype=np.uint8)
    if not dark:
        cv2.line(image, (25, 25), (295, 215), (35, 35, 35), 4)
        if moving:
            cv2.rectangle(image, (180, 95), (300, 165), (80, 80, 80), -1)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise RuntimeError("synthetic_frame_encode_failed")
    return encoded.tobytes()


def _post_frame(client: TestClient, session_id: str, payload: bytes, *, stream_epoch: str, sequence: int, captured_at: str, capture_monotonic_ms: int) -> dict:
    response = client.post(
        f"/v1/sessions/{session_id}/observation-frame",
        json={"frame_base64": base64.b64encode(payload).decode("ascii"),
              "capture_sample": {"stream_epoch": stream_epoch, "sequence": sequence,
                                 "captured_at": captured_at, "capture_monotonic_ms": capture_monotonic_ms}},
    )
    response.raise_for_status()
    return response.json()


def _matrix(records: list[dict]) -> dict[str, dict[str, int]]:
    labels = ("present", "writing_like", "away", "unknown", "paused_like")
    return {
        expected: {predicted: sum(item["expected"] == expected and item["predicted"] == predicted for item in records) for predicted in labels}
        for expected in labels
    }


def run_synthetic_observation_acceptance(data_dir: Path) -> dict:
    """Return only aggregate safe metrics from the real local API route."""
    settings = Settings(
        data_dir=data_dir,
        database_path=data_dir / "observation-eval.db",
        data_encryption_key=b"o" * 32,
        maintenance_enabled=False,
    )
    records: list[dict] = []
    with TestClient(create_app(settings)) as client:
        session_id = client.post("/v1/sessions", json={
            "child_id": "child-primary-4", "client_action_id": "observation-quality-session",
        }).json()["session_id"]
        engine = client.app.state.observation_engine
        clock = _Clock()
        engine._now = clock.now
        base = clock.current.replace(microsecond=0)
        # Dark returns before the detector; the remaining six calls provide a
        # session baseline, movement, temporary absence, sustained absence, and
        # stillness duration gate without any identity-bearing source media.
        engine._face = _SequencedFaceDetector([True, True, False, False, True, True, True, True])
        cases = [
            ("dark_scene", "unknown", 0, _frame(dark=True)),
            ("baseline_present", "present", 1, _frame()),
            ("motion_writing_like", "writing_like", 2, _frame(moving=True)),
            ("absence_before_gate", "unknown", 3, _frame()),
            ("away_after_six_seconds", "away", 10, _frame()),
            ("still_before_gate", "present", 11, _frame()),
            ("paused_after_twenty_seconds", "paused_like", 32, _frame()),
            ("paused_prompt_duration", "paused_like", 93, _frame()),
        ]
        stream_epoch = "public-quality-benchmark-epoch-1"
        client.post(f"/v1/sessions/{session_id}/observation/stream/start", json={
            "stream_epoch": stream_epoch, "client_action_id": "quality-benchmark-stream-1"}).raise_for_status()
        for sequence, (case_id, expected, seconds, payload) in enumerate(cases, 1):
            clock.current = base + timedelta(seconds=seconds)
            started = perf_counter()
            analysis = _post_frame(client, session_id, payload, stream_epoch=stream_epoch, sequence=sequence,
                                   captured_at=clock.current.isoformat(), capture_monotonic_ms=seconds * 1000)
            records.append({
                "case_id": case_id,
                "expected": expected,
                "predicted": analysis["state"],
                "ready": bool(analysis["observation_ready"]),
                "shadow": bool(analysis["shadow"]),
                "intervention_created": bool(analysis.get("intervention")),
                "longitudinal_recorded": bool(analysis["longitudinal_recorded"]),
                "latency_ms": round((perf_counter() - started) * 1000, 1),
            })
        correction = client.post(f"/v1/sessions/{session_id}/observation/correct", json={
            "correction_text": "本地评测纠正", "client_action_id": "observation-quality-correction-1",
        })
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        client.put("/v1/parent/children/child-primary-4/family-rule", headers={"X-HSC-Parent": token}, json={
            "latest_end_time": None, "max_learning_minutes": 90, "task_segment_minutes": 25,
            "break_minutes": 5, "pet_min_minutes": 0, "competition_minutes": 0,
            "observation_mode": "gentle", "reminder_budget_per_10m": 1,
            "misclassification_shadow_threshold": 2,
        }).raise_for_status()
        clock.current = base + timedelta(seconds=154)
        # A mode change invalidates the previous server binding; open a new stream.
        stream_epoch = "public-quality-benchmark-epoch-2"
        client.post(f"/v1/sessions/{session_id}/observation/stream/start", json={
            "stream_epoch": stream_epoch, "client_action_id": "quality-benchmark-stream-2"}).raise_for_status()
        _post_frame(client, session_id, _frame(), stream_epoch=stream_epoch, sequence=1,
                    captured_at=clock.current.isoformat(), capture_monotonic_ms=154000)
        auto_shadow = client.post(f"/v1/sessions/{session_id}/observation/correct", json={
            "correction_text": "第二次本地评测纠正", "client_action_id": "observation-quality-correction-2",
        })
        with client.app.state.repository.database.connect() as db:
            raw_media = db.execute("SELECT COUNT(*) FROM capture_artifact").fetchone()[0]
            state_rows = db.execute("SELECT COUNT(*) FROM child_state_event WHERE session_id = ?", (session_id,)).fetchone()[0]
        replay = client.get("/v1/children/child-primary-4/behavior/replay", headers={"X-HSC-Parent": token}).json()
        # A separate, real session keeps the budget stream outside the corrected
        # child's observation scope without bypassing session ownership checks.
        switched = client.post(f"/v1/sessions/{session_id}/switch-child", json={
            "target_child_id": "child-junior-3", "client_action_id": "observation-quality-policy-session",
        })
        switched.raise_for_status()
        policy_session_id = switched.json()["session_id"]
        # These explicit synthetic instants test one day's cap even if the
        # benchmark itself runs across midnight; they follow session creation.
        policy_now = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        policy_statuses: list[str] = []
        for index, minutes in enumerate((0, 11, 22, 33)):
            at = (policy_now + timedelta(minutes=minutes)).isoformat()
            event_id = f"observation-quality-policy-{index}"
            client.app.state.behavior_model.ingest(ObservableEvent(
                event_id=event_id, child_id="child-junior-3", session_id=policy_session_id, occurred_at=at,
                event_type="work.stall", source="synthetic_evaluation", confidence=.95,
                missing_sources=("app_interaction", "child_self_report"),
                alternative_explanations=("Synthetic policy-budget signal only.",),
            ))
            decision = client.app.state.behavior_model.decide_intervention("child-junior-3", policy_session_id, at)
            policy_statuses.append(str(decision.get("reason") or decision["status"]))
            if decision.get("status") == "suggested":
                client.app.state.behavior_model.record_feedback(
                    decision["intervention_id"], True, at, client_action_id=f"observation-quality-policy-feedback-{index}",
                    child_id="child-junior-3", session_id=policy_session_id,
                )
    errors = [item for item in records if item["expected"] != item["predicted"]]
    unknown = sum(item["predicted"] == "unknown" for item in records)
    interventions = sum(item["intervention_created"] for item in records)
    return {
        "evaluation": "synthetic_observation_api_acceptance_v1",
        "data_boundary": "Synthetic non-identifying frames and deterministic detector signals only; no source frame, embedding, identity, or child media is persisted or reported.",
        "production_entry": "/v1/sessions/{session_id}/observation-frame",
        "results": records,
        "metrics": {
            "confusion_matrix": _matrix(records),
            "unknown_rate": round(unknown / len(records), 4),
            "misclassification_rate": round(len(errors) / len(records), 4),
            "false_intervention_rate": round(interventions / len(records), 4),
            "longitudinal_recorded_count": sum(item["longitudinal_recorded"] for item in records),
            "missing_sources": ["app_interaction", "child_self_report"],
            "latency_p95_ms": sorted(item["latency_ms"] for item in records)[max(0, round((len(records) - 1) * .95))],
            "raw_media_artifact_count": raw_media,
            "persisted_state_event_count": state_rows,
            "reminder_budget_statuses": policy_statuses,
        },
        "safety_checks": {
            "dark_scene_is_unknown": records[0]["predicted"] == "unknown" and not records[0]["ready"],
            "absence_duration_gate": records[3]["predicted"] == "unknown" and records[4]["predicted"] == "away",
            "stillness_duration_gate": records[5]["predicted"] == "present" and records[6]["predicted"] == "paused_like",
            "no_automatic_interventions_in_shadow": interventions == 0,
            "correction_retraction_recorded": correction.status_code == 200 and any(event.get("retracted") for event in replay["events"]),
            "auto_shadow_after_threshold": auto_shadow.status_code == 200 and auto_shadow.json().get("effect") == "auto_shadowed",
            "reminder_daily_cap": policy_statuses == ["suggested", "suggested", "suggested", "daily_frequency_cap"],
            "raw_frames_not_persisted": raw_media == 0,
        },
        "boundary": "This validates rules, duration gates, privacy, correction, and shadow fallback. It is not a camera-model accuracy, identity, attention, emotion, or psychological assessment.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local non-identifying observation quality acceptance.")
    parser.add_argument("--output", type=Path, required=True, help="Aggregate-only JSON report")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="hsc-observation-quality-") as temporary:
        report = run_synthetic_observation_acceptance(Path(temporary))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "metrics": report["metrics"], "safety_checks": report["safety_checks"]}, ensure_ascii=False))
    return 0 if all(report["safety_checks"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
