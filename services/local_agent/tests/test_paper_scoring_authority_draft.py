from __future__ import annotations

import asyncio
import json
from pathlib import Path

from home_study_agent.model_gateway import ModelResult
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_supply import (
    PaperScoringAuthorityDraftService,
    ensure_schema,
)
from home_study_agent.repository import StudyRepository

from test_answer_attempt_ledger import setup_case


class DraftGateway:
    configured = True

    def __init__(self, payload: dict):
        self.payload = payload
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        request.pre_dispatch()
        return ModelResult(
            content=json.dumps(self.payload, ensure_ascii=False),
            provider="fixture", model="fixture-draft", latency_ms=4,
            prompt_tokens=21, completion_tokens=13,
        )


def pending_request(tmp_path: Path):
    database, monitor = setup_case(tmp_path)
    monitor.record_revision("study", "page", "page-signature", "draft-revision")
    with database.connect() as connection:
        candidate = connection.execute(
            "SELECT candidate_id,question_text FROM mistake_capture_candidate"
        ).fetchone()
    request = PaperScoringService(database).request(
        candidate_id=candidate["candidate_id"], child_id="child", session_id="study",
        question_text=candidate["question_text"], answer_text="先列式，再说明理由",
        answer_source="child_spoken", visual_answer_evidence_id=None,
        client_action_id="paper-draft-request",
    )
    return database, request


def valid_payload():
    return {
        "subject": "数学", "answer_mode": "subjective",
        "task_points": ["列出已知量与未知量", "说明所用数量关系"],
        "source_search_terms": ["对应练习册题号", "本章参考答案与评分说明"],
        "confidence": 0.82, "requires_external_source": True,
    }


def test_cloud_draft_is_persisted_but_never_becomes_scoring_authority(tmp_path: Path) -> None:
    database, request = pending_request(tmp_path)
    gateway = DraftGateway(valid_payload())
    service = PaperScoringAuthorityDraftService(
        database, gateway, StudyRepository(database), lambda: True,
    )

    result = asyncio.run(service.process_next())

    assert result["status"] == "completed"
    assert result["provider"] == "fixture"
    current = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert current["decision_id"] is None
    assert current["authority_supply"]["status"] == "rubric_candidate"
    assert current["authority_supply"]["latest_evidence"]["authority_granted"] is False
    assert current["authority_supply"]["latest_evidence"]["correctness_decided"] is False
    assert current["authority_supply"]["supply_work"]["status"] == "completed"

    sent = json.loads(gateway.requests[0].messages[1]["content"])
    assert set(sent) == {"grade", "curriculum_region", "question", "student_answer"}
    assert "nickname" not in sent
    assert "image" not in gateway.requests[0].messages[1]["content"]
    with database.connect() as connection:
        model_call = connection.execute(
            "SELECT * FROM model_call WHERE purpose='paper_scoring_authority_draft'"
        ).fetchone()
    assert model_call["status"] == "completed"
    assert model_call["verification_status"] == "partially_verified"


def test_invalid_draft_retries_then_blocks_without_deciding_correctness(tmp_path: Path) -> None:
    database, request = pending_request(tmp_path)
    payload = {**valid_payload(), "correctness": True}
    service = PaperScoringAuthorityDraftService(
        database, DraftGateway(payload), StudyRepository(database), lambda: True,
    )

    first = asyncio.run(service.process_next())
    second = asyncio.run(service.process_next())
    third = asyncio.run(service.process_next())

    assert first["status"] == "pending"
    assert second["status"] == "pending"
    assert third["status"] == "blocked"
    assert third["attempt_count"] == 3
    current = PaperScoringService(database).for_candidate(request["candidate_id"], "child")
    assert current["decision_id"] is None
    assert current["authority_supply"]["status"] == "blocked_source"


def test_disabled_cloud_does_not_claim_or_consume_attempt(tmp_path: Path) -> None:
    database, _ = pending_request(tmp_path)
    gateway = DraftGateway(valid_payload())
    service = PaperScoringAuthorityDraftService(
        database, gateway, StudyRepository(database), lambda: False,
    )

    assert asyncio.run(service.process_next()) is None
    assert gateway.requests == []
    with database.connect() as connection:
        ensure_schema(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_scoring_supply_work"
        ).fetchone()[0] == 0


def test_concurrent_workers_claim_one_obligation_once(tmp_path: Path) -> None:
    database, _ = pending_request(tmp_path)
    gateway = DraftGateway(valid_payload())
    service = PaperScoringAuthorityDraftService(
        database, gateway, StudyRepository(database), lambda: True,
    )

    async def run_workers():
        return await asyncio.gather(
            service.process_next(), service.process_next(), service.process_next()
        )

    outcomes = asyncio.run(run_workers())

    assert len(gateway.requests) == 1
    assert len([item for item in outcomes if item]) == 1


def test_stale_processing_claim_recovers_without_losing_obligation(tmp_path: Path) -> None:
    database, request = pending_request(tmp_path)
    service = PaperScoringAuthorityDraftService(
        database, DraftGateway(valid_payload()), StudyRepository(database), lambda: True,
    )
    service.ensure_schema()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        obligation = connection.execute(
            "SELECT * FROM paper_scoring_supply_obligation WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()
        connection.execute(
            """INSERT INTO paper_scoring_supply_work(
                 obligation_id,child_id,status,attempt_count,owner_token,claimed_at,updated_at
               ) VALUES(?,?,'processing',1,'dead-worker','2000-01-01T00:00:00+00:00',
                        '2000-01-01T00:00:00+00:00')""",
            (obligation["obligation_id"], "child"),
        )
        connection.commit()

    assert service.recover() == 1
    recovered = service.work(obligation["obligation_id"])
    assert recovered["status"] == "pending"
    assert recovered["attempt_count"] == 1
