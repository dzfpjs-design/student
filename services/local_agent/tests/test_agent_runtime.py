from pathlib import Path
import ast
from datetime import datetime, timedelta, timezone
import inspect
import json
import re
import sqlite3
import textwrap

import pytest

from home_study_agent.agent_runtime import (
    AgentRuntimeStore,
    CapabilityRegistry,
    DEFAULT_ACTION_CAPABILITY_MAP,
    InvalidAgentPlanError,
    action_from_plan,
    build_planner_messages,
    context_manifest,
    parse_agent_plan_output,
)
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor, AgentExecutionConflict
from home_study_agent.child_model import ChildModelService
from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.database import Database
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from home_study_agent.voice_control_intent import VoiceControlIntent


def seed_session(database: Database) -> str:
    session_id = "session-runtime-test"
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at)
            VALUES ('child-runtime', '测试同学', '初三', '江苏南京', 'active', datetime('now'), datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT INTO daily_session(
              session_id, child_id, status, state, started_at, last_checkpoint_at, client_action_id
            ) VALUES (?, 'child-runtime', 'active', 'check_in', datetime('now'), datetime('now'), 'session-runtime-action')
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO family_rule(
              child_id, latest_end_time, max_learning_minutes, task_segment_minutes,
              break_minutes, pet_min_minutes, competition_minutes, observation_mode,
              reminder_budget_per_10m, misclassification_shadow_threshold, version, updated_at
            ) VALUES ('child-runtime', '21:30', 120, 25, 5, 0, 0, 'shadow', 1, 3, 1, datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT INTO session_planning_context(
              session_id, energy_level, fixed_commitment_minutes,
              tomorrow_event_minutes, updated_at
            ) VALUES (?, 'medium', 0, 0, datetime('now'))
            """,
            (session_id,),
        )
        connection.commit()
    return session_id


def learning_plan_json() -> str:
    return json.dumps({
        "objective": {
            "mode": "new",
            "type": "learning",
            "summary": "理解正弦和余弦的区别",
            "success_criteria": ["能独立说明二者分别表示什么", "完成一道近迁移检查"],
            "phase": "diagnose",
        },
        "next_step": {
            "capability": "learning.explain",
            "arguments": {"question": "正弦和余弦有什么区别", "subject": "数学", "support_level": 6},
            "reason": "孩子明确提出概念问题",
            "expected_evidence": ["child_check_response"],
        },
        "spoken_transition": "我们不只听讲，最后你要自己解释一次。我先用一个图景讲清楚。",
        "requires_child_reply": True,
        "confidence": 0.91,
    }, ensure_ascii=False)


def test_followup_plan_uses_child_words_not_model_paraphrase():
    payload = json.loads(learning_plan_json())
    payload['objective']['mode'] = 'continue'
    payload['next_step']['arguments']['question_relation'] = 'followup'
    decision = parse_agent_plan_output(json.dumps(payload, ensure_ascii=False), 'study', CapabilityRegistry())
    action = action_from_plan(decision, '为什么要取倒数？', None)
    assert action['question'] == '为什么要取倒数？'
    assert action['question_relation'] == 'followup'
    assert 'tutor_followup_binding' not in action


def test_model_cannot_choose_followup_source():
    payload = json.loads(learning_plan_json())
    payload['next_step']['arguments'].update(question_relation='followup', source_turn_id='invented')
    with pytest.raises(InvalidAgentPlanError):
        parse_agent_plan_output(json.dumps(payload, ensure_ascii=False), 'study', CapabilityRegistry())


@pytest.mark.parametrize('command', ['inspect', 'review', 'pause', 'resume'])
@pytest.mark.parametrize('scope_kind', ['valid', 'missing', 'ungrounded', 'duplicate', 'stop', 'await_reply'])
def test_document_review_owner_requires_explicit_teaching_scope(command, scope_kind):
    from dataclasses import replace
    from test_cloud_first_agent import Planner
    payload = Planner('document.monitor_control', {'command': command}, '看看题目再讲我不会的', objective_type='learning').payload
    decision = parse_agent_plan_output(json.dumps(payload, ensure_ascii=False), 'study', CapabilityRegistry())
    scope = ({'capability': 'learning.explain', 'arguments': {'subject': '数学'}, 'request_quote': '讲我不会的'},)
    if scope_kind == 'missing':
        scope = ()
    elif scope_kind == 'ungrounded':
        scope = ({**scope[0], 'request_quote': '用户没有说过这句话'},)
    elif scope_kind == 'duplicate':
        scope = scope + scope
    decision = replace(decision, after_execution='stop' if scope_kind == 'stop' else 'replan',
        continuation_scope=scope, requires_child_reply=scope_kind == 'await_reply')
    action = action_from_plan(decision, '看看题目再讲我不会的', None)
    assert action.get('defer_document_review', False) is (scope_kind == 'valid' and command in {'inspect', 'review'})


def test_registry_planner_and_context_form_one_discoverable_contract(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    registry = CapabilityRegistry()

    context = store.assemble_context(session_id, ({"role": "user", "content": "我不懂正弦"},))
    messages = build_planner_messages(context, "讲讲正弦和余弦", registry)
    decision = parse_agent_plan_output(learning_plan_json(), "check_in", registry)
    action = action_from_plan(decision, "讲讲正弦和余弦", None)

    assert "learning.explain" in messages[1]["content"]
    assert context["child"]["grade"] == "初三"
    assert decision.objective.success_criteria[-1] == "完成一道近迁移检查"
    assert action == {
        "type": "ask_tutor",
        "capability": "learning.explain",
        "execution_location": "local_model",
        "question": "正弦和余弦有什么区别",
        "subject": "数学",
        "hint_level": 6,
    }


def test_every_deterministic_action_has_one_registered_default_owner() -> None:
    source = textwrap.dedent(inspect.getsource(AgentOrchestrator._classify))
    tree = ast.parse(source)
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "type"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                emitted.add(value.value)

    registry = CapabilityRegistry()
    owners = {
        action_type: registry.owner_for_action_type(action_type)
        for action_type in emitted
    }

    assert set(owners) == emitted
    assert all(item.name for item in owners.values())
    assert all(item.execution_location for item in owners.values())
    assert emitted <= registry.action_types()


def test_compound_classroom_recall_is_split_without_losing_subjects() -> None:
    intent, response, action = AgentOrchestrator._classify(
        "classroom_recall",
        "数学老师讲了分式，语文学了《白鹭》，道法讲了规则意识",
        5,
    )

    assert intent == "add_classroom_recall_batch"
    assert action["type"] == "add_recall_batch"
    assert [item["subject"] for item in action["items"]] == ["数学", "语文", "道德与法治"]
    assert "3科" in response


def test_compound_homework_is_split_with_independent_minutes_and_types() -> None:
    intent, response, action = AgentOrchestrator._classify(
        "homework_capture",
        "数学作业第12页预计30分钟，语文背诵第2课15分钟，英语听写10分钟",
        5,
    )

    assert intent == "add_homework_batch"
    assert action["type"] == "add_homework_batch"
    assert [item["subject"] for item in action["items"]] == ["数学", "语文", "英语"]
    assert [item["expected_minutes"] for item in action["items"]] == [30, 15, 10]
    assert action["items"][1]["task_type"] == "memorization"
    assert "3项" in response


def test_subject_detection_covers_primary_and_junior_curriculum() -> None:
    assert AgentOrchestrator._subject_from_text("科学做实验记录") == "科学"
    assert AgentOrchestrator._subject_from_text("道法复习第三单元") == "道德与法治"
    assert AgentOrchestrator._subject_from_text("历史背时间线") == "历史"


def test_model_planned_batch_rejects_malformed_or_partial_items() -> None:
    payload = json.loads(learning_plan_json())
    payload["objective"] = {
        "mode": "new", "type": "execution", "summary": "保存作业",
        "success_criteria": ["作业逐项保存"], "phase": "capture",
    }
    payload["next_step"] = {
        "capability": "homework.add_batch",
        "arguments": {"items": [{"subject": "数学", "title": ""}, {"subject": "语文", "title": "背诵"}]},
        "reason": "一次说了多项", "expected_evidence": ["homework_items_saved"],
    }
    with pytest.raises(InvalidAgentPlanError, match="missing required fields"):
        parse_agent_plan_output(
            json.dumps(payload, ensure_ascii=False), "homework_capture", CapabilityRegistry()
        )


def test_unowned_or_mismatched_actions_fail_before_persistence() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(InvalidAgentPlanError, match="unowned agent action"):
        registry.bind_action({"type": "future_unimplemented_action"})
    with pytest.raises(InvalidAgentPlanError, match="cannot own action"):
        registry.bind_action({"type": "generate_plan", "capability": "learning.explain"})


def test_backend_action_owners_match_child_client_execution_contract() -> None:
    client_contract = (
        Path(__file__).parents[3] / "src" / "renderer" / "src" / "agentActionContract.ts"
    ).read_text(encoding="utf-8")
    array_source = client_contract.split("as const", 1)[0]
    client_actions = set(re.findall(r"'([a-z_]+)'", array_source))

    assert client_actions == set(DEFAULT_ACTION_CAPABILITY_MAP)


def test_document_stall_acceptance_dispatches_real_page_inspection() -> None:
    assert AgentOrchestrator._behavior_feedback_choice("好，帮我看看") is not None
    intent, response, action = AgentOrchestrator._behavior_feedback_result(
        "accepted", None, "blocked_candidate", "document_monitor"
    )
    assert intent == "behavior_intervention_feedback"
    assert "本机检查当前页" in response
    assert action == {
        "type": "control_document_monitor",
        "command": "inspect",
        "feedback": "accepted",
        "response_kind": "accepted",
    }


def test_companion_activity_is_restart_safe_and_has_a_natural_end(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)

    activity = store.start_companion_activity(
        session_id, "interactive_story", "雨后的秘密小门", "companion-story-start"
    )
    assert activity["status"] == "active"
    assert activity["max_turns"] == 6
    reopened = AgentRuntimeStore(Database(database.path)).active_companion_activity(session_id)
    assert reopened is not None
    assert reopened["remaining_turns"] == 6

    for index in range(5):
        progress = store.advance_companion_activity(session_id)
        assert progress is not None
        assert progress["natural_end"] is False
        assert progress["turn_count"] == index + 1
    ended = store.advance_companion_activity(session_id)
    assert ended is not None
    assert ended["natural_end"] is True


def seed_paused_companion_task(database: Database, session_id: str) -> str:
    item_id = f"companion-task:{session_id}"
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state = 'study' WHERE session_id = ?", (session_id,))
        connection.execute(
            """INSERT INTO daily_plan(
              plan_id, child_id, session_id, plan_date, status, total_minutes,
              generator, version, created_at, client_action_id
            ) SELECT ?, child_id, session_id, date('now'), 'active', 20,
                     'test', 1, datetime('now'), ? FROM daily_session WHERE session_id = ?""",
            (item_id, item_id, session_id),
        )
        connection.execute(
            """INSERT INTO plan_item(
              plan_item_id, plan_id, subject, title, expected_minutes,
              priority, status, position, source_type, source_ref, active_seconds
            ) VALUES (?, ?, 'math', 'Original task', 20, 1, 'paused', 1,
                      'school_homework', 'homework', 37)""",
            (item_id, item_id),
        )
        connection.commit()
    return item_id


@pytest.mark.parametrize("entry", ["proactive", "active", "recovery", "context", "advance", "replay"])
def test_companion_silent_timeout_is_atomic_and_idempotent(tmp_path: Path, entry: str) -> None:
    database = Database(tmp_path / "silent-timeout.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    item_id = seed_paused_companion_task(database, session_id)
    activity = store.start_companion_activity(
        session_id, "riddle", "test", "silent-start", item_id
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE companion_activity SET ends_at = '2000-01-01T00:00:00+00:00' WHERE activity_id = ?",
            (activity["activity_id"],),
        )
        connection.commit()

    # Reopen the durable database without relying on any child turn or in-memory timer.
    store = AgentRuntimeStore(Database(database.path))
    if entry == "proactive":
        assert store.next_proactive_prompt(session_id)["status"] != "companion_active"
    elif entry == "active":
        assert store.active_companion_activity(session_id) is None
    elif entry == "recovery":
        assert store.recover_interrupted_executions() == 0
    elif entry == "context":
        assert store.assemble_context(session_id, ())["active_companion_activity"] is None
    elif entry == "advance":
        ended = store.advance_companion_activity(session_id)
        assert ended["natural_end"] is True
        assert ended["end_reason"] == "time_limit"
        assert ended["turn_count"] == 0
    else:
        assert store.start_companion_activity(
            session_id, "riddle", "test", "silent-start", item_id
        )["status"] == "completed"

    with database.connect() as connection:
        ended = dict(connection.execute("SELECT * FROM companion_activity").fetchone())
        resumed = dict(connection.execute("SELECT * FROM plan_item WHERE plan_item_id = ?", (item_id,)).fetchone())
        assert ended["status"] == "completed"
        assert ended["end_reason"] == "time_limit"
        assert ended["ended_at"] is not None
        assert ended["turn_count"] == 0
        assert ended["ends_at"] == "2000-01-01T00:00:00+00:00"
        assert resumed["status"] == "paused"
        assert resumed["active_seconds"] == 37
        assert resumed["active_segment_started_at"] is None
        assert connection.execute("SELECT COUNT(*) FROM plan_item_event").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 0

    for _ in range(3):
        store.next_proactive_prompt(session_id)
        assert store.active_companion_activity(session_id) is None
        assert store.recover_interrupted_executions() == 0
        assert store.advance_companion_activity(session_id) is None
    with database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM companion_activity").fetchone()) == ended
        assert dict(connection.execute("SELECT * FROM plan_item WHERE plan_item_id = ?", (item_id,)).fetchone()) == resumed
        assert connection.execute("SELECT COUNT(*) FROM plan_item_event").fetchone()[0] == 0


def test_companion_unexpired_activity_survives_ticks_reads_and_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "unexpired.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    item_id = seed_paused_companion_task(database, session_id)
    activity = store.start_companion_activity(session_id, "movement", "test", "unexpired", item_id)
    for _ in range(3):
        store = AgentRuntimeStore(Database(database.path))
        assert store.recover_interrupted_executions() == 0
        assert store.next_proactive_prompt(session_id)["status"] == "companion_active"
        assert store.active_companion_activity(session_id) == activity
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id = ?", (item_id,)).fetchone()[0] == "paused"
        assert connection.execute("SELECT COUNT(*) FROM plan_item_event").fetchone()[0] == 0


@pytest.mark.parametrize("activity_type", ["interactive_story", "word_chain", "riddle", "movement", "tidy_challenge"])
def test_companion_timeout_uses_exact_deadline_without_inventing_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, activity_type: str,
) -> None:
    database = Database(tmp_path / "deadline.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    activity = store.start_companion_activity(session_id, activity_type, "test", "deadline")
    assert store.advance_companion_activity(session_id)["turn_count"] == 1
    deadline = datetime.fromisoformat(activity["ends_at"])
    current_time = deadline - timedelta(microseconds=1)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time

    monkeypatch.setattr("home_study_agent.agent_runtime.datetime", FrozenDateTime)
    assert store.next_proactive_prompt(session_id)["status"] == "companion_active"
    current_time = deadline
    assert store.next_proactive_prompt(session_id)["status"] != "companion_active"
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM companion_activity").fetchone()
        assert row["status"] == "completed"
        assert row["end_reason"] == "time_limit"
        assert row["ended_at"] == activity["ends_at"]
        assert row["turn_count"] == 1
        assert row["max_turns"] == activity["max_turns"]
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 0


@pytest.mark.parametrize("suppression", ["snoozed", "inactive"])
def test_companion_timeout_persists_when_proactive_is_suppressed(tmp_path: Path, suppression: str) -> None:
    database = Database(tmp_path / "suppressed.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    item_id = seed_paused_companion_task(database, session_id)
    store.start_companion_activity(session_id, "movement", "test", "suppressed", item_id)
    with database.connect() as connection:
        connection.execute("UPDATE companion_activity SET ends_at = datetime('now', '-1 minute')")
        if suppression == "snoozed":
            connection.execute("UPDATE daily_session SET snooze_until = datetime('now', '+10 minutes')")
        else:
            connection.execute("UPDATE daily_session SET status = 'completed'")
        connection.commit()
    assert store.next_proactive_prompt(session_id)["status"] == suppression
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM companion_activity").fetchone()[0] == "completed"
        status = connection.execute("SELECT status FROM plan_item WHERE plan_item_id = ?", (item_id,)).fetchone()[0]
        assert status == "paused"


def test_companion_timeout_never_attempts_task_resume_and_survives_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "atomic-timeout.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    item_id = seed_paused_companion_task(database, session_id)
    store.start_companion_activity(session_id, "movement", "test", "atomic", item_id)
    with database.connect() as connection:
        connection.execute("UPDATE companion_activity SET ends_at = datetime('now', '-1 minute')")
        connection.execute(
            """CREATE TRIGGER fail_resume BEFORE INSERT ON plan_item_event
               BEGIN SELECT RAISE(ABORT, 'resume write failed'); END"""
        )
        connection.commit()
    store.next_proactive_prompt(session_id)
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM companion_activity").fetchone()[0] == "completed"
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id = ?", (item_id,)).fetchone()[0] == "paused"
        assert connection.execute("SELECT COUNT(*) FROM plan_item_event").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_resume")
        connection.commit()
    assert AgentRuntimeStore(Database(database.path)).active_companion_activity(session_id) is None
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id = ?", (item_id,)).fetchone()[0] == "paused"
        assert connection.execute("SELECT COUNT(*) FROM plan_item_event").fetchone()[0] == 0


def test_companion_timeout_isolated_by_child_and_session(tmp_path: Path) -> None:
    database = Database(tmp_path / "isolated.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    first_item = seed_paused_companion_task(database, session_id)
    first = store.start_companion_activity(session_id, "riddle", "first", "first-start", first_item)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status = 'cancelled' WHERE session_id = ?", (session_id,))
        connection.execute(
            """INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at)
            VALUES ('child-other', 'Other', '4', 'test', 'active', datetime('now'), datetime('now'))"""
        )
        connection.execute(
            """INSERT INTO daily_session(session_id, child_id, status, state, started_at, last_checkpoint_at, client_action_id)
            VALUES ('session-other', 'child-other', 'active', 'study', datetime('now'), datetime('now'), 'other-action')"""
        )
        connection.commit()
    other_item = seed_paused_companion_task(database, "session-other")
    other = store.start_companion_activity("session-other", "riddle", "other", "other-start", other_item)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status = 'cancelled' WHERE session_id = 'session-other'")
        connection.execute("UPDATE daily_session SET status = 'active' WHERE session_id = ?", (session_id,))
        connection.execute("UPDATE companion_activity SET ends_at = '2000-01-01T00:00:00+00:00'")
        connection.commit()
    store.next_proactive_prompt(session_id)
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM companion_activity WHERE activity_id = ?", (first["activity_id"],)).fetchone()[0] == "completed"
        assert connection.execute("SELECT status FROM companion_activity WHERE activity_id = ?", (other["activity_id"],)).fetchone()[0] == "active"
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id = ?", (other_item,)).fetchone()[0] == "paused"
        connection.execute("UPDATE daily_session SET status = 'cancelled' WHERE session_id = ?", (session_id,))
        connection.execute("UPDATE daily_session SET status = 'active' WHERE session_id = 'session-other'")
        connection.commit()
    assert store.active_companion_activity("session-other") is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type = 'resumed'").fetchone()[0] == 0


def test_proactive_prompt_is_persisted_deduplicated_and_respects_snooze(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state = 'study' WHERE session_id = ?", (session_id,))
        connection.execute(
            """
            INSERT INTO daily_plan(
              plan_id, child_id, session_id, plan_date, status, total_minutes,
              generator, version, created_at, client_action_id
            ) VALUES ('plan-proactive', 'child-runtime', ?, date('now'), 'active', 20,
              'test', 1, datetime('now'), 'plan-proactive-action')
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO plan_item(
              plan_item_id, plan_id, subject, title, expected_minutes,
              priority, status, position, source_type, source_ref
            ) VALUES ('item-proactive', 'plan-proactive', '数学', '判别式复习', 20,
              1, 'pending', 1, 'school_homework', 'homework-proactive')
            """
        )
        connection.commit()

    first = store.next_proactive_prompt(session_id)
    assert first["status"] == "delivered"
    assert first["prompt"]["source_type"] == "task_ready"
    assert "判别式复习" in first["prompt"]["message"]
    assert store.next_proactive_prompt(session_id)["status"] == "awaiting_ack"

    with database.connect() as connection:
        connection.execute(
            "UPDATE agent_proactive_prompt SET last_attempt_at = datetime('now', '-2 minutes')"
        )
        connection.commit()
    retried = store.next_proactive_prompt(session_id)
    assert retried["status"] == "retry_delivery"
    assert retried["prompt"]["prompt_id"] == first["prompt"]["prompt_id"]
    assert store.acknowledge_proactive_prompt(
        session_id, first["prompt"]["prompt_id"]
    )["status"] == "acknowledged"

    with database.connect() as connection:
        connection.execute(
            "UPDATE agent_proactive_prompt SET created_at = datetime('now', '-10 minutes')"
        )
        connection.commit()
    duplicate = store.next_proactive_prompt(session_id)
    assert duplicate["status"] == "already_delivered"
    assert duplicate["prompt"] is None

    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET snooze_until = datetime('now', '+10 minutes') WHERE session_id = ?",
            (session_id,),
        )
        connection.commit()
    snoozed = store.next_proactive_prompt(session_id)
    assert snoozed["status"] == "snoozed"
    assert snoozed["prompt"] is None
    assert store.active_companion_activity(session_id) is None

    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = privacy.export_child("child-runtime")
    exported_payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert len(exported_payload["agent_proactive_prompts"]) == 1
    assert privacy.deletion_preview("child-runtime")["agent_proactive_prompts"] == 1
    deleted = privacy.delete_child_scope(
        "child-runtime", "all_history", "确认删除", "delete-runtime-history"
    )
    assert deleted["status"] == "completed"
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_proactive_prompt WHERE child_id = 'child-runtime'"
        ).fetchone()[0] == 0

    intent, _, action = AgentOrchestrator._classify("break", "给我讲个故事", 5)
    assert intent == "start_companion_activity"
    assert action["activity_type"] == "interactive_story"


def test_camera_and_document_interventions_use_the_single_durable_agent_voice_queue(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO intervention(
              intervention_id, child_id, session_id, source_event_id, decided_at,
              candidate_state, message, intervention_type, status
            ) VALUES (
              'intervention-document-1', 'child-runtime', ?, 'document-revision:revision-1', ?,
              'blocked_candidate', '要不要把卡住的一步圈出来，或选择向我求助？',
              'default_ask_first', 'active'
            )
            """,
            (session_id, datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()

    first = AgentRuntimeStore(database).next_proactive_prompt(session_id)
    assert first["status"] == "delivered"
    assert first["prompt"]["source_type"] == "behavior_intervention"
    assert first["prompt"]["message"] == "要不要把卡住的一步圈出来，或选择向我求助？"

    reopened = AgentRuntimeStore(Database(database.path)).next_proactive_prompt(session_id)
    assert reopened["status"] == "awaiting_ack"
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_proactive_prompt WHERE source_id = 'intervention-document-1'"
        ).fetchone()[0] == 1


def test_proactive_prompt_never_interrupts_waiting_reply_or_reads_due_question(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    now = datetime.now(timezone.utc).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state = 'study' WHERE session_id = ?", (session_id,))
        connection.execute(
            """
            INSERT INTO agent_turn(
              agent_turn_id, child_id, session_id, transcript, intent, response_text,
              action_json, orchestrator_version, tool_status, created_at, client_action_id
            ) VALUES ('turn-waiting', 'child-runtime', ?, '我来回答', 'learning_reply',
              '等你回答', '{}', 'test', 'completed', datetime('now', '-2 minutes'),
              'turn-waiting-action')
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO agent_objective(
              objective_id, child_id, session_id, objective_type, summary,
              success_criteria_json, phase, status, created_at, updated_at
            ) VALUES ('objective-waiting', 'child-runtime', ?, 'learning',
              '理解一元二次方程', '[\"独立回答\"]', 'check', 'active', ?, ?)
            """,
            (session_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO agent_plan(
              plan_id, objective_id, child_id, session_id, agent_turn_id,
              planner_version, context_manifest_json, plan_json, status, created_at, updated_at
            ) VALUES ('plan-waiting', 'objective-waiting', 'child-runtime', ?, 'turn-waiting',
              'test', '{}', '{}', 'waiting_child', ?, ?)
            """,
            (session_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO agent_commitment(
              commitment_id, objective_id, child_id, commitment_type,
              due_at, payload_json, status, created_at, client_action_id
            ) VALUES ('commitment-due', 'objective-waiting', 'child-runtime',
              'review', ?, '{"prompt":"请解释一元二次方程并计算答案"}',
              'due', ?, 'commitment-due-action')
            """,
            (now, now),
        )
        connection.commit()

    assert store.next_proactive_prompt(session_id) == {"status": "waiting_child", "prompt": None}

    with database.connect() as connection:
        connection.execute("UPDATE agent_plan SET status = 'completed' WHERE plan_id = 'plan-waiting'")
        connection.commit()
    prompt = store.next_proactive_prompt(session_id)
    assert prompt["status"] == "delivered"
    assert prompt["prompt"]["source_type"] == "due_commitment"
    assert "一元二次方程" not in prompt["prompt"]["message"]
    assert "开始复测" in prompt["prompt"]["message"]


def test_proactive_prompt_guides_settled_study_into_voice_reflection(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state = 'study' WHERE session_id = ?", (session_id,))
        connection.execute(
            """
            INSERT INTO daily_plan(
              plan_id, child_id, session_id, plan_date, status, total_minutes,
              generator, version, created_at, client_action_id
            ) VALUES ('plan-settled', 'child-runtime', ?, date('now'), 'active', 20,
              'test', 1, datetime('now'), 'plan-settled-action')
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO plan_item(
              plan_item_id, plan_id, subject, title, expected_minutes,
              priority, status, position, source_type, source_ref
            ) VALUES ('item-settled', 'plan-settled', '数学', '练习', 20,
              1, 'completed', 1, 'school_homework', 'homework-settled')
            """
        )
        connection.commit()

    result = store.next_proactive_prompt(session_id)
    assert result["status"] == "delivered"
    assert result["prompt"]["source_type"] == "study_wrap_up"
    assert "开始复盘" in result["prompt"]["message"]

    intent, response, action = AgentOrchestrator._classify("study", "开始复盘", 5)
    assert intent == "request_end_session"
    assert action == {"type": "request_end"}
    assert "三句话复盘" in response
    assert VoiceControlIntent.local_task_navigation("开始复盘") is None


def test_proactive_prompt_recovers_break_timer_across_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state = 'break' WHERE session_id = ?", (session_id,))
        connection.execute(
            """
            INSERT INTO session_transition(
              transition_id, session_id, from_state, to_state, reason, occurred_at, client_action_id
            ) VALUES ('break-transition', ?, 'study', 'break', 'child_requested_break',
                      datetime('now', '-6 minutes'), 'break-transition-action')
            """,
            (session_id,),
        )
        connection.commit()

    result = AgentRuntimeStore(Database(database.path)).next_proactive_prompt(session_id)

    assert result["status"] == "delivered"
    assert result["prompt"]["source_type"] == "break_due"
    assert "休息时间到了" in result["prompt"]["message"]
    assert "继续任务" in result["prompt"]["message"]


def test_proactive_prompt_waits_until_break_is_due(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state = 'break' WHERE session_id = ?", (session_id,))
        connection.execute(
            """
            INSERT INTO session_transition(
              transition_id, session_id, from_state, to_state, reason, occurred_at, client_action_id
            ) VALUES ('break-transition', ?, 'study', 'break', 'child_requested_break',
                      datetime('now'), 'break-transition-action')
            """,
            (session_id,),
        )
        connection.commit()

    result = AgentRuntimeStore(database).next_proactive_prompt(session_id)

    assert result["status"] == "break_running"
    assert result["prompt"] is None
    assert 1 <= result["remaining_seconds"] <= 300


def test_runtime_context_exposes_only_derived_camera_and_document_monitor_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    DocumentMonitoringService(database)
    session_id = seed_session(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET observation_status = 'enabled' WHERE session_id = ?",
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO observable_event(
              event_id, child_id, session_id, occurred_at, event_type, source,
              confidence, source_refs_json, missing_sources_json, payload_json, alternatives_json
            ) VALUES (
              'vision-event-runtime', 'child-runtime', ?, '2026-09-01T08:00:00Z',
              'posture.head_down', 'local_vision', 0.82, '[]', '[]',
              '{"raw_frame_stored":false}', '[]'
            )
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO document_monitor_session(
              monitor_id, child_id, session_id, status, current_state,
              started_at, updated_at, ended_at
            ) VALUES (
              'monitor-runtime', 'child-runtime', ?, 'active', 'writing',
              '2026-09-01T08:00:00Z', '2026-09-01T08:01:00Z', NULL
            )
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO document_monitor_event(
              event_id, monitor_id, child_id, session_id, event_type, confidence,
              metrics_json, observed_at, client_action_id
            ) VALUES (
              'document-event-runtime', 'monitor-runtime', 'child-runtime', ?,
              'writing', 0.91, '{"raw_frame_stored":false}',
              '2026-09-01T08:01:00Z', 'document-event-runtime-action'
            )
            """,
            (session_id,),
        )
        connection.commit()

    context = AgentRuntimeStore(database).assemble_context(session_id, ())

    assert context["sensor_state"] == {
        "camera": {
            "status": "enabled",
            "latest_event_type": "posture.head_down",
            "confidence": 0.82,
            "observed_at": "2026-09-01T08:00:00Z",
        },
        "document_camera": {
            "status": "active",
            "current_state": "writing",
            "latest_event_type": "writing",
            "confidence": 0.91,
            "observed_at": "2026-09-01T08:01:00Z",
        },
    }
    serialized = json.dumps(context["sensor_state"], ensure_ascii=False)
    assert "raw_frame" not in serialized
    assert "extracted_text" not in serialized
    assert AgentRuntimeStore(database).status(session_id)["sensor_state"] == context["sensor_state"]


def test_invalid_or_unregistered_capability_cannot_execute() -> None:
    payload = json.loads(learning_plan_json())
    payload["next_step"]["capability"] = "system.do_anything"
    with pytest.raises(InvalidAgentPlanError, match="unknown capability"):
        parse_agent_plan_output(json.dumps(payload, ensure_ascii=False), "check_in", CapabilityRegistry())


def test_objective_plan_and_waiting_step_survive_database_reopen(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    decision = parse_agent_plan_output(learning_plan_json(), "check_in", CapabilityRegistry())
    context = store.assemble_context(session_id, ())
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_turn(
              agent_turn_id, child_id, session_id, transcript, intent, response_text,
              action_json, orchestrator_version, created_at, client_action_id
            ) VALUES ('turn-runtime', 'child-runtime', ?, '讲讲正弦', 'learning.explain',
                      '我先讲，再检查。', '{}', 'agent_runtime_planner_v1', datetime('now'), 'turn-runtime-action')
            """,
            (session_id,),
        )
        metadata = store.persist_plan(
            connection,
            child_id="child-runtime",
            session_id=session_id,
            agent_turn_id="turn-runtime",
            client_action_id="turn-runtime-action",
            decision=decision,
            context_manifest=context_manifest(context),
        )
        connection.commit()

    reopened = AgentRuntimeStore(Database(database.path)).status(session_id)
    assert metadata["objective_id"] == reopened["active_objective"]["objective_id"]
    assert reopened["active_objective"]["summary"] == "理解正弦和余弦的区别"
    assert reopened["current_step"]["capability_name"] == "learning.explain"
    assert reopened["current_step"]["status"] == "waiting_child"
    assert metadata["decision_record_id"]
    with database.connect() as connection:
        record = connection.execute(
            "SELECT * FROM agent_decision_record WHERE decision_record_id=?",
            (metadata["decision_record_id"],),
        ).fetchone()
        plan = connection.execute(
            """SELECT record.decision_record_id FROM agent_plan AS plan
               JOIN agent_decision_record AS record USING(plan_id)
               WHERE plan.plan_id=?""", (metadata["plan_id"],)
        ).fetchone()
    assert plan["decision_record_id"] == metadata["decision_record_id"]
    assert record["binding_status"] == "bound_current"
    assert record["snapshot_id"] == context["longitudinal_child_model"]["snapshot_id"]
    assert record["evidence_fingerprint"] == context["longitudinal_child_model"]["evidence_fingerprint"]
    assert record["selected_capability"] == "learning.explain"
    assert record["contract_version"] == "decision_record_v1"

    with database.connect() as connection:
        store.complete_turn_execution(
            connection,
            "turn-runtime",
            True,
            None,
            {"capability": "learning.explain", "result": {"next_check": "你能自己说出两者的区别吗？"}},
        )
        connection.commit()
    completed = store.status(session_id)["current_step"]
    assert completed["status"] == "completed"
    assert completed["plan_status"] == "waiting_child"
    pending = AgentRuntimeStore(Database(database.path)).pending_child_reply(session_id)
    assert pending is not None
    assert pending["objective_id"] == metadata["objective_id"]
    assert pending["capability_name"] == "learning.explain"
    assert pending["arguments"]["subject"] == "数学"
    assert pending["result"]["result"]["next_check"].startswith("你能自己说出")


def test_server_capability_executes_behind_one_durable_boundary(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    registry = CapabilityRegistry()
    payload = {
        "objective": {
            "mode": "new", "type": "execution", "summary": "了解今天还有多少任务",
            "success_criteria": ["听到已完成和未完成数量"], "phase": "read_plan",
        },
        "next_step": {
            "capability": "planning.describe", "arguments": {}, "reason": "孩子询问进度",
            "expected_evidence": ["plan_status_read"],
        },
        "spoken_transition": "我看一下今天的进度。", "requires_child_reply": False, "confidence": 0.93,
    }
    decision = parse_agent_plan_output(json.dumps(payload, ensure_ascii=False), "check_in", registry)
    context = store.assemble_context(session_id, ())
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_turn(
              agent_turn_id, child_id, session_id, transcript, intent, response_text,
              action_json, orchestrator_version, created_at, client_action_id
            ) VALUES ('turn-execute', 'child-runtime', ?, '今天还有多少任务', 'planning.describe',
                      '我看一下。', ?, 'agent_runtime_planner_v1', datetime('now'), 'turn-execute-action')
            """,
            (
                session_id,
                json.dumps({
                    **action_from_plan(decision, "今天还有多少任务", None),
                    "source": "agent_runtime_planner",
                }, ensure_ascii=False),
            ),
        )
        store.persist_plan(
            connection, child_id="child-runtime", session_id=session_id,
            agent_turn_id="turn-execute", client_action_id="turn-execute-action",
            decision=decision, context_manifest=context_manifest(context),
        )
        connection.commit()

    result = AgentCapabilityExecutor(database, StudyRepository(database)).execute("turn-execute")
    assert result["capability"] == "planning.describe"
    assert result["result"] == {
        "total": 0,
        "completed": 0,
        "remaining": 0,
        "current_plan_item_id": None,
        "current_status": None,
    }
    assert result["response_text"] == "今天还没有学习计划。先告诉我课堂内容和作业，我会替你排好。"
    with database.connect() as connection:
        step = connection.execute(
            "SELECT status FROM agent_step WHERE idempotency_key = 'agent-step:turn-execute-action:0'"
        ).fetchone()
    assert step["status"] == "completed"


def test_server_execution_rejects_a_plan_bound_to_superseded_child_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime-stale-state.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    registry = CapabilityRegistry()
    payload = {
        "objective": {"mode": "none", "type": None, "summary": None,
                      "success_criteria": [], "phase": "read_plan"},
        "next_step": {"capability": "planning.describe", "arguments": {},
                      "reason": "读取当前安排", "expected_evidence": ["plan_status_read"]},
        "spoken_transition": "我看一下。", "requires_child_reply": False, "confidence": .9,
    }
    decision = parse_agent_plan_output(json.dumps(payload, ensure_ascii=False), "check_in", registry)
    context = store.assemble_context(session_id, ())
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
                 response_text,action_json,orchestrator_version,created_at,client_action_id)
               VALUES('turn-stale','child-runtime',?,'今天安排是什么','planning.describe','我看一下。',
                 '{}','agent_runtime_planner_v1',datetime('now'),'turn-stale-action')""",
            (session_id,),
        )
        store.persist_plan(
            connection, child_id="child-runtime", session_id=session_id,
            agent_turn_id="turn-stale", client_action_id="turn-stale-action",
            decision=decision, context_manifest=context_manifest(context),
        )
        connection.commit()
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET state='study',last_checkpoint_at=? WHERE session_id=?",
            ((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(), session_id),
        )
        connection.commit()
    ChildModelService(database).snapshot("child-runtime")

    with pytest.raises(AgentExecutionConflict, match="CHILD_MODEL_SNAPSHOT_STALE"):
        AgentCapabilityExecutor(database, StudyRepository(database)).execute("turn-stale")
    with database.connect() as connection:
        status = connection.execute(
            "SELECT status FROM agent_step WHERE idempotency_key='agent-step:turn-stale-action:0'"
        ).fetchone()[0]
    assert status == "planned"


def test_compound_recall_executes_once_and_survives_reopen(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime-batch.db")
    database.initialize()
    session_id = seed_session(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET state = 'classroom_recall' WHERE session_id = ?",
            (session_id,),
        )
        connection.commit()
    store = AgentRuntimeStore(database)
    payload = {
        "objective": {
            "mode": "new", "type": "execution", "summary": "保存多学科课堂回想",
            "success_criteria": ["每一科都单独保存"], "phase": "capture",
        },
        "next_step": {
            "capability": "recall.add_batch",
            "arguments": {"items": [
                {"subject": "数学", "summary": "数学老师讲了分式"},
                {"subject": "历史", "summary": "历史讲了明清更替"},
            ]},
            "reason": "孩子一次说了多科", "expected_evidence": ["recalls_saved"],
        },
        "spoken_transition": "我会逐项保存。", "requires_child_reply": False, "confidence": 0.97,
    }
    decision = parse_agent_plan_output(
        json.dumps(payload, ensure_ascii=False), "classroom_recall", CapabilityRegistry()
    )
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_turn(
              agent_turn_id, child_id, session_id, transcript, intent, response_text,
              action_json, orchestrator_version, created_at, client_action_id
            ) VALUES ('turn-batch', 'child-runtime', ?, '数学和历史', 'recall.add_batch',
                      '我会逐项保存。', ?, 'agent_runtime_planner_v1', datetime('now'), 'turn-batch-action')
            """,
            (
                session_id,
                json.dumps({**action_from_plan(decision, "数学和历史", None), "source": "agent_runtime_planner"}, ensure_ascii=False),
            ),
        )
        store.persist_plan(
            connection, child_id="child-runtime", session_id=session_id,
            agent_turn_id="turn-batch", client_action_id="turn-batch-action",
            decision=decision, context_manifest={},
        )
        connection.commit()

    executor = AgentCapabilityExecutor(database, StudyRepository(database))
    first = executor.execute("turn-batch")
    second = AgentCapabilityExecutor(Database(database.path), StudyRepository(Database(database.path))).execute("turn-batch")

    assert first["result"]["count"] == 2
    assert second == first
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT subject, summary FROM classroom_recall
            WHERE session_id = ? AND record_kind = 'child_statement' ORDER BY subject
            """,
            (session_id,),
        ).fetchall()
        candidate_count = connection.execute(
            "SELECT COUNT(*) FROM classroom_recall WHERE session_id = ? AND record_kind = 'system_candidate'",
            (session_id,),
        ).fetchone()[0]
    assert [dict(row) for row in rows] == [
        {"subject": "历史", "summary": "历史讲了明清更替"},
        {"subject": "数学", "summary": "数学老师讲了分式"},
    ]
    assert candidate_count == 2


def test_runtime_recovers_interrupted_capability_as_explicit_failure(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime-recovery.db")
    database.initialize()
    session_id = seed_session(database)
    store = AgentRuntimeStore(database)
    decision = parse_agent_plan_output(learning_plan_json(), "check_in", CapabilityRegistry())
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_turn(
              agent_turn_id, child_id, session_id, transcript, intent, response_text,
              action_json, orchestrator_version, created_at, client_action_id
            ) VALUES ('turn-interrupted', 'child-runtime', ?, '请讲正弦', 'learning.explain',
                      '我来讲。', '{}', 'agent_runtime_planner_v1', datetime('now'), 'turn-interrupted-action')
            """,
            (session_id,),
        )
        metadata = store.persist_plan(
            connection, child_id="child-runtime", session_id=session_id,
            agent_turn_id="turn-interrupted", client_action_id="turn-interrupted-action",
            decision=decision, context_manifest={},
        )
        connection.execute("UPDATE agent_step SET status = 'running' WHERE step_id = ?", (metadata["step_id"],))
        connection.execute("UPDATE agent_plan SET status = 'running' WHERE plan_id = ?", (metadata["plan_id"],))
        connection.commit()

    assert AgentRuntimeStore(Database(database.path)).recover_interrupted_executions() == 1
    with database.connect() as connection:
        step = connection.execute("SELECT status, error_code FROM agent_step WHERE step_id = ?", (metadata["step_id"],)).fetchone()
        plan = connection.execute("SELECT status FROM agent_plan WHERE plan_id = ?", (metadata["plan_id"],)).fetchone()
        turn = connection.execute("SELECT tool_status, tool_error_code FROM agent_turn WHERE agent_turn_id = 'turn-interrupted'").fetchone()
    assert dict(step) == {"status": "failed", "error_code": "APP_RESTARTED"}
    assert plan["status"] == "failed"
    assert dict(turn) == {"tool_status": "failed", "tool_error_code": "APP_RESTARTED"}
    assert store.recover_interrupted_executions() == 0


def test_local_composed_check_response_is_not_a_cloud_planner_capability(tmp_path):
    database = Database(tmp_path / "local-check-planner.db")
    database.initialize()
    sid = seed_session(database)
    context = AgentRuntimeStore(database).assemble_context(sid, ())
    messages = build_planner_messages(context, "请点评刚才的回答", CapabilityRegistry())
    catalog = json.loads(messages[1]["content"])["available_capabilities"]
    assert "tutor.capture_check_response" not in {item["name"] for item in catalog}
    candidate = json.loads(learning_plan_json())
    candidate["next_step"].update(capability="tutor.capture_check_response", arguments={
        "turn_id": "provider-invented", "source_version": "provider-invented", "response": "provider-invented"})
    with pytest.raises(InvalidAgentPlanError, match="CAPABILITY_REQUIRES_LOCAL_COMPOSITION"):
        parse_agent_plan_output(json.dumps(candidate, ensure_ascii=False), "study", CapabilityRegistry())
