"""Evaluate a recorded Windows minimum-PC benchmark without uploading media."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from home_study_agent.evaluation.runtime_benchmark import RuntimeBenchmarkResult, assess_minimum_pc
from home_study_agent.evaluation.minimum_pc_environment import has_collected_provenance


REQUIRED_EVIDENCE = {"platform", "architecture", "logical_cpus", "avx2", "memory_gb", "nvme_gb", "discrete_gpu_required"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("metrics", type=Path, help="Local JSON with aggregate timings and system metrics only")
    parser.add_argument("--output", type=Path, help="Optional local verdict JSON path")
    parser.add_argument("--require-collected-environment", action="store_true", help="Require output from collect_minimum_pc_environment.py")
    args = parser.parse_args()
    payload = json.loads(args.metrics.read_text(encoding="utf-8"))
    missing = REQUIRED_EVIDENCE - payload.keys()
    if missing:
        raise SystemExit(f"Missing target-machine evidence: {', '.join(sorted(missing))}")
    if payload["platform"] != "Windows" or payload["architecture"] != "x64" or payload["logical_cpus"] < 8 or not payload["avx2"] or payload["memory_gb"] < 16 or payload["nvme_gb"] < 512:
        raise SystemExit("Machine is outside the supported minimum Windows x64 CPU-only tier")
    if payload["discrete_gpu_required"]:
        raise SystemExit("Invalid benchmark: the supported tier must not require a discrete GPU")
    if args.require_collected_environment and not has_collected_provenance(payload):
        raise SystemExit("Missing valid Windows collector provenance; run collect_minimum_pc_environment.py on the target machine")
    metrics = {key: payload[key] for key in RuntimeBenchmarkResult.__dataclass_fields__}
    verdict = assess_minimum_pc(RuntimeBenchmarkResult(**metrics))
    verdict["evidence"] = {key: payload[key] for key in REQUIRED_EVIDENCE}
    serialized = json.dumps(verdict, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    return 0 if verdict["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
