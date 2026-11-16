from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path

import pytest

from home_study_agent.child_analysis_review import ChildAnalysisReviewService
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.model_gateway import ModelResult
from home_study_agent.repository import StudyRepository


CHILD = "child-primary-4"


class Gateway:
    configured = True

    def __init__(self, content: dict):
        self.content = content
        self.requests = []

    async def complete(self, request):
        if request.pre_dispatch:
            request.pre_dispatch()
        self.requests.append(request)
        return ModelResult(
            json.dumps(self.content, ensure_ascii=False), "synthetic", "analysis-model", 12,
            prompt_tokens=40, completion_tokens=30,
        )


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "child-analysis.db")
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    LearningStateService(database).seed_catalog()
    return database


def _completed_session(database: Database) -> str:
    repository = StudyRepository(database)
    session_id = repository.start_session(CHILD, "analysis-session-start")["session_id"]
    now = datetime.now(timezone.utc).isoformat()
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET status='completed',state='completed',ended_at=?,last_checkpoint_at=? WHERE session_id=?",
            (now, now, session_id),
        )
        connection.execute(
            """INSERT INTO classroom_recall(recall_id,session_id,subject,summary,confidence,
                 confirmation_status,source_refs_json,created_at,client_action_id)
               VALUES('analysis-recall',?,'数学','今天学习了整数四则运算',.8,'confirmed','[]',?,
                 'analysis-recall-action')""", (session_id, now),
        )
        connection.execute(
            """INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,
                 expected_minutes,confirmation_status,status,created_at,client_action_id)
               VALUES('analysis-homework',?,?, '数学','exercise','练习册第12页',20,'confirmed',
                 'completed',?,'analysis-homework-action')""", (CHILD, session_id, now),
        )
        connection.execute(
            """INSERT INTO daily_reflection(reflection_id,session_id,child_id,learned_text,
                 difficult_text,feeling,next_action,created_at,client_action_id)
               VALUES('analysis-reflection',?,?,'我学会了列竖式','容易漏掉检查','一般',
                 '明天先检查一道题',?,'analysis-reflection-action')""", (session_id, CHILD, now),
        )
        connection.execute(
            """INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,
                 generator,version,created_at,client_action_id)
               VALUES('analysis-plan',?,?,substr(?,1,10),'completed',20,'test',1,?,
                 'analysis-plan-action')""",
            (CHILD, session_id, now, now),
        )
        connection.execute(
            """INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,
                 expected_minutes,actual_minutes,priority,status,position,source_type,source_ref,
                 completion_standard,cognitive_load,task_type,completion_evidence_type,
                 completion_note)
               VALUES('analysis-plan-item','analysis-plan','analysis-homework','数学','练习册第12页',
                 20,18,1,'completed',1,'school_homework','analysis-homework','完成并检查',
                 'medium','written','child_checked','检查时发现一处遗漏')"""
        )
        connection.commit()
    return session_id


def _response() -> dict:
    return {
        "task_type": "daily_learning_review",
        "scope": {"subject": "数学", "period": "today"},
        "claims": [{
            "claim": "检查步骤是否稳定仍需用新题核实。",
            "evidence_refs": ["snapshot"],
            "counterevidence_refs": [],
        }],
        "confidence": .65,
        "uncertainty_reasons": ["仅有当天证据"],
        "valid_days": 7,
        "verification_actions": ["安排一道不同题面的无提示检查"],
        "proposed_actions": [{"action_type": "schedule_check", "rationale": "先核实"}],
        "expected_outcomes": ["获得独立作答证据"],
        "stop_conditions": ["孩子拒绝或当天负荷已满"],
        "rollback_action": "取消待验证项，不改变掌握状态。",
    }


def test_completed_session_runs_minimal_cloud_review_and_persists_validated_candidate(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id = _completed_session(database)
    gateway = Gateway(_response())
    service = ChildAnalysisReviewService(
        database, gateway, StudyRepository(database), cloud_text_allowed=lambda: True,
    )
    job = service.enqueue_for_session(session_id)

    result = asyncio.run(service.process_next())

    assert result["status"] == "completed"
    assert result["candidate_id"]
    request = gateway.requests[0]
    wire = json.loads(request.messages[-1]["content"])
    serialized = json.dumps(wire, ensure_ascii=False)
    assert "孩子四年级" not in serialized
    assert CHILD not in serialized
    assert "raw_audio" not in serialized and "raw_video" not in serialized
    assert wire["evidence_aliases"] == {"snapshot": "current_child_state_snapshot"}
    assert wire["classroom_recall"][0]["summary"] == "今天学习了整数四则运算"
    assert wire["plan_execution"][0]["status"] == "completed"
    assert wire["plan_execution"][0]["completion_note"] == "检查时发现一处遗漏"
    assert request.context_manifest["raw_media"] is False
    assert request.context_manifest["child_identity"] is False

    restarted = ChildAnalysisReviewService(
        Database(database.path), gateway, StudyRepository(Database(database.path)),
        cloud_text_allowed=lambda: True,
    ).job(job["job_id"])
    assert restarted["status"] == "completed"
    snapshot = ChildModelService(Database(database.path)).snapshot(CHILD)
    assert any(item["source"] == "validated_cloud_analysis_candidate"
               for item in snapshot["payload"]["open_hypotheses"])
    with database.connect() as connection:
        call = connection.execute(
            "SELECT status,verification_status,provider,model FROM model_call WHERE purpose='child_daily_analysis'"
        ).fetchone()
    assert tuple(call) == ("completed", "partially_verified", "synthetic", "analysis-model")


def test_invalid_cloud_candidate_retries_without_polluting_child_state(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id = _completed_session(database)
    response = _response()
    response["mastery_probability"] = .99
    gateway = Gateway(response)
    service = ChildAnalysisReviewService(
        database, gateway, StudyRepository(database), cloud_text_allowed=lambda: True,
    )
    job = service.enqueue_for_session(session_id)

    first = asyncio.run(service.process_next())
    assert first["status"] == "pending"
    assert first["attempt_count"] == 1
    assert first["error_code"] == "invalid_model_protocol"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analysis_candidate").fetchone()[0] == 0
    assert all(item.get("source") != "validated_cloud_analysis_candidate"
               for item in ChildModelService(database).snapshot(CHILD)["payload"]["open_hypotheses"])

    asyncio.run(service.process_next())
    blocked = asyncio.run(service.process_next())
    assert blocked["status"] == "blocked"
    assert blocked["attempt_count"] == 3
    assert service.job(job["job_id"])["status"] == "blocked"


def test_cloud_disabled_leaves_job_pending_and_recovery_releases_interrupted_claim(tmp_path: Path) -> None:
    database = _database(tmp_path)
    session_id = _completed_session(database)
    gateway = Gateway(_response())
    service = ChildAnalysisReviewService(
        database, gateway, StudyRepository(database), cloud_text_allowed=lambda: False,
    )
    job = service.enqueue_for_session(session_id)
    assert asyncio.run(service.process_next()) is None
    assert gateway.requests == []

    with database.connect() as connection:
        connection.execute(
            "UPDATE child_analysis_review_job SET status='processing',owner_token='lost' WHERE job_id=?",
            (job["job_id"],),
        )
        connection.commit()
    assert service.recover() == 1
    assert service.job(job["job_id"])["status"] == "pending"
