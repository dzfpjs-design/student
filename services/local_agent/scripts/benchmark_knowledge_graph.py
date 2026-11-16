"""Repeatable CPU-only throughput benchmark for the minimum-PC acceptance gate."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
from time import perf_counter

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.runtime_budget import MinimumRuntimeBudget
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_ingestion import CorpusSpec, LocalCorpusIngestor
from home_study_agent.retrieval import HashingVectorizer


def _peak_rss_bytes() -> int | None:
    """Use only the standard library, including on stock Windows x64 Python."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                           ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                           ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                           ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                           ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                           ("PrivateUsage", ctypes.c_size_t)]
            counters = PROCESS_MEMORY_COUNTERS_EX(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX))
            process = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
        except (AttributeError, OSError):
            return None
        return None
    try:
        import resource
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # macOS reports bytes; Linux/BSD conventionally reports KiB.
        return value if platform.system() == "Darwin" else value * 1024
    except ImportError:
        return None


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * .95 + .999999) - 1))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", type=int, default=100)
    parser.add_argument("--chars-per-resource", type=int, default=8_000)
    parser.add_argument("--vector-dimensions", type=int, default=64, choices=(64, 96, 128))
    parser.add_argument("--queries", type=int, default=20)
    arguments = parser.parse_args(); budget = MinimumRuntimeBudget(vector_dimensions=arguments.vector_dimensions)
    if arguments.resources < 1 or arguments.chars_per_resource < 1 or arguments.queries < 1:
        raise SystemExit("resources, chars-per-resource and queries must be positive")
    with TemporaryDirectory() as location:
        root = Path(location); transcripts = root / "transcripts"; transcripts.mkdir()
        text = ("二次函数判别式与一元二次方程求根公式。通过代入检查结果。" * ((arguments.chars_per_resource // 28) + 1))[:arguments.chars_per_resource]
        for index in range(arguments.resources):
            (transcripts / f"fixture-{index:04d}.txt").write_text(text + str(index), encoding="utf-8")
        database = Database(root / "benchmark.db"); database.initialize()
        spec = CorpusSpec("benchmark", "CPU benchmark", "seyk", transcripts, None, "fixture", "local_fixture")
        start = perf_counter(); importer = LocalCorpusIngestor(database, HashingVectorizer(arguments.vector_dimensions))
        job = importer.start(spec, idempotency_key="benchmark-v1"); result = importer.run(spec, job); ingest_seconds = perf_counter() - start
        graph = KnowledgeGraphService(database); latencies = []
        for _ in range(arguments.queries):
            start = perf_counter(); hits = graph.search_explanations("二次函数判别式求根公式", limit=8)
            latencies.append((perf_counter()-start)*1000)
        with database.connect() as connection:
            segments = connection.execute("SELECT COUNT(*) FROM kg_segment").fetchone()[0]
            bytes_on_disk = (root / "benchmark.db").stat().st_size
        query_p95_ms = _p95(latencies)
        peak_rss = _peak_rss_bytes()
        print(json.dumps({"platform": platform.platform(), "cpu_count": os.cpu_count(), "resources": result["processed_count"], "segments": segments, "ingest_seconds": round(ingest_seconds, 3), "resources_per_second": round(result["processed_count"] / max(ingest_seconds, .001), 2), "queries": arguments.queries, "query_p50_ms": round(sorted(latencies)[len(latencies)//2], 2), "query_p95_ms": round(query_p95_ms, 2), "hits": len(hits), "db_bytes": bytes_on_disk, "raw_vector_bytes_estimate": budget.estimate_vector_bytes(segments, arguments.vector_dimensions), "peak_rss_bytes": peak_rss, "acceptance": {"query_p95_under_500ms": query_p95_ms < 500, "cpu_only": True, "vector_dimensions": arguments.vector_dimensions}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
