"""Local-only stratified quality check for production atomic coverage mapping.

The tracked manifest contains IDs and labels only.  This runner creates a
temporary symlink stage, never copies or emits transcript text, and does not
call ``map_segment``: every candidate comes from ``AtomicCoverageMapper``.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
from home_study_agent.knowledge_graph.coverage_mapping import AtomicCoverageMapper
from home_study_agent.knowledge_graph.quality import GraphQualityService
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_graph.schema import KG_SCHEMA_VERSION
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor
from home_study_agent.repository import utc_now

CORPORA = {"seyk": (Path.home() / 'seyk_kb/transcripts'), "bili": (Path.home() / 'bili_edu_kb/transcripts')}
MANIFEST_PATH = Path(__file__).with_name("real_corpus_quality_v2_truth_manifest.json")
LABELS = {"must_map", "ambiguous", "must_not_map"}
REQUIRED = {"file_id", "grade", "subject", "label", "expected_knowledge_point_key", "atom_title"}
TOP_K = 3


def _manifest(path: Path = MANIFEST_PATH) -> list[dict[str, str]]:
    items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(items, list) or len(items) < 18 or any(not isinstance(item, dict) or set(item) != REQUIRED for item in items):
        raise RuntimeError("quality manifest schema is invalid")
    if any(item["label"] not in LABELS or not all(isinstance(item[key], str) and item[key] for key in REQUIRED) for item in items):
        raise RuntimeError("quality manifest has invalid labels or values")
    if len({item["file_id"] for item in items}) != len(items):
        raise RuntimeError("quality manifest file IDs must be unique")
    return items


def _source(file_id: str) -> tuple[str, Path]:
    found = [(name, root / f"{file_id}.txt") for name, root in CORPORA.items() if (root / f"{file_id}.txt").is_file()]
    if len(found) != 1:
        raise RuntimeError(f"quality file ID must resolve once: {file_id}")
    return found[0]


def _short_chinese(title: str) -> bool:
    return len(title) == 1 and "\u4e00" <= title <= "\u9fff"


def _percentile(values: list[int], percentile: int) -> int:
    if not values: return 0
    return values[max(0, (len(values) * percentile + 99) // 100 - 1)]


def run() -> dict:
    if any(not root.is_dir() for root in CORPORA.values()):
        raise RuntimeError("local quality corpus is unavailable")
    items = _manifest(); started = time.monotonic(); tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="hsc-real-kg-quality-") as directory:
        temp = Path(directory); db = Database(temp / "quality.db"); db.initialize()
        stages = {name: temp / name for name in CORPORA}
        for stage in stages.values(): stage.mkdir()
        for item in items:
            corpus, source = _source(item["file_id"]); item["corpus"] = corpus
            destination = stages[corpus] / f"{item['file_id']}.txt"; destination.symlink_to(source)
            item["external_id"] = destination.stem
        for corpus, stage in stages.items():
            if not any(item["corpus"] == corpus for item in items): continue
            spec = CorpusSpec(corpus, f"quality-{corpus}", corpus, stage, None, "real_corpus_quality_v2", "restricted_local_reference")
            ingestor = LocalCorpusIngestor(db); job = ingestor.start(spec, idempotency_key=f"quality-{corpus}-v2")
            if ingestor.run(spec, job)["status"] != "completed": raise RuntimeError(f"{corpus} ingestion did not complete")
        graph = KnowledgeGraphService(db); nodes: dict[str, str] = {}
        for item in items:
            nodes[item["expected_knowledge_point_key"]] = graph.upsert_node(
                canonical_key=item["expected_knowledge_point_key"], node_type="knowledge_point", title=item["atom_title"],
                scope={"grade":item["grade"], "subject":item["subject"]}, source_ref="real_corpus_quality_truth_v2",
                license_boundary="public_standard", review_status="approved")
        with db.connect() as con:
            for item in items:
                con.execute("UPDATE kg_resource SET grade_scope=?,subject_scope=? WHERE corpus_id=? AND external_id=?", (item["grade"], item["subject"], item["corpus"], item["external_id"]))
            con.commit()
        mapper = AtomicCoverageMapper(db); job = mapper.start(idempotency_key="real-corpus-quality-v2"); state = mapper.run(job, max_resources=len(items) + 2)
        if state["status"] != "completed" or state["failed_resources"]: raise RuntimeError("quality mapper did not complete cleanly")
        with db.connect() as con:
            for item in items:
                resource = con.execute("SELECT resource_id FROM kg_resource WHERE corpus_id=? AND external_id=?", (item["corpus"],item["external_id"])).fetchone()["resource_id"]
                rows = con.execute("""SELECT rn.segment_id,n.canonical_key,n.title FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id
                    WHERE rn.resource_id=? AND rn.source_ref=? ORDER BY rn.confidence DESC,rn.segment_id,n.canonical_key""", (resource, mapper.source_ref)).fetchall()
                item["resource_id"] = resource; item["keys"] = [str(row["canonical_key"]) for row in rows]
                item["short_candidates"] = sum(_short_chinese(str(row["title"])) for row in rows)
                item["target_segments"] = [str(row["segment_id"]) for row in rows if row["canonical_key"] == item["expected_knowledge_point_key"]]
            deduplicated_resources = con.execute("""SELECT COUNT(DISTINCT o.resource_id) FROM kg_segment_occurrence o
                JOIN kg_segment s ON s.segment_id=o.segment_id WHERE o.resource_id != s.resource_id""").fetchone()[0]
        if deduplicated_resources < 1:
            raise RuntimeError("quality fixture no longer exercises duplicate-resource occurrences")
        # A parent-review simulation accepts only explicit must-map samples.  No
        # ambiguous or must-not-map mapping is made child-visible.
        with db.connect() as con:
            for child, grade in (("quality-p4", "四年级"), ("quality-j3", "初三")):
                con.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES(?,?,?,'江苏南京','active',?,?)", (child,"质量验收",grade,utc_now(),utc_now()))
            con.commit()
        facade = KnowledgeGraphApplicationService(db); approvals = 0; visible: list[dict] = []; hidden_candidates = 0
        for item in items:
            before = facade.search_materials_for_child("quality-p4" if item["grade"] == "四年级" else "quality-j3", item["atom_title"], subject=item["subject"])
            before_has_resource = any(row["resource_id"] == item["resource_id"] for row in before)
            if item["label"] == "must_map" and item["target_segments"]:
                GraphQualityService(db).record_review(resource_id=item["resource_id"], segment_id=item["target_segments"][0], node_id=nodes[item["expected_knowledge_point_key"]], relation_type="covers", decision="approved", reviewer="quality-audit", client_action_id=f"quality-approve-{item['file_id']}")
                approvals += 1
                after = facade.search_materials_for_child("quality-p4" if item["grade"] == "四年级" else "quality-j3", item["atom_title"], subject=item["subject"])
                if before_has_resource or not any(row["resource_id"] == item["resource_id"] for row in after): raise RuntimeError("approval gate failed")
                visible.append({"file_id":item["file_id"], "grade":item["grade"], "subject":item["subject"]})
            elif before_has_resource:
                raise RuntimeError("unreviewed candidate leaked to child search")
            else:
                hidden_candidates += 1
        reopened = KnowledgeGraphApplicationService(Database(temp / "quality.db"))
        if any(not any(row["resource_id"] == item["resource_id"] for row in reopened.search_materials_for_child("quality-p4" if item["grade"] == "四年级" else "quality-j3", item["atom_title"], subject=item["subject"])) for item in items if item["label"] == "must_map" and item["target_segments"]):
            raise RuntimeError("approved material was not restart durable")
        strict = [item for item in items if item["label"] != "ambiguous"]
        true_positive = sum(item["label"] == "must_map" and item["expected_knowledge_point_key"] in list(dict.fromkeys(item["keys"]))[:TOP_K] for item in strict)
        false_positive = sum(item["label"] == "must_not_map" and item["expected_knowledge_point_key"] in item["keys"] for item in strict)
        false_negative = sum(item["label"] == "must_map" and item["expected_knowledge_point_key"] not in list(dict.fromkeys(item["keys"]))[:TOP_K] for item in strict)
        potential_mis_mappings = [{"file_id":item["file_id"], "grade":item["grade"], "subject":item["subject"], "candidate_keys":sorted(set(item["keys"]))}
                                  for item in items if item["label"] == "must_not_map" and item["keys"]]
        must_map = [item for item in items if item["label"] == "must_map"]
        must_not_map = [item for item in items if item["label"] == "must_not_map"]
        unique_candidate_counts = sorted(len(set(item["keys"])) for item in items)
        non_target = sum(len(set(item["keys"]) - {item["expected_knowledge_point_key"]}) for item in must_map)
        must_map_candidates = sum(len(set(item["keys"])) for item in must_map)
        current, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        compact = [{key:item[key] for key in ("file_id","grade","subject","label","expected_knowledge_point_key")} for item in items]
        return {"status":"passed","local_only":True,"isolated_database":True,"kg_schema_version":KG_SCHEMA_VERSION,
                "license_boundary":"restricted_local_reference; no transcript text emitted","manifest":compact,
                "metrics":{"top_k":TOP_K,"target_pair_precision_at_k":round(true_positive / max(1,true_positive + false_positive),3),"target_pair_recall_at_k":round(true_positive / max(1,true_positive + false_negative),3),"true_positive":true_positive,"false_positive":false_positive,"false_negative":false_negative,"ambiguous_items":sum(item["label"] == "ambiguous" for item in items),"unmapped_must_not_map":sum(not item["keys"] for item in must_not_map),"must_not_map_any_candidate_rate":round(sum(bool(item["keys"]) for item in must_not_map) / max(1,len(must_not_map)),3),"must_map_non_target_candidate_rate":round(non_target / max(1,must_map_candidates),3),"potential_mis_mapping_resources":len(potential_mis_mappings)},
                "potential_mis_mappings":potential_mis_mappings,
                "review_burden":{"candidates":sum(len(item["keys"]) for item in items),"per_resource_unique_candidate_counts":unique_candidate_counts,"unique_candidate_p50":_percentile(unique_candidate_counts,50),"unique_candidate_p95":_percentile(unique_candidate_counts,95),"short_chinese_truth_items":sum(_short_chinese(item["atom_title"]) for item in items),"short_chinese_candidates":sum(item["short_candidates"] for item in items),"approved_for_child_search":approvals,"remaining_unreviewed":sum(len(item["keys"]) for item in items)-approvals},
                "child_search":{"approved_visible":visible,"hidden_before_approval_or_nonreview":hidden_candidates,"restart_recovered":True},
                "mapper":{"source_ref":mapper.source_ref,"processed_resources":state["processed_resources"],"total_resources":state["total_resources"],"failed_resources":state["failed_resources"],"deduplicated_occurrence_resources":deduplicated_resources},
                "elapsed_seconds":round(time.monotonic()-started,3),"python_peak_bytes":peak}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--report", type=Path); args = parser.parse_args(); report = run()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True); args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))
