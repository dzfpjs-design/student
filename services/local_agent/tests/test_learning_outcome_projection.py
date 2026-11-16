import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository
from test_learning_outcome_evaluator import evaluation_fixture
from test_learning_outcome_mastery_gate import gate, third_item
from test_mistake_gate_reliability import flow, CHILD, ITEM, SECOND


def approved_question(database, item_id=None):
    LearningStateService(database).seed_catalog()
    with database.connect() as connection:
        item = dict(connection.execute("""SELECT di.*,n.code,n.grade_band FROM diagnostic_item di
            JOIN knowledge_node n ON n.knowledge_node_id=di.knowledge_node_id
            WHERE n.subject='数学' AND ((? IS NULL AND n.grade_band='初三') OR di.diagnostic_item_id=?)
            LIMIT 1""", (item_id, item_id)).fetchone())
        connection.execute("""UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.question_asked',?)
            WHERE capability_name='learning.evaluate_response'""", (item["prompt"],))
        connection.commit()
    graph = KnowledgeGraphService(database)
    node = graph.upsert_node(canonical_key="test:verified-math", node_type="knowledge_point", title="Verified math",
                            scope={"grade": item["grade_band"], "subject": "数学"}, source_ref="product_evidence:test",
                            review_status="approved")
    exercise = graph.upsert_node(canonical_key=f"diagnostic:{item['diagnostic_item_id']}", node_type="exercise",
                                title=item["prompt"], source_ref="product_evidence:test", review_status="approved")
    standard = graph.upsert_node(canonical_key="test:standard", node_type="standard_capability", title="Standard",
                                source_ref="curriculum_standard:test", review_status="approved")
    graph.link(node, exercise, "assessed_by", rationale="reviewed exact assessment", source_ref="product_evidence:test",
               review_status="approved")
    graph.link(node, standard, "aligned_to", rationale="reviewed product mapping", source_ref="product_evidence:test",
               review_status="approved")
    return node, exercise


def test_unmapped_free_tutor_outcome_is_visible_without_invented_mastery(tmp_path):
    database, _, evaluator, _, _ = evaluation_fixture(tmp_path)
    asyncio.run(evaluator.evaluate("eval"))
    projection = LearningOutcomeProjection(database).for_child("child-runtime")
    assert projection["level_counts"] == {}
    assert projection["candidate_count"] == 1
    assert projection["available_count"] == projection["unmapped_count"] == 0
    candidate = projection["recent_outcomes"][0]
    assert candidate["mapping_status"] == "unmapped"
    assert candidate["certification_status"] == "candidate"
    assert candidate["mastery_gate"]["missing"] == ["reliable_response_receipt_missing"]
    assert projection["mapped_count"] == 0
    assert projection["knowledge_evidence"] == []
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mastery_state").fetchone()[0] == 0
    assert projection == LearningOutcomeProjection(Database(database.path)).for_child("child-runtime")


def test_only_exact_reviewed_question_maps_and_rejected_evidence_is_removed(tmp_path):
    database, _, evaluator, _, _ = evaluation_fixture(tmp_path)
    node, exercise = approved_question(database)
    asyncio.run(evaluator.evaluate("eval"))
    service = LearningOutcomeProjection(database)
    mapped = service.for_child("child-runtime")
    assert mapped["candidate_count"] == 1
    assert mapped["mapped_count"] == mapped["available_count"] == 0
    assert mapped["knowledge_evidence"] == []
    candidate = mapped["recent_outcomes"][0]
    assert candidate["mapping_status"] == "mapped"
    assert candidate["mapping"]["node_id"] == node
    assert candidate["certification_status"] == "candidate"
    with database.connect() as connection:
        connection.execute("UPDATE kg_node SET review_status='rejected' WHERE node_id=?", (exercise,))
        connection.commit()
    rejected = service.for_child("child-runtime")
    assert rejected["candidate_count"] == 1
    assert rejected["recent_outcomes"][0]["mapping_status"] == "unmapped"
    assert rejected["knowledge_evidence"] == []
    assert rejected["fingerprint"] != mapped["fingerprint"]


def test_reliable_answer_with_reviewed_mapping_counts_dimensions_without_calibrating_mastery(gate):
    node, exercise = approved_question(gate.db, ITEM)
    result, receipt, _ = gate.run()
    assert receipt["score_status"] == "verified_local"
    assert result["evaluation"]["outcome_level"] == "independent"
    service = LearningOutcomeProjection(Database(gate.db.path))
    mapped = service.for_child(CHILD)
    assert mapped["candidate_count"] == 0
    assert mapped["mapped_count"] == 1
    evidence = mapped["knowledge_evidence"][0]
    assert evidence["node_id"] == node
    assert evidence["mastery_probability"] is None
    assert evidence["dimensions"] == {"understanding": 1, "independence": 1, "transfer": 0, "retention": 0}
    with gate.db.connect() as connection:
        connection.execute("UPDATE kg_node SET review_status='rejected' WHERE node_id=?", (exercise,))
        connection.commit()
        connection.execute("PRAGMA query_only=ON")
        before = connection.total_changes
        rejected = service.for_child(CHILD, connection=connection)
        assert connection.total_changes == before
    assert rejected["unmapped_count"] == 1
    assert rejected["level_counts"] == {"independent": 1}
    assert rejected["knowledge_evidence"] == []
    assert rejected["fingerprint"] != mapped["fingerprint"]


@pytest.mark.parametrize("mutation", ["wrong_grade", "wrong_child", "no_material", "ambiguous", "similar_question", "model_node_id"])
def test_mapping_rejects_unreliable_or_cross_child_sources(tmp_path, mutation):
    database, _, evaluator, _, _ = evaluation_fixture(tmp_path)
    node, exercise = approved_question(database)
    with database.connect() as connection:
        if mutation == "wrong_grade":
            connection.execute("UPDATE kg_node SET scope_json=? WHERE node_id=?", ('{"grade":"四年级"}', node))
        elif mutation == "wrong_child":
            connection.execute("UPDATE kg_node SET scope_json=? WHERE node_id=?", ('{"child_id":"other"}', node))
        elif mutation == "no_material":
            connection.execute("UPDATE kg_edge SET review_status='rejected' WHERE relation_type='aligned_to'")
        elif mutation == "ambiguous":
            connection.execute("""INSERT INTO kg_edge SELECT 'duplicate-edge',source_node_id,target_node_id,
                relation_type,rationale,source_ref,'v2',confidence,review_status,created_at,updated_at
                FROM kg_edge WHERE relation_type='assessed_by'""")
        elif mutation == "similar_question":
            connection.execute("""UPDATE agent_step SET arguments_json=json_set(arguments_json,
                '$.question_asked','a merely similar question')""")
        else:
            connection.execute("""UPDATE agent_step SET arguments_json=json_set(arguments_json,
                '$.question_asked','','$.knowledge_node_id',?)""", (node,))
        connection.commit()
    asyncio.run(evaluator.evaluate("eval"))
    projection = LearningOutcomeProjection(database).for_child("child-runtime")
    assert projection["candidate_count"] == 1
    assert projection["available_count"] == projection["unmapped_count"] == 0
    assert projection["recent_outcomes"][0]["mapping_status"] == "unmapped"
    assert projection["knowledge_evidence"] == []


def test_projection_is_child_isolated_and_missing_answer_invalidates_evidence(tmp_path):
    database, _, evaluator, _, _ = evaluation_fixture(tmp_path)
    asyncio.run(evaluator.evaluate("eval"))
    service = LearningOutcomeProjection(database)
    original = service.for_child("child-runtime")
    with database.connect() as connection:
        connection.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES('other','Other','四年级','江苏南京','active',datetime('now'),datetime('now'))""")
        connection.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','')")
        connection.commit()
    assert service.for_child("other")["outcome_count"] == 0
    changed = service.for_child("child-runtime")
    assert changed["available_count"] == 0
    assert changed["level_counts"] == {}
    assert changed["fingerprint"] != original["fingerprint"]
    with pytest.raises(ValueError):
        service.for_child("absent")


def test_outcome_review_is_append_only_idempotent_and_restart_safe(tmp_path):
    database, _, evaluator, _, _ = evaluation_fixture(tmp_path)
    result = asyncio.run(evaluator.evaluate("eval"))
    outcome_id = result["evaluation"]["outcome_id"]
    service = LearningOutcomeProjection(database)
    before = service.for_child("child-runtime")
    request = dict(decision="revoke", reason="Parent corrected the source", client_action_id="revoke-1")
    event = service.review_outcome("child-runtime", outcome_id, **request)
    assert service.review_outcome("child-runtime", outcome_id, **request) == event
    reopened = LearningOutcomeProjection(Database(database.path))
    revoked = reopened.for_child("child-runtime")
    assert revoked["outcome_count"] == 1
    assert revoked["revoked_count"] == 1
    assert revoked["available_count"] == 0
    assert revoked["level_counts"] == {}
    assert revoked["recent_outcomes"][0]["evidence_status"] == "revoked"
    assert revoked["fingerprint"] != before["fingerprint"]
    with pytest.raises(ValueError, match="conflicts"):
        service.review_outcome("child-runtime", outcome_id, **{**request, "reason": "different"})
    with pytest.raises(ValueError, match="owned"):
        service.review_outcome("another-child", outcome_id, **request)
    reopened.review_outcome("child-runtime", outcome_id, decision="restore", reason="Source verified again", client_action_id="restore-1")
    restored = reopened.for_child("child-runtime")
    assert restored["available_count"] == restored["unmapped_count"] == 0
    assert restored["candidate_count"] == 1
    assert restored["recent_outcomes"][0]["mapping_status"] == "unmapped"
    assert restored["level_counts"] == before["level_counts"]
    assert restored["fingerprint"] not in {before["fingerprint"], revoked["fingerprint"]}
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_outcome_review_event").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 1
        # Uses the existing transaction and makes no writes, including schema.
        connection.execute("PRAGMA query_only=ON")
        assert service.for_child("child-runtime", connection=connection) == restored


@pytest.mark.parametrize("deleted", [False, True])
def test_model_source_rejection_or_deletion_is_not_active_evidence(tmp_path, deleted):
    database, _, evaluator, _, _ = evaluation_fixture(tmp_path)
    asyncio.run(evaluator.evaluate("eval"))
    with database.connect() as connection:
        if deleted:
            connection.execute("DELETE FROM model_call")
        else:
            connection.execute("UPDATE model_call SET verification_status='rejected'")
        connection.commit()
    projection = LearningOutcomeProjection(database).for_child("child-runtime")
    assert projection["outcome_count"] == 1
    assert projection["available_count"] == 0
    assert projection["level_counts"] == {}
    assert projection["recent_outcomes"][0]["evidence_status"] == ("source_unavailable" if deleted else "source_revoked")


def start_retention_task(database, session_id, commitment_id, action):
    repository = StudyRepository(Database(database.path))
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (session_id,))
        connection.execute("UPDATE family_rule SET latest_end_time=NULL WHERE child_id=?", (CHILD,))
        connection.commit()
    plan = repository.generate_plan(session_id, action)
    items = [item for item in plan["plan_items"]
             if item["source_type"] == "agent_retention" and item["source_ref"] == commitment_id]
    assert len(items) == 1
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session_id,))
        connection.commit()
    repository.start_plan_item(items[0]["plan_item_id"], f"{action}-start")
    activated = AgentCapabilityExecutor(database, repository)._activate_retention_check(session_id, commitment_id)
    assert activated["phase"] == "retention_check"
    return items[0]


@pytest.fixture
def completed_retention(gate, monkeypatch):
    from home_study_agent import learning_outcome_evaluator, learning_outcome_projection, agent_capability_executor
    from home_study_agent.period_reports import PeriodReportService

    # Daily planning reads evidence withdrawals; match application startup.
    PeriodReportService(gate.db).initialize()

    # Advance source timestamps through the real delay gates, not the stored levels.
    for module in (learning_outcome_projection, agent_capability_executor):
        monkeypatch.setattr(module, "datetime", learning_outcome_evaluator.datetime)
    first, _, _ = gate.run()
    second, _, _ = gate.run(SECOND, "42", "transfer_check", days=1)
    assert first["evaluation"]["outcome_level"] == "independent"
    assert second["evaluation"]["outcome_level"] == "transferred"
    gate.clock[0] += timedelta(days=3)
    with gate.db.connect() as connection:
        commitment_id = connection.execute("SELECT commitment_id FROM agent_commitment WHERE objective_id=?", (gate.objective,)).fetchone()[0]
    start_retention_task(gate.db, gate.sid, commitment_id, "original-retention-plan")
    result, _, _ = gate.run(*third_item(gate), phase="retention_check")
    assert result["evaluation"]["outcome_level"] == "retained"
    with gate.db.connect() as connection:
        objective = dict(connection.execute("SELECT * FROM agent_objective WHERE objective_id=?", (gate.objective,)).fetchone())
        historic = connection.execute("SELECT status,completed_at FROM agent_commitment WHERE commitment_id=?", (commitment_id,)).fetchone()
        assert tuple(historic) == ("completed", objective["completed_at"])
        assert connection.execute("SELECT COUNT(*) FROM learning_evidence WHERE child_id=?", (CHILD,)).fetchone()[0] == 3
    assert LearningOutcomeProjection(Database(gate.db.path)).for_child(CHILD)["level_counts"] == {
        "independent": 1, "transferred": 1, "retained": 1,
    }
    return gate.db, gate.sid, gate.evaluator, result["evaluation"]["outcome_id"], objective


def revoke_retention(database, outcome_id, action="revoke-retained"):
    return LearningOutcomeProjection(database).review_outcome(
        CHILD, outcome_id, decision="revoke", reason="Incorrect retention scoring",
        client_action_id=action,
    )


def evaluate_retention_in_session(gate, session_id):
    item_id, answer = third_item(gate)
    gate.clock[0] += timedelta(seconds=1)
    receipt = gate.learning.submit_diagnostic(session_id, item_id, answer, str(uuid4()), hint_level=0)
    assert receipt["score_status"] == "verified_local"
    turn, plan, step = str(uuid4()), str(uuid4()), str(uuid4())
    now = gate.clock[0].isoformat()
    with gate.db.connect() as connection:
        prompt = connection.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()[0]
        connection.execute("""INSERT INTO agent_turn (agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,?,'learning.evaluate_response','','{}','test',?,?)""", (turn, CHILD, session_id, answer, now, turn))
        connection.execute("INSERT INTO agent_plan VALUES (?,?,?,?,?,'test','{}','{}','planned',?,?)",
                           (plan, gate.objective, CHILD, session_id, turn, now, now))
        connection.execute("""INSERT INTO agent_step VALUES (?,?,0,'learning.evaluate_response','1',
            'local_model','medium',?,'[]','planned',?,NULL,NULL,?,?)""",
            (step, plan, json.dumps({"response": answer, "subject": "数学", "question_asked": prompt,
                                    "evidence_id": receipt["evidence_id"]}), step, now, now))
        connection.commit()
    return asyncio.run(gate.evaluator.evaluate(turn))


def test_retained_revocation_reopens_once_preserves_history_and_runs_recheck(completed_retention, gate, monkeypatch):
    database, session_id, evaluator, outcome_id, before = completed_retention
    repository = StudyRepository(Database(database.path))
    for item in repository.get_workspace(session_id)["plan_items"]:
        if item["status"] == "pending":
            repository.reschedule_plan_item(item["plan_item_id"], "Explicitly defer other synthetic checks", f"defer-{item['plan_item_id']}")
    for source, target in (("study", "reflection"), ("reflection", "evolving"), ("evolving", "completed")):
        repository.transition(session_id, source, target, "End synthetic first-day session", f"close-{target}")
    gate.clock[0] += timedelta(days=1)
    session_id = repository.start_session(CHILD, "next-day-retention-session")["session_id"]
    event = revoke_retention(database, outcome_id)
    assert revoke_retention(database, outcome_id) == event
    revoke_retention(Database(database.path), outcome_id, "repeat-with-new-key")
    assert LearningOutcomeProjection(database).for_child(CHILD)["level_counts"] == {"independent": 1, "transferred": 1}
    with database.connect() as connection:
        objective = dict(connection.execute("SELECT * FROM agent_objective").fetchone())
        assert (objective["status"], objective["phase"], objective["completed_at"]) == ("paused", "retention_due", None)
        assert objective["objective_id"] == before["objective_id"]
        rows = connection.execute("SELECT * FROM agent_commitment WHERE status='pending'").fetchall()
        assert len(rows) == 1
        commitment = dict(rows[0])
        payload = json.loads(commitment["payload_json"])
        assert payload["review_event_id"] == event["event_id"]
        assert payload["previous_completed_at"] == before["completed_at"]
        assert payload["phase"] == "retention_check" and payload["prompt"]
        assert tuple(connection.execute("SELECT status,completed_at FROM agent_commitment WHERE status='completed'").fetchone()) == ("completed", before["completed_at"])
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome WHERE outcome_id=?", (outcome_id,)).fetchone()[0] == 1
    item = start_retention_task(database, session_id, commitment["commitment_id"], "recheck-plan")
    complete_task = evaluator._complete_retention_task

    def reconcile_before_completion(objective_id, **kwargs):
        with database.connect() as connection:
            service = LearningOutcomeProjection(database)
            service.reconcile_commitments(connection, CHILD, gate.clock[0].isoformat())
            connection.commit()
        return complete_task(objective_id, **kwargs)

    monkeypatch.setattr(evaluator, "_complete_retention_task", reconcile_before_completion)
    result = evaluate_retention_in_session(gate, session_id)
    assert result["evaluation"]["outcome_level"] == "retained"
    revoke_retention(database, outcome_id, "old-revoke-after-recheck")
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM agent_objective WHERE objective_id=?", (before["objective_id"],)).fetchone()[0] == "completed"
        assert connection.execute("SELECT status FROM agent_commitment WHERE commitment_id=?", (commitment["commitment_id"],)).fetchone()[0] == "completed"
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 2
    revoke_retention(database, result["evaluation"]["outcome_id"], "revoke-new-retention")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment WHERE status='pending'").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (item["plan_item_id"],)).fetchone()[0] == "completed"
        assert connection.execute("SELECT status FROM agent_commitment WHERE commitment_id=?", (commitment["commitment_id"],)).fetchone()[0] == "completed"


@pytest.mark.parametrize("alternative", ["valid", "low_confidence", "revoked", "source_rejected", "recent_repeat"])
def test_retained_revocation_requires_another_valid_completion_basis(completed_retention, gate, alternative):
    database, _, _, outcome_id, before = completed_retention
    item_id, answer = third_item(gate)
    with database.connect() as connection:
        prompt = connection.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()[0]
    result, receipt, _ = gate.run(item_id, answer, phase="retention_check", days=.01 if alternative == "recent_repeat" else 1,
        adapter=lambda evidence: {"arguments": {"response": answer, "subject": "数学",
            "question_asked": prompt, "evidence_id": evidence["evidence_id"]}})
    if alternative == "recent_repeat":
        assert result["evaluation"]["certification_status"] == "candidate"
        assert result["evaluation"]["mastery_gate"]["missing"] == ["reliable_response_receipt_missing"]
    else:
        assert result["evaluation"]["outcome_level"] == "retained", result["evaluation"]["mastery_gate"]["missing"]
        assert result["evaluation"]["mastery_gate"]["evidence_id"] == receipt["evidence_id"]
    alternative_id = result["evaluation"]["outcome_id"]
    with database.connect() as connection:
        completed_at = connection.execute("SELECT completed_at FROM agent_objective WHERE objective_id=?", (before["objective_id"],)).fetchone()[0]
        if alternative == "low_confidence":
            connection.execute("UPDATE agent_outcome SET confidence=.4 WHERE outcome_id=?", (alternative_id,))
        if alternative == "source_rejected":
            connection.execute("""UPDATE model_call SET verification_status='rejected' WHERE call_id=(
                SELECT json_extract(evidence_refs_json,'$[1].model_call_id') FROM agent_outcome WHERE outcome_id=?)""", (alternative_id,))
        connection.commit()
    if alternative == "revoked":
        revoke_retention(database, alternative_id, "revoke-alternative")
    revoke_retention(database, outcome_id)
    with database.connect() as connection:
        row = connection.execute("SELECT status,phase,completed_at FROM agent_objective").fetchone()
        if alternative == "recent_repeat":
            # A new unsuccessful check is already active; revocation must not interrupt it.
            assert tuple(row) == ("active", "remediate", completed_at)
        else:
            assert tuple(row) == (("completed", "completed", completed_at) if alternative == "valid" else ("paused", "retention_due", None))
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment WHERE status='pending'").fetchone()[0] == (
            0 if alternative in {"valid", "recent_repeat"} else 1)
    current = LearningOutcomeProjection(Database(database.path)).for_child(CHILD)
    assert current["level_counts"].get("retained", 0) == (1 if alternative == "valid" else 0)


def test_retained_revocation_does_not_interrupt_other_active_or_other_child_objectives(completed_retention):
    database, _, _, outcome_id, before = completed_retention
    with database.connect() as connection:
        connection.execute("""INSERT INTO child_profile SELECT 'other-child',nickname,grade,curriculum_region,status,created_at,updated_at
            FROM child_profile WHERE child_id=?""", (CHILD,))
        for objective_id, child_id, status in (("other-active", CHILD, "active"), ("other-child-objective", "other-child", "completed")):
            connection.execute("""INSERT INTO agent_objective SELECT ?,?,session_id,objective_type,summary,
                success_criteria_json,phase,?,created_at,updated_at,completed_at,last_agent_turn_id
                FROM agent_objective WHERE objective_id=?""", (objective_id, child_id, status, before["objective_id"]))
        connection.commit()
    revoke_retention(database, outcome_id)
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM agent_objective WHERE objective_id='other-active'").fetchone()[0] == "active"
        assert connection.execute("SELECT status FROM agent_objective WHERE objective_id='other-child-objective'").fetchone()[0] == "completed"
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment WHERE child_id='other-child'").fetchone()[0] == 0


def test_restoring_retained_basis_cancels_only_unstarted_recheck(completed_retention):
    database, _, _, outcome_id, before = completed_retention
    revoke_retention(database, outcome_id)
    service = LearningOutcomeProjection(Database(database.path))
    service.review_outcome(CHILD, outcome_id, decision="restore", reason="Verified original evidence", client_action_id="restore-retained")
    with database.connect() as connection:
        assert tuple(connection.execute("SELECT status,completed_at FROM agent_objective").fetchone()) == ("completed", before["completed_at"])
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment WHERE status IN ('pending','due')").fetchone()[0] == 0
    revoke_retention(database, outcome_id, "revoke-restored-retained")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment WHERE status='pending'").fetchone()[0] == 1


def test_concurrent_retained_revocations_create_one_recheck(completed_retention):
    database, _, _, outcome_id, _ = completed_retention
    with ThreadPoolExecutor(max_workers=4) as workers:
        events = list(workers.map(lambda index: revoke_retention(Database(database.path), outcome_id, f"concurrent-{index}"), range(4)))
    assert len({event["event_id"] for event in events}) == 4
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment WHERE status='pending'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 2


def test_recheck_insert_failure_rolls_back_review_and_objective_then_retries(completed_retention):
    database, _, _, outcome_id, before = completed_retention
    with database.connect() as connection:
        connection.execute("""CREATE TRIGGER reject_recheck BEFORE INSERT ON agent_commitment
            WHEN json_extract(NEW.payload_json,'$.origin')='retained_outcome_recheck'
            BEGIN SELECT RAISE(ABORT,'injected recheck failure'); END""")
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected recheck failure"):
        revoke_retention(database, outcome_id)
    with database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM agent_objective").fetchone()) == before
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 1
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='learning_outcome_review_event'").fetchone():
            assert connection.execute("SELECT COUNT(*) FROM learning_outcome_review_event").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_recheck")
        connection.commit()
    revoke_retention(Database(database.path), outcome_id)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment WHERE status='pending'").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["active", "paused", "cancelled", "failed"])
def test_revocation_does_not_reopen_noncompleted_objective(completed_retention, status):
    database, _, _, outcome_id, _ = completed_retention
    with database.connect() as connection:
        connection.execute("UPDATE agent_objective SET status=?,phase='check'", (status,))
        connection.commit()
    revoke_retention(database, outcome_id)
    with database.connect() as connection:
        assert tuple(connection.execute("SELECT status,phase FROM agent_objective").fetchone()) == (status, "check")
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 1


def test_recheck_receipt_rejects_cross_child_and_missing_review(completed_retention):
    database, _, _, outcome_id, _ = completed_retention
    revoke_retention(database, outcome_id)
    service = LearningOutcomeProjection(database)
    with database.connect() as connection:
        receipt = dict(connection.execute("SELECT * FROM agent_commitment WHERE status='pending'").fetchone())
        assert service.commitment_available(connection, receipt)
        assert not service.commitment_available(connection, {**receipt, "child_id": "other"})
        assert not service.commitment_available(connection, {**receipt, "objective_id": "other"})
        payload = json.loads(receipt["payload_json"])
        assert not service.commitment_available(connection, {**receipt, "payload_json": json.dumps({**payload, "review_event_id": "absent"})})
