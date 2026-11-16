"""Synthetic multiday integration; real SQLite/services, not measured child outcomes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from home_study_agent import (
    child_model, goal_program, goal_roadmap, learning_feedback, learning_obligations,
    learning_state, local_supply, mistake_book, repository, specialized_supply,
    strategy_evolution,
)
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from home_study_agent.strategy_evolution import STUCK_VERSION, StrategyEvolutionService


CHILD = "child-junior-3"
OTHER = "child-primary-4"
NODE = "node-j3-math-discriminant"


def _services(path):
    db = Database(path)
    return SimpleNamespace(db=db, repo=StudyRepository(db), learning=LearningStateService(db),
                           model=ChildModelService(db), goals=GoalProgramService(db),
                           strategies=StrategyEvolutionService(db), mistakes=MistakeBookService(db),
                           supply=SpecializedDiagnosticSupplyService(db))


@pytest.fixture
def journey(tmp_path, monkeypatch):
    clock = [datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
             - timedelta(days=9)]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return clock[0].date()

    # Only clocks are substituted; qualification, scoring and projections stay real.
    for module in (repository, learning_state, child_model, goal_program, goal_roadmap, strategy_evolution,
                   learning_feedback, learning_obligations, local_supply, specialized_supply, mistake_book):
        if hasattr(module, "datetime"):
            monkeypatch.setattr(module, "datetime", FrozenDatetime)
        if hasattr(module, "date"):
            monkeypatch.setattr(module, "date", FrozenDate)
    service = _services(tmp_path / "adaptation-journey.db")
    service.db.initialize()
    service.repo.seed_family()
    service.learning.seed_catalog()
    service.supply.enqueue_local("diag-j3-discriminant-1", CHILD, "journey-local-items", count=3)
    service.supply.process_local_pending()
    items = []
    for candidate in service.supply.list(CHILD)["items"]:
        qualified = service.supply.qualify_local(candidate["candidate_id"], CHILD, str(uuid4()))
        with service.db.connect() as connection:
            item = dict(connection.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?",
                                           (qualified["diagnostic_item_id"],)).fetchone())
        assert json.loads(item["expected_answer_json"])["local_rule"] == "local_exact_v1"
        assert item["knowledge_node_id"] == NODE
        items.append(item)
    assert len({item["diagnostic_item_id"] for item in items}) == 3
    return service, clock, items


def _start_day(service, *, declared_recall=False):
    sid = service.repo.start_session(CHILD, str(uuid4()))["session_id"]
    service.repo.transition(sid, "check_in", "classroom_recall", "synthetic check-in", str(uuid4()))
    if declared_recall:
        service.repo.add_recall(sid, "数学", "今天课堂复习了二次方程，需要安排后续诊断。", str(uuid4()))
    else:
        service.repo.declare_empty_stage(sid, "no_recall", str(uuid4()))
    service.repo.transition(sid, "classroom_recall", "homework_capture", "synthetic recall", str(uuid4()))
    service.repo.declare_empty_stage(sid, "no_homework", str(uuid4()))
    service.supply.bind_due_reviews(CHILD, limit=50)
    workspace = service.repo.generate_plan(sid, str(uuid4()))
    service.repo.transition(sid, "plan_review", "study", "synthetic plan confirmation", str(uuid4()))
    return sid, workspace


def _end_day(service, sid):
    for item in service.repo.get_workspace(sid)["plan_items"]:
        if item["status"] in {"pending", "paused"}:
            service.repo.reschedule_plan_item(item["plan_item_id"], "Synthetic day ended; work remains scheduled", str(uuid4()))
    service.repo.transition(sid, "study", "reflection", "synthetic day end", str(uuid4()))
    service.repo.complete_with_reflection(sid, "", "", "", "", True, str(uuid4()))
    assert service.repo.get_session(sid)["status"] == "completed"


def _observe(service, status, expected_refs):
    cycle = service.goals.cycles(CHILD)[0]
    snapshot = service.model.snapshot(CHILD)
    node = next(node for node in snapshot["payload"]["learning"]["reliable_feedback"]["nodes"]
                if node["knowledge_node_id"] == NODE)
    assert node["status"] == cycle["learning_outcome"]["status"] == status
    assert set(node["evidence_refs"]) == set(cycle["focus"]["evidence_refs"]) == set(expected_refs)
    assert cycle["focus"]["next_action"] == node["next_action"]
    assert cycle["learning_outcome"]["goal_attainment"] == "not_established"
    assert node["mastery_claim_allowed"] is False
    assert service.model.snapshot(OTHER)["payload"]["learning"]["reliable_feedback"]["nodes"] == []
    return snapshot, node, cycle


def _plan_evidence(service, sid):
    with service.db.connect() as connection:
        row = connection.execute(
            """SELECT pcd.metadata_json FROM plan_constraint_decision pcd
            JOIN daily_plan dp ON dp.plan_id=pcd.plan_id
            WHERE dp.session_id=? AND dp.status='active' AND pcd.obligation_type='learning_feedback'""", (sid,),
        ).fetchone()
    return json.loads(row[0])


def _teaching_receipt(service, sid, item, clock):
    # Teaching happens after the preceding answer, not at its frozen timestamp.
    clock[0] += timedelta(minutes=1)
    context = service.strategies.tutor_context(CHILD, 2)
    receipt = service.repo.save_tutor_turn(
        sid, None, "数学", item["prompt"], "Synthetic teaching execution receipt; no model-quality claim.",
        "fixture", "fixture", str(uuid4()), hint_level=2,
    )
    service.strategies.record_tutor_turn(receipt["turn_id"], CHILD, context, "passed")
    clock[0] += timedelta(minutes=1)
    return context, receipt


def _answer_review(service, sid, source_id, interval, *, hint_level=0):
    with service.db.connect() as connection:
        review = connection.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label=?",
                                    (source_id, interval)).fetchone()
        assert review and review["status"] == "pending"
        item = connection.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
                                  (review["diagnostic_item_id"],)).fetchone()
    rubric = json.loads(item[0])
    assert rubric["local_rule"] == "local_exact_v1", "Review supply did not bind a qualified exact item"
    planned = next((item for item in service.repo.get_workspace(sid)["plan_items"]
                    if item["source_type"] == "due_review" and item["source_ref"] == review["review_task_id"]), None)
    if planned:
        service.repo.start_plan_item(planned["plan_item_id"], str(uuid4()))
    attempt = service.learning.submit_diagnostic(sid, review["diagnostic_item_id"], rubric["exact_answers"][0],
                                                str(uuid4()), review["review_task_id"], hint_level=hint_level)
    assert attempt["correctness"] == 1
    if planned:
        completed = service.repo.complete_plan_item(planned["plan_item_id"], 1, str(uuid4()))
        assert completed["status"] == "completed"
    return attempt


@pytest.mark.parametrize("sources_remain", [True, False])
def test_multiday_evidence_adaptation_survives_restart_and_withdrawal(journey, sources_remain):
    service, clock, items = journey
    sid, _ = _start_day(service)
    first = service.learning.submit_diagnostic(sid, items[0]["diagnostic_item_id"], "不知道", "initial-wrong")
    assert first["correctness"] == 0
    refs = [first["evidence_id"]]
    _observe(service, "recheck_needed", refs)

    candidate = service.strategies.maybe_propose(CHILD)
    eid = candidate["experiment_id"]
    assert candidate["candidate_version"] == STUCK_VERSION
    assert candidate["candidate_config"]["selection"]["evidence_kind"] == "qualified_diagnostic_answer"
    service.strategies.run_replay(eid)
    service.strategies.start_shadow(eid)
    shadow_receipts = []
    for _ in range(3):
        context, shadow_receipt = _teaching_receipt(service, sid, items[0], clock)
        shadow_receipts.append(shadow_receipt["turn_id"])
        assert context is not None
        assert context["prompt_extension"] is None
    trial = service.strategies.activate(eid)
    assert trial["shadow_metrics"]["learning_gain"] is None
    assert trial["shadow_metrics"]["candidate_executed_turns"] == 0
    context, receipt = _teaching_receipt(service, sid, items[0], clock)
    assert context["prompt_extension"]
    service.repo.save_tutor_feedback(receipt["turn_id"], "我明白了", "understood", "self-report")
    service.strategies.record_tutor_feedback(receipt["turn_id"], "understood")

    failed = service.learning.submit_diagnostic(sid, items[1]["diagnostic_item_id"], "不知道", "new-independent-failure")
    assert failed["correctness"] == 0
    with service.db.connect() as connection:
        condition = connection.execute("SELECT independent,hint_level,occurred_at FROM learning_evidence WHERE evidence_id=?",
                                       (failed["evidence_id"],)).fetchone()
        taught_at = connection.execute("SELECT created_at FROM tutor_turn WHERE turn_id=?", (receipt["turn_id"],)).fetchone()[0]
    assert tuple(condition)[:2] == (1, 0)
    assert datetime.fromisoformat(condition["occurred_at"]) > datetime.fromisoformat(taught_at)
    rolled_back = service.strategies.list_for_child(CHILD)[0]
    assert rolled_back["status"] == "rolled_back"
    check = rolled_back["active_metrics"]["learning_evidence"]["checks"][0]
    assert check["evidence_ref"] == failed["evidence_id"]
    assert check["strategy_version"] == candidate["candidate_version"]
    assert check["check_kind"] == "new_item_check"
    refs.append(failed["evidence_id"])
    before_restart, _, _ = _observe(service, "recheck_needed", refs)
    service = _services(service.db.path)
    assert service.model.snapshot(CHILD)["snapshot_id"] == before_restart["snapshot_id"]
    assert service.strategies.get(eid)["status"] == "rolled_back"
    assert service.strategies.tutor_context(CHILD, 2) is None
    _end_day(service, sid)

    clock[0] += timedelta(hours=25)
    sid, plan = _start_day(service)
    assert _plan_evidence(service, sid)["nodes"][0]["status"] == "recheck_needed"
    assert any(item["source_type"] == "due_review" for item in plan["plan_items"])
    hinted = _answer_review(service, sid, first["evidence_id"], "immediate", hint_level=2)
    refs.append(hinted["evidence_id"])
    _, supported, _ = _observe(service, "supported_only", refs)
    assert supported["supported_attempt_count"] == 1
    assert supported["independent_correct"] == 0
    with service.db.connect() as connection:
        projection = learning_obligations.project_mastery_evidence(connection, CHILD, NODE, as_of=clock[0].isoformat())
    effective = {row["evidence_id"]: row for row in projection["effective_evidence"]}
    assert {ref for ref, row in effective.items() if row["hint_level"] or not row["independent"]} == {
        hinted["evidence_id"],
    }
    # Subsequent teaching must not retroactively assist the original wrong answer.
    assert effective[first["evidence_id"]]["independent"] == 1
    assert effective[first["evidence_id"]]["tutor_refs"] == []
    with service.db.connect() as connection:
        shadow_times = {row["turn_id"]: datetime.fromisoformat(row["created_at"]) for row in connection.execute(
            "SELECT turn_id,created_at FROM tutor_turn WHERE turn_id IN (?,?,?)", shadow_receipts)}
    assert set(shadow_times) == set(shadow_receipts)
    answer_time = datetime.fromisoformat(effective[first["evidence_id"]]["occurred_at"])
    assert all(answer_time < occurred for occurred in shadow_times.values())
    assert effective[hinted["evidence_id"]]["hint_level"] == 2
    assert effective[failed["evidence_id"]]["independent"] == 1
    _end_day(service, sid)

    clock[0] += timedelta(hours=25)
    sid, _ = _start_day(service)
    assert _plan_evidence(service, sid)["nodes"][0]["next_action"] == "fade_hint_then_check"
    success = _answer_review(service, sid, first["evidence_id"], "48h")
    refs.append(success["evidence_id"])
    _, independent, _ = _observe(service, "independent_observed", refs)
    assert independent["independent_correct"] == 1
    assert independent["retention_success_count"] == 0
    _end_day(service, sid)

    clock[0] += timedelta(hours=49)
    sid, _ = _start_day(service)
    retained = _answer_review(service, sid, failed["evidence_id"], "48h")
    refs.append(retained["evidence_id"])
    _, observation, _ = _observe(service, "retention_observed", refs)
    assert observation["retention_success_count"] == 1
    assert observation["retention_basis_refs"] == [success["evidence_id"]]
    assert service.strategies.get(eid)["effectiveness_status"] == "unverified"
    _end_day(service, sid)

    clock[0] += timedelta(hours=25)
    sid, new_plan = _start_day(service)
    decision = _plan_evidence(service, sid)["nodes"][0]
    assert decision["status"] == "retention_observed"
    assert decision["next_action"] == "check_transfer"
    assert set(decision["evidence_refs"]) == set(refs)
    cycle = service.goals.cycles(CHILD)[0]
    action = service.goals.next_daily_action(CHILD)
    assert cycle["learning_outcome"]["next_action"] == decision["next_action"]
    # Reliable retention for NODE can move to another prerequisite only when
    # the expanded reviewed catalog supplies a concrete source-bound item.
    pending = cycle["roadmap"]["next_daily_action"]
    assert pending and pending["knowledge_node_id"] != NODE
    assert pending["action"] == "establish_baseline"
    assert pending["status"] == "suggested" and pending["dispatchable"]
    assert action is not None
    assert all(action[key] == pending[key] for key in (
        "action", "knowledge_node_id", "status", "dispatchable"
    ))
    # The plan was generated before the new roadmap decision in this journey,
    # so only the already-bound review belongs to this session.
    assert any(item["source_type"] == "due_review" for item in new_plan["plan_items"])
    assert not any(item["source_type"] == "junior_exam_goal" for item in new_plan["plan_items"])
    with service.db.connect() as connection:
        reviews = connection.execute("""SELECT rt.* FROM plan_item pi
            JOIN daily_plan dp USING(plan_id) JOIN review_task rt ON rt.review_task_id=pi.source_ref
            WHERE dp.session_id=? AND pi.source_type='due_review'""", (sid,)).fetchall()
    assert reviews and all(r["child_id"] == CHILD and r["knowledge_node_id"] == NODE
                           and r["source_evidence_id"] in refs and r["diagnostic_item_id"] for r in reviews)

    # Create the scored mistake records only when this journey reaches its
    # source-withdrawal phase. Earlier days intentionally exercise the generic
    # review/strategy path; a diagnosed mistake has its own higher-priority
    # remediation plan and must not be treated as an interchangeable due review.
    first_case = service.mistakes.capture_from_attempt(first)
    failed_case = service.mistakes.capture_from_attempt(failed)
    # Real exclusion withdraws both roots and their scored review descendants.
    for case in (first_case, failed_case):
        service.mistakes.confirm(case["mistake_id"], CHILD, "exclude", str(uuid4()))
    for _ in range(2):
        service = _services(service.db.path)
        service.learning.replay_mastery(CHILD)
        clean = service.model.snapshot(CHILD)
        assert clean["payload"]["learning"]["reliable_feedback"]["nodes"] == []
        goal = service.goals.cycles(CHILD)[0]
        assert goal["learning_outcome"]["status"] == "insufficient_evidence"
        assert goal["focus"]["evidence_refs"] == []
        strategy = service.strategies.get(eid)
        assert strategy["status"] == "rolled_back"
        assert strategy["active_metrics"]["learning_evidence"]["checks"] == []
        assert service.strategies.tutor_context(CHILD, 2) is None
    with service.db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt WHERE child_id=?", (CHILD,)).fetchone()[0] == 5
        assert connection.execute("SELECT count(*) FROM review_task WHERE child_id=? AND status='pending'", (CHILD,)).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    _end_day(service, sid)
    clock[0] += timedelta(hours=25)
    if not sources_remain:
        with service.db.connect() as connection:
            connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE knowledge_node_id=?", (NODE,))
            connection.commit()
    sid, after_withdrawal = _start_day(service, declared_recall=True)
    assert _plan_evidence(service, sid)["nodes"] == []
    assert not any(item["source_type"] in {"due_review", "mistake_review"} for item in after_withdrawal["plan_items"])
    baseline_cycle = service.goals.cycles(CHILD)[0]
    assert baseline_cycle["learning_outcome"]["status"] == "insufficient_evidence"
    baseline = baseline_cycle["roadmap"]["next_daily_action"]
    assert baseline["action"] == "establish_baseline"
    assert baseline["mastery_claim_allowed"] is False
    planned = [item for item in after_withdrawal["plan_items"] if item["source_type"] == "junior_exam_goal"]
    if sources_remain:
        # Withdrawing scored evidence does not withdraw independently qualified
        # questions. A new baseline is legitimate, never a retained-mastery claim.
        assert baseline["dispatchable"] and len(planned) == 1
        assert baseline["knowledge_node_id"] == NODE
        resolver = goal_roadmap.GoalRoadmapService(service.db)
        resolved = resolver.resolve_execution(sid, planned[0]["plan_item_id"])
        assert resolved["status"] == "ready", resolved
        with service.db.connect() as connection:
            selected = local_supply.validate_item(connection, CHILD, resolved["diagnostic_item_id"])
        assert selected["knowledge_node_id"] == NODE
        assert resolved["binding"]["action"] == "establish_baseline"
        assert goal_roadmap.GoalRoadmapService(Database(service.db.path)).resolve_execution(
            sid, planned[0]["plan_item_id"]) == resolved
    else:
        assert not planned
        assert baseline["status"] == "qualified_source_needed" and not baseline["dispatchable"]
        assert "qualified_question_missing" in {risk["code"] for risk in baseline_cycle["roadmap"]["risks"]}
    with service.db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt WHERE child_id=?", (CHILD,)).fetchone()[0] == 5
        assert connection.execute("SELECT count(*) FROM review_task WHERE child_id=? AND status='pending'", (CHILD,)).fetchone()[0] == 0
    assert service.model.snapshot(OTHER)["payload"]["learning"]["reliable_feedback"]["nodes"] == []
