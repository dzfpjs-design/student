"""Acceptance evaluation for the Windows x64 no-discrete-GPU deployment tier."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil


MINIMUM_LATENCY_SAMPLES = 20


@dataclass(frozen=True)
class RuntimeBenchmarkResult:
    observation_minutes: float
    camera_disconnects: int
    app_crashes: int
    asr_latencies_seconds: tuple[float, ...]
    ocr_latencies_seconds: tuple[float, ...]
    steady_cpu_percent: float
    peak_memory_mb: int
    continuous_runtime_minutes: float
    memory_growth_mb: int
    observation_max_width: int
    observation_max_height: int
    observation_max_fps: float
    ocr_capture_mode: str
    behavior_route: str
    asr_route: str
    ocr_route: str
    local_7b_or_larger_started: bool
    continuous_video_vlm_started: bool


def percentile(values: tuple[float, ...], point: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(len(ordered) * point) - 1)]


def assess_minimum_pc(result: RuntimeBenchmarkResult) -> dict:
    """Do not infer this verdict from simulated metrics; target Windows evidence is required."""
    asr_p95 = percentile(result.asr_latencies_seconds, .95)
    ocr_p95 = percentile(result.ocr_latencies_seconds, .95)
    checks = {
        "dual_camera_60_minutes": result.observation_minutes >= 60 and result.camera_disconnects == 0 and result.app_crashes == 0,
        "asr_10_second_p95_le_2_seconds": len(result.asr_latencies_seconds) >= MINIMUM_LATENCY_SAMPLES and asr_p95 is not None and asr_p95 <= 2,
        "ocr_p95_le_5_seconds": len(result.ocr_latencies_seconds) >= MINIMUM_LATENCY_SAMPLES and ocr_p95 is not None and ocr_p95 <= 5,
        "steady_cpu_le_65_percent": result.steady_cpu_percent <= 65,
        "peak_memory_le_12gb": result.peak_memory_mb <= 12 * 1024,
        "two_hour_no_sustained_memory_growth": result.continuous_runtime_minutes >= 120 and result.memory_growth_mb <= 512,
        "behavior_input_cpu_budget": result.observation_max_width <= 640 and result.observation_max_height <= 640 and result.observation_max_fps <= 2 and result.behavior_route == "cpu",
        "ocr_cpu_single_frame_budget": result.ocr_capture_mode == "single_frame" and result.ocr_route == "cpu",
        "asr_cpu_budget": result.asr_route == "cpu",
        "no_forbidden_local_models": not result.local_7b_or_larger_started and not result.continuous_video_vlm_started,
    }
    return {"tier": "windows_x64_cpu_only_minimum", "passed": all(checks.values()), "checks": checks,
            "metrics": {"asr_p95_seconds": asr_p95, "ocr_p95_seconds": ocr_p95, "asr_sample_count": len(result.asr_latencies_seconds), "ocr_sample_count": len(result.ocr_latencies_seconds), "steady_cpu_percent": result.steady_cpu_percent, "peak_memory_mb": result.peak_memory_mb},
            "required_environment": "Windows x64, 4-core/8-thread AVX2 CPU, 16GB RAM, 512GB NVMe; no discrete GPU required"}

