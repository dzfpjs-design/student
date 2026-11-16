#!/usr/bin/env python3
"""Verify the companion context path against the real local databases."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from time import perf_counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/home_study_agent/knowledge_graph/atomic_companion.py"
SPEC = importlib.util.spec_from_file_location("atomic_companion_verifier", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
AtomicGraphCompanionService = MODULE.AtomicGraphCompanionService


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, default=(Path.home() / '.home-study-companion/home-study.db'))
    parser.add_argument("--build-db", type=Path, default=(Path.home() / '.home-study-companion/knowledge-build/atomic-full-v6.db'))
    args = parser.parse_args()
    before = file_sha256(args.build_db)
    service = AtomicGraphCompanionService(args.app_db, args.build_db)
    samples = [
        ("child-primary-4", "小数的意义和计算", "数学"),
        ("child-junior-3", "压强与受力面积的关系", "物理"),
    ]
    evidence = []
    for child_id, query, subject in samples:
        started = perf_counter()
        context = service.context_for_child(child_id=child_id, query=query, subject=subject, limit=4)
        elapsed_ms = round((perf_counter() - started) * 1000, 1)
        evidence.append({
            "scope": context["scope"],
            "node_count": len(context["nodes"]),
            "matched_node_count": sum(node["graph_role"] == "matched" for node in context["nodes"]),
            "relation_count": len(context["relations"]),
            "titles": [node["title"] for node in context["nodes"][:4]],
            "policies": sorted({node["usage_policy"]["mode"] for node in context["nodes"]}),
            "elapsed_ms": elapsed_ms,
            "audit_event_id": context["audit_event_id"],
        })
    after = file_sha256(args.build_db)
    if before != after:
        raise RuntimeError("原子构建数据库发生变化，违反只读约束。")
    # Reopen to prove events survive service restart.
    restarted = AtomicGraphCompanionService(args.app_db, args.build_db)
    print(json.dumps({
        "build_unchanged": True,
        "samples": evidence,
        "persisted_usage": restarted.usage_summary(),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
