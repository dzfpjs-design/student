from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "verify_real_corpus_mapping_quality.py"
    spec = importlib.util.spec_from_file_location("real_corpus_quality", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_quality_manifest_is_stratified_and_contains_only_fixed_labels() -> None:
    script = _module(); items = script._manifest()
    assert script.MANIFEST_PATH.name == "real_corpus_quality_v2_truth_manifest.json"
    assert {item["grade"] for item in items} == {"四年级", "初三"}
    assert {item["subject"] for item in items} == {"数学", "英语", "语文"}
    assert {item["label"] for item in items} == script.LABELS
    assert all(set(item) == script.REQUIRED for item in items)


def test_quality_runner_uses_production_mapper_not_direct_segment_mapping() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "verify_real_corpus_mapping_quality.py").read_text(encoding="utf-8")
    assert "AtomicCoverageMapper(db)" in source
    assert ".map_segment(" not in source
    assert "target_pair_precision_at_k" in source
    assert "must_map_non_target_candidate_rate" in source
    assert "must_not_map_any_candidate_rate" in source
    assert "unique_candidate_p50" in source and "unique_candidate_p95" in source
