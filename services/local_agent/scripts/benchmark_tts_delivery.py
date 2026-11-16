"""Measure the deployed TTS delivery path without handling provider credentials.

The Electron main process is the only component allowed to decrypt and inject
the parent-approved voice configuration into the local agent.  This client
therefore exercises the already-running local HTTP service, using an active
test session and one generic sentence.  It never accepts an API key, never
stores PCM, and never writes the benchmark sentence into the report.

Use this only after a parent has enabled cloud text delivery and selected an
approved provider in the app's protected voice settings.  A 503 or 4xx result
is evidence of a blocked deployment, not a passing synthetic benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4

import httpx


BENCHMARK_TEXT = "现在开始今天的学习计划。"


def process_memory_bytes() -> int | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters(); counters.cb = ctypes.sizeof(Counters)
        if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
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


async def one_round(client: httpx.AsyncClient, base_url: str, session_id: str, cancel_after_first_frame: bool) -> dict:
    started = perf_counter()
    first_frame_ms: float | None = None
    pcm_bytes = 0
    status_code: int | None = None
    try:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/v1/sessions/{session_id}/voice/synthesize/stream",
            json={"text": BENCHMARK_TEXT, "client_action_id": f"tts-benchmark-{uuid4()}"},
            headers={"Content-Type": "application/json"},
        ) as response:
            status_code = response.status_code
            if response.status_code >= 400:
                body = await response.aread()
                code = None
                try:
                    code = json.loads(body).get("code")
                except (ValueError, AttributeError):
                    pass
                return {"status": "failed", "http_status": status_code, "error_code": code or "HTTP_ERROR", "first_frame_ms": None, "total_ms": round((perf_counter() - started) * 1000, 1), "pcm_bytes": 0}
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                pcm_bytes += len(chunk)
                if first_frame_ms is None:
                    first_frame_ms = round((perf_counter() - started) * 1000, 1)
                    if cancel_after_first_frame:
                        return {"status": "cancelled_by_client", "http_status": status_code, "error_code": None, "first_frame_ms": first_frame_ms, "total_ms": round((perf_counter() - started) * 1000, 1), "pcm_bytes": pcm_bytes}
        return {"status": "completed", "http_status": status_code, "error_code": None, "first_frame_ms": first_frame_ms, "total_ms": round((perf_counter() - started) * 1000, 1), "pcm_bytes": pcm_bytes}
    except httpx.HTTPError as error:
        return {"status": "failed", "http_status": status_code, "error_code": type(error).__name__, "first_frame_ms": first_frame_ms, "total_ms": round((perf_counter() - started) * 1000, 1), "pcm_bytes": pcm_bytes}


async def run(args: argparse.Namespace) -> dict:
    memory_before = process_memory_bytes()
    timeout = httpx.Timeout(connect=args.timeout_seconds, read=args.timeout_seconds, write=args.timeout_seconds, pool=args.timeout_seconds)
    # The target is a local loopback service.  Inheriting a household or CI
    # HTTP proxy can route 127.0.0.1 to a proxy and turn a valid local failure
    # into an unrelated 502, so benchmark the direct connection only.
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        rounds: list[dict] = []
        for index in range(args.runs):
            # Closing the response context immediately after the first frame is
            # the same cancellation boundary used by AbortController in the
            # renderer.  The following normal round proves recovery.
            cancel = args.cancel_every > 0 and (index + 1) % args.cancel_every == 0
            rounds.append(await one_round(client, args.base_url, args.session_id, cancel))
    completed = [item for item in rounds if item["status"] == "completed"]
    cancelled = [item for item in rounds if item["status"] == "cancelled_by_client"]
    failures = [item for item in rounds if item["status"] == "failed"]
    first_frames = [item["first_frame_ms"] for item in completed if item["first_frame_ms"] is not None]
    totals = [item["total_ms"] for item in completed]
    recovery_after_cancel = all(rounds[index + 1]["status"] == "completed" for index, item in enumerate(rounds[:-1]) if item["status"] == "cancelled_by_client")
    return {
        "benchmark": "hsc_tts_delivery_v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "target": {"base_url": args.base_url.rstrip("/"), "session_id": "not_recorded"},
        "runs": args.runs,
        "cancel_every": args.cancel_every,
        "generic_text_chars": len(BENCHMARK_TEXT),
        "completed": len(completed),
        "cancelled_by_client": len(cancelled),
        "failed": len(failures),
        "recovery_after_cancel": recovery_after_cancel,
        "first_frame_ms": {"p50": percentile(first_frames, 0.5), "p95": percentile(first_frames, 0.95)},
        "total_ms": {"p50": percentile(totals, 0.5), "p95": percentile(totals, 0.95)},
        "working_set_bytes_before": memory_before,
        "working_set_bytes_after": process_memory_bytes(),
        "pcm_persisted": False,
        "credential_handling": "none; credentials remain in Electron safeStorage and the local-agent child process",
        "rounds": rounds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real local-service TTS benchmark without accepting a cloud credential.")
    parser.add_argument("--base-url", required=True, help="Loopback local-agent base URL, e.g. http://127.0.0.1:8765")
    parser.add_argument("--session-id", required=True, help="Active parent-approved test session ID")
    parser.add_argument("--output", type=Path, required=True, help="Explicit JSON report path; report contains no text or PCM")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--cancel-every", type=int, default=5, help="Close one response after first PCM frame every N rounds; use 0 to disable")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    args = parser.parse_args()
    if args.runs < 1 or args.cancel_every < 0 or args.timeout_seconds <= 0:
        parser.error("runs and timeout must be positive; cancel-every must be non-negative")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run(args))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    # Completed 30-round natural-voice validation must include cancellations
    # and no failed normal rounds.  A failed setup exits nonzero honestly.
    return 0 if report["failed"] == 0 and report["completed"] + report["cancelled_by_client"] == args.runs else 2


if __name__ == "__main__":
    raise SystemExit(main())
