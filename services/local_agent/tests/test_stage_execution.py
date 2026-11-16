"""Execution resolves the original persisted plan slot, never today's next item."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from test_specialized_stages import case, baseline, answer, node, reviewed_variants, chain, CHILD, OTHER, NODE


def persisted_plan(case, *, retention=False):
    if retention:
        chain(case)
    else:
        answer(case, baseline(case))
        reviewed_variants(case)
    action = node(case)["next_action"]
    fields = ("track", "domain_id", "knowledge_node_id", "diagnostic_item_id", "source_version", "review_task_id", "not_before")
    binding = {key: action[key] for key in fields}
    binding["stage_constraint"] = {key: action["stage_constraint"][key] for key in ("version", "current_stage", "prerequisite_version")}
    with case[0].connect() as c:
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,total_minutes,generator,version,status,created_at,client_action_id)
            VALUES('stage-plan',?,?,'2026-09-07',15,'test',1,'active',?,'stage-plan-create')""", (CHILD, case[2], case[3][0].isoformat()))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type)
            VALUES('stage-plan-item','stage-plan','数学','Synthetic stage',15,1,'active',1,'competition_math','competition_math.counting','written_practice')""")
        c.execute("""INSERT INTO plan_constraint_decision(decision_id,plan_id,obligation_type,source_ref,decision,reason,requested_minutes,allocated_minutes,metadata_json,created_at)
            VALUES('stage-decision','stage-plan','competition_math','competition_math.counting','scheduled','Synthetic',15,15,?,?)""",
            (json.dumps({"plan_item_id": "stage-plan-item", "planning_evidence": binding}), case[3][0].isoformat()))
        c.commit()
    return binding


def resolve(case, sid=None, pid="stage-plan-item"):
    from home_study_agent.stage_execution import resolve_stage_execution
    with case[0].connect() as c:
        return resolve_stage_execution(c, sid or case[2], pid, as_of=case[3][0].isoformat())


def test_ready_is_exact_original_item_and_typable_version_without_writes(case):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService, _version
    binding = persisted_plan(case)
    with case[0].connect() as c:
        before = "\n".join(c.iterdump())
        expected = DiagnosticPromptService._item(c, CHILD, binding["diagnostic_item_id"])
    result = resolve(case)
    assert result["status"] == "ready"
    assert result["diagnostic_item_id"] == binding["diagnostic_item_id"]
    assert result["item_version"] == _version(expected)
    assert result["source_version"] == binding["source_version"]
    assert "expected_answer_json" not in result["diagnostic_item"]
    assert result["requires_prompt_binding"] is True and not result["reservation_created"]
    assert result["cloud_transmission_allowed"] is False
    with case[0].connect() as c:
        assert "\n".join(c.iterdump()) == before


@pytest.mark.parametrize("mutation", ["foreign_session", "completed", "cancelled_plan", "withdrawn_source", "missing_binding", "corrupt_binding", "foreign_child"])
def test_inactive_foreign_missing_or_changed_binding_never_falls_back(case, mutation):
    binding = persisted_plan(case)
    with case[0].connect() as c:
        if mutation == "completed":
            c.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id='stage-plan-item'")
        elif mutation == "cancelled_plan":
            c.execute("UPDATE daily_plan SET status='superseded' WHERE plan_id='stage-plan'")
        elif mutation == "withdrawn_source":
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (binding["diagnostic_item_id"],))
        elif mutation == "missing_binding":
            c.execute("UPDATE plan_constraint_decision SET metadata_json='{}'")
        elif mutation == "corrupt_binding":
            c.execute("UPDATE plan_constraint_decision SET metadata_json='not json'")
        elif mutation == "foreign_child":
            c.execute("UPDATE daily_plan SET child_id=? WHERE plan_id='stage-plan'", (OTHER,))
        c.commit()
    result = resolve(case, sid="another-session" if mutation == "foreign_session" else None)
    assert result["status"] == "blocked"
    assert result["diagnostic_item_id"] is None and result["diagnostic_item"] is None


def test_new_stage_question_after_answer_never_replaces_persisted_question(case):
    binding = persisted_plan(case)
    case[3][0] += timedelta(minutes=1)
    answer(case, binding["diagnostic_item_id"])
    assert node(case)["current_stage"] == "far_transfer"
    assert resolve(case)["status"] == "blocked"


def test_retention_due_transition_and_revised_due_preserve_original_lower_bound(case):
    binding = persisted_plan(case, retention=True)
    assert resolve(case)["reason_code"] == "stage_not_due"
    with case[0].connect() as c:
        c.execute("UPDATE review_task SET due_at=? WHERE review_task_id=?", (case[3][0].isoformat(), binding["review_task_id"]))
        c.commit()
    assert resolve(case)["status"] == "blocked"
    case[3][0] += timedelta(days=7)
    result = resolve(case)
    assert result["status"] == "ready"
    assert result["review_task_id"] == binding["review_task_id"]


def test_original_plan_id_marker_rejects_conflicting_decision_without_leaking_metadata(case):
    persisted_plan(case)
    with case[0].connect() as c:
        c.execute("UPDATE plan_constraint_decision SET metadata_json=json_set(metadata_json,'$.plan_item_id','some-other-item','$.PRIVATE','do not forward')")
        c.commit()
    result = resolve(case)
    assert result["status"] == "blocked"
    assert "PRIVATE" not in json.dumps(result)


def test_same_transaction_uncommitted_source_withdrawal_and_restart(case):
    from home_study_agent.stage_execution import resolve_stage_execution
    from home_study_agent.database import Database
    binding = persisted_plan(case)
    with case[0].connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (binding["diagnostic_item_id"],))
        changes = c.total_changes
        assert resolve_stage_execution(c, case[2], "stage-plan-item", as_of=case[3][0].isoformat())["status"] == "blocked"
        assert c.in_transaction and c.total_changes == changes
        c.rollback()
    with Database(case[0].path).connect() as c:
        assert resolve_stage_execution(c, case[2], "stage-plan-item", as_of=case[3][0].isoformat()) == resolve(case)


@pytest.mark.parametrize("phase", ["check_in", "capture", "plan", "study", "break", "reflection", "summary"])
def test_active_nonterminal_phase_does_not_change_original_binding(case, phase):
    binding = persisted_plan(case)
    with case[0].connect() as c:
        c.execute("UPDATE daily_session SET state=? WHERE session_id=?", (phase, case[2]))
        c.commit()
    assert resolve(case)["diagnostic_item_id"] == binding["diagnostic_item_id"]


def test_main_whitelist_without_track_or_item_marker_and_no_private_payload_spread(case):
    persisted_plan(case)
    with case[0].connect() as c:
        row = c.execute("SELECT metadata_json FROM plan_constraint_decision").fetchone()
        metadata = json.loads(row[0])
        metadata.pop("plan_item_id")
        binding = metadata["planning_evidence"]
        binding.pop("track")
        binding.update(execution_status="ready", mastery_claim_allowed=False, PRIVATE="must not spread")
        binding["stage_constraint"]["evidence_refs"] = ["private-ref-not-part-of-contract"]
        c.execute("UPDATE plan_constraint_decision SET metadata_json=?", (json.dumps(metadata),))
        c.commit()
    result = resolve(case)
    assert result["status"] == "ready"
    assert "PRIVATE" not in json.dumps(result) and "private-ref-not-part-of-contract" not in json.dumps(result)
    with case[0].connect() as c:
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type)
            SELECT 'duplicate-source',plan_id,subject,title,expected_minutes,priority,status,2,source_type,source_ref,task_type
            FROM plan_item WHERE plan_item_id='stage-plan-item'""")
        c.commit()
    assert resolve(case)["reason_code"] == "stage_plan_binding_ambiguous"
