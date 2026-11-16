from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.decision_record import ensure_schema as ensure_decision_schema
from home_study_agent.outcome_attribution import (
    OutcomeAttributionError,
    ensure_schema as ensure_outcome_attribution_schema,
    for_outcome,
    persist,
)
from home_study_agent.repository import StudyRepository


CHILD = "child-primary-4"


def _seed(tmp_path: Path, *, mismatched_plan_child: bool = False) -> tuple[Database, str]:
    database = Database(tmp_path / "outcome-attribution.db")
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    session = repository.start_session(CHILD, "outcome-attribution-session")
    session_id = session["session_id"]
    snapshot = ChildModelService(database).snapshot(CHILD)
    now = datetime.now(timezone.utc).isoformat()
    with database.connect() as connection:
        ensure_decision_schema(connection)
        ensure_outcome_attribution_schema(connection)
        connection.execute(
            """INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
                 response_text,action_json,orchestrator_version,tool_status,created_at,client_action_id)
               VALUES('turn-attribution',?,?,'我来检查','answer','好','{}','test','completed',?,
                 'turn-attribution-action')""",
            (CHILD, session_id, now),
        )
        connection.execute(
            """INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,
                 summary,success_criteria_json,phase,status,created_at,updated_at,last_agent_turn_id)
               VALUES('objective-attribution',?,?,'learning','检查整数运算',?,
                 'guided_practice','active',?,?, 'turn-attribution')""",
            (CHILD, session_id, json.dumps(["独立完成"]), now, now),
        )
        plan_child = "child-junior-3" if mismatched_plan_child else CHILD
        connection.execute(
            """INSERT INTO agent_plan(plan_id,objective_id,child_id,session_id,agent_turn_id,
                 planner_version,context_manifest_json,plan_json,status,created_at,updated_at)
               VALUES('plan-attribution','objective-attribution',?,?, 'turn-attribution','test',
                 '{}','{}','completed',?,?)""",
            (plan_child, session_id, now, now),
        )
        connection.execute(
            """INSERT INTO agent_step(step_id,plan_id,step_index,capability_name,
                 capability_version,execution_location,risk_level,arguments_json,
                 expected_evidence_json,status,idempotency_key,created_at,updated_at)
               VALUES('step-attribution','plan-attribution',0,'learning.evaluate_response','v2',
                 'server','medium',?,?,'completed','step-key',?,?)""",
            (
                json.dumps({"knowledge_node_id": "math.integer.add", "evidence_id": "evidence-1"}),
                json.dumps(["answer_receipt"]), now, now,
            ),
        )
        connection.execute(
            """INSERT INTO agent_decision_record(
                 decision_record_id,plan_id,child_id,session_id,agent_turn_id,snapshot_id,
                 evidence_fingerprint,binding_status,strategy_version,strategy_experiment_id,
                 selected_capability,selected_capability_version,candidate_set_json,
                 constraints_json,adoption_reason,rejected_candidates_json,
                 expected_outcomes_json,stop_conditions_json,rollback_action,
                 planner_version,contract_version,created_at)
               VALUES('decision-attribution','plan-attribution',?,?, 'turn-attribution',?,?,
                 'bound_current','strategy-v2','experiment-1','learning.evaluate_response','v2',
                 '[]','{}','需要核实','[]',?,?,
                 '取消检查','test','decision_record_v1',?)""",
            (
                CHILD, session_id, snapshot["snapshot_id"], snapshot["evidence_fingerprint"],
                json.dumps(["independent_answer"]), json.dumps(["stop_on_refusal"]), now,
            ),
        )
        connection.execute(
            """INSERT INTO agent_outcome(outcome_id,objective_id,child_id,outcome_level,
                 evidence_refs_json,confidence,summary,created_at,client_action_id)
               VALUES('outcome-attribution','objective-attribution',?,'independent',?,.86,
                 '本次独立作答证据合格',?,'outcome-attribution-action')""",
            (CHILD, json.dumps([{"type": "child_response", "agent_step_id": "step-attribution"}]), now),
        )
        connection.commit()
    return database, now


def test_outcome_is_bound_to_decision_snapshot_strategy_and_task(tmp_path: Path) -> None:
    database, now = _seed(tmp_path)
    with database.connect() as connection:
        attribution_id = persist(
            connection, outcome_id="outcome-attribution", step_id="step-attribution",
            created_at=now,
        )
        connection.commit()
        saved = for_outcome(connection, "outcome-attribution")
        repeated = persist(
            connection, outcome_id="outcome-attribution", step_id="step-attribution",
            created_at=now,
        )

    assert repeated == attribution_id
    assert saved["binding_status"] == "bound_decision"
    assert saved["decision_record_id"] == "decision-attribution"
    assert saved["strategy_version"] == "strategy-v2"
    assert saved["task_binding"] == {
        "evidence_id": "evidence-1", "knowledge_node_id": "math.integer.add",
    }
    assert saved["expected_outcomes"] == ["independent_answer"]
    assert saved["observed_outcome"]["outcome_level"] == "independent"
    assert saved["attribution_strength"] == "association_only"
    assert saved["causal_claim_allowed"] is False
    assert saved["cloud_transmission_allowed"] is False

    restarted = Database(database.path)
    with restarted.connect() as connection:
        restored = for_outcome(connection, "outcome-attribution")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE agent_outcome_attribution SET strategy_version='changed' WHERE outcome_id='outcome-attribution'"
            )
    assert restored["outcome_evidence_digest"] == saved["outcome_evidence_digest"]


def test_cross_scope_outcome_cannot_be_attributed(tmp_path: Path) -> None:
    database, now = _seed(tmp_path, mismatched_plan_child=True)
    with database.connect() as connection:
        with pytest.raises(OutcomeAttributionError, match="SCOPE_MISMATCH"):
            persist(
                connection, outcome_id="outcome-attribution", step_id="step-attribution",
                created_at=now,
            )
