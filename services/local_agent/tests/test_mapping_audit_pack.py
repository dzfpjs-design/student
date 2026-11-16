from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.audit_pack import MappingAuditPackService
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor


def test_audit_pack_calculates_precision_only_from_human_reviews(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db")
    database.initialize()
    transcript_root = tmp_path / "transcripts"
    transcript_root.mkdir()
    (transcript_root / "lesson.txt").write_text(
        "欧姆定律说明电流、电压和电阻的关系。", encoding="utf-8"
    )
    spec = CorpusSpec(
        "audit-corpus",
        "审计语料",
        "local",
        transcript_root,
        None,
        "local",
        "user_local",
    )
    ingestor = LocalCorpusIngestor(database)
    ingestor.run(spec, ingestor.start(spec, idempotency_key="audit-ingest"))
    graph = KnowledgeGraphService(database)
    node_id = graph.upsert_node(
        canonical_key="physics:ohm",
        node_type="knowledge_point",
        title="欧姆定律",
        scope={"subject": "物理", "grade": "初三", "track": "school"},
        source_ref="standard:test",
        license_boundary="test",
    )
    with database.connect() as connection:
        segment = connection.execute(
            "SELECT resource_id,segment_id FROM kg_segment LIMIT 1"
        ).fetchone()
    graph.map_segment(
        segment["resource_id"],
        segment["segment_id"],
        node_id,
        "explains",
        confidence=0.91,
        source_ref="strict-title-match:test",
    )

    service = MappingAuditPackService(database)
    created = service.create()
    assert created["metrics"]["human_precision"] is None
    assert created["metrics"]["mean_candidate_confidence"] == 0.91
    item = service.items(created["pack"]["audit_pack_id"])[0]
    assert item["candidate_confidence"] == 0.91

    reviewed = service.record_review(
        item["audit_item_id"], verdict="accept", reviewer="local-parent"
    )
    assert reviewed["pack"]["status"] == "completed"
    assert reviewed["metrics"]["human_precision"] == 1.0
    with database.connect() as connection:
        mapping_status = connection.execute(
            "SELECT review_status FROM kg_resource_node WHERE node_id=?", (node_id,)
        ).fetchone()[0]
    assert mapping_status == "unreviewed"

    restarted = MappingAuditPackService(Database(tmp_path / "study.db"))
    assert restarted.summary(created["pack"]["audit_pack_id"])["metrics"] == reviewed["metrics"]
    assert restarted.create()["pack"]["audit_pack_id"] == created["pack"]["audit_pack_id"]
