from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "verify_real_corpus_vertical_slice.py"
    spec = importlib.util.spec_from_file_location("real_corpus_slice", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_real_corpus_truth_manifest_is_minimal_and_resolves_exactly_one_local_file(tmp_path: Path) -> None:
    script = _module()
    truth = script._load_truth_manifest()
    assert len(truth) == 8
    assert all(set(item) == {"file_id", "category", "expected_knowledge_point_key"} for item in truth)
    seyk = tmp_path / "seyk"; bili = tmp_path / "bili"; seyk.mkdir(); bili.mkdir()
    (seyk / "one.txt").write_text("not inspected", encoding="utf-8")
    assert script._source_for_truth("one", {"seyk": seyk, "bili": bili}) == ("seyk", seyk / "one.txt")
    (bili / "one.txt").write_text("not inspected", encoding="utf-8")
    try:
        script._source_for_truth("one", {"seyk": seyk, "bili": bili})
        assert False, "a truth file ID must not be ambiguous across corpora"
    except RuntimeError as error:
        assert "exactly one" in str(error)


def test_real_corpus_slice_uses_production_mapper_not_direct_segment_mapping() -> None:
    path = Path(__file__).parents[1] / "scripts" / "verify_real_corpus_vertical_slice.py"
    source = path.read_text(encoding="utf-8")
    assert "AtomicCoverageMapper(database)" in source
    assert ".map_segment(" not in source
