"""Report atomic-knowledge quality without treating candidate count as accuracy."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.atomic_model_pipeline import DISCOURSE_NOISE, GENERIC_COMPONENT, UMBRELLA_COMPONENTS
from home_study_agent.knowledge_graph.schema import ensure_schema
from home_study_agent.repository import utc_now


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    database = Database(arguments.app_db); database.initialize()
    with database.connect() as migration:
        ensure_schema(migration, utc_now()); migration.commit()
    connection = sqlite3.connect(arguments.app_db); connection.row_factory = sqlite3.Row
    drafts = [dict(row) for row in connection.execute("SELECT * FROM kg_atomic_definition_draft")]
    counts = {
        "drafts": len(drafts),
        "approved_drafts": sum(item["review_status"] == "approved" for item in drafts),
        "resources_with_draft_evidence": connection.execute("SELECT COUNT(DISTINCT resource_id) FROM kg_atomic_draft_evidence").fetchone()[0],
        "evidence_rows": connection.execute("SELECT COUNT(*) FROM kg_atomic_draft_evidence").fetchone()[0],
        "standard_alignment_rows": connection.execute("SELECT COUNT(*) FROM kg_atomic_draft_standard_alignment").fetchone()[0],
        "relations": connection.execute("SELECT COUNT(*) FROM kg_atomic_relation_candidate").fetchone()[0],
        "merge_candidates": connection.execute("SELECT COUNT(*) FROM kg_atomic_merge_candidate").fetchone()[0],
    }
    evidence_mismatch = connection.execute(
        """SELECT COUNT(*) FROM kg_atomic_draft_evidence e JOIN kg_segment s ON s.segment_id=e.segment_id
           WHERE instr(s.text,e.exact_quote)=0"""
    ).fetchone()[0]
    invalid_components = [
        {"draft_id": item["draft_id"], "knowledge_component": item["knowledge_component"]}
        for item in drafts
        if item["knowledge_component"] in UMBRELLA_COMPONENTS
        or GENERIC_COMPONENT.fullmatch(item["knowledge_component"])
        or DISCOURSE_NOISE.search(item["knowledge_component"])
    ]
    malformed_learning_contract = [
        item["draft_id"] for item in drafts
        if set(json.loads(item["mastery_criteria_json"] or "{}")) != {"recognition", "independent", "transfer"}
        or {task.get("level") for task in json.loads(item["diagnostic_blueprint_json"] or "[]")}
        != {"recognition", "independent", "transfer"}
    ]
    jobs = [dict(row) for row in connection.execute(
        "SELECT job_id,status,total_batches,completed_batches,failed_batches,draft_count,prompt_tokens,completion_tokens FROM kg_atomic_extraction_job ORDER BY created_at"
    )]
    release_ready = bool(
        drafts and counts["approved_drafts"] and not evidence_mismatch
        and not invalid_components and not malformed_learning_contract
        and all(item["status"] == "completed" for item in jobs)
    )
    report = {
        "release_ready": release_ready,
        "truth_boundary": "draft count is coverage inventory, not teaching accuracy; only approved drafts can become mastery-active",
        "counts": counts, "jobs": jobs,
        "quality_failures": {
            "evidence_quote_mismatch": evidence_mismatch,
            "invalid_components": invalid_components[:50],
            "malformed_learning_contract": malformed_learning_contract[:50],
        },
        "remaining_gate": (
            "Run model batches, generate merge candidates, complete stratified human review, approve relations, then project approved atoms."
            if not release_ready else "All machine gates pass; retain periodic stratified curriculum review."
        ),
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
