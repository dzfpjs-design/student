"""Run a small, local-only real-transcript KG acceptance slice.

The script resolves a fixed independent truth manifest against each local corpus.
It creates symlinks in a temporary directory (never copies video/text), uses an
isolated SQLite DB, and writes only IDs and aggregate metrics.
No transcript excerpt or child data is emitted.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.host_diagnostic_bridge import HostDiagnosticKnowledgeGraphBridge
from home_study_agent.knowledge_graph.coverage_mapping import AtomicCoverageMapper
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
from home_study_agent.knowledge_graph.quality import GraphQualityService
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_graph.schema import KG_SCHEMA_VERSION
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import utc_now


CORPORA = {
    "seyk": (Path.home() / 'seyk_kb/transcripts'),
    "bili": (Path.home() / 'bili_edu_kb/transcripts'),
}
CATEGORY_SPECS = {
    "j3_math": {"grade": "初三", "subject": "数学", "query": "一元二次方程", "atom_title": "一元二次方程"},
    "j3_english": {"grade": "初三", "subject": "英语", "query": "阅读理解", "atom_title": "阅读理解"},
    "p4_math": {"grade": "四年级", "subject": "数学", "query": "万位", "atom_title": "万位"},
    "p4_english": {"grade": "四年级", "subject": "英语", "query": "apple", "atom_title": "apple"},
}
TRUTH_MANIFEST_PATH = Path(__file__).with_name("real_corpus_slice_truth_v1.json")
DISTRACTORS = {
    "j3_math": ("truth:j3-math-inverse-function", "反比例函数"),
    "j3_english": ("truth:j3-english-relative-clause", "定语从句"),
    "p4_math": ("truth:p4-math-triangle-area", "三角形面积"),
    "p4_english": ("truth:p4-english-banana", "banana"),
}
SHORT_CHINESE_COMPARISON_ATOMS = {
    "p4_math": ("truth:p4-math-ten-thousand-short", "万"),
}
TOP_K = 3


def _load_truth_manifest(path: Path = TRUTH_MANIFEST_PATH) -> list[dict[str, str]]:
    truth = json.loads(path.read_text(encoding="utf-8"))
    required = {"file_id", "category", "expected_knowledge_point_key"}
    if not isinstance(truth, list) or not truth or any(not isinstance(item, dict) or set(item) != required for item in truth):
        raise RuntimeError("truth manifest must contain only file_id, category, expected_knowledge_point_key")
    if len({item["file_id"] for item in truth}) != len(truth) or any(item["category"] not in CATEGORY_SPECS for item in truth):
        raise RuntimeError("truth manifest has duplicate file IDs or unknown categories")
    if any(not all(isinstance(item[field], str) and item[field].strip() for field in required) for item in truth):
        raise RuntimeError("truth manifest contains an invalid value")
    return truth


def _source_for_truth(file_id: str, roots: dict[str, Path] = CORPORA) -> tuple[str, Path]:
    matches = [(corpus_name, root / f"{file_id}.txt") for corpus_name, root in roots.items() if (root / f"{file_id}.txt").is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"truth file ID must resolve to exactly one local corpus: {file_id}")
    return matches[0]


def _is_single_chinese_atom(title: str) -> bool:
    return len(title) == 1 and "\u4e00" <= title <= "\u9fff"


def run() -> dict:
    if any(not root.is_dir() for root in CORPORA.values()):
        missing = [name for name, root in CORPORA.items() if not root.is_dir()]
        raise RuntimeError(f"missing local-only corpus roots: {missing}")
    started = time.monotonic(); tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="hsc-real-kg-audit-") as temporary:
        temp = Path(temporary); database = Database(temp / "isolated.db"); database.initialize()
        truth = _load_truth_manifest()
        stages = {corpus_name: temp / corpus_name for corpus_name in CORPORA}
        for stage in stages.values():
            stage.mkdir()
        selected: list[dict] = []
        for item in truth:
            corpus_name, source = _source_for_truth(item["file_id"])
            spec = CATEGORY_SPECS[item["category"]]
            destination = stages[corpus_name] / f"{item['category']}--{source.name}"
            destination.symlink_to(source)
            selected.append({"corpus": corpus_name, "category": item["category"], "grade": spec["grade"],
                             "subject": spec["subject"], "external_id": destination.stem,
                             "source_file_id": item["file_id"], "query": spec["query"],
                             "expected_knowledge_point_key": item["expected_knowledge_point_key"]})
        for corpus_name, root in CORPORA.items():
            if not any(item["corpus"] == corpus_name for item in selected):
                continue
            stage = stages[corpus_name]
            spec = CorpusSpec(corpus_name, f"real-local-{corpus_name}", corpus_name, stage, None,
                              "local_real_corpus_acceptance_v1", "restricted_local_reference")
            ingestor = LocalCorpusIngestor(database)
            job = ingestor.start(spec, idempotency_key=f"real-{corpus_name}-v1")
            result = ingestor.run(spec, job)
            if result["status"] != "completed":
                raise RuntimeError(f"{corpus_name} ingest did not complete: {result['status']}")

        graph = KnowledgeGraphService(database)
        nodes: dict[str, str] = {}
        for category, spec in CATEGORY_SPECS.items():
            expected_keys = {item["expected_knowledge_point_key"] for item in truth if item["category"] == category}
            if len(expected_keys) != 1:
                raise RuntimeError(f"truth manifest needs one expected knowledge point per category: {category}")
            expected_key = expected_keys.pop()
            # These are explicit independent truth-catalog atoms, not approved
            # transcript mappings.  Every resource association below is created
            # exclusively by AtomicCoverageMapper as unreviewed evidence.
            nodes[category] = graph.upsert_node(canonical_key=expected_key, node_type="knowledge_point", title=spec["atom_title"],
                scope={"grade": spec["grade"], "subject": spec["subject"]}, source_ref="real_corpus_slice_truth_catalog_v1",
                license_boundary="public_standard", review_status="approved")
            distractor_key, distractor_title = DISTRACTORS[category]
            graph.upsert_node(canonical_key=distractor_key, node_type="knowledge_point", title=distractor_title,
                scope={"grade": spec["grade"], "subject": spec["subject"]}, source_ref="real_corpus_slice_truth_catalog_v1",
                license_boundary="public_standard", review_status="unreviewed")
            if category in SHORT_CHINESE_COMPARISON_ATOMS:
                short_key, short_title = SHORT_CHINESE_COMPARISON_ATOMS[category]
                graph.upsert_node(canonical_key=short_key, node_type="knowledge_point", title=short_title,
                    scope={"grade": spec["grade"], "subject": spec["subject"]}, source_ref="real_corpus_slice_truth_catalog_v1",
                    license_boundary="public_standard", review_status="unreviewed")
        with database.connect() as connection:
            for item in selected:
                connection.execute("UPDATE kg_resource SET grade_scope=?,subject_scope=? WHERE external_id=?", (item["grade"], item["subject"], item["external_id"]))
            connection.commit()
        mapper = AtomicCoverageMapper(database)
        mapping_job = mapper.start(idempotency_key="real-corpus-atomic-coverage-v1")
        mapping_state = mapper.run(mapping_job, max_resources=25)
        if mapping_state["status"] != "completed":
            raise RuntimeError(f"production candidate mapper did not complete: {mapping_state['status']}")
        with database.connect() as connection:
            for item in selected:
                row = connection.execute("SELECT resource_id FROM kg_resource WHERE external_id=?", (item["external_id"],)).fetchone()
                item["resource_id"] = row["resource_id"]
                mappings = connection.execute("""SELECT rn.segment_id,n.canonical_key,n.title FROM kg_resource_node rn
                    JOIN kg_node n ON n.node_id=rn.node_id WHERE rn.resource_id=? AND rn.source_ref=?
                    ORDER BY rn.confidence DESC,rn.segment_id,n.canonical_key""", (row["resource_id"], mapper.source_ref)).fetchall()
                item["candidate_segment_ids"] = sorted({str(mapping["segment_id"]) for mapping in mappings})
                item["candidate_keys"] = [str(mapping["canonical_key"]) for mapping in mappings]
                item["short_chinese_candidate_mappings"] = sum(_is_single_chinese_atom(str(mapping["title"])) for mapping in mappings)

        # A child's grade is server-owned.  Before parent review no candidate is
        # returned; approving exactly one real J3 mapping makes only that local
        # material visible.  The other seven remain unreviewed by design.
        with database.connect() as connection:
            connection.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('audit-j3','验收','初三','江苏南京','active',?,?)", (utc_now(), utc_now()))
            connection.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('audit-study','audit-j3','active','study',?,?,?)", (utc_now(), utc_now(), "real-audit-session"))
            connection.commit()
        j3 = next(item for item in selected if item["corpus"] == "seyk" and item["category"] == "j3_math")
        if j3["expected_knowledge_point_key"] not in j3["candidate_keys"]:
            raise RuntimeError("production mapper missed the selected J3 truth atom")
        facade = KnowledgeGraphApplicationService(database)
        before = facade.search_materials_for_child("audit-j3", j3["query"], subject="数学")
        with database.connect() as connection:
            expected_segment = connection.execute("""SELECT rn.segment_id FROM kg_resource_node rn
                WHERE rn.resource_id=? AND rn.node_id=? AND rn.relation_type='covers' AND rn.source_ref=?
                ORDER BY rn.confidence DESC,rn.segment_id LIMIT 1""", (j3["resource_id"], nodes["j3_math"], mapper.source_ref)).fetchone()["segment_id"]
        GraphQualityService(database).record_review(resource_id=j3["resource_id"], segment_id=expected_segment, node_id=nodes["j3_math"],
                                                     relation_type="covers", decision="approved", reviewer="real-local-audit",
                                                     client_action_id="real-local-audit-approve-j3")
        after = facade.search_materials_for_child("audit-j3", j3["query"], subject="数学")
        if before or not after:
            raise RuntimeError("parent approval gate failed for real local sample")

        # Host-owned deterministic diagnostic → host mistake case → one KG
        # evidence → restart replay.  The prompt/answer stay synthetic and no
        # student data leaves the isolated database.
        learning = LearningStateService(database); learning.seed_catalog()
        exercise = graph.upsert_node(canonical_key="diagnostic:diag-j3-discriminant-1", node_type="exercise", title="host diagnostic",
                                     scope={"grade":"初三","subject":"数学"}, source_ref="product_evidence:real-audit", review_status="approved")
        graph.link(nodes["j3_math"], exercise, "assessed_by", rationale="host scored real-corpus slice", source_ref="product_evidence:real-audit", review_status="approved")
        attempt = learning.submit_diagnostic("audit-study", "diag-j3-discriminant-1", "四个根", "real-audit-host-attempt")
        mistake = MistakeBookService(database).capture_from_attempt(attempt)
        bridge = HostDiagnosticKnowledgeGraphBridge(database)
        state = bridge.record_attempt(attempt["attempt_id"], mistake_id=mistake["mistake_id"])
        replay = HostDiagnosticKnowledgeGraphBridge(Database(temp / "isolated.db")).record_attempt(attempt["attempt_id"], mistake_id=mistake["mistake_id"])
        if state["status"] != "recorded" or state["mastery"]["evidence_count"] != 1 or replay["mastery"]["evidence_count"] != 1:
            raise RuntimeError("host evidence was not single-counted and restart-safe")
        current, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        top_k_hits = sum(item["expected_knowledge_point_key"] in list(dict.fromkeys(item["candidate_keys"]))[:TOP_K] for item in selected)
        unmapped = [item["source_file_id"] for item in selected if not item["candidate_keys"]]
        unexpected_candidates = [{"file_id": item["source_file_id"], "category": item["category"], "candidate_keys": sorted({key for key in item["candidate_keys"] if key != item["expected_knowledge_point_key"]})} for item in selected if any(key != item["expected_knowledge_point_key"] for key in item["candidate_keys"])]
        return {
            "status": "passed", "local_only": True, "isolated_database": True, "kg_schema_version": KG_SCHEMA_VERSION,
            "license_boundary": "restricted_local_reference; no transcript text emitted",
            "sample_count": len(selected), "truth_manifest": truth,
            "candidate_mappings": sum(len(item["candidate_keys"]) for item in selected), "approved_mappings": 1,
            "unreviewed_mappings": sum(len(item["candidate_keys"]) for item in selected) - 1,
            "review_queue_burden": {"unreviewed_candidate_mappings": sum(len(item["candidate_keys"]) for item in selected) - 1,
                                    "short_chinese_atom_candidate_mappings": sum(item["short_chinese_candidate_mappings"] for item in selected),
                                    "short_chinese_atom_candidate_resources": sum(item["short_chinese_candidate_mappings"] > 0 for item in selected),
                                    "policy": "all automatic coverage mappings remain unreviewed; short Chinese atoms never bypass parent review"},
            "production_mapper": {"source_ref": mapper.source_ref, "mapping_job_status": mapping_state["status"],
                                  "processed_resources": mapping_state["processed_resources"], "total_resources": mapping_state["total_resources"],
                                  "failed_resources": mapping_state["failed_resources"],
                                  "top_k": TOP_K, "top_k_hit_count": top_k_hits, "top_k_hit_rate": round(top_k_hits / len(selected), 3),
                                  "unmapped_file_ids": unmapped, "potential_mis_mappings": unexpected_candidates},
            "approval_gate": {"before_hits": len(before), "after_hits": len(after)},
            "host_mistake_evidence": {"evidence_count_after": state["mastery"]["evidence_count"], "replay_count": replay["mastery"]["evidence_count"], "mistake_recorded": state["mistake_recorded"]},
            "elapsed_seconds": round(time.monotonic() - started, 3), "python_peak_bytes": peak,
            "quality_proxy": {"expected_truth_items": len(selected), "top_k_hits": top_k_hits, "unmapped": len(unmapped),
                              "potential_mis_mappings": len(unexpected_candidates), "approved_by_human": 1,
                              "note": "exact-title top-k plus four scope-matched comparison atoms; it is not corpus recall or a human precision estimate."},
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--report", type=Path); parser.add_argument("--truth-output", type=Path)
    arguments = parser.parse_args(); report = run()
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if arguments.truth_output:
        arguments.truth_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.truth_output.write_text(json.dumps(report["truth_manifest"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
