"""Merge Windows-collected host evidence with local runtime timings for the acceptance evaluator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.evaluation.minimum_pc_environment import collect_environment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-metrics", type=Path, required=True, help="Local aggregate timing JSON; no media")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-avx2", action="store_true", help="Use only after confirming the CPU model's AVX2 support when cpuinfo is unavailable")
    args = parser.parse_args()
    metrics = json.loads(args.runtime_metrics.read_text(encoding="utf-8"))
    payload = {**metrics, **collect_environment(confirm_avx2=args.confirm_avx2)}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "platform": payload["platform"], "avx2_evidence": payload["avx2_evidence"], "nvme_gb": payload["nvme_gb"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
