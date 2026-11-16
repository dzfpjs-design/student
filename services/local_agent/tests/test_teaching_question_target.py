"""Exact persisted question identity, not subject similarity or model node guesses."""

from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.repository import StudyRepository
from home_study_agent.tutor_ocr_confirmation import OcrTutorConfirmationService
from test_teaching_context import flow, change, score, CHILD, OTHER, NODE, ITEM, SECOND


@pytest.fixture
def target_case(flow):
    score(flow)
    service = DiagnosticPromptService(flow[0])
    prompt = service.prepare(flow[4], preferred_item_id=SECOND, client_action_id="target-prepare")["prompt"]
    flow[3][0] = datetime.now(timezone.utc)
    return flow, service, prompt


def request(case, **overrides):
    prompt = case[2]
    return {"subject": "数学", "question": prompt["item"]["prompt"],
            "prompt_id": prompt["prompt_id"], "item_version": prompt["item_version"], **overrides}


def resolve(case, **overrides):
    from home_study_agent.teaching_question_target import resolve_question_target
    with case[0][0].connect() as c:
        return resolve_question_target(c, case[0][4], **request(case, **overrides))


def controls(case, **overrides):
    flow = case[0]
    return ChildModelService(flow[0]).tutor_controls(CHILD, 1, None, {}, session_id=flow[4], **request(case, **overrides))


def save_parent(case, *, result=None, **overrides):
    flow = case[0]
    result = result or controls(case)
    return flow[1].save_tutor_turn(flow[4], None, "数学", request(case)["question"], "Synthetic explanation",
        "test", "test", "parent-turn", output_envelope={"local_teaching_control": result["audit"]}, **overrides)


def test_exact_typed_prompt_selects_current_node_and_support(target_case):
    result = resolve(target_case)
    assert result["status"] == "resolved"
    assert result["knowledge_node_id"] == NODE
    assert result["task_type"] == "diagnostic"
    assert result["binding"]["diagnostic_item_id"] == SECOND
    supported = controls(target_case)
    assert supported["audit"]["question_target"] == result
    assert supported["audit"]["controls"]["explanation_pacing"] == "short_step_then_restate"
    assert not result["cloud_transmission_allowed"]
    assert not result["mastery_claim_allowed"]


def test_original_content_boundary_is_not_cloud_authorization(target_case):
    result = resolve(target_case)
    assert result["question_content_cloud_blocked"] is False
    assert result["question_content_cloud_reason"] == "verified_shipped_original_runtime_checks_required"
    assert result["cloud_authorization_granted"] is False
    assert result["cloud_transmission_allowed"] is False


@pytest.mark.parametrize("source", ["local_graph:v1", "unreviewed_diagnostic", "hsc_original_forged", ""])
def test_resolved_local_or_unknown_item_stays_cloud_blocked(flow, source):
    change(flow[0], "UPDATE diagnostic_item SET source_ref=? WHERE diagnostic_item_id=?", (source, SECOND))
    service = DiagnosticPromptService(flow[0])
    prompt = service.prepare(flow[4], preferred_item_id=SECOND, client_action_id="local-target")["prompt"]
    case = flow, service, prompt
    result = resolve(case)
    assert result["status"] == "resolved"
    assert result["question_content_cloud_blocked"] is True
    assert result["question_content_cloud_reason"] == "diagnostic_source_local_or_unknown"
    parent = save_parent(case)
    change(flow[0], """UPDATE tutor_turn SET output_envelope_json=json_set(output_envelope_json,
        '$.local_teaching_control.question_target.question_content_cloud_blocked',json('false'))
        WHERE turn_id=?""", (parent["turn_id"],))
    restored = resolve(case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])
    assert restored["question_content_cloud_blocked"] is True
    from home_study_agent.teaching_question_target import resolve_question_target
    with Database(flow[0].path).connect() as c:
        assert resolve_question_target(c, flow[4], **request(case, prompt_id=None, item_version=None,
            parent_turn_id=parent["turn_id"])) == restored


def test_original_label_with_changed_content_does_not_authorize_cloud(flow):
    change(flow[0], "UPDATE diagnostic_item SET prompt='PRIVATE imported question' WHERE diagnostic_item_id=?", (SECOND,))
    service = DiagnosticPromptService(flow[0])
    prompt = service.prepare(flow[4], preferred_item_id=SECOND, client_action_id="changed-original")["prompt"]
    result = resolve((flow, service, prompt))
    assert result["status"] == "resolved"
    assert result["question_content_cloud_blocked"] is True
    assert result["question_content_cloud_reason"] == "shipped_original_content_not_verified"


def test_approved_local_node_is_not_cloud_permission(flow):
    change(flow[0], "UPDATE knowledge_node SET source_ref='private-node-source' WHERE knowledge_node_id=?", (NODE,))
    change(flow[0], """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
        source_ref,license_status,review_status,content_hash,imported_at)
        VALUES('private-node-source',?,'test','PRIVATE','数学','四年级',
        'private-node-source','local','approved','v1',?)""", (CHILD, flow[3][0].isoformat()))
    service = DiagnosticPromptService(flow[0])
    prompt = service.prepare(flow[4], preferred_item_id=SECOND, client_action_id="local-node")["prompt"]
    result = resolve((flow, service, prompt))
    assert result["status"] == "resolved"
    assert result["question_content_cloud_blocked"] is True
    assert result["question_content_cloud_reason"] == "node_source_local_or_unknown"


def test_unknown_unbound_keeps_existing_boundary_but_invalid_reference_blocks(target_case):
    unbound = resolve(target_case, prompt_id=None, item_version=None)
    assert unbound["status"] == "unknown"
    assert unbound["question_content_cloud_blocked"] is False
    assert unbound["question_content_cloud_reason"] == "unbound_requires_existing_privacy_checks"
    invalid = resolve(target_case, item_version="wrong")
    assert invalid["question_content_cloud_blocked"] is True


def test_cross_child_control_wrapper_also_blocks_question_content(target_case):
    result = ChildModelService(target_case[0][0]).tutor_controls(OTHER, 1, None, {},
        session_id=target_case[0][4], **request(target_case))["audit"]["question_target"]
    assert result["status"] == "invalid"
    assert result["question_content_cloud_blocked"] is True


@pytest.mark.parametrize("extra", [
    {"question": "同样是数学，但现在问分数"}, {"subject": "英语"},
    {"item_version": "changed-version"}, {"item_version": None}, {"prompt_id": None},
])
def test_half_binding_or_question_subject_version_mismatch_cannot_relabel(target_case, extra):
    result = resolve(target_case, **extra)
    assert result["status"] == "invalid"
    assert result["knowledge_node_id"] is None
    assert controls(target_case, **extra)["audit"]["controls"]["explanation_pacing"] == "standard_layered"


def test_unbound_question_does_not_automatically_take_even_matching_pending_prompt(target_case):
    result = resolve(target_case, prompt_id=None, item_version=None)
    assert result["status"] == "unknown"
    assert result["reason_code"] == "no_explicit_question_reference"
    assert controls(target_case, prompt_id=None, item_version=None)["audit"]["controls"]["explanation_pacing"] == "standard_layered"


def test_question_mode_never_falls_back_to_explicit_node_or_active_plan(target_case):
    from test_teaching_context import bind_plan
    flow = target_case[0]
    pid = bind_plan(flow)
    result = ChildModelService(flow[0]).tutor_controls(CHILD, 1, None, {}, session_id=flow[4], subject="数学",
        question="A different math problem", plan_item_id=pid, knowledge_node_id=NODE, task_type="diagnostic")
    assert result["audit"]["question_target"]["status"] == "unknown"
    assert result["audit"]["teaching_context"]["learning"]["status"] == "unknown"


@pytest.mark.parametrize("mutation", ["cancelled", "item_changed", "source_withdrawn", "snapshot_forged", "session_closed"])
def test_current_durable_source_is_rechecked_readonly(target_case, mutation):
    db, _, _, _, sid = target_case[0]
    if mutation == "cancelled":
        target_case[1].cancel(sid, prompt_id=target_case[2]["prompt_id"], item_version=target_case[2]["item_version"])
    elif mutation == "item_changed":
        change(db, "UPDATE diagnostic_item SET prompt='Changed prompt' WHERE diagnostic_item_id=?", (SECOND,))
    elif mutation == "source_withdrawn":
        change(db, "UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (SECOND,))
    elif mutation == "snapshot_forged":
        change(db, "UPDATE diagnostic_prompt SET item_json=json_set(item_json,'$.knowledge_node_id','fake-node')")
    else:
        change(db, "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
    with db.connect() as c:
        before = "\n".join(c.iterdump())
    assert resolve(target_case)["status"] == "invalid"
    with db.connect() as c:
        assert "\n".join(c.iterdump()) == before


def test_cancelled_old_prompt_cannot_rebind_to_later_prompt(target_case):
    flow, service, old = target_case
    service.cancel(flow[4], prompt_id=old["prompt_id"], item_version=old["item_version"])
    new = service.prepare(flow[4], preferred_item_id=ITEM, client_action_id="new-target")["prompt"]
    assert new["prompt_id"] != old["prompt_id"]
    assert resolve(target_case)["status"] == "invalid"


def test_original_parent_question_is_revalidated_after_persistence_and_restart(target_case):
    parent = save_parent(target_case)
    original = resolve(target_case)
    replay = resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])
    assert replay["status"] == "resolved"
    assert replay["binding"] == original["binding"]
    assert replay["fingerprint"] == original["fingerprint"]
    kwargs = request(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])
    script = """import json,sys
from home_study_agent.database import Database
from home_study_agent.teaching_question_target import resolve_question_target
with Database(sys.argv[1]).connect() as c:
 print(json.dumps(resolve_question_target(c,sys.argv[2],**json.loads(sys.argv[3]))))
"""
    process = subprocess.run([sys.executable, "-c", script, str(target_case[0][0].path), target_case[0][4], json.dumps(kwargs)],
                             env=os.environ.copy(), capture_output=True, text=True, check=True)
    assert json.loads(process.stdout) == replay
    change(target_case[0][0], "UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (SECOND,))
    assert resolve(target_case, **{k: kwargs[k] for k in ("prompt_id", "item_version", "parent_turn_id")})["status"] == "invalid"


def test_legacy_parent_and_parent_changed_question_do_not_create_target(target_case):
    parent = save_parent(target_case, result={"audit": {"teaching_context": {"scope": {"knowledge_node_id": NODE}}}})
    assert resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])["status"] == "unknown"
    assert resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"], question="New question")["status"] == "invalid"


def test_parent_cannot_use_a_new_prompt_or_tampered_local_target(target_case):
    parent = save_parent(target_case)
    assert resolve(target_case, parent_turn_id=parent["turn_id"], item_version="wrong")["status"] == "invalid"
    change(target_case[0][0], """UPDATE tutor_turn SET output_envelope_json=json_set(output_envelope_json,
        '$.local_teaching_control.question_target.knowledge_node_id','fake-node') WHERE turn_id=?""", (parent["turn_id"],))
    assert resolve(target_case, parent_turn_id=parent["turn_id"], prompt_id=None, item_version=None)["status"] == "invalid"


def confirm_ocr(case, *, text=None):
    flow, _, prompt = case
    text = text or prompt["item"]["prompt"]
    change(flow[0], """INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,
        extracted_text,confidence,details_json,status,created_at,client_action_id)
        VALUES('target-artifact',?,?,'document_ocr',NULL,?,.9,'{}','processed',?,'target-artifact')""",
        (CHILD, flow[4], text, datetime.now(timezone.utc).isoformat()))
    service = OcrTutorConfirmationService(flow[0])
    service.ensure_schema()
    return service.confirm(flow[4], "target-artifact", text, "ocr_verified", "confirm-target")


def test_ocr_confirmation_requires_exact_original_reference_to_supply_node(target_case):
    ocr = confirm_ocr(target_case)
    unknown = resolve(target_case, prompt_id=None, item_version=None, ocr_confirmation_id=ocr["confirmation_id"])
    assert unknown["status"] == "unknown"
    assert unknown["reason_code"] == "ocr_without_original_reference"
    target = resolve(target_case, ocr_confirmation_id=ocr["confirmation_id"])
    assert target["status"] == "resolved"
    assert target["binding"]["ocr_confirmation_id"] == ocr["confirmation_id"]
    assert "confirmed_text" not in json.dumps(target)
    assert target_case[2]["item"]["prompt"] not in json.dumps(target, ensure_ascii=False)


@pytest.mark.parametrize("mutation", ["expired", "revoked", "artifact_removed", "text_changed", "other_question"])
def test_ocr_source_changes_never_keep_old_node(target_case, mutation):
    ocr = confirm_ocr(target_case, text="A different question" if mutation == "other_question" else None)
    db = target_case[0][0]
    if mutation == "expired":
        change(db, "UPDATE tutor_ocr_confirmation SET expires_at='2000-01-01T00:00:00+00:00'")
    elif mutation == "revoked":
        change(db, "UPDATE tutor_ocr_confirmation SET revoked_at=?", (datetime.now(timezone.utc).isoformat(),))
    elif mutation == "artifact_removed":
        change(db, "DELETE FROM capture_artifact WHERE artifact_id='target-artifact'")
    elif mutation == "text_changed":
        change(db, "UPDATE tutor_ocr_confirmation SET confirmed_text='A different question'")
    assert resolve(target_case, ocr_confirmation_id=ocr["confirmation_id"])["status"] == "invalid"


def test_ocr_parent_omission_inherits_original_confirmation_not_free_text(target_case):
    ocr = confirm_ocr(target_case)
    controlled = controls(target_case, ocr_confirmation_id=ocr["confirmation_id"])
    parent = save_parent(target_case, result=controlled, ocr_confirmation_id=ocr["confirmation_id"])
    assert resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])["status"] == "resolved"
    change(target_case[0][0], "UPDATE tutor_ocr_confirmation SET revoked_at=?", (datetime.now(timezone.utc).isoformat(),))
    assert resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])["status"] == "invalid"


@pytest.mark.parametrize("phase", ["check_in", "classroom_recall", "homework_capture", "planning", "study", "break", "summary"])
def test_target_resolution_is_cross_phase_without_state_changes(target_case, phase):
    change(target_case[0][0], "UPDATE daily_session SET state=? WHERE session_id=?", (phase, target_case[0][4]))
    assert resolve(target_case)["status"] == "resolved"
    with target_case[0][0].connect() as c:
        assert c.execute("SELECT state FROM daily_session WHERE session_id=?", (target_case[0][4],)).fetchone()[0] == phase


@pytest.mark.parametrize("other_child", [CHILD, OTHER])
def test_prompt_and_parent_cannot_cross_sessions_or_children(target_case, other_child):
    from home_study_agent.teaching_question_target import resolve_question_target
    parent = save_parent(target_case)
    flow = target_case[0]
    change(flow[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (flow[4],))
    sid = flow[1].start_session(other_child, "next-session")["session_id"]
    with flow[0].connect() as c:
        for kwargs in (request(target_case), request(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])):
            result = resolve_question_target(c, sid, **kwargs)
            assert result["status"] == "invalid"
            assert result["binding"] is None and result["knowledge_node_id"] is None


def test_answer_saved_crash_and_answered_prompt_remain_original_targets(target_case, monkeypatch):
    flow, service, prompt = target_case
    parent = save_parent(target_case)
    original = resolve(target_case)
    def crash(*args, **kwargs):
        raise RuntimeError("saved before crash")
    with monkeypatch.context() as patch:
        patch.setattr(service, "resume", crash)
        with pytest.raises(RuntimeError, match="saved before crash"):
            service.answer(flow[4], prompt_id=prompt["prompt_id"], item_version=prompt["item_version"],
                           answer="42", client_action_id="saved-answer")
    assert resolve(target_case)["fingerprint"] == original["fingerprint"]
    assert DiagnosticPromptService(Database(flow[0].path)).resume(flow[4], prompt_id=prompt["prompt_id"])["status"] == "answered"
    assert resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])["status"] == "resolved"
    new = service.prepare(flow[4], preferred_item_id=ITEM, client_action_id="next-question")["prompt"]
    assert new["prompt_id"] != prompt["prompt_id"]
    assert resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])["binding"]["prompt_id"] == prompt["prompt_id"]


def test_actual_review_prompt_resolves_review_not_generic_diagnostic(flow):
    flow[3][0] -= timedelta(days=3)
    attempt = score(flow)
    with flow[0].connect() as c:
        review = dict(c.execute("SELECT * FROM review_task WHERE source_evidence_id=?", (attempt["evidence_id"],)).fetchone())
    flow[3][0] = datetime.now(timezone.utc)
    service = DiagnosticPromptService(flow[0])
    prompt = service.prepare(flow[4], preferred_item_id=review["diagnostic_item_id"], review_task_id=review["review_task_id"],
                             client_action_id="review-target")["prompt"]
    case = flow, service, prompt
    result = resolve(case)
    assert result["status"] == "resolved" and result["task_type"] == "review"
    change(flow[0], "UPDATE review_task SET status='cancelled' WHERE review_task_id=?", (review["review_task_id"],))
    assert resolve(case)["status"] == "invalid"


def test_stimulus_change_invalidates_same_text_item_version(target_case):
    change(target_case[0][0], "UPDATE diagnostic_item SET stimulus_text='PRIVATE replacement material' WHERE diagnostic_item_id=?", (SECOND,))
    assert resolve(target_case)["status"] == "invalid"


def test_question_target_and_local_evidence_never_enter_fixed_prompt(target_case):
    ocr = confirm_ocr(target_case)
    result = controls(target_case, ocr_confirmation_id=ocr["confirmation_id"])
    target = result["audit"]["question_target"]
    for secret in (target["fingerprint"], target["source_fingerprint"], target["binding"]["prompt_id"],
                   ocr["confirmation_id"], CHILD, NODE, SECOND, target_case[2]["item"]["prompt"]):
        assert secret not in result["prompt_extension"]


def test_resolver_no_schema_creation_and_no_cross_request_cache(flow):
    from home_study_agent.teaching_question_target import resolve_question_target
    with flow[0].connect() as c:
        before = "\n".join(c.iterdump())
        result = resolve_question_target(c, flow[4], subject="数学", question="An unbound question")
        assert result["status"] == "unknown"
        assert not c.in_transaction
        assert "\n".join(c.iterdump()) == before


def test_context_and_target_are_read_in_the_same_transaction(target_case, monkeypatch):
    from home_study_agent import teaching_context
    build = teaching_context.build_teaching_context
    observed = []
    def checked(c, child, **kwargs):
        observed.append(c.in_transaction)
        assert c.execute("SELECT item_version FROM diagnostic_prompt WHERE prompt_id=?", (target_case[2]["prompt_id"],)).fetchone()[0] == target_case[2]["item_version"]
        return build(c, child, **kwargs)
    monkeypatch.setattr(teaching_context, "build_teaching_context", checked)
    assert controls(target_case)["audit"]["question_target"]["status"] == "resolved"
    assert observed == [True]


def test_unbound_same_question_followup_remains_unknown_not_invalid(target_case):
    unbound = controls(target_case, prompt_id=None, item_version=None)
    parent = save_parent(target_case, result=unbound)
    result = resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])
    assert result["status"] == "unknown"
    assert result["knowledge_node_id"] is None


@pytest.mark.parametrize("safety,status,accepted", [
    ("answer_leak_replaced", "completed", True),
    ("blocked_replaced", "completed", False),
    ("blocked", "completed", False),
    ("degraded", "completed", False),
    ("answer_leak_replaced", "failed", False),
])
def test_only_completed_safe_hint_replacement_continues_an_unbound_question(target_case, safety, status, accepted):
    unbound = controls(target_case, prompt_id=None, item_version=None)
    parent = save_parent(target_case, result=unbound, safety_status=safety)
    change(target_case[0][0], "UPDATE tutor_turn SET status=? WHERE turn_id=?", (status, parent["turn_id"]))
    result = resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])
    assert result["status"] == ("unknown" if accepted else "invalid")
    assert result["knowledge_node_id"] is None and result["binding"] is None
    assert result["mastery_claim_allowed"] is False
    assert result["cloud_authorization_granted"] is False
    if accepted:
        assert result["question_content_cloud_blocked"] is False
        assert resolve(target_case, prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"],
                       question="Different question")["status"] == "invalid"


@pytest.mark.parametrize("legacy", [True, False])
def test_unbound_ocr_parent_still_revalidates_original_confirmation(target_case, legacy):
    ocr = confirm_ocr(target_case)
    result = {"audit": {}} if legacy else controls(target_case, prompt_id=None, item_version=None, ocr_confirmation_id=ocr["confirmation_id"])
    parent = save_parent(target_case, result=result, ocr_confirmation_id=ocr["confirmation_id"])
    kwargs = dict(prompt_id=None, item_version=None, parent_turn_id=parent["turn_id"])
    assert resolve(target_case, **kwargs)["status"] == "unknown"
    change(target_case[0][0], "UPDATE tutor_ocr_confirmation SET revoked_at=?", (datetime.now(timezone.utc).isoformat(),))
    assert resolve(target_case, **kwargs)["status"] == "invalid"
