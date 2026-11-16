from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from home_study_agent.behavior_model import BehaviorEventAdapter
from home_study_agent.evaluation.runtime_benchmark import RuntimeBenchmarkResult, assess_minimum_pc
from home_study_agent.local_ocr import CpuScheduledMediaAdapter, LocalOcrService
from home_study_agent.local_ocr.resource_budget import CpuResourceQueue, ResourceSnapshot


class FakeOcrEngine:
    name = "fake-cpu"
    capability = {"printed_zh"}

    def health(self) -> dict:
        return {"ready": True, "route": "cpu"}

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        assert route == "cpu"
        return [{"text": "1. 本地题目", "confidence": .9, "box": None, "kind": "printed"}]


def png_bytes() -> bytes:
    image = np.full((200, 300, 3), 200, dtype=np.uint8)
    success, encoded = cv2.imencode(".png", image)
    assert success
    return encoded.tobytes()


def test_cpu_budget_rejects_oversized_observation_and_forbidden_local_models() -> None:
    accepted = CpuResourceQueue.validate_behavior_input(640, 640, 2)
    assert accepted == {"accepted": True, "route": "cpu", "fps_cap": 0.5}
    assert CpuResourceQueue.validate_behavior_input(641, 640, .5)["code"] == "behavior_input_budget_exceeded"
    assert CpuResourceQueue.validate_ocr_input(4160, 3120, "single_frame")["accepted"] is True
    assert CpuResourceQueue.validate_ocr_input(5000, 4000, "single_frame")["code"] == "ocr_frame_above_4k_budget"
    assert CpuResourceQueue.validate_ocr_input(1920, 1080, "continuous")["accepted"] is False
    assert CpuResourceQueue.validate_model("local-llm", 7, "text")["accepted"] is False
    assert CpuResourceQueue.validate_model("vlm", None, "continuous_video_vlm")["accepted"] is False
    try:
        BehaviorEventAdapter.from_vision_signal(
            child_id="child", session_id="session", occurred_at="2026-08-28T10:00:00Z",
            signal="writing", confidence=.8, derived_event_id="vision-event-1", frame_width=1920, frame_height=1080, input_fps=2,
        )
    except ValueError as error:
        assert "budget" in str(error)
    else:
        raise AssertionError("oversized vision frame was accepted")


def test_cpu_queue_is_durable_and_degrades_ocr_under_steady_cpu_pressure(tmp_path: Path) -> None:
    queue = CpuResourceQueue(tmp_path / "resources.db")
    ocr = queue.enqueue("ocr", {"image_hash": "not-raw"}, "ocr-1")
    assert queue.next(ResourceSnapshot(cpu_percent=66, memory_mb=1024)) is None
    behavior = queue.enqueue("behavior", task_id="behavior-1")
    next_task = queue.next(ResourceSnapshot(cpu_percent=66, memory_mb=1024))
    assert next_task is not None and next_task["task_id"] == behavior["task_id"]
    assert queue.finish(next_task["task_id"])["status"] == "completed"
    running_ocr = queue.next(ResourceSnapshot(cpu_percent=30, memory_mb=1024))
    assert running_ocr is not None and running_ocr["task_id"] == ocr["task_id"]
    assert running_ocr["budget"]["threads"] == 3
    assert running_ocr["behavior_fps_cap"] == .25
    assert queue.behavior_fps_cap() == .25
    assert CpuResourceQueue(tmp_path / "resources.db").finish("ocr-1", "cancelled")["status"] == "cancelled"
    assert queue.retry("ocr-1")["status"] == "queued"
    claimed_again = queue.next(ResourceSnapshot(cpu_percent=0, memory_mb=0))
    assert claimed_again and claimed_again["task_id"] == "ocr-1"
    assert queue.recover_orphaned(max_running_seconds=0) == 1
    assert queue.retry("ocr-1")["status"] == "queued"  # idempotent pending recovery does not lose work


def test_scheduled_ocr_adapter_keeps_media_out_of_queue_and_runs_cpu(tmp_path: Path) -> None:
    queue = CpuResourceQueue(tmp_path / "queue.db")
    adapter = CpuScheduledMediaAdapter(queue, LocalOcrService(tmp_path / "ocr.db", [FakeOcrEngine()]))
    payload = png_bytes()
    scheduled = adapter.enqueue_ocr(payload, "ocr-action-1")
    assert "image_hash" in scheduled["task"]["payload"]
    assert "image_bytes" not in scheduled["task"]["payload"]
    claim = adapter.next(ResourceSnapshot(cpu_percent=10, memory_mb=1024))
    assert claim and claim["task_type"] == "ocr"
    completed = adapter.run_ocr_claim(claim, payload)
    assert completed["status"] == "completed"
    assert queue.next(ResourceSnapshot(cpu_percent=10, memory_mb=1024)) is None


def test_scheduled_asr_and_behavior_callbacks_release_their_cpu_reservations(tmp_path: Path) -> None:
    queue = CpuResourceQueue(tmp_path / "queue.db")
    adapter = CpuScheduledMediaAdapter(queue, LocalOcrService(tmp_path / "ocr.db", [FakeOcrEngine()]))
    asr = adapter.enqueue_asr("voice-action-1")
    claim = adapter.next(ResourceSnapshot(cpu_percent=10, memory_mb=1024))
    assert claim and claim["task_id"] == asr["task_id"]
    assert adapter.run_asr_claim(claim, lambda: {"transcript": "本地结果"})["transcript"] == "本地结果"
    behavior = adapter.enqueue_behavior("event-1")
    claim = adapter.next(ResourceSnapshot(cpu_percent=10, memory_mb=1024))
    assert claim and claim["task_id"] == behavior["task_id"]
    assert adapter.run_behavior_claim(claim, lambda: {"state": "writing"})["state"] == "writing"


def test_windows_minimum_benchmark_requires_real_thresholds() -> None:
    passing = RuntimeBenchmarkResult(
        observation_minutes=60, camera_disconnects=0, app_crashes=0,
        asr_latencies_seconds=(1.2,) * 20, ocr_latencies_seconds=(4.8,) * 20,
        steady_cpu_percent=65, peak_memory_mb=12 * 1024, continuous_runtime_minutes=120, memory_growth_mb=512,
        observation_max_width=640, observation_max_height=640, observation_max_fps=.5, ocr_capture_mode="single_frame",
        behavior_route="cpu", asr_route="cpu", ocr_route="cpu", local_7b_or_larger_started=False, continuous_video_vlm_started=False,
    )
    assert assess_minimum_pc(passing)["passed"] is True
    failed = RuntimeBenchmarkResult(**{**passing.__dict__, "ocr_latencies_seconds": (5.1,)})
    verdict = assess_minimum_pc(failed)
    assert verdict["passed"] is False
    assert verdict["checks"]["ocr_p95_le_5_seconds"] is False
    insufficient = RuntimeBenchmarkResult(**{**passing.__dict__, "asr_latencies_seconds": (1.0,) * 19})
    assert assess_minimum_pc(insufficient)["checks"]["asr_10_second_p95_le_2_seconds"] is False
