"""Exercise the real-corpus P4 graph loop safely on an isolated SQLite snapshot.

The production database is opened read-only and copied through SQLite's backup API.
All diagnostic evidence below is written only to that temporary copy, so the
verification can be repeated without creating fictional student records.
"""
from __future__ import annotations

import argparse
import gc
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
from home_study_agent.knowledge_graph.catalog_bootstrap import CatalogGraphBootstrapper
from home_study_agent.repository import utc_now


TARGET_KEY = "starter:J3.MATH.QUADRATIC.FORMULA"
SOURCE_KIND = "local_keyword_mapper_v3"
SYNTHETIC_SOURCE_KIND = "synthetic_approved_material_v1"


def _snapshot(source: Path, destination: Path) -> None:
    """Create a consistent SQLite copy without copying a potentially live WAL by hand."""
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as read_connection:
        with sqlite3.connect(destination) as write_connection:
            read_connection.backup(write_connection)


def _node_id(database: Database, canonical_key: str) -> str:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT node_id FROM kg_node WHERE canonical_key=?", (canonical_key,)
        ).fetchone()
    if not row:
        raise RuntimeError(f"required graph node is unavailable: {canonical_key}")
    return str(row["node_id"])


def _verification_child(database: Database, child_id: str) -> None:
    """The isolated copy still observes the production invariant: evidence needs a child."""
    with database.connect() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES(?, '图谱验证专用', '初三', '江苏南京', 'active', datetime('now'), datetime('now'))""",
            (child_id,),
        )
        connection.commit()


def _production_evidence_count(app_db: Path) -> int:
    source_uri = f"file:{app_db.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM kg_mastery_evidence").fetchone()[0])


def _seed_synthetic_approved_material(database: Database, node_id: str) -> None:
    """Add only a labelled, disposable approval fixture to a snapshot copy."""
    now = utc_now(); corpus_id = "kg-vertical-synthetic-corpus"
    resource_id = "kg-vertical-synthetic-resource"; segment_id = "kg-vertical-synthetic-segment"
    text = "合成验收材料：用求根公式解一元二次方程，并检查判别式。"
    with database.connect() as connection:
        connection.execute("""INSERT OR IGNORE INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at)
            VALUES(?, '合成纵向验收', 'fixture', 'fixture', NULL, NULL, ?, 'local_fixture', 'approved', 'v1', ?, ?)""", (corpus_id, SYNTHETIC_SOURCE_KIND, now, now))
        connection.execute("""INSERT OR IGNORE INTO kg_resource(resource_id,corpus_id,external_id,title,grade_scope,subject_scope,semester_scope,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,video_fingerprint,metadata_json,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at)
            VALUES(?, ?, 'fixture-j3-formula', '合成：求根公式', '初三', '数学', NULL, 'fixture.txt', NULL, 'fixture-sha', NULL, NULL, '{}', ?, 'local_fixture', 1, 'approved', 'v1', ?, ?)""", (resource_id, corpus_id, SYNTHETIC_SOURCE_KIND, now, now))
        connection.execute("""INSERT OR IGNORE INTO kg_segment(segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,source_ref,confidence,review_status,created_at)
            VALUES(?, ?, 0, 0, ?, ?, 'fixture-segment-sha', ?, 1, 'approved', ?)""", (segment_id, resource_id, len(text), text, SYNTHETIC_SOURCE_KIND, now))
        connection.execute("INSERT OR IGNORE INTO kg_segment_occurrence(resource_id,segment_id,ordinal,start_char,end_char) VALUES(?, ?, 0, 0, ?)", (resource_id, segment_id, len(text)))
        connection.commit()
    database_graph = KnowledgeGraphApplicationService(database).graph
    database_graph.map_segment(resource_id, segment_id, node_id, "explains", confidence=1.0,
                               source_ref=SYNTHETIC_SOURCE_KIND, review_status="approved")
    exercise_id = database_graph.upsert_node(
        canonical_key="fixture:J3.MATH.QUADRATIC.FORMULA.RETEST", node_type="exercise",
        title="合成复测：求根公式", description="仅用于隔离验收，不属于学生题库。",
        scope={"track": "school", "subject": "数学", "grade": "初三"},
        source_ref=SYNTHETIC_SOURCE_KIND, version_label="v1", license_boundary="local_fixture",
        confidence=1.0, review_status="approved",
    )
    database_graph.link(node_id, exercise_id, "assessed_by", rationale="合成验收复测题",
                        source_ref=SYNTHETIC_SOURCE_KIND, version_label="v1",
                        confidence=1.0, review_status="approved")


def _remove_snapshot(path: Path) -> None:
    """Windows may release a closed SQLite WAL handle one GC turn later."""
    gc.collect()
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            gc.collect(); time.sleep(.15 * (attempt + 1))


def verify_vertical_slice(app_db: Path, *, synthetic_fixture: bool = False) -> dict:
    """Verify approved material → diagnosis → retest → mastery → restart on a snapshot."""
    if not app_db.is_file():
        raise FileNotFoundError(app_db)
    production_evidence_before = _production_evidence_count(app_db)
    temporary = Path(tempfile.mkdtemp(prefix="hsc-kg-vertical-")).resolve(strict=True)
    try:
        snapshot_path = temporary / "verification.db"
        _snapshot(app_db, snapshot_path)
        database = Database(snapshot_path)
        application = KnowledgeGraphApplicationService(database)
        application.graph.ensure_ready()
        CatalogGraphBootstrapper(database).run(rebuild_local_candidates=False)
        target_id = _node_id(database, TARGET_KEY)
        mapping_source = SYNTHETIC_SOURCE_KIND if synthetic_fixture else SOURCE_KIND
        if synthetic_fixture:
            _seed_synthetic_approved_material(database, target_id)
        child_id = "kg-verification-not-a-student"
        action_id = "kg-vertical-j3-formula-v1"
        _verification_child(database, child_id)
        before = application.learning_path(child_id, target_id)
        if not before["materials"]:
            raise RuntimeError("J3 target has no automatic local explanation material")
        if not before["diagnosis"]["retest_items"]:
            raise RuntimeError("J3 target has no approved retest item")
        if not any(item["mapping_source_ref"] == mapping_source for item in before["materials"]):
            raise RuntimeError("J3 explanation is not grounded in the required approved mapping")

        after_write = application.record_diagnostic_result(
            child_id=child_id,
            node_id=target_id,
            correctness=0.0,
            independent=True,
            action_id=action_id,
            note="isolated verification; never written to the production database",
        )
        # The same client action is deliberately replayed to validate idempotency.
        after_replay = application.record_diagnostic_result(
            child_id=child_id,
            node_id=target_id,
            correctness=0.0,
            independent=True,
            action_id=action_id,
            note="isolated verification; never written to the production database",
        )
        review_plan = application.review_plan(child_id=child_id, action_id="kg-vertical-p4-review-plan-v1", max_items=3)
        reopened = KnowledgeGraphApplicationService(Database(snapshot_path))
        persisted = reopened.learning_path(child_id, target_id)
        target_state = persisted["mastery"]
        prerequisite_states = [reopened.graph.mastery(child_id, item["node_id"]) for item in before["diagnosis"]["prerequisites"]]
        fingerprint = reopened.graph._child_fingerprint(child_id)
        with Database(snapshot_path).connect() as connection:
            projections = connection.execute(
                """SELECT node_type,COUNT(*) count FROM kg_node
                WHERE json_extract(scope_json,'$.child_fingerprint')=?
                GROUP BY node_type ORDER BY node_type""", (fingerprint,)
            ).fetchall()
            projection_edges = connection.execute(
                "SELECT relation_type,COUNT(*) count FROM kg_edge WHERE relation_type IN ('evidenced_by','generated_from') GROUP BY relation_type"
            ).fetchall()
        projected_counts = {str(row["node_type"]): int(row["count"]) for row in projections}
        projected_edge_counts = {str(row["relation_type"]): int(row["count"]) for row in projection_edges}

        if target_state["evidence_count"] != 1 or after_replay["evidence_count"] != 1:
            raise RuntimeError("diagnostic replay was not idempotent")
        if target_state["mastery_probability"] >= 0.5:
            raise RuntimeError("incorrect diagnostic did not lower target mastery")
        if before["diagnosis"]["prerequisites"] and not any(item["weakness_signal"] > 0 for item in prerequisite_states):
            raise RuntimeError("incorrect diagnostic did not propagate prerequisite evidence")
        if not persisted["materials"] or not persisted["diagnosis"]["retest_items"]:
            raise RuntimeError("persisted learning path lost material or retest linkage")
        if not review_plan["items"] or reopened.latest_review_plan(child_id) != review_plan:
            raise RuntimeError("persisted review plan was not recoverable after restart")
        if any("root" in item for item in persisted["materials"]):
            raise RuntimeError("learning path leaked an absolute media root")
        if projected_counts.get("diagnostic_evidence", 0) != 1 or projected_counts.get("mastery_state", 0) < 2:
            raise RuntimeError("evidence and derived mastery were not projected as graph nodes")
        if not projected_edge_counts.get("evidenced_by") or not projected_edge_counts.get("generated_from"):
            raise RuntimeError("evidence graph edges were not projected")

        production_evidence_after = _production_evidence_count(app_db)
        if production_evidence_after != production_evidence_before:
            raise RuntimeError("production evidence changed during isolated verification")
        return {
            "passed": True,
            "production_database_written": False,
            "production_evidence_rows_before_after": [production_evidence_before, production_evidence_after],
            "snapshot_storage": "temporary_directory_removed_after_verification",
            "source": mapping_source,
            "synthetic_fixture": synthetic_fixture,
            "target_canonical_key": TARGET_KEY,
            "mapping_selection": "synthetic_approved_fixture" if synthetic_fixture else "approved_local_evidence",
            "material_count": len(persisted["materials"]),
            "retest_count": len(persisted["diagnosis"]["retest_items"]),
            "target_mastery": target_state,
            "prerequisite_mastery": prerequisite_states,
            "idempotent_replay_evidence_count": after_replay["evidence_count"],
            "persisted_review_plan_items": len(review_plan["items"]),
            "graph_projection_nodes": projected_counts,
            "graph_projection_edges": projected_edge_counts,
            "next_action": persisted["next_action"],
        }
    finally:
        _remove_snapshot(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--synthetic-fixture", action="store_true", help="Seed only a labelled approved material in the temporary snapshot")
    arguments = parser.parse_args()
    print(json.dumps(verify_vertical_slice(arguments.app_db, synthetic_fixture=arguments.synthetic_fixture), ensure_ascii=False))


if __name__ == "__main__":
    main()
