import json

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, InvalidDeviceReceiptError, parse_agent_plan_output
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository


@pytest.fixture
def setup(tmp_path):
    database = Database(tmp_path / "executor.db")
    database.initialize()
    repository = StudyRepository(database)
    repository.seed_family()
    session = repository.start_session("child-primary-4", "start-executor-test")
    sid = session["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('daily','child-primary-4',?,date('now'),'active',20,'test',1,datetime('now'),'daily')""", (sid,))
        connection.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type,active_seconds)
            VALUES ('task','daily','数学','数学练习',20,1,'active',1,'school_homework','homework','written',60)""")
        connection.commit()
    return database, repository, sid


def turn(setup, key, capability, arguments, transcript="嗯", requires_reply=False):
    database, _, sid = setup
    learning = capability.startswith("learning.")
    payload = {
        "objective": {"mode": "new", "type": "learning" if learning else "execution", "summary": "当前任务", "success_criteria": ["可靠核实"]},
        "next_step": {"capability": capability, "arguments": arguments},
        "spoken_transition": "请说说你的理解。" if learning else "我们核对一下。",
        "requires_child_reply": requires_reply, "confidence": .99,
    }
    decision = parse_agent_plan_output(json.dumps(payload, ensure_ascii=False), "study", CapabilityRegistry())
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        connection.execute("""INSERT INTO agent_turn(agent_turn_id,session_id,child_id,transcript,intent,response_text,action_json,tool_status,created_at,client_action_id,orchestrator_version)
            VALUES (?,?,'child-primary-4',?,?,?,'{}','pending',datetime('now'),?,'test')""",
            (key, sid, transcript, capability, payload["spoken_transition"], key))
        store.persist_plan(connection, child_id="child-primary-4", session_id=sid,
            agent_turn_id=key, client_action_id=key, decision=decision, context_manifest={})
        connection.commit()
    return key


def test_model_cannot_fill_child_checked_checklist_or_complete_task(setup):
    database, repository, _ = setup
    key = turn(setup, "forged", "task.complete", {"completion_evidence_type": "child_checked", "confirmed_checks": ["task_finished", "missing_items_checked"], "confirmation_quote": "嗯"})
    result = AgentCapabilityExecutor(database, repository).execute(key)
    assert result["result"]["status"] == "awaiting_completion_check"
    with database.connect() as connection:
        row = connection.execute("SELECT status,completion_checklist_json FROM plan_item WHERE plan_item_id='task'").fetchone()
        assert tuple(row) == ("active", "[]")
        assert connection.execute("SELECT status FROM agent_plan WHERE agent_turn_id=?", (key,)).fetchone()[0] == "waiting_child"


def test_request_response_executes_and_waits_across_callback_restart_and_replay(setup):
    database, repository, sid = setup
    key = turn(setup, "ask", "learning.request_response", {"prompt": "请用自己的话说明约分。", "subject": "数学"}, requires_reply=False)
    executor = AgentCapabilityExecutor(database, repository)
    result = executor.execute(key)
    assert result["response_text"] == "请用自己的话说明约分。"
    assert result["result"]["status"] == "awaiting_child_response"
    assert result["evidence"] == ["response_requested"]
    store = AgentRuntimeStore(database)
    assert store.pending_child_reply(sid) is not None
    with database.connect() as connection:
        store.complete_turn_execution(connection, key, True, None, result)
        connection.commit()
    reopened = Database(database.path)
    assert AgentRuntimeStore(reopened).pending_child_reply(sid) is not None
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(key) == result
    with reopened.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mastery_update_event").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM agent_objective").fetchone()[0] == "active"
        assert connection.execute("SELECT status FROM agent_step").fetchone()[0] == "completed"


def test_real_second_turn_confirmation_completes_only_bound_task_and_replays(setup):
    database, repository, _ = setup
    executor = AgentCapabilityExecutor(database, repository)
    first = executor.execute(turn(setup, "ask-check", "task.complete", {"completion_evidence_type": "child_checked"}))
    assert first["result"]["status"] == "awaiting_completion_check"
    answer = "题目全做完了，我也逐项看过，没有漏题"
    key = turn(setup, "confirm-check", "task.complete", {"completion_evidence_type": "child_checked", "confirmed_checks": ["task_finished", "missing_items_checked"], "confirmation_quote": answer}, transcript=answer)
    result = executor.execute(key)
    assert result["result"]["status"] == "completed"
    assert executor.execute(key) == result
    with database.connect() as connection:
        row = connection.execute("SELECT status,completion_checklist_json FROM plan_item WHERE plan_item_id='task'").fetchone()
        assert row["status"] == "completed"
        assert json.loads(row["completion_checklist_json"]) == ["task_finished", "missing_items_checked"]
        assert connection.execute("SELECT COUNT(*) FROM mastery_update_event").fetchone()[0] == 0


@pytest.mark.parametrize("quote,checks", [("捏造的回答", ["task_finished", "missing_items_checked"]), ("只做完了", ["task_finished"])])
def test_unbound_or_partial_confirmation_keeps_task_open(setup, quote, checks):
    database, repository, _ = setup
    executor = AgentCapabilityExecutor(database, repository)
    executor.execute(turn(setup, "ask-first", "task.complete", {"completion_evidence_type": "child_checked"}))
    key = turn(setup, "not-confirmed", "task.complete", {"completion_evidence_type": "child_checked", "confirmed_checks": checks, "confirmation_quote": quote}, transcript="只做完了")
    result = executor.execute(key)
    assert result["result"]["status"] == "awaiting_completion_check"
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id='task'").fetchone()[0] == "active"


def test_task_confirmation_wait_survives_outcome_callback_and_cannot_bind_another_task(setup):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    key = turn(setup, "check-original", "task.complete", {"completion_evidence_type": "child_checked"})
    result = executor.execute(key)
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        store.complete_turn_execution(connection, key, True, None, result)
        connection.commit()
    pending = store.pending_task_completion_check(sid)
    assert pending is not None and pending["plan_item_id"] == "task"
    assert AgentCapabilityExecutor(Database(database.path), repository).execute(key) == result
    with database.connect() as connection:
        connection.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id='task'")
        connection.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type,active_seconds)
            VALUES ('other-task','daily','数学','另一项练习',20,1,'active',2,'school_homework','other-homework','written',60)""")
        connection.commit()
    answer = "都检查过了"
    confirm = turn(setup, "wrong-task-confirm", "task.complete", {"completion_evidence_type": "child_checked", "confirmed_checks": ["task_finished", "missing_items_checked"], "confirmation_quote": answer}, transcript=answer)
    response = executor.execute(confirm)
    assert response["result"]["status"] == "awaiting_completion_check"
    assert response["result"]["plan_item_id"] == "other-task"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_item WHERE status='completed'").fetchone()[0] == 0


def test_untrusted_completion_evidence_type_is_rejected_before_execution(setup):
    with pytest.raises(ValueError, match="unsupported capability argument"):
        turn(setup, "fake-evidence", "task.complete", {"completion_evidence_type": "model_verified"})


def test_two_callers_cannot_execute_same_step_twice(setup, monkeypatch):
    database, repository, _ = setup
    first, second = AgentCapabilityExecutor(database, repository), AgentCapabilityExecutor(database, repository)
    key = turn(setup, "concurrent-ask", "learning.request_response", {"prompt": "请说说约分。", "subject": "数学"})
    actual_dispatch = first._dispatch

    def dispatch(step):
        with pytest.raises(ValueError, match="AGENT_STEP_ALREADY_EXECUTING"):
            second.execute(key)
        return actual_dispatch(step)

    monkeypatch.setattr(first, "_dispatch", dispatch)
    result = first.execute(key)
    assert second.execute(key) == result


def check_turn(setup, key, quote, confirmed=(), revoked=(), evidence="child_checked"):
    return turn(setup, key, "task.complete", {
        "completion_evidence_type": evidence, "confirmed_checks": list(confirmed),
        "revoked_checks": list(revoked), "confirmation_quote": quote,
    }, transcript=quote)


def test_partial_checks_accumulate_after_restart_without_repeating_confirmed_items(setup):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    first_quote = "题做完了，但还没检查"
    first_id = check_turn(setup, "partial-first", first_quote, ["task_finished"], ["missing_items_checked"])
    first = executor.execute(first_id)
    assert first["result"]["confirmed_checks"] == ["task_finished"]
    assert first["result"]["missing_checks"] == ["missing_items_checked"]
    assert "题目是否都做完" not in first["response_text"]
    assert "是否逐项检查过漏题" in first["response_text"]
    with database.connect() as connection:
        AgentRuntimeStore(database).complete_turn_execution(connection, first_id, True, None, first)
        connection.commit()
    reopened = Database(database.path)
    restored = (reopened, StudyRepository(reopened), sid)
    executor = AgentCapabilityExecutor(*restored[:2])
    pending = AgentRuntimeStore(reopened).pending_task_completion_check(sid)
    assert pending["confirmed_checks"] == ["task_finished"]
    assert pending["check_confirmations"]["task_finished"]["response_turn_id"] == first_id
    assert pending["check_confirmations"]["task_finished"]["quote"] == first_quote
    assert executor.execute(first_id) == first
    last_quote = "漏题我也检查过了"
    last_id = check_turn(restored, "partial-last", last_quote, ["missing_items_checked"])
    last = executor.execute(last_id)
    assert last["result"]["status"] == "completed"
    evidence = last["result"]["completion_confirmation"]["check_confirmations"]
    assert evidence["task_finished"]["response_turn_id"] == first_id
    assert evidence["missing_items_checked"]["quote"] == last_quote
    assert executor.execute(last_id) == last
    assert AgentRuntimeStore(reopened).pending_task_completion_check(sid) is None
    with reopened.connect() as connection:
        item = connection.execute("SELECT status,completion_checklist_json FROM plan_item WHERE plan_item_id='task'").fetchone()
        assert item["status"] == "completed"
        assert json.loads(item["completion_checklist_json"]) == ["task_finished", "missing_items_checked"]
        assert connection.execute("SELECT COUNT(*) FROM mastery_update_event").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_plan WHERE status='waiting_child'").fetchone()[0] == 0


@pytest.mark.parametrize("evidence", ["child_checked", "voice_confirmed", "needs_correction"])
def test_correction_revokes_previous_check_and_only_remaining_item_is_requested(setup, evidence):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    executor.execute(check_turn(setup, "done-first", "题做完了，但还没检查", ["task_finished"], ["missing_items_checked"]))
    corrected = executor.execute(check_turn(setup, "correct-first", "刚才说错了，还漏了一道，漏题已经检查过了",
                                          ["missing_items_checked"], ["task_finished"], evidence))
    assert corrected["result"]["status"] == "awaiting_completion_check"
    assert corrected["result"]["confirmed_checks"] == ["missing_items_checked"]
    assert corrected["result"]["missing_checks"] == ["task_finished"]
    assert "是否逐项检查过漏题" not in corrected["response_text"]
    reopened = Database(database.path)
    pending = AgentRuntimeStore(reopened).pending_task_completion_check(sid)
    revoked = pending["check_confirmations"]["task_finished"]
    assert revoked["confirmed"] is False and revoked["response_turn_id"] == "correct-first"
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute("done-first")["result"]["confirmed_checks"] == ["task_finished"]
    assert AgentRuntimeStore(reopened).pending_task_completion_check(sid)["confirmed_checks"] == ["missing_items_checked"]
    final_id = check_turn(setup, "done-again", "那道漏题现在也做完了", ["task_finished"])
    final = executor.execute(final_id)
    assert final["result"]["status"] == "completed"
    assert final["result"]["completion_confirmation"]["check_confirmations"]["task_finished"]["response_turn_id"] == final_id


def test_revocation_wins_over_overlapping_model_confirmation(setup):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    executor.execute(check_turn(setup, "has-one", "做完了，但还没检查", ["task_finished"], ["missing_items_checked"]))
    result = executor.execute(check_turn(setup, "deny-one", "不是，题其实也没做完", ["task_finished"], ["task_finished"]))
    assert result["result"]["confirmed_checks"] == []
    assert result["result"]["missing_checks"] == ["task_finished", "missing_items_checked"]
    assert AgentRuntimeStore(database).pending_task_completion_check(sid)["check_confirmations"]["task_finished"]["confirmed"] is False


def test_unbound_quote_does_not_add_or_revoke_durable_checks(setup):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    executor.execute(check_turn(setup, "grounded-one", "题做完了，还没检查", ["task_finished"], ["missing_items_checked"]))
    invalid = turn(setup, "invalid-quote", "task.complete", {"completion_evidence_type": "child_checked",
                   "confirmed_checks": ["missing_items_checked"], "revoked_checks": ["task_finished"], "confirmation_quote": "编造的确认"},
                   transcript="你再说说要检查什么")
    result = executor.execute(invalid)
    assert result["result"]["confirmed_checks"] == ["task_finished"]
    assert result["result"]["status"] == "awaiting_completion_check"
    assert AgentRuntimeStore(database).pending_task_completion_check(sid)["check_confirmations"]["task_finished"]["response_turn_id"] == "grounded-one"


def test_late_completion_callbacks_cannot_reopen_superseded_check_prompts(setup):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    first_id = check_turn(setup, "late-first", "题做完了，还没检查", ["task_finished"], ["missing_items_checked"])
    first = executor.execute(first_id)
    corrected_id = check_turn(setup, "late-correction", "说错了，题没做完", revoked=["task_finished"])
    executor.execute(corrected_id)
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        store.complete_turn_execution(connection, first_id, True, None, first)
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM agent_plan WHERE status='waiting_child'").fetchone()[0] == 1
    assert store.pending_task_completion_check(sid)["confirmed_checks"] == []
    answer = "题现在补完了，漏题也检查过了"
    last_id = turn(setup, "late-final", "task.complete", {"completion_evidence_type": "child_checked",
                   "confirmed_checks": ["task_finished", "missing_items_checked"], "confirmation_quote": answer},
                   transcript=answer, requires_reply=True)
    last = executor.execute(last_id)
    assert last["result"]["status"] == "completed"
    with database.connect() as connection:
        store.complete_turn_execution(connection, last_id, True, None, last)
        store.complete_turn_execution(connection, first_id, True, None, first)
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM agent_plan WHERE status='waiting_child'").fetchone()[0] == 0


def test_pending_checks_cannot_be_bypassed_by_voice_confirmed_without_item_updates(setup):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    executor.execute(check_turn(setup, "partial-check", "题做完了，但没检查", ["task_finished"], ["missing_items_checked"]))
    result = executor.execute(turn(setup, "no-check-update", "task.complete", {"completion_evidence_type": "voice_confirmed"}, transcript="嗯"))
    assert result["result"]["status"] == "awaiting_completion_check"
    assert result["result"]["missing_checks"] == ["missing_items_checked"]
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id='task'").fetchone()[0] == "active"


@pytest.mark.parametrize("source", ["agent_runtime_deterministic_v1", "agent_runtime_plan_v1"])
def test_only_local_legacy_reply_can_bind_explicit_all_items_answer_without_cloud_fields(setup, source):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    first_id = turn(setup, "legacy-first", "task.complete", {"completion_evidence_type": "child_checked"},
                    transcript="做完了并且检查过了")
    first = executor.execute(first_id)
    assert first["result"]["status"] == "awaiting_completion_check"
    answer = "刚才问的每一项我都完成并检查了"
    reply_id = turn(setup, "legacy-explicit-reply", "task.complete", {"completion_evidence_type": "child_checked"}, transcript=answer)
    with database.connect() as connection:
        connection.execute("UPDATE agent_turn SET orchestrator_version=? WHERE agent_turn_id=?", (source, reply_id))
        connection.commit()
    result = executor.execute(reply_id)
    if source != "agent_runtime_deterministic_v1":
        assert result["result"]["status"] == "awaiting_completion_check"
        return
    assert result["result"]["status"] == "completed"
    records = result["result"]["completion_confirmation"]["check_confirmations"]
    assert set(records) == {"task_finished", "missing_items_checked"}
    for record in records.values():
        assert record["quote"] == answer and record["response_turn_id"] == reply_id
        assert record["prompt_step_id"] is not None
        assert record["evidence_type"] == "child_self_report"
    assert AgentRuntimeStore(database).pending_task_completion_check(sid) is None
    assert executor.execute(reply_id) == result
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mastery_update_event").fetchone()[0] == 0


@pytest.mark.parametrize("answer", ["刚才问的每一项我都完成并检查了吗？", "我没说刚才问的每一项我都完成并检查了", "刚才问的每一项我都完成并检查了，但其实还没检查漏题"])
def test_legacy_negative_question_or_qualified_reply_cannot_fill_checks(setup, answer):
    database, repository, _ = setup
    executor = AgentCapabilityExecutor(database, repository)
    executor.execute(turn(setup, "legacy-prompt", "task.complete", {"completion_evidence_type": "child_checked"}))
    reply_id = turn(setup, "legacy-negative", "task.complete", {"completion_evidence_type": "child_checked"}, transcript=answer)
    with database.connect() as connection:
        connection.execute("UPDATE agent_turn SET orchestrator_version='agent_runtime_deterministic_v1' WHERE agent_turn_id=?", (reply_id,))
        connection.commit()
    result = executor.execute(reply_id)
    assert result["result"]["status"] == "awaiting_completion_check"
    assert result["result"]["confirmed_checks"] == []


@pytest.mark.parametrize("switch_task", [False, True])
def test_check_history_cannot_cross_task_or_started_at_scope(setup, switch_task):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    executor.execute(check_turn(setup, "old-scope", "题做完了，还没检查", ["task_finished"], ["missing_items_checked"]))
    with database.connect() as connection:
        if switch_task:
            connection.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id='task'")
            connection.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type,active_seconds)
                VALUES ('new-task','daily','数学','新任务',20,1,'active',2,'school_homework','new','written',60)""")
        else:
            connection.execute("UPDATE plan_item SET started_at='2026-09-07T09:00:00+00:00' WHERE plan_item_id='task'")
        connection.commit()
    assert AgentRuntimeStore(database).pending_task_completion_check(sid) is None
    result = executor.execute(check_turn(setup, "new-scope", "漏题已经检查过了", ["missing_items_checked"]))
    assert result["result"]["status"] == "awaiting_completion_check"
    assert "task_finished" not in result["result"]["confirmed_checks"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_item WHERE status='completed'").fetchone()[0] == 0


def test_check_context_keeps_raw_confirmation_history_local(setup):
    database, repository, sid = setup
    executor = AgentCapabilityExecutor(database, repository)
    quote = "题做完了，但还没检查"
    executor.execute(check_turn(setup, "private-check-history", quote, ["task_finished"], ["missing_items_checked"]))
    context = AgentRuntimeStore(database).assemble_context(sid, [])
    projected = context["pending_task_completion_check"]
    assert projected["confirmed_checks"] == ["task_finished"]
    assert projected["missing_checks"] == ["missing_items_checked"]
    assert "check_confirmations" not in projected
    assert quote not in json.dumps(projected, ensure_ascii=False)


@pytest.mark.parametrize("revoked", ["task_finished", [1], ["task_finished"] * 21])
def test_revoked_check_arguments_are_validated(setup, revoked):
    with pytest.raises(ValueError, match="invalid revoked checks"):
        turn(setup, "invalid-revocation", "task.complete", {"revoked_checks": revoked})


@pytest.mark.parametrize("capability", ["recall.add", "recall.add_batch", "homework.add", "homework.add_batch"])
def test_capture_capabilities_accept_only_the_three_collection_states(capability):
    registry = CapabilityRegistry()
    states = ("check_in", "classroom_recall", "homework_capture")
    for state in states:
        spec = registry.require(capability, state)
        assert spec.allowed_states == states
        assert "不自动跳阶段" in spec.description
        assert "不虚构空回忆" in spec.description
    for state in ("study", "plan_review"):
        with pytest.raises(ValueError, match="unavailable"):
            registry.require(capability, state)


@pytest.mark.parametrize("phase,step", [("naming", None)] + [
    ("active", step) for step in ("wake_practice", "study_practice", "document_practice", "finish_practice")
])
def test_onboarding_resume_capability_requires_a_saved_restart_receipt(setup, phase, step):
    database, _, _ = setup
    spec = CapabilityRegistry().require("onboarding.resume", "check_in")
    assert spec.action_type == "resume_voice_tutorial" and spec.execution_location == "client_device_bridge"
    assert spec.objective_types == ("execution", "companion")
    assert spec.allowed_states == spec.required_arguments == ()
    assert spec.expected_evidence == ("onboarding_restarted",)
    key = turn(setup, "restart-onboarding", "onboarding.resume", {})
    with database.connect() as connection:
        connection.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?", (json.dumps({"type": "resume_voice_tutorial"}), key))
        connection.commit()
    receipt = {"execution_receipt": {"capability": "onboarding.resume", "execution_location": "client_device_bridge", "action_type": "resume_voice_tutorial", "status": "completed", "completed_at": "2026-09-06T10:00:00+00:00", "output": {"onboarding_restarted": True, "onboarding_phase": phase, "onboarding_step": step}}}
    with database.connect() as connection:
        AgentRuntimeStore.validate_device_receipt(connection, key, receipt)
        for change in ({"onboarding_restarted": False}, {"onboarding_restarted": 1}, {"onboarding_phase": "error"}, {"onboarding_phase": "saving"}, {"onboarding_phase": "idle", "onboarding_step": "completed"}, {"onboarding_phase": "active", "onboarding_step": None}, {"onboarding_phase": "naming", "onboarding_step": "wake_practice"}, {"onboarding_step": "deferred"}, {"extra": "untrusted"}):
            invalid = json.loads(json.dumps(receipt))
            invalid["execution_receipt"]["output"].update(change)
            with pytest.raises(InvalidDeviceReceiptError):
                AgentRuntimeStore.validate_device_receipt(connection, key, invalid)
        for missing in receipt["execution_receipt"]["output"]:
            invalid = json.loads(json.dumps(receipt))
            del invalid["execution_receipt"]["output"][missing]
            with pytest.raises(InvalidDeviceReceiptError):
                AgentRuntimeStore.validate_device_receipt(connection, key, invalid)
