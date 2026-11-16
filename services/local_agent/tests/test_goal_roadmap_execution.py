"""Read-only W17 execution resolution against real, disposable SQLite plans."""

from datetime import date, timedelta
import hashlib
import json
import os
import subprocess
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_goal_program import goal_db, exact_goal_item


CHILD = "child-junior-3"
OTHER = "child-primary-4"


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def snapshot(db):
    with db.connect() as c:
        return hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()


@pytest.fixture
def execution(goal_db, exact_goal_item):
    db, repo = goal_db, StudyRepository(goal_db)
    GoalProgramService(db).seed_defaults()
    with db.connect() as c:
        node = c.execute("SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?", (exact_goal_item[0],)).fetchone()[0]
        c.execute("UPDATE knowledge_node SET status='inactive' WHERE grade_band LIKE '初三%' AND knowledge_node_id!=?", (node,))
        c.execute("DELETE FROM knowledge_edge WHERE target_node_id=?", (node,))
        c.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?", (CHILD,))
        c.commit()
    sid = repo.start_session(CHILD, "execution-session")["session_id"]
    change(db, "UPDATE daily_session SET state='homework_capture',recall_declared_empty=1 WHERE session_id=?", (sid,))
    repo.add_homework(sid, "数学", "校内作业", 10, "written", None, None, None, None, None, "execution-homework")
    workspace = repo.generate_plan(sid, "execution-plan")
    item = next(item for item in workspace["plan_items"] if item["source_type"] == "junior_exam_goal")
    return db, sid, item["plan_item_id"], node, exact_goal_item


def resolve(execution):
    return GoalRoadmapService(execution[0]).resolve_execution(execution[1], execution[2])


def rewrite_binding(execution, **fields):
    db, _, pid, _, _ = execution
    with db.connect() as c:
        decision = c.execute("""SELECT d.* FROM plan_constraint_decision d JOIN plan_item pi USING(plan_id)
            WHERE pi.plan_item_id=? AND d.obligation_type=pi.source_type AND d.source_ref=pi.source_ref""", (pid,)).fetchone()
        metadata = json.loads(decision["metadata_json"])
        metadata["planning_evidence"].update(fields)
        c.execute("UPDATE plan_constraint_decision SET metadata_json=? WHERE decision_id=?", (json.dumps(metadata), decision["decision_id"]))
        c.commit()


def test_resolves_original_node_despite_own_budget_reservation_without_any_write(execution):
    db, sid, pid, node, exact = execution
    change(db, "UPDATE goal_program SET default_weekly_minutes=15 WHERE child_id=?", (CHILD,))
    assert GoalRoadmapService(db).read(CHILD, "junior_exam_readiness")["next_daily_action"] is None
    before = snapshot(db)
    result = resolve(execution)
    assert result["status"] == "ready", result
    assert result["binding"]["knowledge_node_id"] == node
    assert result["diagnostic_item_id"] == exact[0]
    assert result["diagnostic_item"]["knowledge_node_id"] == node
    assert result["session_id"] == sid and result["plan_item_id"] == pid
    assert result["requires_prompt_binding"] and result["read_only"]
    assert not result["reservation_created"] and not result["cloud_transmission_allowed"]
    assert not {"expected_answer_json", "explanation", "correctness"} & set(result["diagnostic_item"])
    assert resolve(execution) == result
    assert snapshot(db) == before


def test_real_subprocess_restart_reads_same_original_binding_and_item(execution):
    db, sid, pid, _, _ = execution
    result = resolve(execution)
    code = "from home_study_agent.database import Database; from home_study_agent.goal_roadmap import GoalRoadmapService; import json,sys; print(json.dumps(GoalRoadmapService(Database(sys.argv[1])).resolve_execution(sys.argv[2],sys.argv[3])))"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    restarted = subprocess.run([sys.executable, "-c", code, str(db.path), sid, pid], env=env, capture_output=True, text=True, check=True)
    assert json.loads(restarted.stdout) == result


@pytest.mark.parametrize("phase", ["check_in", "classroom_recall", "homework_capture", "plan_confirm", "study", "reflection", "companion"])
def test_nonterminal_phase_is_not_a_page_dependency(execution, phase):
    change(execution[0], "UPDATE daily_session SET state=? WHERE session_id=?", (phase, execution[1]))
    assert resolve(execution)["status"] == "ready"


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE daily_session SET state='completed'", "session_inactive"),
    ("UPDATE daily_session SET state='evolving'", "session_inactive"),
    ("UPDATE daily_session SET status='completed'", "session_inactive"),
    ("UPDATE child_profile SET status='archived'", "session_inactive"),
    ("UPDATE daily_plan SET status='cancelled'", "plan_inactive"),
    ("UPDATE plan_item SET status='completed' WHERE source_type='junior_exam_goal'", "plan_item_inactive"),
    ("UPDATE goal_program SET status='paused'", "goal_inactive"),
])
def test_inactive_owner_plan_and_goal_block_without_a_question(execution, mutation, reason):
    change(execution[0], mutation)
    result = resolve(execution)
    assert result["status"] == "blocked" and result["reason_code"] == reason
    assert result["diagnostic_item"] is None


@pytest.mark.parametrize("mutation", ["DELETE FROM plan_constraint_decision WHERE obligation_type='junior_exam_goal'",
    "UPDATE plan_constraint_decision SET metadata_json='{}' WHERE obligation_type='junior_exam_goal'",
    "UPDATE plan_constraint_decision SET metadata_json='not-json' WHERE obligation_type='junior_exam_goal'"])
def test_missing_or_legacy_binding_cannot_fall_back_to_title(execution, mutation):
    change(execution[0], mutation)
    result = resolve(execution)
    assert result["status"] == "blocked" and result["reason_code"] == "binding_unavailable"
    assert result["diagnostic_item_id"] is None


def test_current_prerequisite_change_blocks_old_plan_instead_of_selecting_parent(execution):
    db, _, _, node, _ = execution
    with db.connect() as c:
        parent = c.execute("SELECT knowledge_node_id FROM knowledge_node WHERE grade_band LIKE '初三%' AND knowledge_node_id!=? LIMIT 1", (node,)).fetchone()[0]
        c.execute("UPDATE knowledge_node SET status='active' WHERE knowledge_node_id=?", (parent,))
        c.execute("DELETE FROM knowledge_edge WHERE target_node_id=?", (parent,))
        c.execute("INSERT INTO knowledge_edge VALUES('execution-prerequisite',?,?,'prerequisite_of','Synthetic revised prerequisite','hsc_original_v1','v2','active')", (parent, node))
        c.commit()
    result = resolve(execution)
    assert result["status"] == "blocked" and result["reason_code"] == "prerequisite_blocked"
    assert result["diagnostic_item_id"] is None
    assert result["binding"]["knowledge_node_id"] == node


@pytest.mark.parametrize("condition", ["node_withdrawn", "item_withdrawn", "candidate_withdrawn", "source_expired", "source_withdrawn", "rubric_changed"])
def test_current_source_qualification_is_required(execution, condition):
    db, _, _, node, exact = execution
    if condition == "node_withdrawn":
        change(db, "UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (node,))
    elif condition == "item_withdrawn":
        change(db, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (exact[0],))
    elif condition == "candidate_withdrawn":
        change(db, "UPDATE specialized_supply_candidate SET status='needs_review' WHERE diagnostic_item_id=?", (exact[0],))
    elif condition == "rubric_changed":
        change(db, "UPDATE diagnostic_item SET expected_answer_json='{}' WHERE diagnostic_item_id=?", (exact[0],))
    else:
        change(db, """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,source_ref,
            license_status,review_status,content_hash,imported_at,effective_to)
            VALUES('execution-source',?,'test','Synthetic','数学','初三','execution-source','local',?,'execution-source','2026-01-01',?)""",
            (CHILD, "withdrawn" if condition == "source_withdrawn" else "approved", (date.today() - timedelta(days=1)).isoformat() if condition == "source_expired" else None))
        change(db, "UPDATE knowledge_node SET source_ref='execution-source' WHERE knowledge_node_id=?", (node,))
    before = snapshot(db)
    result = resolve(execution)
    assert result["status"] == "blocked" and result["diagnostic_item"] is None
    assert snapshot(db) == before


def test_new_real_answer_invalidates_original_baseline_action(execution):
    db, sid, _, _, exact = execution
    change(db, "UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
    LearningStateService(db).submit_diagnostic(sid, exact[0], exact[1], "execution-baseline-answer")
    result = resolve(execution)
    assert result["status"] == "blocked" and result["reason_code"] == "action_changed"


def test_item_version_matches_existing_typed_prompt_without_second_scoring_store(execution):
    db, sid, _, _, _ = execution
    result = resolve(execution)
    prepared = DiagnosticPromptService(db).prepare(sid, track=result["diagnostic_item"]["track"],
        preferred_item_id=result["diagnostic_item_id"], client_action_id="execution-typed-prompt")
    assert prepared["status"] == "waiting_answer"
    assert prepared["prompt"]["item_version"] == result["item_version"]
    assert prepared["prompt"]["diagnostic_item_id"] == result["diagnostic_item_id"]


def test_cross_child_and_cross_session_plan_ids_never_reveal_the_original_question(execution):
    db, sid, pid, _, _ = execution
    change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
    other_session = StudyRepository(db).start_session(OTHER, "execution-other-child")["session_id"]
    result = GoalRoadmapService(db).resolve_execution(other_session, pid)
    assert result["reason_code"] == "plan_ownership_mismatch" and result["diagnostic_item"] is None
    assert result["binding"] is None
    change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (other_session,))
    same_child_new_session = StudyRepository(db).start_session(CHILD, "execution-new-session")["session_id"]
    assert GoalRoadmapService(db).resolve_execution(same_child_new_session, pid)["reason_code"] == "plan_ownership_mismatch"


def test_persisted_action_and_version_must_be_typed_not_legacy_flags(execution):
    rewrite_binding(execution, action="mark_mastered", mastery_claim_allowed=True)
    assert resolve(execution)["reason_code"] == "binding_unavailable"


def test_new_higher_priority_node_does_not_rebind_original_planned_node(execution):
    db, _, _, node, exact = execution
    with db.connect() as c:
        other = c.execute("""SELECT knowledge_node_id FROM knowledge_node WHERE grade_band LIKE '初三%'
            AND track='school' AND node_type='skill' AND knowledge_node_id<? ORDER BY knowledge_node_id LIMIT 1""", (node,)).fetchone()[0]
        c.execute("UPDATE knowledge_node SET status='active' WHERE knowledge_node_id=?", (other,))
        c.execute("DELETE FROM knowledge_edge WHERE target_node_id=?", (other,))
        c.commit()
    current = GoalRoadmapService(db).read(CHILD, "junior_exam_readiness")
    assert current["next_daily_action"]["knowledge_node_id"] == other
    result = resolve(execution)
    assert result["status"] == "ready" and result["diagnostic_item_id"] == exact[0]
    assert result["binding"]["knowledge_node_id"] == node


def test_withdrawn_answer_does_not_erase_recent_question_exposure(execution):
    from home_study_agent.mistake_book import MistakeBookService
    db, sid, _, _, exact = execution
    change(db, "UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
    attempt = LearningStateService(db).submit_diagnostic(sid, exact[0], "不会", "withdrawn-execution-answer")
    mistake = MistakeBookService(db).capture_from_attempt(attempt)
    change(db, "UPDATE mistake_case SET status='excluded' WHERE mistake_id=?", (mistake["mistake_id"],))
    result = resolve(execution)
    assert result["reason_code"] == "qualified_item_unavailable"
    assert result["diagnostic_item_id"] is None


@pytest.mark.parametrize("conflicting", [False, True])
def test_segmented_identical_bindings_are_allowed_but_conflicting_bindings_block(execution, conflicting):
    db, _, pid, _, _ = execution
    with db.connect() as c:
        row = dict(c.execute("""SELECT d.* FROM plan_constraint_decision d JOIN plan_item pi USING(plan_id)
            WHERE pi.plan_item_id=? AND d.obligation_type=pi.source_type AND d.source_ref=pi.source_ref""", (pid,)).fetchone())
        row["decision_id"] = "additional-plan-segment"
        if conflicting:
            metadata = json.loads(row["metadata_json"])
            metadata["planning_evidence"]["action"] = "independent_check"
            row["metadata_json"] = json.dumps(metadata)
        c.execute(f"INSERT INTO plan_constraint_decision({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        c.commit()
    result = resolve(execution)
    assert result["status"] == ("blocked" if conflicting else "ready")
    if conflicting:
        assert result["reason_code"] == "binding_ambiguous"


def test_a_foreign_cycle_cannot_be_used_as_this_plans_source(execution):
    db, _, pid, _, _ = execution
    other_cycle = GoalProgramService(db).cycles(OTHER)[0]["cycle_id"]
    change(db, "UPDATE plan_item SET source_ref=? WHERE plan_item_id=?", (other_cycle, pid))
    change(db, "UPDATE plan_constraint_decision SET source_ref=? WHERE obligation_type='junior_exam_goal'", (other_cycle,))
    assert resolve(execution)["reason_code"] == "binding_scope_mismatch"


def test_internal_prompt_transaction_hook_sees_current_source_and_does_not_commit(execution):
    db, sid, pid, _, exact = execution
    before = snapshot(db)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE specialized_supply_candidate SET status='needs_review' WHERE diagnostic_item_id=?", (exact[0],))
        result = GoalRoadmapService._resolve_execution(c, sid, pid)
        assert result["status"] == "blocked" and result["diagnostic_item_id"] is None
        assert c.in_transaction
        c.rollback()
    assert snapshot(db) == before
    assert resolve(execution)["diagnostic_item_id"] == exact[0]


def test_internal_hook_requires_a_caller_owned_transaction(execution):
    with execution[0].connect() as c, pytest.raises(RuntimeError, match="requires a transaction"):
        GoalRoadmapService._resolve_execution(c, execution[1], execution[2])
