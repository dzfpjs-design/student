from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService


class FakeSupplyGateway:
    configured = True

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request) -> ModelResult:
        self.requests.append(request)
        context = json.loads(request.messages[1]["content"])
        candidates = []
        for index, stage in enumerate(context["required_stages"]):
            candidates.append({
                "stage": stage,
                "prompt": f"专项闭题{index + 1}：给定数{index + 2}和{index + 4}，请只写出这两个数之和。",
                "scoring_contract": {"schema": "specialized_closed_exact_v1", "answer_type": "text",
                    "assessment_mode": "objective", "expected_answer": {"local_rule": "local_exact_v1",
                    "exact_answers": [str(2 * index + 6)]}},
                "explanation": f"先识别条件，再使用该能力域的方法完成第{index + 1}项判断。",
                "difficulty": min(index + 2, 5),
            })
        return ModelResult(
            content=json.dumps({"candidates": candidates}, ensure_ascii=False),
            provider="fake-deepseek", model="fake-specialized", latency_ms=20,
            prompt_tokens=120, completion_tokens=180,
        )


class UnconfiguredGateway:
    configured = False


def prepared(tmp_path: Path) -> Database:
    database = Database(tmp_path / "specialized-supply.db")
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    return database


def test_specialized_supply_fills_depth_only_after_parent_review(tmp_path: Path) -> None:
    database = prepared(tmp_path)
    gateway = FakeSupplyGateway()
    service = SpecializedDiagnosticSupplyService(database, gateway)

    gaps = service.gaps("child-primary-4", "competition_math", 4)
    number_sense = next(row for row in gaps["domains"] if row["domain_id"] == "competition_math.number_sense")
    assert number_sense["active_count"] == 2
    assert number_sense["missing_count"] == 3
    assert number_sense["missing_stages"] == ["parallel", "discrimination", "transfer"]

    queued = service.enqueue_gaps("child-primary-4", "competition_math", "supply-all-domains", 4)
    assert queued["created_count"] > 0
    first = asyncio.run(service.process_next())
    assert first and first["status"] == "completed"
    assert first["candidate_count"] >= 1
    request = gateway.requests[0]
    assert request.request_type == "specialized_diagnostic_generation"
    assert request.context_manifest["child_identity"] is False
    assert request.context_manifest["learning_history"] is False
    assert "小明" not in request.messages[1]["content"]

    candidates = service.list("child-primary-4", "needs_review")["items"]
    assert candidates
    candidate = candidates[0]
    with database.connect() as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM diagnostic_item WHERE source_ref LIKE 'parent_approved_specialized:%'"
        ).fetchone()[0]
    assert before == 0

    promoted = service.review(candidate["candidate_id"], "approve", "题意与答案已核对", "review-specialized-1")
    assert promoted["status"] == "promoted"
    with database.connect() as connection:
        item = connection.execute(
            "SELECT status,source_ref FROM diagnostic_item WHERE diagnostic_item_id=?",
            (promoted["diagnostic_item_id"],),
        ).fetchone()
    assert item["status"] == "active"
    assert item["source_ref"].startswith("parent_approved_specialized:")
    for index, remaining in enumerate(candidates[1:], start=2):
        service.review(
            remaining["candidate_id"], "approve", "题意与答案已核对",
            f"review-specialized-{index}",
        )
    domain_gap = next(
        row for row in service.gaps("child-primary-4", "competition_math", 4)["domains"]
        if row["domain_id"] == candidate["domain_id"]
    )
    assert domain_gap["ready"] is True
    assert domain_gap["promoted_stages"] == ["discrimination", "parallel", "transfer"]

    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = privacy.export_child("child-primary-4")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert payload["specialized_supply_jobs"]
    assert payload["specialized_supply_candidates"]
    assert privacy.deletion_preview("child-primary-4")["specialized_supply"] > 0
    deleted = privacy.delete_child_scope(
        "child-primary-4", "learning", "确认删除", "delete-specialized-supply",
    )
    assert deleted["status"] == "completed"
    assert privacy.deletion_preview("child-primary-4")["specialized_supply"] == 0


def test_specialized_supply_recovers_processing_job(tmp_path: Path) -> None:
    database = prepared(tmp_path)
    gateway = FakeSupplyGateway()
    service = SpecializedDiagnosticSupplyService(database, gateway)
    queued = service.enqueue_gaps("child-junior-3", "junior_exam", "supply-junior", 4)
    assert queued["created_count"] > 0
    with database.connect() as connection:
        connection.execute(
            "UPDATE specialized_supply_job SET status='processing' WHERE job_id=?",
            (queued["created_job_ids"][0],),
        )
        connection.commit()
    assert service.recover() == 1
    completed = asyncio.run(service.process_next())
    assert completed and completed["status"] == "completed"


def test_specialized_supply_processes_multiple_distinct_domains_in_one_batch(tmp_path: Path) -> None:
    database = prepared(tmp_path)
    gateway = FakeSupplyGateway()
    service = SpecializedDiagnosticSupplyService(database, gateway)
    queued = service.enqueue_gaps("child-primary-4", "competition_math", "batch-supply", 4)
    assert queued["created_count"] >= 3
    completed = asyncio.run(service.process_batch(3))
    assert len(completed) == 3
    assert len({item["domain_id"] for item in completed}) == 3
    assert all(item["status"] == "completed" for item in completed)


def test_parent_api_exposes_depth_and_durable_prepare_queue(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "app.db",
        data_encryption_key=b"s" * 32, maintenance_enabled=False,
    )
    with TestClient(create_app(settings, model_gateway=UnconfiguredGateway())) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2846"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        gaps = client.get(
            "/v1/parent/children/child-primary-4/specialized-supply/gaps?track=competition_math",
            headers=headers,
        )
        prepared = client.post(
            "/v1/parent/children/child-primary-4/specialized-supply/prepare",
            headers=headers,
            json={"track": "competition_math", "target_per_domain": 4, "client_action_id": "prepare-through-api"},
        )
        blocked = client.post("/v1/parent/specialized-supply/jobs/process-next", headers=headers)
        candidates = client.get(
            "/v1/parent/children/child-primary-4/specialized-supply/candidates", headers=headers,
        )
        health = client.get("/v1/parent/system-health", headers=headers)

    assert gaps.status_code == 200
    assert gaps.json()["total_domain_count"] == 10
    assert prepared.status_code == 200
    assert prepared.json()["created_count"] > 0
    assert blocked.status_code == 200
    assert blocked.json()["job"]["status"] == "blocked"
    assert candidates.status_code == 200
    assert candidates.json()["items"] == []
    assert health.status_code == 200
    assert health.json()["recent_content_generation_failures"][0]["source"] == "专项诊断"


def test_closed_model_supply_does_not_fake_pet_audio_or_writing_capacity(tmp_path: Path) -> None:
    database = prepared(tmp_path)
    gateway = FakeSupplyGateway()
    service = SpecializedDiagnosticSupplyService(database, gateway)

    before = service.blueprint_gaps("child-primary-4", "pet", "英语")
    assert before["required_item_count"] == 63
    reading = next(item for item in before["sections"] if item["section_id"] == "reading")
    listening = next(item for item in before["sections"] if item["section_id"] == "listening")
    assert reading["item_target"] == 32 and reading["deficit"] > 0
    assert listening["item_target"] == 25 and listening["deficit"] > 0
    assert before["ready"] is False

    queued = service.enqueue_blueprint_gaps(
        "child-primary-4", "pet", "英语", "prepare-pet-blueprint-full",
    )
    assert queued["created_count"] == 5
    assert len(queued["skipped"]) == 4
    assert all(item["domain_id"].startswith("pet.listening.") for item in queued["skipped"])
    assert {item["reason"] for item in queued["skipped"]} == {
        "SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED"
    }
    while True:
        completed = asyncio.run(service.process_batch(6))
        if not completed:
            break
    candidates = service.list("child-primary-4", "needs_review")["items"]
    assert candidates
    assert not any('.listening.' in item['domain_id'] or '.speaking.' in item['domain_id']
                   or '.writing.' in item['domain_id'] for item in candidates)
    assert any(
        sum(1 for item in candidates if item["domain_id"] == domain_id) > 3
        for domain_id in {item["domain_id"] for item in candidates}
    )
    for index, candidate in enumerate(candidates):
        service.review(
            candidate["candidate_id"], "approve", "蓝图题型与答案已核对",
            f"approve-pet-blueprint-{index}",
        )
    after = service.blueprint_gaps("child-primary-4", "pet", "英语")
    assert after["ready"] is False
    assert next(section for section in after['sections'] if section['section_id'] == 'listening')['deficit'] > 0
    assert not [job for job in service.list('child-primary-4')['jobs'] if job['status'] == 'blocked']


def test_parent_api_exposes_and_prepares_blueprint_readiness(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "blueprint-api.db",
        data_encryption_key=b"b" * 32, maintenance_enabled=False,
    )
    with TestClient(create_app(settings, model_gateway=UnconfiguredGateway())) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2846"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        readiness = client.get(
            "/v1/parent/children/child-primary-4/exam-blueprint-readiness",
            params={"track": "pet", "subject": "英语"}, headers=headers,
        )
        prepared_result = client.post(
            "/v1/parent/children/child-primary-4/exam-blueprint-readiness/prepare",
            headers=headers,
            json={
                "track": "pet", "subject": "英语",
                "client_action_id": "prepare-pet-blueprint-api",
            },
        )
    assert readiness.status_code == 200
    assert readiness.json()["required_item_count"] == 63
    assert prepared_result.status_code == 200
    assert prepared_result.json()["created_count"] == 5
    assert len(prepared_result.json()["skipped"]) == 4
    assert {item["reason"] for item in prepared_result.json()["skipped"]} == {
        "SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED"
    }
