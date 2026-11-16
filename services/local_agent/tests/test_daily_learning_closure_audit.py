import asyncio
import json
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import StudyRepository, PlanInputError
from home_study_agent import learning_state as module
from test_daily_learning_closure import session as original_session, finish
from test_due_review_plan_completion import freeze_clock
from test_agent_runtime import seed_session as original_seed_session
from test_learning_outcome_evaluator import EvaluationGateway, persist_evaluation_turn as original_evaluation_turn


CHILD = "child-primary-4"
OTHER = "child-junior-3"
ITEM = "diag-p4-arithmetic-closed-1"


@pytest.fixture
def learning_clock(monkeypatch):
    clock = [(datetime.now(timezone.utc) - timedelta(days=3)).isoformat()]
    freeze_clock(monkeypatch, clock)
    for name in ("agent_runtime", "agent_orchestrator", "agent_capability_executor", "learning_outcome_evaluator"):
        monkeypatch.setattr(import_module(f"home_study_agent.{name}"), "datetime", module.datetime)
    return clock


def learning_day(clock, offset=0):
    instant = datetime.fromisoformat(clock[0]) + timedelta(days=offset)
    return instant.astimezone(timezone(timedelta(hours=8))).date().isoformat()


def advance_day(clock):
    clock[0] = (datetime.fromisoformat(clock[0]) + timedelta(days=1)).isoformat()


@pytest.fixture
def db(tmp_path, learning_clock):
    database = Database(tmp_path / "closure.db")
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    return database


def session(db, child=CHILD):
    return original_session(db, child)


def seed_session(db):
    sid = original_seed_session(db)
    # The imported helper uses SQLite datetime('now'), outside the Python clock.
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET started_at=?,last_checkpoint_at=? WHERE session_id=?",
                           (module.utc_now(), module.utc_now(), sid))
        connection.commit()
    return sid


def persist_evaluation_turn(db, sid, turn_id, *args):
    result = original_evaluation_turn(db, sid, turn_id, *args)
    with db.connect() as connection:
        connection.execute("UPDATE agent_turn SET created_at=? WHERE agent_turn_id=?", (module.utc_now(), turn_id))
        connection.commit()
    return result


def wrong(db, sid):
    result = LearningStateService(db).submit_diagnostic(sid, ITEM, "12", str(uuid4()))
    with db.connect() as connection:
        from home_study_agent.learning_obligations import project_mastery_evidence

        evidence = project_mastery_evidence(connection, CHILD, "node-p4-math-arithmetic")["effective_evidence"]
        assert any(row["evidence_id"] == result["evidence_id"] and row["correctness"] == 0 for row in evidence)
    return result


def set_state(db, sid, state):
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state=?,homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (state, sid))
        connection.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120")
        connection.commit()


def make_plan(db, sid):
    set_state(db, sid, "homework_capture")
    return StudyRepository(db).generate_plan(sid, str(uuid4()))


def test_reflection_fields_survive_daily_dedup(db):
    sid = session(db)
    set_state(db, sid, "reflection")
    StudyRepository(db).complete_with_reflection(sid, "今天学了方程", "符号还要核对", "", "再做一题", False, "three-fields")
    result = LearningStateService(db).daily_learning_summary(CHILD)
    fields = {item["field"]: item["title"] for item in result["needs_verification"]}
    assert fields == {"learned_text": "今天学了方程", "difficult_text": "符号还要核对", "next_action": "再做一题"}


def test_later_correct_answer_does_not_erase_previous_day_weakness(db, learning_clock):
    service = LearningStateService(db)
    sid = session(db)
    first = wrong(db, sid)
    advance_day(learning_clock)
    corrected = service.submit_diagnostic(sid, ITEM, "90", "tomorrow-correct")
    assert corrected["correctness"] == 1
    finish(db, sid)
    service.close_session_learning(sid)
    service = LearningStateService(Database(db.path))
    yesterday = service.daily_learning_summary(CHILD, learning_day(learning_clock, -1))
    today = service.daily_learning_summary(CHILD, learning_day(learning_clock))
    assert [item["source"]["source_id"] for item in yesterday["weak_points"]] == [first["evidence_id"]]
    assert today["weak_points"] == []
    assert len(yesterday["learning_content"]) == len(today["learning_content"]) == 1


def test_excluded_source_cancels_four_reviews_and_unstarted_plan_not_other_evidence(db, learning_clock):
    sid = session(db)
    first = wrong(db, sid)
    case = MistakeBookService(db).capture_from_attempt(first)
    assert case is not None
    finish(db, sid)
    service = LearningStateService(db)
    service.close_session_learning(sid)
    advance_day(learning_clock)
    next_sid = session(db)
    plan = make_plan(db, next_sid)
    selected = next(item for item in plan["plan_items"]
                    if item["source_type"] == "mistake_review" and item["source_ref"] == case["mistake_id"])
    set_state(db, next_sid, "study")
    second = wrong(db, next_sid)
    MistakeBookService(db).confirm(case["mistake_id"], CHILD, "exclude", "exclude-source")
    with db.connect() as connection:
        source_reviews = [tuple(row) for row in connection.execute(
            "SELECT interval_label,status FROM review_task WHERE source_evidence_id=? ORDER BY due_at",
            (first["evidence_id"],),
        )]
        assert {label for label, _ in source_reviews} == {"immediate", "48h", "7d"}
        assert source_reviews and all(status == "cancelled" for _, status in source_reviews)
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (selected["plan_item_id"],)).fetchone()[0] == "cancelled"
        assert connection.execute("SELECT count(*) FROM review_task WHERE source_evidence_id=? AND status='pending'", (second["evidence_id"],)).fetchone()[0] == 3
    finish(db, next_sid)
    service.close_session_learning(next_sid)
    again = session(db)
    selected = [item for item in make_plan(db, again)["plan_items"] if item["source_type"] == "due_review"]
    assert selected
    with db.connect() as connection:
        assert all(connection.execute("SELECT source_evidence_id FROM review_task WHERE review_task_id=?", (item["source_ref"],)).fetchone()[0] == second["evidence_id"] for item in selected)
    assert all(item["source"]["source_id"] != first["evidence_id"] for item in service.daily_learning_summary(CHILD, learning_day(learning_clock, -1))["weak_points"])


@pytest.mark.parametrize("after_closure", [False, True])
def test_revoked_tutor_outcome_cannot_seed_or_keep_cached_followup(db, after_closure, learning_clock):
    sid = seed_session(db)
    gateway = EvaluationGateway()
    evaluator = LearningOutcomeEvaluator(db, StudyRepository(db), gateway, SimpleNamespace(model_provider="test", model_name="test"))
    persist_evaluation_turn(db, sid, "eval-close", "eval-close-action", "new", "diagnose")
    result = asyncio.run(evaluator.evaluate("eval-close"))
    oid = result["evaluation"]["outcome_id"]
    finish(db, sid)
    service = LearningStateService(db)
    if after_closure:
        closure = service.close_session_learning(sid)
        assert closure["remediation"][0]["status"] == "pending"
        advance_day(learning_clock)
        next_sid = session(db, "child-runtime")
        plan = make_plan(db, next_sid)
        planned = next(item for item in plan["plan_items"] if item["source_type"] == "agent_retention")
    LearningOutcomeProjection(db).review_outcome("child-runtime", oid, decision="revoke", reason="回答是复述，评价撤回", client_action_id="revoke-close")
    service.close_session_learning(sid)
    service = LearningStateService(Database(db.path))
    assert service.session_learning_summary(sid)["remediation"] == []
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM agent_commitment WHERE child_id='child-runtime' AND status IN ('pending','due')").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM mastery_state WHERE child_id='child-runtime'").fetchone()[0] == 0
        if after_closure:
            assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (planned["plan_item_id"],)).fetchone()[0] == "cancelled"
    assert len(gateway.requests) == 1


def homework_plan(db, sid):
    set_state(db, sid, "homework_capture")
    repository = StudyRepository(db)
    homework = repository.add_homework(sid, "数学", "练习册第六页", 10, "written", "第1到3题", None, None, None, None, str(uuid4()))
    result = repository.generate_plan(sid, str(uuid4()))
    item = next(item for item in result["plan_items"] if item["homework_id"] == homework["homework_id"])
    set_state(db, sid, "study")
    return item


def test_correction_durable_carryover_is_confirmed_then_enters_next_plan(db, learning_clock):
    sid = session(db)
    item = homework_plan(db, sid)
    repository = StudyRepository(db)
    repository.start_plan_item(item["plan_item_id"], "start-homework")
    repository.complete_plan_item(item["plan_item_id"], 10, "homework-needs-correction", "needs_correction", "第2题还要改")
    service = LearningStateService(db)
    preview = service.preview_session_learning(sid)
    assert preview["homework"][0]["status"] == "needs_correction"
    assert preview["remediation"][0]["status"] == "needs_scheduling"
    finish(db, sid)
    first = service.close_session_learning(sid)
    reopened = LearningStateService(Database(db.path))
    assert reopened.close_session_learning(sid) == first
    correction = next(item for item in first["remediation"] if item.get("kind") == "homework_correction")
    assert correction["status"] == "needs_confirmation" and "knowledge_node_id" not in correction
    advance_day(learning_clock)
    assert reopened.daily_learning_summary(CHILD)["homework"] == []
    next_sid = session(db)
    set_state(db, next_sid, "homework_capture")
    assert repository.get_workspace(next_sid)["carryover_candidates"][0]["task_type"] == "correction"
    with pytest.raises(PlanInputError):
        repository.generate_plan(next_sid, "unconfirmed-correction-plan")
    receipt = repository.review_deferred_task(next_sid, correction["source_ref"], "continue_today", "今天订正第2题", "carry-correction")
    assert repository.review_deferred_task(next_sid, correction["source_ref"], "continue_today", "今天订正第2题", "carry-correction") == receipt
    plan = repository.generate_plan(next_sid, "confirmed-correction-plan")
    tasks = [task for task in plan["plan_items"] if task["task_type"] == "correction"]
    assert len(tasks) == 1 and tasks[0]["source_type"] == "school_homework"
    finish(db, next_sid)
    other = session(db, OTHER)
    assert repository.get_workspace(other)["carryover_candidates"] == []


def test_active_overnight_recall_and_homework_not_reported_as_today(db, learning_clock):
    sid = session(db)
    set_state(db, sid, "classroom_recall")
    StudyRepository(db).add_recall(sid, "数学", "昨天的课堂内容", "yesterday-recall")
    item = homework_plan(db, sid)
    repository = StudyRepository(db)
    repository.start_plan_item(item["plan_item_id"], "yesterday-start")
    advance_day(learning_clock)
    service = LearningStateService(Database(db.path))
    assert service.daily_learning_summary(CHILD)["sessions"] == []
    repository.complete_plan_item(item["plan_item_id"], 10, "today-complete", "needs_correction", "还要改一题")
    today = service.daily_learning_summary(CHILD)
    assert today["learning_content"] == [] and today["homework"][0]["status"] == "needs_correction"
    yesterday = service.daily_learning_summary(CHILD, learning_day(learning_clock, -1))
    assert yesterday["homework"][0]["status"] == "unfinished"
    assert yesterday["homework"][0]["actual_minutes"] == 0
    assert len(yesterday["learning_content"]) == 1


def test_empty_answer_does_not_write_evidence(db):
    sid = session(db)
    with pytest.raises(ValueError, match="blank"):
        LearningStateService(db).submit_diagnostic(sid, ITEM, "  \n", "blank-answer")
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt").fetchone()[0] == 0


def test_legacy_completed_review_without_answer_cannot_close_reflection(db, learning_clock):
    sid = session(db)
    wrong(db, sid)
    finish(db, sid)
    LearningStateService(db).close_session_learning(sid)
    advance_day(learning_clock)
    next_sid = session(db)
    plan = make_plan(db, next_sid)
    item = next(item for item in plan["plan_items"] if item["source_type"] == "due_review")
    with db.connect() as connection:
        connection.execute("UPDATE plan_item SET status='completed',started_at=?,completed_at=? WHERE plan_item_id=?", (module.utc_now(), module.utc_now(), item["plan_item_id"]))
        connection.commit()
    set_state(db, next_sid, "reflection")
    with pytest.raises(PlanInputError):
        StudyRepository(db).complete_with_reflection(next_sid, "我听懂了", "", "", "", False, "invalid-review-closure")
    with db.connect() as connection:
        assert connection.execute("SELECT status FROM daily_session WHERE session_id=?", (next_sid,)).fetchone()[0] == "active"
        assert connection.execute("SELECT count(*) FROM daily_reflection WHERE session_id=?", (next_sid,)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM report_job WHERE session_id=?", (next_sid,)).fetchone()[0] == 0


def test_legacy_tutor_followup_enters_plan_but_no_receipt_cannot_certify_transfer(db, learning_clock):
    from home_study_agent import agent_capability_executor as executor_module
    from home_study_agent.agent_runtime import AgentRuntimeStore
    sid = seed_session(db)
    gateway = EvaluationGateway()
    repository = StudyRepository(db)
    evaluator = LearningOutcomeEvaluator(db, repository, gateway, SimpleNamespace(model_provider="test", model_name="test"))
    persist_evaluation_turn(db, sid, "tutor-day1", "tutor-day1-action", "new", "diagnose")
    first = asyncio.run(evaluator.evaluate("tutor-day1"))
    finish(db, sid)
    service = LearningStateService(db)
    closure = service.close_session_learning(sid)
    followup = closure["remediation"][0]
    assert followup["source_type"] == "agent_retention" and followup["status"] == "pending"
    with db.connect() as connection:
        payload = json.loads(connection.execute("SELECT payload_json FROM agent_commitment WHERE commitment_id=?", (followup["source_ref"],)).fetchone()[0])
        assert payload["source_outcome_id"] == first["evaluation"]["outcome_id"]
    advance_day(learning_clock)
    next_sid = session(db, "child-runtime")
    plan = make_plan(db, next_sid)
    item = next(item for item in plan["plan_items"] if item["source_type"] == "agent_retention")
    set_state(db, next_sid, "study")
    repository.start_plan_item(item["plan_item_id"], "start-tutor-followup")
    activated = executor_module.AgentCapabilityExecutor(db, repository)._activate_retention_check(next_sid, followup["source_ref"])
    assert activated["phase"] == "check"
    assert AgentRuntimeStore(db).pending_child_reply(next_sid)["phase"] == "check"
    persist_evaluation_turn(db, next_sid, "tutor-day2", "tutor-day2-action", "continue", "check")
    response = asyncio.run(evaluator.evaluate("tutor-day2"))
    assert response["evaluation"]["certification_status"] == "candidate"
    assert response["evaluation"]["mastery_gate"]["missing"] == ["reliable_response_receipt_missing"]
    assert response["evaluation"]["outcome_level"] not in {"independent", "transferred", "retained"}
    with db.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (item["plan_item_id"],)).fetchone()[0] == "completed"
        assert connection.execute("SELECT status FROM agent_commitment WHERE commitment_id=?", (followup["source_ref"],)).fetchone()[0] == "completed"
        assert connection.execute("SELECT count(*) FROM agent_commitment WHERE commitment_type='review' AND status='pending'").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM agent_objective WHERE child_id='child-runtime'").fetchone()[0] == 'active'
        assert connection.execute("SELECT count(*) FROM mastery_state WHERE child_id='child-runtime'").fetchone()[0] == 0
    finish(db, next_sid)
    service.close_session_learning(next_sid)
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM agent_commitment WHERE status='pending'").fetchone()[0] == 1
    assert len(gateway.requests) == 2


def test_closure_export_and_delete_are_child_scoped_without_cached_prose(db, tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    sid = session(db)
    finish(db, sid, learned="本地私密学习记录")
    service = LearningStateService(db)
    service.close_session_learning(sid)
    other = session(db, OTHER)
    finish(db, other, learned="另一个孩子的学习")
    service.close_session_learning(other)
    privacy = PrivacyLifecycleService(db, tmp_path)
    exported = json.loads(Path(privacy.export_child(CHILD)["path"]).read_text(encoding="utf-8"))
    assert exported["cloud_transmission_allowed"] is False
    rows = exported["learning_session_closures"]
    assert len(rows) == 1 and rows[0]["child_id"] == CHILD
    assert "本地私密学习记录" not in rows[0]["summary_json"]
    privacy.delete_child_scope(CHILD, "all_history", "确认删除", "delete-closure-child")
    with db.connect() as connection:
        assert [row[0] for row in connection.execute("SELECT child_id FROM learning_session_closure")] == [OTHER]
    LearningStateService(Database(db.path)).process_pending_closures()
    assert service.daily_learning_summary(CHILD)["sessions"] == []
    assert service.daily_learning_summary(OTHER)["sessions"]
