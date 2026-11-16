"""CPU-only, opt-in benchmark for the family-device voice baseline.

Use only synthetic or separately consented test WAV files. This script never
stores WAV input and reports a failed model load rather than producing made-up
latency. It is intentionally independent of CUDA/NPU availability.
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter, process_time
from uuid import uuid4

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.repository import StudyRepository


def process_memory_bytes() -> int | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters(); counters.cb = ctypes.sizeof(Counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
        return None
    try:
        import resource
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if os.uname().sysname == "Darwin" else value * 1024
    except (ImportError, AttributeError):
        return None


def percentile(values: list[float], point: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * point))], 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure local ASR CPU/RAM latency on the minimum family-device baseline.")
    parser.add_argument("--wav", type=Path, action="append", required=True, help="16-bit mono test WAV; may be supplied more than once")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True, help="Empty isolated benchmark output directory")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1, choices=(1, 2))
    args = parser.parse_args()
    if args.runs < 1 or not all(path.is_file() for path in args.wav):
        parser.error("--runs must be positive and every --wav must exist")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=args.work_dir, database_path=args.work_dir / "benchmark.db", local_model_dir=args.model_dir, maintenance_enabled=False)
    database = Database(settings.database_path); database.initialize()
    repository = StudyRepository(database); repository.seed_family()
    session_id = repository.start_session("child-primary-4", f"cpu-benchmark-{uuid4()}")["session_id"]
    payloads = [path.read_bytes() for path in args.wav] * args.runs
    started_memory = process_memory_bytes(); started_cpu = process_time(); started = perf_counter()

    def transcribe(index: int, wav: bytes) -> dict:
        service = LocalMediaAI(database, args.work_dir, args.model_dir)
        began = perf_counter()
        try:
            result = service.transcribe(session_id, wav, "cpu_benchmark", f"cpu-asr-{index}-{uuid4()}", False)
            return {"ok": True, "latency_ms": round((perf_counter() - began) * 1000, 1), "duration_ms": result["duration_ms"], "transcript_chars": len(result["transcript"])}
        except Exception as error:  # benchmark output must retain the real failure
            return {"ok": False, "latency_ms": round((perf_counter() - began) * 1000, 1), "error": type(error).__name__, "message": str(error)[:180]}

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(transcribe, index, wav) for index, wav in enumerate(payloads)]
        measurements = [future.result() for future in as_completed(futures)]
    latencies = [item["latency_ms"] for item in measurements if item["ok"]]
    report = {
        "benchmark": "hsc_voice_cpu_v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "cpu_only": True,
        "os": os.name,
        "cpu_count": os.cpu_count(),
        "concurrency": args.concurrency,
        "runs": len(payloads),
        "successes": len(latencies),
        "failures": len(payloads) - len(latencies),
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95), "max": max(latencies) if latencies else None},
        "process_cpu_seconds": round(process_time() - started_cpu, 3),
        "wall_seconds": round(perf_counter() - started, 3),
        "working_set_bytes_before": started_memory,
        "working_set_bytes_after": process_memory_bytes(),
        "measurements": measurements,
        "raw_audio_persisted": False,
    }
    output = args.work_dir / "voice-cpu-benchmark.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0 if report["failures"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
