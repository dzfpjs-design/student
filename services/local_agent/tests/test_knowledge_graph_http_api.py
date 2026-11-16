from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor


class ParentAuth:
    def require(self, token: str | None) -> None:
        if token != "parent-test-token":
            raise HTTPException(status_code=401, detail="parent session required")


def _active_child(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES('child-api','测试学生','初三','江苏南京','active',datetime('now'),datetime('now'))""")
        connection.commit()


def test_reviewed_mapping_http_vertical_slice_persists(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize(); _active_child(database)
    transcripts = tmp_path / "transcripts"; transcripts.mkdir()
    (transcripts / "lesson.txt").write_text("求根公式可以解一元二次方程。代入前先确认 a、b、c。", encoding="utf-8")
    spec = CorpusSpec("http", "HTTP 纵向样本", "seyk", transcripts, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); ingestor.run(spec, ingestor.start(spec, idempotency_key="http-v1"))
    graph = KnowledgeGraphService(database)
    node_id = graph.upsert_node(canonical_key="http-formula", node_type="knowledge_point", title="求根公式", scope={"grade":"初三","subject":"数学"}, source_ref="test", license_boundary="test", review_status="approved")
    exercise = graph.upsert_node(canonical_key="http-formula-retest", node_type="exercise", title="求根公式复测", source_ref="teacher", license_boundary="teacher_authored", review_status="approved")
    graph.link(node_id, exercise, "assessed_by", rationale="复测", source_ref="teacher", review_status="approved")
    second_exercise = graph.upsert_node(canonical_key="http-formula-retest-2", node_type="exercise", title="求根公式第二复测", source_ref="teacher", license_boundary="teacher_authored", review_status="approved")
    graph.link(node_id, second_exercise, "assessed_by", rationale="第二复测", source_ref="teacher", review_status="approved")
    unapproved_exercise = graph.upsert_node(canonical_key="http-formula-unreviewed", node_type="exercise", title="未审核复测", source_ref="candidate", license_boundary="local_candidate", review_status="unreviewed")
    graph.link(node_id, unapproved_exercise, "assessed_by", rationale="候选", source_ref="candidate", review_status="unreviewed")
    with database.connect() as connection:
        connection.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child-api-2','第二学生','初三','江苏南京','active',datetime('now'),datetime('now'))")
        connection.commit()
    with database.connect() as connection:
        resource_id = connection.execute("SELECT resource_id FROM kg_resource").fetchone()[0]
        segment_id = connection.execute("SELECT segment_id FROM kg_segment").fetchone()[0]
    graph.map_segment(resource_id, segment_id, node_id, "explains", confidence=.9, source_ref="human-review-candidate")
    application = FastAPI(); install_knowledge_graph_routes(application, database, ParentAuth())

    with TestClient(application) as client:
        before = client.get(f"/v1/children/child-api/knowledge-graph/learning-path?node_id={node_id}")
        assert before.status_code == 422

        queue = client.get("/v1/parent/knowledge-graph/review-queue", headers={"X-HSC-Parent":"parent-test-token"})
        assert queue.status_code == 200 and len(queue.json()["items"]) == 1
        resource_queue = client.get("/v1/parent/knowledge-graph/resource-review-queue", headers={"X-HSC-Parent":"parent-test-token"})
        assert resource_queue.status_code == 200 and resource_queue.json()["items"][0]["evidence_segment_count"] == 1
        assert resource_queue.json()["summary"]["pending_resource_review_items"] == 1
        preview = client.get("/v1/parent/knowledge-graph/resource-review-evidence", headers={"X-HSC-Parent":"parent-test-token"}, params={
            "resource_id":resource_id, "node_id":node_id, "relation_type":"explains"})
        assert preview.status_code == 200 and len(preview.json()["items"]) == 1
        assert "transcript_relative_path" not in preview.json()["items"][0]
        approved = client.post("/v1/parent/knowledge-graph/resource-mapping-reviews", headers={"X-HSC-Parent":"parent-test-token"}, json={
            "resource_id":resource_id, "node_id":node_id, "relation_type":"explains",
            "decision":"approved", "reviewer":"parent", "client_action_id":"approve-http-01", "note":"公式讲解明确",
        })
        assert approved.status_code == 200 and approved.json()["review"]["statuses"] == {"approved":1}
        replay = client.post("/v1/parent/knowledge-graph/resource-mapping-reviews", headers={"X-HSC-Parent":"parent-test-token"}, json={
            "resource_id":resource_id, "node_id":node_id, "relation_type":"explains",
            "decision":"approved", "reviewer":"parent", "client_action_id":"approve-http-01", "note":"公式讲解明确",
        })
        history = client.get("/v1/parent/knowledge-graph/resource-review-history", headers={"X-HSC-Parent":"parent-test-token"}, params={"reviewer":"parent"})
        assert replay.status_code == 200 and history.json()["items"][0]["decision"] == "approved"
        approved_path = client.get(f"/v1/children/child-api/knowledge-graph/learning-path?node_id={node_id}")
        assert approved_path.json()["materials"][0]["segment_id"] == segment_id

        # Browser payloads cannot forge a mistake/correctness update; the host
        # diagnostic endpoint is the only scoring bridge.
        mistake = client.post("/v1/children/child-api/knowledge-graph/mistakes", json={"node_id":node_id, "mistake_id":"mistake-http-1", "note":"公式代入错误"})
        assert mistake.status_code == 409 and mistake.json()["detail"]["code"] == "HOST_MISTAKE_REQUIRED"
        learning = client.get(f"/v1/children/child-api/knowledge-graph/learning-path?node_id={node_id}").json()
        assert learning["materials"][0]["segment_id"] == segment_id
        assert learning["diagnosis"]["retest_items"][0]["node_id"] == exercise
        assert learning["truthfulness"]["mapping_status"] == "approved_parent_or_product_evidence_only"
        plan = client.post("/v1/children/child-api/knowledge-graph/review-plans", json={"client_action_id":"review-plan-http-01", "max_items":3})
        assert plan.status_code == 200 and plan.json()["items"] == []
        assert client.get("/v1/children/child-api/knowledge-graph/review-plans/latest").json()["plan"] == plan.json()

        retests = client.get(f"/v1/children/child-api/knowledge-graph/retest-items?node_id={node_id}")
        assert retests.status_code == 200 and {item['node_id'] for item in retests.json()['items']} == {exercise, second_exercise}
        answered = client.post('/v1/children/child-api/knowledge-graph/exercise-results', json={
            'node_id':node_id, 'exercise_id':exercise, 'correctness':1, 'independent':True,
            'client_action_id':'exercise-result-http-01', 'note':'本地判分的独立复测通过',
        })
        replayed = client.post('/v1/children/child-api/knowledge-graph/exercise-results', json={
            'node_id':node_id, 'exercise_id':exercise, 'correctness':1, 'independent':True,
            'client_action_id':'exercise-result-http-01', 'note':'本地判分的独立复测通过',
        })
        assert answered.status_code == replayed.status_code == 409
        assert answered.json()['detail']['code'] == 'HOST_GRADED_ATTEMPT_REQUIRED'
        changed_exercise = client.post('/v1/children/child-api/knowledge-graph/exercise-results', json={
            'node_id':node_id, 'exercise_id':second_exercise, 'correctness':1, 'independent':True,
            'client_action_id':'exercise-result-http-01', 'note':'本地判分的独立复测通过',
        })
        candidate_exercise = client.post('/v1/children/child-api/knowledge-graph/exercise-results', json={
            'node_id':node_id, 'exercise_id':unapproved_exercise, 'correctness':1, 'independent':True,
            'client_action_id':'exercise-result-unreviewed-01',
        })
        forged_child = client.post('/v1/children/child-api-2/knowledge-graph/exercise-results', json={
            'node_id':node_id, 'exercise_id':exercise, 'correctness':1, 'independent':True,
            'client_action_id':'exercise-result-http-01', 'note':'本地判分的独立复测通过',
        })
        assert changed_exercise.status_code == candidate_exercise.status_code == forged_child.status_code == 409

        undone = client.post("/v1/parent/knowledge-graph/resource-mapping-reviews/undo", headers={"X-HSC-Parent":"parent-test-token"}, json={
            "resource_id":resource_id, "node_id":node_id, "relation_type":"explains",
            "reviewer":"parent", "client_action_id":"undo-http-01",
        })
        assert undone.status_code == 200 and undone.json()["review"]["statuses"] == {"unreviewed":1}
        assert client.get(f"/v1/children/child-api/knowledge-graph/learning-path?node_id={node_id}").status_code == 422

    # The same SQLite database can be reopened after the HTTP server exits.
    reopened = Database(tmp_path / "study.db")
    with reopened.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM kg_mapping_review_event").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM kg_review_plan").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM kg_mastery_evidence WHERE origin_type='exercise'").fetchone()[0] == 0


def test_host_registers_graph_routes_with_real_parent_pin_authentication(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "host.db", maintenance_enabled=False)
    application = create_app(settings)
    with TestClient(application) as client:
        bootstrap = client.get("/v1/bootstrap")
        assert bootstrap.status_code == 200
        # The host's existing parent PIN service, rather than a test-only bypass,
        # protects the review route supplied by the knowledge-graph adapter.
        token = client.post("/v1/parent/setup", json={"pin":"2468"}).json()["parent_token"]
        queue = client.get("/v1/parent/knowledge-graph/review-queue", headers={"X-HSC-Parent":token})
        assert queue.status_code == 200 and queue.json()["operator_only"] is True
    # App startup must project every host diagnostic into an approved product
    # relationship, but it must not claim that product structure itself is a
    # child-visible local explanation mapping.
    database = Database(tmp_path / "host.db")
    with database.connect() as connection:
        row = connection.execute("""SELECT COUNT(*) FROM kg_edge e JOIN kg_node exercise ON exercise.node_id=e.target_node_id
            WHERE e.relation_type='assessed_by' AND e.review_status='approved'
              AND exercise.canonical_key IN ('diagnostic:diag-j3-discriminant-1','diagnostic:diag-j3-discriminant-2')""").fetchone()
    assert row[0] == 2


def test_child_search_enforces_profile_grade_and_approved_mapping_boundary(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child-4','小四','四年级','江苏南京','active',datetime('now'),datetime('now'))")
        connection.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child-9','小九','初三','江苏南京','active',datetime('now'),datetime('now'))")
        connection.commit()
    transcripts = tmp_path / "transcripts"; transcripts.mkdir()
    (transcripts / "grade4.txt").write_text("apple grade boundary 四年级 material", encoding="utf-8")
    (transcripts / "grade9.txt").write_text("apple grade boundary 初三 material", encoding="utf-8")
    spec = CorpusSpec("child-search", "孩子检索隔离", "seyk", transcripts, None, "local", "user_owned")
    ingestor = LocalCorpusIngestor(database); ingestor.run(spec, ingestor.start(spec, idempotency_key="child-search-v1"))
    graph = KnowledgeGraphService(database)
    four_node = graph.upsert_node(canonical_key="four-apple", node_type="knowledge_point", title="四年级 apple", scope={"grade":"四年级","subject":"英语"}, source_ref="test", license_boundary="user_local", review_status="approved")
    nine_node = graph.upsert_node(canonical_key="nine-apple", node_type="knowledge_point", title="初三 apple", scope={"grade":"初三","subject":"英语"}, source_ref="test", license_boundary="user_local", review_status="approved")
    with database.connect() as connection:
        rows = connection.execute("SELECT r.resource_id,r.title,s.segment_id FROM kg_resource r JOIN kg_segment s ON s.resource_id=r.resource_id ORDER BY r.title").fetchall()
        for row in rows:
            grade = '四年级' if 'grade4' in row['title'] else '初三'
            connection.execute("UPDATE kg_resource SET grade_scope=?,subject_scope='英语' WHERE resource_id=?", (grade, row['resource_id']))
        connection.commit()
    for row in rows:
        grade = '四年级' if 'grade4' in row['title'] else '初三'
        graph.map_segment(row['resource_id'], row['segment_id'], four_node if grade == '四年级' else nine_node, 'explains', confidence=.9, source_ref='parent-approved', review_status='approved')
    with database.connect() as connection:
        # This would rank for the same query but must never reach either child.
        connection.execute("UPDATE kg_resource_node SET review_status='unreviewed' WHERE node_id=?", (nine_node,))
        connection.commit()
    application = FastAPI(); install_knowledge_graph_routes(application, database, ParentAuth())
    with TestClient(application) as client:
        four = client.get('/v1/children/child-4/knowledge-graph/materials/search', params={'query':'apple grade boundary','grade':'初三'})
        nine = client.get('/v1/children/child-9/knowledge-graph/materials/search', params={'query':'apple grade boundary','grade':'四年级'})
        missing = client.get('/v1/children/not-a-child/knowledge-graph/materials/search', params={'query':'apple'})
        cross_path = client.get('/v1/children/child-4/knowledge-graph/learning-path', params={'node_id':nine_node})
        cross_result = client.post('/v1/children/child-4/knowledge-graph/exercise-results', json={
            'node_id':nine_node, 'exercise_id':'not-approved', 'correctness':1, 'independent':True,
            'client_action_id':'cross-grade-result-001',
        })
        assert four.status_code == 200 and [item['grade_scope'] for item in four.json()['items']] == ['四年级']
        assert four.json()['items'][0]['approvals'][0]['mapping_review_status'] == 'approved'
        assert nine.status_code == 200 and nine.json()['items'] == []
        assert missing.status_code == 404
        assert cross_path.status_code == 422 and cross_result.status_code == 409
