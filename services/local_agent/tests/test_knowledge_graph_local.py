from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from time import sleep

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.service import KnowledgeGraphChildNotFoundError, KnowledgeGraphService
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor, IngestRunAlreadyOwned
from home_study_agent.mastery.evidence import MasteryEvidenceService
from home_study_agent.knowledge_graph.quality import GraphQualityService
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
from home_study_agent.knowledge_graph.catalog_bootstrap import CatalogGraphBootstrapper
from home_study_agent.knowledge_graph.official_catalog import OfficialCurriculumCatalogProjector
from home_study_agent.knowledge_graph.reporting import quality_snapshot, render_quality_markdown, write_or_check_report


def _active_child(database: Database, child_id: str) -> None:
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES(?, '测试学生', '初二', '江苏南京', 'active', '2026-08-28T00:00:00+00:00', '2026-08-28T00:00:00+00:00')""",
            (child_id,),
        )
        connection.commit()



def _observe_original_run_claims(ingestor):
    owners=[];original=ingestor._claim_run
    def observe(*args):
        token=original(*args)
        if token is not None:owners.append(token)
        return token
    ingestor._claim_run=observe
    return owners


def _original_result_or_proven_checkpoint_handoff(first,second,owners):
    try:return first.result(timeout=5)
    except IngestRunAlreadyOwned as error:
        # Only a real later original claim plus a completed recovery can account
        # for this exact post-resource checkpoint fence. Other failures escape.
        assert str(error)=='ingest checkpoint lease was taken by a recovery worker'
        assert bool(len(owners)==2 and owners[0]!=owners[1])
        assert second['status']=='completed'
        return None


def test_local_corpus_ingest_run_lease_prevents_concurrent_checkpoint_advancement(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'transcripts';root.mkdir()
    (root/'one.txt').write_text('第一段可恢复导入文本。',encoding='utf-8')
    spec=CorpusSpec('lease','租约','seyk',root,None,'local','user_local')
    ingestor=LocalCorpusIngestor(database);job_id=ingestor.start(spec,idempotency_key='lease-v1')
    owners=_observe_original_run_claims(ingestor)
    ingestor.run_lease_seconds=.05;ingestor.heartbeat_interval_seconds=.01
    entered=Event();release=Event();original=ingestor._upsert_file
    def paused(*args,**kwargs):
        entered.set();assert release.wait(timeout=5);return original(*args,**kwargs)
    ingestor._upsert_file=paused  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(ingestor.run,spec,job_id)
        assert entered.wait(timeout=5)
        sleep(.12)  # Longer than the lease: the heartbeat must retain ownership.
        second=ingestor.run(spec,job_id)
        release.set();final=_original_result_or_proven_checkpoint_handoff(first,second,owners)
    # A 50ms lease can legitimately be recovered after the protected write has
    # committed and before this particular worker finishes its final checkpoint.
    # The durable job must finish once; neither caller has a fixed winner identity.
    assert second['status'] in {'processing','completed'} and (final is None or final['status'] in {'processing','completed'})
    assert ingestor.job(job_id)['status'] == 'completed'
    with database.connect() as con:
        row=con.execute("SELECT processed_count,run_token,run_lease_expires_at FROM kg_ingest_job WHERE job_id=?",(job_id,)).fetchone()
    assert dict(row)=={'processed_count':1,'run_token':None,'run_lease_expires_at':None}


def test_local_corpus_renews_lease_inside_slow_write_transaction_before_commit(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'transcripts';root.mkdir()
    (root/'one.txt').write_text('事务内写入的转录。',encoding='utf-8')
    spec=CorpusSpec('write-lease','写锁','seyk',root,None,'local','user_local')
    ingestor=LocalCorpusIngestor(database);job_id=ingestor.start(spec,idempotency_key='write-lease-v1')
    owners=_observe_original_run_claims(ingestor)
    ingestor.run_lease_seconds=.05;ingestor.heartbeat_interval_seconds=.01;entered=Event()
    def slow_commit() -> None:
        entered.set();sleep(.12)  # Holds BEGIN IMMEDIATE beyond the normal lease.
    ingestor._before_resource_commit=slow_commit  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(ingestor.run,spec,job_id)
        assert entered.wait(timeout=5)
        second=ingestor.run(spec,job_id)
        final=_original_result_or_proven_checkpoint_handoff(first,second,owners)
    assert second['status'] in {'processing','completed'} and (final is None or final['status']=='completed')
    assert ingestor.job(job_id)['status']=='completed'
    with database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM kg_resource WHERE corpus_id='write-lease'").fetchone()[0]==1
        row=con.execute("SELECT processed_count,run_token,run_lease_expires_at FROM kg_ingest_job WHERE job_id=?",(job_id,)).fetchone()
        assert dict(row)=={'processed_count':1,'run_token':None,'run_lease_expires_at':None}


def test_local_corpus_resource_commit_renews_after_slow_write_without_heartbeat(tmp_path: Path) -> None:
    """Observe the durable transaction renewal without timing a recovery winner."""
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'transcripts';root.mkdir()
    path=root/'one.txt';path.write_text('仅用于事务续租验收的合成文本。',encoding='utf-8')
    spec=CorpusSpec('commit-renew','事务续租','seyk',root,None,'local','user_local')
    ingestor=LocalCorpusIngestor(database);job_id=ingestor.start(spec,idempotency_key='commit-renew-v1')
    ingestor.run_lease_seconds=.05
    with database.connect() as con:
        token=ingestor._claim_run(con,job_id)
    assert token
    write_finished=[]
    def slow_write() -> None:
        sleep(.12)
        write_finished.append(datetime.now(timezone.utc))
    ingestor._before_resource_commit=slow_write  # type: ignore[method-assign]
    # No heartbeat or later run checkpoint can mask a missing transaction renewal.
    ingestor._upsert_file(spec,job_id,token,'one',path,None,{},False)
    with database.connect() as con:
        row=con.execute('SELECT run_token,run_lease_expires_at FROM kg_ingest_job WHERE job_id=?',(job_id,)).fetchone()
        assert row['run_token']==token
        assert datetime.fromisoformat(row['run_lease_expires_at']) > write_finished[0]
        assert con.execute("SELECT COUNT(*) FROM kg_resource WHERE corpus_id='commit-renew'").fetchone()[0]==1


def test_local_corpus_failed_resource_replay_reports_current_resources_not_attempts(tmp_path: Path) -> None:
    database=Database(tmp_path/'study.db');database.initialize();root=tmp_path/'transcripts';root.mkdir()
    (root/'one.txt').write_text('失败后修复的转录。',encoding='utf-8')
    spec=CorpusSpec('retry','重试','seyk',root,None,'local','user_local')
    ingestor=LocalCorpusIngestor(database);job_id=ingestor.start(spec,idempotency_key='retry-v1')
    original=ingestor._upsert_file;attempts=0
    def fail_once(*args,**kwargs):
        nonlocal attempts
        attempts+=1
        if attempts==1: raise ValueError('temporary parse failure')
        return original(*args,**kwargs)
    ingestor._upsert_file=fail_once  # type: ignore[method-assign]
    assert ingestor.run(spec,job_id)['status']=='failed'
    repaired=ingestor.run(spec,job_id)
    assert repaired['status']=='completed' and repaired['processed_count']==1 and repaired['failed_count']==0
    assert repaired['processed_count'] + repaired['failed_count'] <= repaired['total_count']
    with database.connect() as con:
        retry=con.execute("SELECT retry_count,status FROM kg_ingest_failure WHERE job_id=?",(job_id,)).fetchone()
    assert dict(retry)=={'retry_count':1,'status':'resolved'}


def test_local_vertical_slice_persists_deduplicates_and_recovers(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db")
    database.initialize()
    root = tmp_path / "transcripts"; root.mkdir()
    (root / "lesson-a.txt").write_text("三角形两边的和大于第三边。判断三条线段能否组成三角形，只需比较两短边之和。", encoding="utf-8")
    (root / "lesson-b.txt").write_text("三角形两边的和大于第三边。判断三条线段能否组成三角形，只需比较两短边之和。", encoding="utf-8")
    spec = CorpusSpec("sample", "本地样本", "seyk", root, None, "user_local_sample", "user_owned_local_only")
    ingestor = LocalCorpusIngestor(database)
    job_id = ingestor.start(spec, idempotency_key="sample-v1")
    first = ingestor.run(spec, job_id, max_resources=1)
    assert first["status"] == "processing"
    final = ingestor.run(spec, job_id)
    assert final["status"] == "completed"

    graph = KnowledgeGraphService(database)
    prior = graph.upsert_node(canonical_key="js-j2-math-triangle-definition", node_type="knowledge_point", title="三角形定义", scope={"grade":"初二","subject":"数学"}, source_ref="official_standard:demo", license_boundary="public_standard", confidence=.9, review_status="approved")
    triangle = graph.upsert_node(canonical_key="js-j2-math-triangle-inequality", node_type="knowledge_point", title="三角形三边关系", scope={"grade":"初二","subject":"数学"}, source_ref="official_standard:demo", license_boundary="public_standard", confidence=.9, review_status="approved")
    misconception = graph.upsert_node(canonical_key="misconception-triangle-equality", node_type="misconception", title="把两边和等于第三边当成可组成三角形", source_ref="teacher_review:demo", license_boundary="teacher_authored", confidence=.8, review_status="approved")
    graph.link(prior, triangle, "prerequisite_of", rationale="先理解定义才能判断构成", source_ref="official_standard:demo", confidence=.9, review_status="approved")
    graph.link(misconception, triangle, "misconception_of", rationale="退化三角形常见误判", source_ref="teacher_review:demo", confidence=.8, review_status="approved")
    exercise = graph.upsert_node(canonical_key="triangle-retest", node_type="exercise", title="三角形复测题", source_ref="teacher_review:demo", license_boundary="teacher_authored", review_status="approved")
    graph.link(triangle, exercise, "assessed_by", rationale="变式复测", source_ref="teacher_review:demo", review_status="approved")
    related = graph.upsert_node(canonical_key="triangle-related", node_type="knowledge_point", title="三角形面积", source_ref="official_standard:demo", license_boundary="public_standard", review_status="approved")
    related_exercise = graph.upsert_node(canonical_key="triangle-related-retest", node_type="exercise", title="三角形面积关联题", source_ref="teacher_review:demo", license_boundary="teacher_authored", review_status="approved")
    graph.link(triangle, related, "related_to", rationale="同一图形概念下的关联练习", source_ref="official_standard:demo", review_status="approved")
    graph.link(related, related_exercise, "assessed_by", rationale="关联知识点练习", source_ref="teacher_review:demo", review_status="approved")
    with database.connect() as con:
        resource = con.execute("SELECT resource_id FROM kg_resource WHERE external_id='lesson-a'").fetchone()[0]
        segment = con.execute("SELECT segment_id FROM kg_segment LIMIT 1").fetchone()[0]
    graph.map_segment(resource, segment, triangle, "explains", confidence=.83, source_ref="user_local_sample")

    results = graph.search_explanations("三角形两边的和大于第三边")
    assert results and "三角形" in results[0]["text"]
    assert graph.prerequisites(triangle)[0]["node_id"] == prior
    _active_child(database, "child-demo")
    state = MasteryEvidenceService(graph).apply_mistake("child-demo", triangle, "mistake-1", "把5+8=13判成可组成")
    assert state["mastery_probability"] < .5
    with database.connect() as connection:
        projected = connection.execute("SELECT node_type,canonical_key FROM kg_node WHERE node_type IN ('mistake','mastery_state') ORDER BY node_type").fetchall()
        evidence_edges = connection.execute("SELECT relation_type FROM kg_edge WHERE relation_type IN ('evidenced_by','generated_from')").fetchall()
    assert {row["node_type"] for row in projected} == {"mistake", "mastery_state"}
    assert all("child-demo" not in row["canonical_key"] for row in projected)
    assert {row["relation_type"] for row in evidence_edges} == {"evidenced_by", "generated_from"}
    diagnosis = graph.diagnose_mistake("child-demo", triangle)
    assert diagnosis["misconception_candidates"][0]["node_id"] == misconception
    assert diagnosis["weaknesses"][0]["node_id"] in {prior, triangle}
    assert diagnosis["explanation_materials"][0]["segment_id"] == segment
    assert diagnosis["retest_items"][0]["node_id"] == exercise
    similar = graph.similar_exercises(triangle, limit=3)
    assert {item["node_id"] for item in similar} == {exercise, related_exercise}
    assert {item["basis"] for item in similar} == {"same_knowledge_point", "explicit_related_knowledge_point"}
    # Exact local transcript evidence stays in the parent/operator queue until
    # someone accepts it; candidates are never silently treated as teaching material.
    try:
        KnowledgeGraphApplicationService(database).learning_path("child-demo", triangle)
        assert False, "approved graph structure without approved source material must stay child-hidden"
    except ValueError as error:
        assert "evidence" in str(error)
    GraphQualityService(database).record_review(
        resource_id=resource, segment_id=segment, node_id=triangle, relation_type="explains",
        decision="approved", reviewer="teacher", client_action_id="triangle-approve-1",
    )
    path = KnowledgeGraphApplicationService(database).learning_path("child-demo", triangle)
    assert path["materials"][0]["transcript_relative_path"] == "lesson-a.txt"
    assert path["materials"][0]["mapping_source_ref"] == "user_local_sample"
    assert path["materials"][0]["media_status"]["transcript_available"] is True
    assert path["next_action"] == "explain"
    assert path["truthfulness"]["mapping_status"] == "approved_parent_or_product_evidence_only"
    facade = KnowledgeGraphApplicationService(database)
    search = facade.search_materials("三角形两边的和大于第三边")
    assert search and search[0]["source_occurrences"] and "root" not in search[0]
    assert {item["node_id"] for item in facade.similar_exercises(triangle)} == {exercise, related_exercise}
    plan = facade.review_plan(child_id="child-demo", action_id="review-plan-1", max_items=3)
    assert plan["items"][0]["knowledge_node_id"] == triangle
    assert plan["items"][0]["recommended_action"] == "relearn_with_material"
    assert plan["items"][0]["materials"][0]["segment_id"] == segment
    assert facade.review_plan(child_id="child-demo", action_id="review-plan-1", max_items=1) == plan
    assert facade.latest_review_plan("child-demo") == plan
    coverage = graph.coverage(grade="初二", subject="数学")
    assert coverage["candidate_coverage_ratio"] == 0.5
    assert coverage["approved_coverage_ratio"] == 0.5

    # Reopen the durable SQLite file, then rerun the same job: no duplicate resource.
    reopened = KnowledgeGraphService(Database(tmp_path / "study.db"))
    assert reopened.mastery("child-demo", triangle)["evidence_count"] == 1
    assert KnowledgeGraphApplicationService(Database(tmp_path / "study.db")).latest_review_plan("child-demo") == plan
    assert ingestor.start(spec, idempotency_key="sample-v1") == job_id
    with database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM kg_resource").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM kg_segment").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM kg_segment_occurrence").fetchone()[0] == 2


def test_replays_failed_item_after_it_is_repaired(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir()
    broken = root / "repair-me.txt"; broken.symlink_to(root / "missing.txt")
    spec = CorpusSpec("repair", "恢复样本", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); job_id = ingestor.start(spec, idempotency_key="repair-v1")
    assert ingestor.run(spec, job_id)["status"] == "failed"
    broken.unlink(); broken.write_text("可恢复的导入内容。", encoding="utf-8")
    recovered = ingestor.run(spec, job_id)
    assert recovered["status"] == "completed"
    assert recovered["open_failures"] == 0
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM kg_ingest_failure").fetchone()[0] == "resolved"


def test_registers_a_video_without_a_transcript(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    transcripts = tmp_path / "transcripts"; videos = tmp_path / "videos"
    transcripts.mkdir(); videos.mkdir(); (videos / "video-only.mp4").write_bytes(b"small local video")
    spec = CorpusSpec("video-only", "视频样本", "bilibili", transcripts, videos, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); job_id = ingestor.start(spec, idempotency_key="video-only-v1")
    assert ingestor.run(spec, job_id)["processed_count"] == 1
    with database.connect() as connection:
        row = connection.execute("SELECT transcript_relative_path, video_relative_path, video_fingerprint FROM kg_resource").fetchone()
        assert row["transcript_relative_path"] == ""
        assert row["video_relative_path"] == "video-only.mp4"
        assert row["video_fingerprint"].startswith("size:")


def test_vector_fallback_respects_subject_scope(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir()
    (root / "english.txt").write_text("A short school email about a library meeting.", encoding="utf-8")
    (root / "math.txt").write_text("一元二次方程与判别式。", encoding="utf-8")
    spec = CorpusSpec("scope", "范围样本", "bilibili", root, None, "local", "user_owned")
    importer = LocalCorpusIngestor(database); job_id = importer.start(spec, idempotency_key="scope-v1")
    assert importer.run(spec, job_id)["status"] == "completed"
    with database.connect() as connection:
        connection.execute("UPDATE kg_resource SET subject_scope='英语' WHERE external_id='english'")
        connection.execute("UPDATE kg_resource SET subject_scope='数学' WHERE external_id='math'")
        connection.execute("UPDATE kg_segment_fts SET subject_scope='英语' WHERE segment_id IN (SELECT segment_id FROM kg_segment WHERE resource_id=(SELECT resource_id FROM kg_resource WHERE external_id='english'))")
        connection.execute("UPDATE kg_segment_fts SET subject_scope='数学' WHERE segment_id IN (SELECT segment_id FROM kg_segment WHERE resource_id=(SELECT resource_id FROM kg_resource WHERE external_id='math'))")
        connection.commit()
    hits = KnowledgeGraphService(database).search_explanations("not-a-lexical-match", subject="英语", limit=8)
    assert hits and all(hit["subject_scope"] == "英语" for hit in hits)


def test_mapping_review_keeps_auditable_decision(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir(); (root / "lesson.txt").write_text("判别式可以判断实数根。", encoding="utf-8")
    spec = CorpusSpec("review", "审核样本", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); job_id = ingestor.start(spec, idempotency_key="review-v1"); ingestor.run(spec, job_id)
    graph = KnowledgeGraphService(database)
    node_id = graph.upsert_node(canonical_key="review-node", node_type="knowledge_point", title="判别式", source_ref="test", license_boundary="test")
    with database.connect() as connection:
        resource_id = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
        segment_id = connection.execute("SELECT segment_id FROM kg_segment").fetchone()[0]
    graph.map_segment(resource_id, segment_id, node_id, "covers", confidence=.6, source_ref="test")
    quality = GraphQualityService(database)
    quality.record_review(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="covers", decision="approved", reviewer="teacher")
    report = quality.report()
    assert report["mapping"]["human_mapping_accuracy"] is None
    assert report["mapping"]["human_decision_acceptance_rate"] == 1.0
    queue = quality.review_queue()
    assert not queue  # approved mapping no longer needs review
    with database.connect() as connection:
        assert connection.execute("SELECT review_status FROM kg_resource_node").fetchone()[0] == "approved"


def test_child_gate_requires_approved_material_not_any_approved_graph_edge(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize(); _active_child(database, "child-demo")
    graph = KnowledgeGraphService(database)
    node = graph.upsert_node(canonical_key="gate-node", node_type="knowledge_point", title="材料准入点",
                             scope={"grade": "初二"}, source_ref="test", review_status="approved")
    prerequisite = graph.upsert_node(canonical_key="gate-prerequisite", node_type="knowledge_point", title="前置点",
                                     scope={"grade": "初二"}, source_ref="test", review_status="approved")
    graph.link(prerequisite, node, "prerequisite_of", rationale="只是结构关系", source_ref="official_standard:demo", review_status="approved")
    facade = KnowledgeGraphApplicationService(database)
    try:
        facade.learning_path("child-demo", node)
        assert False, "approved prerequisite must not make material child-usable"
    except ValueError as error:
        assert "evidence" in str(error)
    # Mastery/evidence graph edges are also diagnostic history, not approved source material.
    graph.link(node, prerequisite, "evidenced_by", rationale="local history", source_ref="local_mastery:demo", review_status="approved")
    try:
        facade.learning_path("child-demo", node)
        assert False, "approved mastery edge must not make material child-usable"
    except ValueError as error:
        assert "evidence" in str(error)
    # A second currently-approved mapping is the durable, child-facing evidence.
    with database.connect() as connection:
        connection.execute("INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at) VALUES('gate','gate','test','','',NULL,'test','test','approved','v1',datetime('now'),datetime('now'))")
        connection.execute("INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,grade_scope,subject_scope,semester_scope,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,video_fingerprint,metadata_json,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at) VALUES('gate-resource','gate','r','批准材料','','','','','', 'gate',NULL,NULL,'{}','test','test',1,'approved','v1',datetime('now'),datetime('now'))")
        connection.execute("INSERT INTO kg_resource_node(resource_id,segment_id,node_id,relation_type,confidence,review_status,source_ref,created_at) VALUES('gate-resource',NULL,?,'covers',1,'approved','parent_review:gate',datetime('now'))", (node,))
        connection.commit()
    assert facade.learning_path("child-demo", node)["node_id"] == node


def test_curriculum_alignment_is_diagnosable_but_never_claims_missing_explanation(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize(); _active_child(database, "child-demo")
    graph = KnowledgeGraphService(database)
    node = graph.upsert_node(canonical_key="alignment-node", node_type="knowledge_point", title="仅课标对齐",
                             scope={"grade": "初二"}, source_ref="curriculum_standard:demo", review_status="approved")
    standard = graph.upsert_node(canonical_key="alignment-standard", node_type="standard_capability", title="课标能力",
                                 source_ref="curriculum_standard:demo", review_status="approved")
    graph.link(node, standard, "aligned_to", rationale="课标对齐", source_ref="curriculum_standard:demo", review_status="approved")
    path = KnowledgeGraphApplicationService(database).learning_path("child-demo", node)
    assert path["materials"] == []
    assert path["next_action"] == "manual_material_required"
    assert path["material_availability"] == "material_unavailable"


def test_review_is_idempotent_undoable_and_persists_across_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir(); (root / "lesson.txt").write_text("判别式可用于判断实数根。", encoding="utf-8")
    spec = CorpusSpec("undo", "撤销审核样本", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); ingestor.run(spec, ingestor.start(spec, idempotency_key="undo-v1"))
    graph = KnowledgeGraphService(database)
    node_id = graph.upsert_node(canonical_key="undo-node", node_type="knowledge_point", title="判别式", source_ref="test", license_boundary="test")
    with database.connect() as connection:
        resource_id = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
        segment_id = connection.execute("SELECT segment_id FROM kg_segment").fetchone()[0]
    graph.map_segment(resource_id, segment_id, node_id, "explains", confidence=.6, source_ref="test")
    quality = GraphQualityService(database)
    approved = quality.record_review(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="explains",
                                     decision="approved", reviewer="teacher", note="证据充分", client_action_id="approve-once")
    assert approved["review_status"] == "approved"
    assert quality.record_review(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="explains",
                                 decision="approved", reviewer="teacher", client_action_id="approve-once") == approved
    assert graph.explanation_materials(node_id, approved_only=True)
    undone = quality.undo_review(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="explains",
                                 reviewer="teacher", note="重新检查", client_action_id="undo-once")
    assert undone["review_status"] == "unreviewed"
    assert not graph.explanation_materials(node_id, approved_only=True)
    reopened = GraphQualityService(Database(tmp_path / "study.db"))
    assert [event["action"] for event in reopened.review_history(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="explains")] == ["decision", "undo"]


def test_needs_split_leaves_mapping_unavailable_and_out_of_review_queue(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir(); (root / "lesson.txt").write_text("一段需要拆分的讲解。", encoding="utf-8")
    spec = CorpusSpec("split", "拆分审核样本", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); ingestor.run(spec, ingestor.start(spec, idempotency_key="split-v1"))
    graph = KnowledgeGraphService(database)
    node_id = graph.upsert_node(canonical_key="split-node", node_type="knowledge_point", title="拆分知识点", source_ref="test", license_boundary="test")
    with database.connect() as connection:
        resource_id = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
        segment_id = connection.execute("SELECT segment_id FROM kg_segment").fetchone()[0]
    graph.map_segment(resource_id, segment_id, node_id, "explains", confidence=.6, source_ref="test")
    quality = GraphQualityService(database)
    assert quality.record_review(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="explains", decision="needs_split", reviewer="teacher", client_action_id="split-once")["review_status"] == "unreviewed"
    assert not graph.explanation_materials(node_id, approved_only=True)
    assert not quality.review_queue()
    quality.undo_review(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="explains", reviewer="teacher", client_action_id="split-undo")
    assert quality.review_queue()


def test_rejected_mapping_is_not_used_for_explanation_location(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir(); (root / "lesson.txt").write_text("判别式可用于判断实数根。", encoding="utf-8")
    spec = CorpusSpec("rejected", "拒绝映射样本", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); job_id = ingestor.start(spec, idempotency_key="rejected-v1"); ingestor.run(spec, job_id)
    graph = KnowledgeGraphService(database)
    node_id = graph.upsert_node(canonical_key="rejected-node", node_type="knowledge_point", title="判别式", source_ref="test", license_boundary="test")
    with database.connect() as connection:
        resource_id = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
        segment_id = connection.execute("SELECT segment_id FROM kg_segment").fetchone()[0]
    graph.map_segment(resource_id, segment_id, node_id, "explains", confidence=.6, source_ref="test")
    assert graph.explanation_materials(node_id)
    GraphQualityService(database).record_review(resource_id=resource_id, segment_id=segment_id, node_id=node_id, relation_type="explains", decision="rejected", reviewer="teacher")
    assert graph.explanation_materials(node_id) == []
    assert GraphQualityService(database).report()["mapping"]["candidate_or_approved_mapped_segments"] == 0


def test_review_queue_exposes_auditable_local_context(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir(); (root / "lesson.txt").write_text("判别式可用于判断实数根。", encoding="utf-8")
    spec = CorpusSpec("queue", "审核上下文样本", "seyk", root, None, "local-source", "user_owned")
    ingestor = LocalCorpusIngestor(database); job_id = ingestor.start(spec, idempotency_key="queue-v1"); ingestor.run(spec, job_id)
    graph = KnowledgeGraphService(database)
    node_id = graph.upsert_node(canonical_key="queue-node", node_type="knowledge_point", title="判别式", scope={"grade":"初三","track":"school"}, source_ref="test", license_boundary="test")
    with database.connect() as connection:
        resource_id = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
        segment_id = connection.execute("SELECT segment_id FROM kg_segment").fetchone()[0]
    graph.map_segment(resource_id, segment_id, node_id, "covers", confidence=.6, source_ref="keyword-candidate")
    item = GraphQualityService(database).review_queue()[0]
    assert item["resource_title"].endswith("lesson")
    assert item["resource_source_ref"] == "local-source"
    assert item["mapping_source_ref"] == "keyword-candidate"
    assert item["transcript_relative_path"] == "lesson.txt"
    assert "root" not in item


def test_scope_node_reaches_resource_through_contains_edges(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir(); (root / "lesson.txt").write_text("四年级数学的面积练习。", encoding="utf-8")
    spec = CorpusSpec("scope-tree", "范围样本", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); job_id = ingestor.start(spec, idempotency_key="scope-tree-v1"); ingestor.run(spec, job_id)
    graph = KnowledgeGraphService(database)
    subject = graph.upsert_node(canonical_key="scope-p4-math", node_type="subject", title="数学", source_ref="metadata", license_boundary="metadata")
    resource_node = graph.upsert_node(canonical_key="resource-lesson", node_type="explanation", title="课程资源", source_ref="metadata", license_boundary="metadata")
    graph.link(subject, resource_node, "contains", rationale="学科范围", source_ref="metadata")
    with database.connect() as connection:
        resource_id = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
    graph.map_segment(resource_id, None, resource_node, "explains", confidence=.7, source_ref="metadata")
    assert graph.explanation_materials(subject)[0]["resource_id"] == resource_id


def test_mastery_evidence_requires_an_active_child(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    graph = KnowledgeGraphService(database)
    node = graph.upsert_node(canonical_key="active-child-required", node_type="knowledge_point", title="验证节点", source_ref="test", license_boundary="test")
    try:
        graph.record_evidence(child_id="missing", node_id=node, correctness=1, independent=True, origin_type="diagnostic", idempotency_key="missing-child")
    except KnowledgeGraphChildNotFoundError:
        pass
    else:
        raise AssertionError("evidence write accepted a nonexistent child")
    _active_child(database, "active")
    assert graph.record_evidence(child_id="active", node_id=node, correctness=1, independent=True, origin_type="diagnostic", idempotency_key="active-child")["evidence_count"] == 1


def test_keyword_mapper_excludes_metadata_ineligible_candidates(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir()
    (root / "math.txt").write_text("数列规律和归纳方法。", encoding="utf-8")
    (root / "biology.txt").write_text("生态系统中的数列规律只是偶然词语。", encoding="utf-8")
    spec = CorpusSpec("metadata-filter", "元数据筛选", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); job_id = ingestor.start(spec, idempotency_key="metadata-filter-v1"); ingestor.run(spec, job_id)
    with database.connect() as connection:
        connection.execute("UPDATE kg_resource SET subject_scope='数学', grade_scope='四年级' WHERE external_id='math'")
        connection.execute("UPDATE kg_resource SET subject_scope='生物', grade_scope='初二' WHERE external_id='biology'")
        connection.commit()
    result = CatalogGraphBootstrapper(database).run()
    assert result["candidate_segment_mappings"]["upserted"] > 0
    with database.connect() as connection:
        matches = connection.execute("""SELECT r.external_id FROM kg_resource_node rn
            JOIN kg_node n ON n.node_id=rn.node_id JOIN kg_resource r ON r.resource_id=rn.resource_id
            WHERE n.canonical_key='starter:OLY.P4.PATTERN' AND rn.source_ref='local_keyword_mapper_v3'""").fetchall()
    assert [row["external_id"] for row in matches] == ["math"]


def test_catalog_startup_projection_does_not_rescan_local_corpus(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    root = tmp_path / "transcripts"; root.mkdir()
    (root / "lesson.txt").write_text("根的判别式是本地候选。", encoding="utf-8")
    spec = CorpusSpec("startup-no-scan", "启动不重扫", "seyk", root, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); ingestor.run(spec, ingestor.start(spec, idempotency_key="startup-no-scan-v1"))
    result = CatalogGraphBootstrapper(database).run(rebuild_local_candidates=False)
    assert result["candidate_segment_mappings"]["skipped"] == "startup_does_not_rescan_local_corpus"
    assert result["official_catalog"]["exact_title_candidates"]["skipped"] == "startup_does_not_rescan_local_corpus"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM kg_resource_node").fetchone()[0] == 0


def test_official_catalogue_builds_realistic_grade_level_atom_counts_with_truthful_evidence(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("""INSERT INTO curriculum_standard_source(source_id,source_key,jurisdiction,source_type,subject_title,title,
            version_label,official_url,local_root_path,pdf_filename,pages_filename,pdf_sha256,text_sha256,page_count,extracted_page_count,status,imported_at,updated_at)
            VALUES('math-standard','moe-2022-mathematics','CN','national_standard','数学','数学课标','2022','local://math','/local',NULL,NULL,NULL,NULL,1,1,'ocr_ready',datetime('now'),datetime('now'))""")
        connection.execute("""INSERT INTO curriculum_standard_page(source_id,page_number,content_text,content_hash,char_count)
            VALUES('math-standard',1,'数与代数 图形与几何 数据分析','hash',15)""")
        connection.commit()
    result = OfficialCurriculumCatalogProjector(database).run()
    assert result["atomic_nodes"] >= 200
    assert result["aligned_atoms"] >= 100
    with database.connect() as connection:
        counts = dict(connection.execute("""SELECT json_extract(scope_json,'$.grade'),COUNT(*) FROM kg_node
            WHERE node_type='knowledge_point' GROUP BY json_extract(scope_json,'$.grade')"""))
        unresolved = connection.execute("""SELECT COUNT(*) FROM kg_node WHERE node_type='knowledge_point'
            AND source_ref LIKE 'curriculum_standard_unresolved:%'""").fetchone()[0]
    assert counts["初三"] >= 140
    assert counts["四年级"] >= 70
    assert unresolved > 0  # Other subject standards were deliberately not fabricated in this fixture.


def test_official_catalogue_maps_only_exact_local_title_evidence(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    transcripts = tmp_path / "transcripts"; transcripts.mkdir()
    (transcripts / "physics.txt").write_text("本节课说明电流、电压和电阻的关系。", encoding="utf-8")
    spec = CorpusSpec("catalog-exact", "精确证据样本", "seyk", transcripts, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); ingestor.run(spec, ingestor.start(spec, idempotency_key="catalog-exact-v1"))
    with database.connect() as connection:
        connection.execute("UPDATE kg_resource SET subject_scope='物理',grade_scope='初三',title='欧姆定律讲解'")
        connection.commit()
    result = OfficialCurriculumCatalogProjector(database).run()
    assert result["exact_title_candidates"]["covered_atomic_nodes"] >= 1
    with database.connect() as connection:
        rows = connection.execute("""SELECT rn.review_status,rn.source_ref FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id
            WHERE n.title='欧姆定律'""").fetchall()
    assert [tuple(row) for row in rows] == [("unreviewed", "official_catalog_exact_local_evidence_v2")]


def test_quality_report_is_rendered_from_one_snapshot_and_has_a_staleness_gate(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    graph = KnowledgeGraphService(database)
    graph.upsert_node(canonical_key="report-node", node_type="knowledge_point", title="报告节点", source_ref="test", license_boundary="test")
    snapshot = quality_snapshot(database.path)
    assert snapshot["snapshot"]["database_sha256"]
    assert snapshot["snapshot"]["integrity_check"] == "ok"
    assert snapshot["snapshot"]["schema_version"] >= 1
    assert snapshot["snapshot"]["table_counts"] == snapshot["counts"]
    markdown = render_quality_markdown(snapshot)
    assert "学生端只消费家长或产品审核为 `approved` 的映射" in markdown
    assert "未审核自动映射可给孩子使用" not in markdown
    output = tmp_path / "quality.md"; payload = tmp_path / "quality.json"
    assert write_or_check_report(app_db=database.path, markdown_output=output, json_output=payload)
    assert output.read_text(encoding="utf-8") == markdown
    assert write_or_check_report(app_db=database.path, markdown_output=output, json_output=payload, check=True)
    output.write_text("stale", encoding="utf-8")
    assert not write_or_check_report(app_db=database.path, markdown_output=output, json_output=payload, check=True)
