import json
from uuid import uuid4

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, AgentPlanDecision, ObjectiveDraft, PlannedStep, CapabilityRegistry
from home_study_agent.learning_input_revision import LearningInputRevisionService, InputRevisionError
from home_study_agent.repository import StudyRepository
from test_mistake_gate_reliability import flow, CHILD
from test_agent_continuation import orchestrator_for
import asyncio


@pytest.fixture
def inputs(flow):
    db, _, sid, _ = flow
    service = LearningInputRevisionService(db)
    service.initialize()
    with db.connect() as c:
        for hid, title in (("math-a", "口算"), ("math-b", "应用题")):
            c.execute("""INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,
                expected_minutes,confirmation_status,status,created_at,client_action_id)
                VALUES (?,?,?,'数学','written',?,20,'child_confirmed','pending',datetime('now'),?)""", (hid, CHILD, sid, title, hid))
        c.execute("""INSERT INTO classroom_recall(recall_id,session_id,subject,summary,confidence,confirmation_status,
            source_refs_json,record_kind,created_at,client_action_id)
            VALUES ('unknown',?,'科学','暂时想不起来',0,'unknown','[]','child_statement',datetime('now'),'unknown')""", (sid,))
        c.commit()
    return db, sid, service


def command(inputs, utterance, capability, arguments, *, arrival=None, key=None):
    db, sid, service = inputs
    key = key or str(uuid4())
    arrival = arrival or service.capture_arrival(sid, key, utterance)
    turn = str(uuid4())
    store = AgentRuntimeStore(db)
    cap = CapabilityRegistry().require(capability, "study")
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,?,'input_revision','','{}','pending','test',datetime('now'),?)""", (turn, CHILD, sid, utterance, key))
        store.persist_plan(c, child_id=CHILD, session_id=sid, agent_turn_id=turn, client_action_id=key,
            context_manifest={"learning_input_arrival_id": arrival["arrival_id"]}, decision=AgentPlanDecision(
                ObjectiveDraft("none", None, None, (), "execute"),
                PlannedStep(cap, arguments, "Explicit revision", cap.expected_evidence), "", False, 1))
        c.execute("UPDATE agent_step SET status='running' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        c.execute("UPDATE agent_plan SET status='running' WHERE agent_turn_id=?", (turn,))
        c.commit()
    result = service.execute(turn)
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET tool_status='completed',response_text=? WHERE agent_turn_id=?", (result["response_text"], turn))
        c.execute("UPDATE agent_plan SET status='completed' WHERE agent_turn_id=?", (turn,))
        c.execute("UPDATE agent_step SET status='completed' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        c.commit()
    return result, turn


def propose(inputs, utterance="把口算的学科改成英语", target="口算", **overrides):
    args = {"kind": "homework", "operation": "update", "target_quote": target,
        "request_quote": utterance, "changes": {"subject": "英语"}, "change_quotes": {"subject": "英语"}, **overrides}
    return command(inputs, utterance, "learning_inputs.propose_revision", args)


def confirm(inputs):
    return command(inputs, "确认", "learning_inputs.confirm_revision", {"confirmation_quote": "确认"})


def test_named_item_requires_readback_then_versioned_confirmation(inputs):
    db, _, service = inputs
    result, _ = propose(inputs)
    assert result["status"] == "awaiting_confirmation" and "口算" in result["response_text"]
    with db.connect() as c:
        assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == "数学"
    applied, turn = confirm(inputs)
    assert applied["status"] == "applied" and applied["target_id"] == "math-a"
    assert service.execute(turn) == applied
    with db.connect() as c:
        assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == "英语"
        assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-b'").fetchone()[0] == "数学"


def test_same_subject_is_clarified_not_resolved_to_last_item(inputs):
    result, _ = propose(inputs, utterance="把数学那项改成英语", target="数学")
    assert result["status"] == "clarification_required"
    selected, _ = command(inputs, "口算", "learning_inputs.select_target", {"selection_quote": "口算"})
    assert selected["status"] == "awaiting_confirmation" and selected["target_id"] == "math-a"
    assert confirm(inputs)[0]["status"] == "applied"


def test_unknown_classroom_subject_edit_does_not_invent_classroom_facts(inputs):
    propose(inputs, utterance="科学改成英语", target="科学", kind="recall")
    confirm(inputs)
    with inputs[0].connect() as c:
        row = c.execute("SELECT subject,confirmation_status,summary FROM classroom_recall WHERE recall_id='unknown'").fetchone()
        assert tuple(row) == ("英语", "unknown", "暂时想不起来")


def test_changed_source_cannot_be_overwritten_by_old_confirmation(inputs):
    propose(inputs)
    with inputs[0].connect() as c:
        c.execute("UPDATE homework_item SET title='新的任务内容' WHERE homework_id='math-a'")
        c.commit()
    result, _ = confirm(inputs)
    assert result["status"] == "conflict"
    with inputs[0].connect() as c:
        assert c.execute("SELECT subject FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == "数学"


@pytest.mark.parametrize("quote,value", [("二十分钟", 20), ("十五分钟", 15), ("三分钟", 3),
    ("两百零五分钟", 205), ("一百二十三分钟", 123), ("20分钟", 20)])
def test_spoken_duration_is_grounded_without_arabic_digit_requirement(inputs, quote, value):
    result, _ = propose(inputs, utterance=f"口算改成{quote}", changes={"expected_minutes": value},
                        change_quotes={"expected_minutes": quote})
    assert result["status"] == "awaiting_confirmation"
    confirm(inputs)
    with inputs[0].connect() as c:
        assert c.execute("SELECT expected_minutes FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == value


@pytest.mark.parametrize("quote", ["不是二十分钟", "二十或三十分钟", "二十百分钟", "约二十分钟", "六百零一分钟"])
def test_ambiguous_duration_is_not_invented(inputs, quote):
    with pytest.raises(InputRevisionError, match="DURATION_NOT_GROUNDED"):
        propose(inputs, utterance=f"口算改成{quote}", changes={"expected_minutes": 20},
                change_quotes={"expected_minutes": quote})


@pytest.mark.parametrize("quote", ["就按你说的改吧", "好，那就这样改吧", "没问题", "可以", "是的"])
def test_natural_confirmation_is_only_for_current_readback(inputs, quote):
    propose(inputs)
    result, _ = command(inputs, quote, "learning_inputs.confirm_revision", {"confirmation_quote": quote})
    assert result["status"] == "applied"


def test_delete_and_undo_are_versioned_and_persist_history(inputs):
    db, sid, service = inputs
    propose(inputs, utterance="删除口算", operation="delete", changes={}, change_quotes={})
    deleted, _ = confirm(inputs)
    with db.connect() as c:
        assert c.execute("SELECT status FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == "cancelled"
    reopened = LearningInputRevisionService(db)
    propose((db, sid, reopened), utterance="撤销口算刚才的修改", operation="undo", changes={}, change_quotes={})
    undone, _ = confirm((db, sid, reopened))
    history = reopened.history(CHILD, "homework", "math-a")["items"]
    assert len(history) == 2 and history[-1]["undo_event_id"] == deleted["event_id"]
    with db.connect() as c:
        assert c.execute("SELECT status FROM homework_item WHERE homework_id='math-a'").fetchone()[0] == "pending"


def test_unknown_summary_stays_unknown(inputs):
    propose(inputs, utterance="科学课堂内容改成我不知道", target="科学", kind="recall",
            changes={"summary": "我不知道"}, change_quotes={"summary": "我不知道"})
    confirm(inputs)
    with inputs[0].connect() as c:
        row = c.execute("SELECT summary,confirmation_status,confidence FROM classroom_recall WHERE recall_id='unknown'").fetchone()
        assert tuple(row) == ("我不知道", "unknown", 0)


def test_later_unrelated_turn_does_not_lend_its_yes_to_old_revision(inputs):
    propose(inputs)
    db, sid, _ = inputs
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id,tool_status)
            VALUES ('unrelated',?,?,'喝水','conversation','想休息吗？','{}','test',datetime('now'),'unrelated','completed')""", (CHILD, sid))
        c.commit()
    with pytest.raises(InputRevisionError, match="NO_CURRENT_READBACK"):
        confirm(inputs)


def revision_plan(utterance):
    return {"objective": {"mode": "none"}, "next_step": {"capability": "learning_inputs.propose_revision",
        "arguments": {"kind": "homework", "operation": "update", "target_quote": "口算",
                      "request_quote": utterance, "changes": {"subject": "英语"}, "change_quotes": {"subject": "英语"}}},
        "spoken_transition": "我先核对要修改的那项。", "requires_child_reply": False, "confidence": .98,
        "utterance_binding": {"role": "correction", "quote": utterance}}


def execute_interpreted(inputs, orchestrator, key):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    db, _, _ = inputs
    with db.connect() as c:
        turn = c.execute("SELECT agent_turn_id FROM agent_turn WHERE client_action_id=?", (key,)).fetchone()[0]
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(turn)
    orchestrator.complete_turn(turn, result["response_text"], "completed", None, result)
    return result["result"]


def test_real_interpret_readback_restart_natural_confirmation_no_cloud(inputs, tmp_path):
    db, sid, _ = inputs
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    utterance = "把口算的学科改成英语"
    orchestrator, gateway = orchestrator_for(db, tmp_path, revision_plan(utterance))
    asyncio.run(orchestrator.interpret(sid, utterance, "propose-real", True))
    proposal = execute_interpreted(inputs, orchestrator, "propose-real")
    assert proposal["status"] == "awaiting_confirmation"
    restored, unused = orchestrator_for(db, tmp_path, {})
    asyncio.run(restored.interpret(sid, "就按你说的改吧", "confirm-real", False))
    result = execute_interpreted(inputs, restored, "confirm-real")
    assert result["status"] == "applied" and result["target_id"] == "math-a"
    assert not unused.requests
    for request in gateway.requests:
        assert "math-a" not in json.dumps(request.messages)
        assert "snapshot_json" not in json.dumps(request.messages)


def test_target_is_frozen_before_model_latency(inputs, tmp_path):
    db, sid, _ = inputs
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    utterance = "把口算的学科改成英语"
    orchestrator, gateway = orchestrator_for(db, tmp_path, revision_plan(utterance))
    async def add_later():
        with db.connect() as c:
            c.execute("UPDATE homework_item SET title='口算' WHERE homework_id='math-b'")
            c.commit()
    gateway.hook = add_later
    asyncio.run(orchestrator.interpret(sid, utterance, "frozen", True))
    result = execute_interpreted(inputs, orchestrator, "frozen")
    assert result["status"] == "awaiting_confirmation" and result["target_id"] == "math-a"


@pytest.mark.parametrize("change", ["cancel", "apply", "plan_cancel", "new_offer", "source_change"])
def test_old_proposal_replay_never_reopens_confirmation(inputs, change):
    db, sid, service = inputs
    offered, turn = propose(inputs)
    if change == "cancel":
        command(inputs, "不改了", "learning_inputs.cancel_revision", {"confirmation_quote": "不改了"})
    elif change == "apply":
        confirm(inputs)
    elif change == "new_offer":
        propose(inputs, utterance="把应用题改成英语", target="应用题")
    else:
        with db.connect() as c:
            if change == "plan_cancel":
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn,))
            else:
                c.execute("UPDATE homework_item SET title='新口算内容' WHERE homework_id='math-a'")
            c.commit()
    restored = LearningInputRevisionService(db)
    replay = restored.execute(turn)
    assert replay["status"] == "superseded" and replay["requires_child_reply"] is False
    assert "readback_version" not in replay
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    with db.connect() as c:
        cached = AgentOrchestrator._get_replayed(c, turn)
        assert cached["action"]["type"] == "none"
        assert cached["response_text"] != offered["response_text"]


def test_cancelled_offer_cannot_lend_authority_to_a_fresh_confirmation(inputs):
    _, turn = propose(inputs)
    with inputs[0].connect() as c:
        c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn,))
        c.commit()
    with pytest.raises(InputRevisionError, match="NO_CURRENT_READBACK"):
        confirm(inputs)


def test_changed_original_quote_blocks_even_historical_replay(inputs):
    _, turn = propose(inputs)
    with inputs[0].connect() as c:
        c.execute("UPDATE agent_turn SET transcript='另一句话' WHERE agent_turn_id=?", (turn,))
        c.commit()
    with pytest.raises(InputRevisionError, match="REPLAY_BINDING_CHANGED"):
        inputs[2].execute(turn)


def test_revision_obligation_preserves_executed_work_budget_and_evidence(inputs):
    db, sid, service = inputs
    with db.connect() as c:
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,
            version,created_at,client_action_id) VALUES ('revision-plan',?,?,date('now'),'active',60,'test',1,datetime('now'),'revision-plan')""", (CHILD, sid))
        for state in ("pending", "active", "completed"):
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
                position,source_type,source_ref,homework_id,active_seconds,started_at,completed_at)
                VALUES (?,'revision-plan','数学','口算',20,1,?,1,'school_homework','math-a','math-a',?,?,?)""",
                (state, state, 0 if state == "pending" else 45, None if state == "pending" else "2026-09-01T09:00:00+00:00",
                 "2026-09-01T09:05:00+00:00" if state == "completed" else None))
        before = [tuple(r) for r in c.execute("SELECT * FROM plan_item WHERE status!='pending' ORDER BY plan_item_id")]
        evidence_count = c.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0]
        c.commit()
    propose(inputs)
    result, turn = confirm(inputs)
    obligation = result["impact"]["replan_obligation"]
    assert obligation["cancelled_unstarted_plan_ids"] == ["pending"]
    assert set(obligation["preserve_plan_item_ids"]) == {"active", "completed"}
    assert obligation["budget_snapshot"]["max_learning_minutes"] > 0
    assert obligation["original_plan_items"][0]["original_plan_minutes"] == 60
    view = LearningInputRevisionService(db).inspect_replan_obligation(CHILD, result["event_id"])
    assert view["source_current"] is True and view["obligation"] == obligation
    assert service.execute(turn) == result
    with db.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM plan_item WHERE status IN ('active','completed') ORDER BY plan_item_id")] == before
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='pending'").fetchone()[0] == "cancelled"
        assert c.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == evidence_count
        c.execute("UPDATE homework_item SET title='A newer correction' WHERE homework_id='math-a'")
        c.commit()
    assert service.inspect_replan_obligation(CHILD, result["event_id"])["source_current"] is False
    with pytest.raises(InputRevisionError, match="EVENT_NOT_FOUND"):
        service.inspect_replan_obligation("other-child", result["event_id"])


def test_noop_revision_does_not_cancel_work_or_create_an_obligation(inputs):
    propose(inputs, utterance="口算改成数学", changes={"subject": "数学"}, change_quotes={"subject": "数学"})
    result, _ = confirm(inputs)
    assert result["status"] == "unchanged"
    with inputs[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_revision_event").fetchone()[0] == 0
