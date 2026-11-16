from __future__ import annotations

import asyncio
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.child_model import ChildModelService
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from home_study_agent.variant_supply import VariantSupplyService


class FakeVariantGateway:
    configured = True

    async def complete(self, request) -> ModelResult:
        assert request.request_type == "mistake_variant_generation"
        assert request.context_manifest["raw_media"] is False
        assert request.context_manifest["child_identity"] is False
        candidates = [
            {
                "stage": "near_transfer",
                "prompt": "方程 x²+6x+9=0 的判别式是多少？说明不同实数根的个数。",
                "answer_type": "text",
                "expected_answer": {"keywords": ["0", "一个不同实数根"]},
                "explanation": "判别式36-36=0，所以两个根相等。",
                "difficulty": 1,
            },
            {
                "stage": "near_transfer",
                "prompt": "不解方程 x²-2x-3=0，只用判别式判断实数根的情况并写出依据。",
                "answer_type": "text",
                "expected_answer": {"keywords": ["16", "两个不同实数根"]},
                "explanation": "判别式4+12=16，大于0。",
                "difficulty": 2,
            },
            {
                "stage": "representation_transfer",
                "prompt": "已知二次方程的 a=3、b=2、c=1，不求根，判断它是否有实数解。",
                "answer_type": "text",
                "expected_answer": {"keywords": ["-8", "没有实数解"]},
                "explanation": "代入判别式得4-12=-8，小于0。",
                "difficulty": 2,
            },
            {
                "stage": "discrimination",
                "prompt": "小明说判别式等于0时方程没有实数根。这个结论对吗？给出正确结论。",
                "answer_type": "text",
                "expected_answer": {"keywords": ["不对", "两个相等实数根"]},
                "explanation": "判别式等于0表示有两个相等的实数根。",
                "difficulty": 2,
            },
        ]
        return ModelResult(
            content=json.dumps({"candidates": candidates}, ensure_ascii=False),
            provider="fake-deepseek", model="fake-variant-v1", latency_ms=18,
            prompt_tokens=220, completion_tokens=310,
        )


def _prepared(tmp_path: Path):
    database = Database(tmp_path / "variant.db")
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    mistakes = MistakeBookService(database)
    mistake = mistakes.capture(
        child_id="child-junior-3",
        knowledge_node_id="node-j3-math-discriminant",
        subject="数学",
        title="根的判别式",
        prompt_summary="方程 x²-4x+4=0 的判别式是多少？它有几个不同的实数根？",
        submitted_answer="判别式是4，有两个根",
        source_type="diagnostic",
        source_ref="diag-j3-discriminant-1",
        confidence=1.0,
        client_action_id="variant-capture-action",
        diagnostic_item_id="diag-j3-discriminant-1",
    )
    mistakes.confirm(
        mistake["mistake_id"], "child-junior-3", "confirm", "variant-confirm-action"
    )
    mistakes.diagnose(
        mistake["mistake_id"], "child-junior-3", "concept_gap",
        "没有正确理解判别式与根数量的关系。", .9, "variant-diagnose-action",
    )
    return database, mistakes, mistake["mistake_id"]


def test_variant_pipeline_requires_review_then_promotes_scored_items(tmp_path: Path) -> None:
    database, mistakes, mistake_id = _prepared(tmp_path)
    supply = VariantSupplyService(database, FakeVariantGateway())
    job = supply.enqueue(mistake_id, "child-junior-3", "variant-generation-action")
    assert job["status"] == "pending"
    completed = asyncio.run(supply.process_next())
    assert completed["status"] == "completed"
    assert completed["candidate_count"] == 4
    assert completed["provider"] == "fake-deepseek"

    queued = supply.list("child-junior-3", "needs_review")["items"]
    assert len(queued) == 4
    assert all(item["validation"]["passed"] for item in queued)
    with database.connect() as connection:
        unapproved = connection.execute(
            "SELECT COUNT(*) FROM diagnostic_item WHERE source_ref LIKE 'parent_approved_variant:%'"
        ).fetchone()[0]
    assert unapproved == 0

    with database.connect() as connection:
        connection.execute(
            """UPDATE diagnostic_item SET status = 'inactive'
               WHERE knowledge_node_id = 'node-j3-math-discriminant'
                 AND diagnostic_item_id != 'diag-j3-discriminant-1'"""
        )
        connection.commit()
    mistakes.add_remediation(
        mistake_id, "child-junior-3", "concept_explanation", "completed",
        "模拟补救完成", "variant-remediation-action",
    )
    waiting = mistakes.next_activity(mistake_id, "child-junior-3")
    assert waiting["kind"] == "awaiting_parent_review"
    assert waiting["candidate_count"] == 2

    promoted_ids = []
    for index, candidate in enumerate(queued):
        promoted = supply.review(
            candidate["candidate_id"], "approve", "题目与答案已人工核对。",
            f"variant-review-action-{index}",
        )
        assert promoted["status"] == "promoted"
        promoted_ids.append(promoted["diagnostic_item_id"])
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT diagnostic_item_id, status, source_ref FROM diagnostic_item
            WHERE diagnostic_item_id IN (?, ?, ?, ?)
            """,
            promoted_ids,
        ).fetchall()
    assert len(rows) == 4
    assert all(row["status"] == "active" for row in rows)
    assert all(row["source_ref"].startswith("parent_approved_variant:") for row in rows)

    activity = mistakes.next_activity(mistake_id, "child-junior-3")
    assert activity["kind"] == "attempt"
    assert activity["item"]["diagnostic_item_id"] != "diag-j3-discriminant-1"


def test_variant_jobs_recover_without_deleting_candidates(tmp_path: Path) -> None:
    database, _, mistake_id = _prepared(tmp_path)
    supply = VariantSupplyService(database, FakeVariantGateway())
    job = supply.enqueue(mistake_id, "child-junior-3", "variant-recovery-action")
    with database.connect() as connection:
        connection.execute(
            "UPDATE variant_generation_job SET status = 'processing' WHERE job_id = ?",
            (job["job_id"],),
        )
        connection.commit()
    restarted = VariantSupplyService(database, FakeVariantGateway())
    assert restarted.recover() == 1
    resumed = asyncio.run(restarted.process_next())
    assert resumed["status"] == "completed"
    assert len(restarted.list("child-junior-3")["items"]) == 4


def test_new_learning_models_are_exported_and_deleted_as_one_child_scope(tmp_path: Path) -> None:
    database, _, mistake_id = _prepared(tmp_path)
    goals = GoalProgramService(database)
    goals.seed_defaults()
    junior_cycles = goals.cycles("child-junior-3")
    assert junior_cycles
    ChildModelService(database).snapshot("child-junior-3")

    supply = VariantSupplyService(database, FakeVariantGateway())
    supply.enqueue(mistake_id, "child-junior-3", "variant-privacy-action")
    asyncio.run(supply.process_next())

    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = privacy.export_child("child-junior-3")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert payload["goal_programs"]
    assert payload["goal_cycles"]
    assert payload["child_model_snapshots"]
    assert payload["variant_generation_jobs"]
    assert payload["variant_candidates"]

    preview = privacy.deletion_preview("child-junior-3")
    assert preview["goal_programs"] >= 2
    assert preview["child_model_snapshots"] == 1
    assert preview["variant_supply"] == 5

    deleted = privacy.delete_child_scope(
        "child-junior-3", "learning", "确认删除", "delete-new-learning-models"
    )
    assert deleted["status"] == "completed"
    with database.connect() as connection:
        for table in (
            "goal_program", "goal_cycle", "child_model_snapshot",
            "variant_generation_job", "variant_candidate",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE child_id = 'child-junior-3'"
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM goal_program WHERE child_id = 'child-primary-4'"
        ).fetchone()[0] > 0


def test_variant_supply_checks_dedicated_capability_instead_of_general_gateway(tmp_path: Path) -> None:
    database, _, mistake_id = _prepared(tmp_path)

    class DedicatedGateway(FakeVariantGateway):
        configured = False

        def configured_for(self, capability):
            assert capability == 'mistake_variant_generation'
            return True

    supply = VariantSupplyService(database, DedicatedGateway())
    supply.enqueue(mistake_id, 'child-junior-3', 'variant-dedicated-action')
    assert asyncio.run(supply.process_next())['status'] == 'completed'


def test_variant_supply_missing_dedicated_config_blocks_without_spending_attempt(tmp_path: Path) -> None:
    database, _, mistake_id = _prepared(tmp_path)

    class MissingDedicatedGateway(FakeVariantGateway):
        configured = True

        def configured_for(self, capability):
            assert capability == 'mistake_variant_generation'
            return False

        async def complete(self, request):
            raise AssertionError('must not dispatch without dedicated configuration')

    supply = VariantSupplyService(database, MissingDedicatedGateway())
    job = supply.enqueue(mistake_id, 'child-junior-3', 'variant-missing-action')
    blocked = asyncio.run(supply.process_next())
    assert blocked['status'] == 'blocked'
    assert blocked['error_code'] == 'MODEL_NOT_CONFIGURED'
    with database.connect() as connection:
        row = connection.execute('SELECT attempt_count, retry_count FROM variant_generation_job WHERE job_id=?', (job['job_id'],)).fetchone()
    assert tuple(row) == (0, 0)
