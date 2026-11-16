from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from home_study_agent.agent_runtime import (
    AgentRuntimeStore, CapabilityRegistry, ObjectiveDraft, PlannedStep, AgentPlanDecision,
)
from home_study_agent.database import Database
from home_study_agent.tutor_ocr_confirmation import OcrTutorConfirmationService
from test_agent_runtime import seed_session


@pytest.fixture
def runtime(tmp_path):
    database = Database(tmp_path / "receipts.db")
    database.initialize()
    OcrTutorConfirmationService(database).ensure_schema()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    now = datetime.now(timezone.utc)
    capability = CapabilityRegistry().owner_for_action_type("request_capture")
    with database.connect() as connection:
        connection.execute("""INSERT INTO agent_turn
            (agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
             orchestrator_version,created_at,client_action_id)
            VALUES ('capture-turn','child-runtime',?,'capture','capture','waiting',?,'test',?,'capture-action')""",
            (session_id, json.dumps({"type": "request_capture"}), now.isoformat()))
        decision = AgentPlanDecision(
            objective=ObjectiveDraft(mode="none", objective_type=None, summary=None, success_criteria=(), phase="capture"),
            step=PlannedStep(capability=capability, arguments={}, reason="capture", expected_evidence=capability.expected_evidence),
            spoken_transition="Waiting for capture", requires_child_reply=True, confidence=.9,
        )
        store.persist_plan(connection, child_id="child-runtime", session_id=session_id,
                           agent_turn_id="capture-turn", client_action_id="capture-action",
                           decision=decision, context_manifest={})
        connection.execute("""INSERT INTO capture_artifact
            (artifact_id,child_id,session_id,artifact_type,extracted_text,details_json,status,created_at,client_action_id)
            VALUES ('artifact','child-runtime',?,'document_ocr','1 + 1 = ?', '{}','completed',?,'ocr-action')""",
            (session_id, (now + timedelta(milliseconds=1)).isoformat()))
        connection.commit()
    return database, store, session_id, capability.name


def receipt(runtime, status="recognized"):
    # Exact envelope emitted by deviceCommandReceipt.ts, with a real persisted artifact.
    value = {
        "capability": runtime[3], "execution_location": "client_device_bridge",
        "action_type": "request_capture", "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "output": {
            "command_id": "capture-turn", "device_status": status,
            "capture_artifact_id": "artifact", "artifact_id": "artifact",
            "ocr": {"status": "completed" if status == "recognized" else "not_completed",
                    "artifact_id": "artifact", "error_code": None},
        },
    }
    return {"execution_receipt": value, "client_bridge_receipt": deepcopy(value)}


def complete(runtime, result, succeeded=True, error=None):
    with runtime[0].connect() as connection:
        runtime[1].complete_turn_execution(connection, "capture-turn", succeeded, error, result)
        connection.commit()


def stored(runtime):
    with runtime[0].connect() as connection:
        return dict(connection.execute("SELECT status,result_json FROM agent_step").fetchone())


def rejects(runtime, result, error=None):
    before = stored(runtime)
    with pytest.raises(ValueError, match="DEVICE_RECEIPT_"):
        complete(runtime, result, error=error)
    assert stored(runtime) == before


@pytest.mark.parametrize("result", [None, {}, {"execution_receipt": None}, {"execution_receipt": []}, {"execution_receipt": {}}])
def test_success_requires_a_real_receipt(runtime, result):
    rejects(runtime, result)


@pytest.mark.parametrize("field,value", [
    ("capability", "profile.switch"), ("execution_location", "server"),
    ("action_type", "retry_capture"), ("status", "failed"),
    ("completed_at", "not-a-date"), ("output", None),
])
def test_receipt_must_match_the_persisted_execution(runtime, field, value):
    result = receipt(runtime)
    for item in result.values():
        item[field] = value
    rejects(runtime, result)


def test_duplicate_envelopes_cannot_disagree(runtime):
    result = receipt(runtime)
    result["client_bridge_receipt"]["output"]["device_status"] = "failed"
    rejects(runtime, result)


@pytest.mark.parametrize("field,value", [
    ("command_id", "another-turn"), ("device_status", "accepted"),
    ("device_status", "failed"), ("device_status", "cancelled"),
    ("capture_artifact_id", "missing"), ("artifact_id", "different"),
    ("ocr", {"status": "failed", "artifact_id": "artifact", "error_code": "OCR_FAILED"}),
    ("ocr", {"status": "completed", "artifact_id": "different", "error_code": None}),
])
def test_capture_success_requires_associated_evidence(runtime, field, value):
    result = receipt(runtime)
    for item in result.values():
        item["output"][field] = value
    rejects(runtime, result)


@pytest.mark.parametrize("mutation", ["missing", "other_child", "other_session", "wrong_type", "failed", "empty_text", "stale"])
def test_artifact_must_be_current_and_owned(runtime, mutation):
    with runtime[0].connect() as connection:
        if mutation == "missing":
            connection.execute("DELETE FROM capture_artifact")
        elif mutation in {"other_child", "other_session"}:
            connection.execute("""INSERT INTO daily_session
                (session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                SELECT 'other-session',child_id,'completed',state,started_at,last_checkpoint_at,'other-session-action' FROM daily_session LIMIT 1""")
            if mutation == "other_child":
                connection.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                    SELECT 'other-child','Other',grade,curriculum_region,status,created_at,updated_at FROM child_profile LIMIT 1""")
                connection.execute("UPDATE capture_artifact SET child_id='other-child'")
            else:
                connection.execute("UPDATE capture_artifact SET session_id='other-session'")
        else:
            column, value = {
                "wrong_type": ("artifact_type", "voice"), "failed": ("status", "failed"),
                "empty_text": ("extracted_text", " \n"),
                "stale": ("created_at", "2000-01-01T00:00:00+00:00"),
            }[mutation]
            connection.execute(f"UPDATE capture_artifact SET {column}=?", (value,))
        connection.commit()
    rejects(runtime, receipt(runtime))


@pytest.mark.parametrize("status", ["captured", "recognized"])
@pytest.mark.parametrize("artifact_status", ["completed", "quality_warning"])
def test_valid_capture_stages_do_not_confirm_the_question(runtime, status, artifact_status):
    with runtime[0].connect() as connection:
        connection.execute("UPDATE capture_artifact SET status=?", (artifact_status,))
        if status == "captured":
            connection.execute("UPDATE capture_artifact SET extracted_text=''")
        connection.commit()
    result = receipt(runtime, status)
    complete(runtime, result)
    assert stored(runtime)["status"] == "completed"
    assert json.loads(stored(runtime)["result_json"]) == result
    with runtime[0].connect() as connection:
        assert connection.execute("SELECT status FROM agent_plan").fetchone()[0] == "waiting_child"
        assert connection.execute("SELECT status FROM capture_artifact").fetchone()[0] == artifact_status
        assert connection.execute("SELECT COUNT(*) FROM tutor_ocr_confirmation").fetchone()[0] == 0


@pytest.mark.parametrize("envelope", ["execution_receipt", "client_bridge_receipt"])
def test_single_supported_envelope_is_accepted(runtime, envelope):
    result = receipt(runtime)
    complete(runtime, {envelope: result[envelope]})
    assert stored(runtime)["status"] == "completed"


@pytest.mark.parametrize("error", ["DEVICE_FAILED", "OCR_FAILED"])
def test_success_with_error_is_rejected(runtime, error):
    rejects(runtime, receipt(runtime), error)


def test_failure_without_receipt_remains_recordable(runtime):
    complete(runtime, None, succeeded=False, error="DEVICE_DISCONNECTED")
    assert stored(runtime)["status"] == "failed"


def test_legacy_device_turn_without_plan_cannot_bypass_validation(runtime):
    with runtime[0].connect() as connection:
        connection.execute("DELETE FROM agent_step")
        connection.execute("DELETE FROM agent_plan")
        connection.commit()
    with pytest.raises(ValueError, match="DEVICE_RECEIPT_"):
        complete(runtime, None)


def test_non_device_completion_contract_is_unchanged(runtime):
    with runtime[0].connect() as connection:
        connection.execute("UPDATE agent_step SET execution_location='server',capability_name='session.describe_plan'")
        connection.execute("UPDATE agent_turn SET action_json=?", ('{"type":"describe_plan"}',))
        connection.commit()
    complete(runtime, {"result": "unchanged"})
    assert stored(runtime)["status"] == "completed"


def test_invalid_device_result_rolls_back_the_orchestrator_turn(runtime):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.repository import StudyRepository
    from home_study_agent.config import Settings

    orchestrator = AgentOrchestrator(runtime[0], StudyRepository(runtime[0]), None,
        Settings(data_dir=runtime[0].path.parent, database_path=runtime[0].path))
    with pytest.raises(ValueError, match="DEVICE_RECEIPT_"):
        orchestrator.complete_turn("capture-turn", "Incorrect success", "completed", None)
    with runtime[0].connect() as connection:
        turn = connection.execute("SELECT tool_status,response_text FROM agent_turn WHERE agent_turn_id='capture-turn'").fetchone()
        assert turn["tool_status"] == "pending"
        assert turn["response_text"] == "waiting"


@pytest.mark.parametrize("field,value", [("device_status", []), ("artifact_id", {}), ("ocr", []),
                                         ("ocr", {"artifact_id": [], "status": "completed"})])
def test_malformed_nested_values_raise_the_typed_error(runtime, field, value):
    result = receipt(runtime)
    for item in result.values():
        item["output"][field] = value
    rejects(runtime, result)


def test_artifact_already_receipted_to_another_command_is_rejected(runtime):
    with runtime[0].connect() as connection:
        connection.execute("""INSERT INTO agent_turn
            (agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
             orchestrator_version,created_at,client_action_id)
            SELECT 'other-turn',child_id,session_id,transcript,intent,response_text,action_json,
             orchestrator_version,created_at,'other-turn-action' FROM agent_turn LIMIT 1""")
        connection.execute("""INSERT INTO agent_plan
            SELECT 'other-plan',objective_id,child_id,session_id,'other-turn',planner_version,
                context_manifest_json,plan_json,status,created_at,updated_at FROM agent_plan LIMIT 1""")
        connection.execute("""INSERT INTO agent_step
            SELECT 'other-step','other-plan',step_index,capability_name,capability_version,
                execution_location,risk_level,arguments_json,expected_evidence_json,'completed',
                'other-idempotency',?,error_code,created_at,updated_at FROM agent_step LIMIT 1""",
                (json.dumps(receipt(runtime)),))
        connection.commit()
    rejects(runtime, receipt(runtime))


def test_completed_receipt_survives_restart_and_exact_retry(runtime):
    result = receipt(runtime)
    complete(runtime, result)
    reopened = AgentRuntimeStore(Database(runtime[0].path))
    with runtime[0].connect() as connection:
        reopened.complete_turn_execution(connection, "capture-turn", True, None, result)
        connection.commit()
    assert json.loads(stored(runtime)["result_json"]) == result


def test_plan_and_turn_session_must_match(runtime):
    with runtime[0].connect() as connection:
        connection.execute("""INSERT INTO daily_session
            (session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            SELECT 'other-session',child_id,'completed',state,started_at,last_checkpoint_at,'other-session-action' FROM daily_session LIMIT 1""")
        connection.execute("UPDATE agent_plan SET session_id='other-session'")
        connection.commit()
    rejects(runtime, receipt(runtime))


def test_camera_pause_receipt_requires_saved_state_and_release_without_capture_fields(runtime):
    capability = CapabilityRegistry().owner_for_action_type("pause_observation")
    with runtime[0].connect() as connection:
        connection.execute("UPDATE agent_step SET capability_name=?", (capability.name,))
        connection.execute("UPDATE agent_turn SET action_json=?", ('{"type":"pause_observation"}',))
        connection.commit()
    rejects(runtime, None)
    result = receipt(runtime)
    for item in result.values():
        item.update(capability=capability.name, action_type="pause_observation", output={"observation_mode": "off"})
    rejects(runtime, result)
    with runtime[0].connect() as connection:
        connection.execute("UPDATE daily_session SET observation_status='paused' WHERE session_id=?", (runtime[2],))
        connection.commit()
    for item in result.values():
        item["output"] = {"device_type": "camera", "device_status": "paused", "frame_confirmed": False,
                          "lease_confirmed": True, "observation_status": "paused"}
    complete(runtime, result)
    assert stored(runtime)["status"] == "completed"
