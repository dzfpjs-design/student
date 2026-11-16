from datetime import timedelta
import json
import sqlite3
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.mistake_book import MistakeBookService
from test_mistake_gate_reliability import CHILD, ITEM, SECOND, alias, flow, prepare


def propose(book, case, **kwargs):
    return book.propose_diagnosis_hypotheses(case, CHILD, [
        {"error_type": "concept_gap", "stance": "candidate", "explanation": "Possible rule gap", "confidence": .99},
        {"error_type": "incidental", "stance": "candidate", "explanation": "Alternative"},
        {"error_type": "reading_gap", "stance": "denied", "explanation": "Child denies this"},
    ], book.diagnosis_state(case, CHILD)["version"], kwargs.pop("action", str(uuid4())), **kwargs)


def ask(book, case, item=SECOND):
    state = book.diagnosis_state(case, CHILD)
    predictions = {h["hypothesis_id"]: "incorrect" if h["error_type"] == "concept_gap" else "correct"
                   for h in state["hypotheses"] if h["stance"] == "candidate"}
    return book.start_diagnosis_probe(case, CHILD, item, predictions, state["version"], str(uuid4()))


def respond(book, case, probe, answer="42", **kwargs):
    return book.answer_diagnosis_probe(case, CHILD, probe, answer,
        book.diagnosis_state(case, CHILD)["version"], str(uuid4()), **kwargs)


def score(flow, book, case, answer_event, item=SECOND, answer="42"):
    _, learning, sid, clock = flow
    clock[0] += timedelta(seconds=1)
    receipt = learning.submit_diagnostic(sid, item, answer, str(uuid4()))
    return book.attach_diagnosis_evidence(case, CHILD, answer_event, receipt["evidence_id"],
        book.diagnosis_state(case, CHILD)["version"], str(uuid4()))


def retract(book, case, target):
    return book.retract_diagnosis_event(case, CHILD, target, "Correction requested",
        book.diagnosis_state(case, CHILD)["version"], str(uuid4()))


def test_multihypotheses_denial_unknown_and_gate_survive_restart(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    state = propose(book, case)
    assert state["version"] == 1
    assert {h["stance"] for h in state["hypotheses"]} == {"candidate", "denied", "unknown"}
    assert all(h["status"] == "candidate" for h in state["hypotheses"])
    restarted = MistakeBookService(Database(db.path))
    assert restarted.diagnosis_state(case, CHILD) == book.diagnosis_state(case, CHILD)
    assert state["assessment"]["causal_status"] == "unconfirmed"
    assert state["next_action"]["kind"] == "start_probe"
    assert book.get(case, CHILD)["mastery_gate"]["requirements"]["delayed_48h"] == 1


def test_question_answer_score_are_separate_bound_durable_events(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    question = ask(book, case)
    probe = question["probes"][0]
    assert "粗心" not in probe["question"]
    assert probe["binding"]["item"]["diagnostic_item_id"] == SECOND
    assert probe["binding"]["original"]["submitted_answer"] == "wrong"
    assert question["next_action"]["kind"] == "answer_probe"
    answered = respond(book, case, question["event_id"])
    assert answered["assessment"]["support"] == []
    assert answered["next_action"]["kind"] == "await_score"
    state = score(flow, book, case, answered["event_id"])
    assert state["version"] == 4
    assert state["assessment"]["support"] and state["assessment"]["counterevidence"]
    assert state["assessment"]["status"] == "insufficient"
    assert "replication_required" in state["assessment"]["insufficiency"]
    restarted = MistakeBookService(Database(db.path))
    assert restarted.diagnosis_state(case, CHILD) == book.diagnosis_state(case, CHILD)
    assert [e["kind"] for e in state["history"]] == ["hypotheses", "probe", "answer", "evidence"]
    assert state["history"][-1]["payload"]["recorded_score"]["correctness"] == 1
    assert state["history"][-1]["payload"]["recorded_qualification"]["score_eligible"] is True
    with db.connect() as c:
        c.execute("PRAGMA query_only=ON")
        from home_study_agent.mistake_diagnosis import MistakeDiagnosisService
        before = c.total_changes
        assert MistakeDiagnosisService(db).project(c, case, CHILD)["version"] == 4
        assert c.total_changes == before


def test_repeated_independent_observations_offer_remediation_not_causal_confirmation(flow):
    book, case = prepare(flow, original=None)
    propose(book, case)
    for item, answer in [(SECOND, "42"), (ITEM, "90")]:
        q = ask(book, case, item)
        a = respond(book, case, q["event_id"], answer)
        state = score(flow, book, case, a["event_id"], item, answer)
    assert state["assessment"]["status"] == "pattern_observed"
    assert state["next_action"]["kind"] == "remediation"
    assert state["assessment"]["causal_status"] == "unconfirmed"
    assert all(h["status"] == "candidate" for h in state["hypotheses"])
    assert book.get(case, CHILD)["mastery_gate"]["missing"] == ["near_transfer", "delayed_48h"]


@pytest.mark.parametrize("mutation", [
    "UPDATE learning_evidence SET confidence=.1",
    "UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-2'",
    "UPDATE diagnostic_item SET source_ref='unreviewed' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-2'",
    "UPDATE diagnostic_attempt SET submitted_answer='wrong'",
    "UPDATE learning_evidence SET independent=0",
    "UPDATE learning_evidence SET hint_level=1",
    "UPDATE mistake_case SET submitted_answer='revised original answer'",
])
def test_current_projection_invalidates_stale_support_without_erasing_events(flow, mutation):
    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    state = score(flow, book, case, a["event_id"])
    assert state["assessment"]["support"]
    with db.connect() as c:
        history = [tuple(r) for r in c.execute("SELECT * FROM mistake_diagnosis_event ORDER BY version")]
        c.execute(mutation)
        c.commit()
    current = MistakeBookService(Database(db.path)).diagnosis_state(case, CHILD)
    assert current["version"] == state["version"]
    assert current["projection_version"] != state["projection_version"]
    assert current["assessment"]["support"] == []
    assert current["assessment"]["counterevidence"] == []
    with db.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM mistake_diagnosis_event ORDER BY version")] == history


def test_answer_revision_and_retraction_have_transitive_effect(flow):
    book, case = prepare(flow)
    p = propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    score(flow, book, case, a["event_id"])
    revised = respond(book, case, q["event_id"], "90", supersedes_event_id=a["event_id"])
    assert revised["assessment"]["support"] == []
    assert revised["next_action"]["kind"] == "await_score"
    assert next(e for e in revised["history"] if e["event_id"] == a["event_id"])["active"] is False
    state = retract(book, case, p["event_id"])
    assert not state["probes"]
    assert state["next_action"]["kind"] == "propose_hypotheses"
    assert len(state["history"]) == 6


def test_reference_score_and_self_report_remain_insufficient(flow):
    book, case = prepare(flow, node="node-p4-english-phonics-discrimination", original="diag-p4-english-phonics-3")
    propose(book, case)
    q = ask(book, case, "diag-p4-english-phonics-1")
    a = respond(book, case, q["event_id"], "bag")
    state = score(flow, book, case, a["event_id"], "diag-p4-english-phonics-1", "bag")
    assert state["assessment"]["support"] == []
    assert state["probes"][0]["evidence"][0]["eligible"] is False
    assert state["assessment"]["causal_status"] == "unconfirmed"


def test_same_question_alias_cannot_be_replication_even_after_24h(flow, monkeypatch):
    book, case = prepare(flow)
    renamed = alias(flow, monkeypatch, "diagnosis-alias")
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    score(flow, book, case, a["event_id"])
    flow[3][0] += timedelta(hours=25)
    q = ask(book, case, renamed)
    a = respond(book, case, q["event_id"])
    state = score(flow, book, case, a["event_id"], renamed)
    assert state["assessment"]["status"] == "insufficient"
    assert len(state["assessment"]["support"]) == 1


def test_versions_idempotency_wrong_child_and_append_only(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    state = propose(book, case, action="same-action")
    assert propose(book, case, action="same-action")["event_id"] == state["event_id"]
    from home_study_agent.mistake_diagnosis import DiagnosisConflictError
    with pytest.raises(DiagnosisConflictError):
        book.propose_diagnosis_hypotheses(case, CHILD, [{"error_type": "unknown", "stance": "unknown", "explanation": "different"}], 1, "same-action")
    with pytest.raises(DiagnosisConflictError):
        book.start_diagnosis_probe(case, CHILD, SECOND, {}, 0, "stale")
    with pytest.raises(ValueError):
        book.diagnosis_state(case, "child-junior-3")
    with db.connect() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE mistake_diagnosis_event SET version=9")
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_evidence_cannot_bind_other_answer_or_predate_question(flow):
    _, learning, sid, _ = flow
    old = learning.submit_diagnostic(sid, SECOND, "42", "old")
    flow[3][0] += timedelta(hours=25)
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"], "90")
    state = book.attach_diagnosis_evidence(case, CHILD, a["event_id"], old["evidence_id"], a["version"], "attach-old")
    assert not state["assessment"]["support"]
    assert state["probes"][0]["evidence"][0]["eligible"] is False


def test_revising_hypotheses_does_not_reuse_old_predictions(flow):
    book, case = prepare(flow)
    p = propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    score(flow, book, case, a["event_id"])
    state = propose(book, case, supersedes_event_id=p["event_id"])
    assert not state["assessment"]["support"]
    assert not state["probes"]
    assert len(state["history"]) == 5


def test_self_report_scored_as_wrong_is_not_a_differential_observation(flow):
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"], "I was just careless")
    state = score(flow, book, case, a["event_id"], answer="I was just careless")
    assert not state["assessment"]["support"]
    assert "answer_not_observable" in state["assessment"]["insufficiency"]


def test_reliable_wrong_numeric_response_supplies_counterevidence(flow):
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"], "90")
    state = score(flow, book, case, a["event_id"], answer="90")
    assert state["assessment"]["support"][0]["observed"] == "incorrect"
    assert state["assessment"]["counterevidence"][0]["observed"] == "incorrect"
    assert state["assessment"]["causal_status"] == "unconfirmed"


def test_known_receipt_with_same_timestamp_cannot_be_registered_after_scoring(flow):
    _, learning, sid, _ = flow
    old = learning.submit_diagnostic(sid, SECOND, "42", "already-scored")
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    state = book.attach_diagnosis_evidence(case, CHILD, a["event_id"], old["evidence_id"], a["version"], "late-registration")
    assert not state["assessment"]["support"]
    assert "evidence_predates_probe" in state["assessment"]["insufficiency"]


def test_stale_unanswered_probe_offers_recovery_not_old_question(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    ask(book, case)
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id=?", (SECOND,))
        c.commit()
    state = book.diagnosis_state(case, CHILD)
    assert state["next_action"]["kind"] == "additional_probe"
    assert state["probes"][0]["binding_status"] == "question_source_changed"


def test_unreviewed_source_is_not_exposed_as_a_probe(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET source_ref='parent_approved_variant:other-child' WHERE diagnostic_item_id=?", (SECOND,))
        c.commit()
    with pytest.raises(ValueError):
        ask(book, case)


def test_original_revision_requires_revised_hypotheses_before_new_question(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    p = propose(book, case)
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET submitted_answer='new original' WHERE mistake_id=?", (case,))
        c.commit()
    state = book.diagnosis_state(case, CHILD)
    assert state["next_action"]["kind"] == "revise_hypotheses"
    with pytest.raises(ValueError):
        ask(book, case)
    propose(book, case, supersedes_event_id=p["event_id"])
    assert ask(book, case)["next_action"]["kind"] == "answer_probe"


def test_actual_learning_evidence_withdrawal_removes_support(flow):
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    state = score(flow, book, case, a["event_id"])
    evidence_id = state["assessment"]["support"][0]["evidence_id"]
    withdrawal = book.capture(child_id=CHILD, knowledge_node_id="node-p4-math-arithmetic", subject="Math",
        title="Withdrawal", prompt_summary="Withdraw this observation", submitted_answer="wrong", source_type="manual",
        source_ref=None, confidence=1, client_action_id="withdraw-diagnosis", source_evidence_id=evidence_id)
    book.confirm(withdrawal["mistake_id"], CHILD, "exclude", "withdraw-confirm")
    state = book.diagnosis_state(case, CHILD)
    assert not state["assessment"]["support"]
    assert "withdrawn" in state["assessment"]["insufficiency"]


def test_evidence_retraction_and_failed_write_are_durable(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    state = score(flow, book, case, a["event_id"])
    state = retract(book, case, state["event_id"])
    assert not state["assessment"]["support"]
    assert state["next_action"]["kind"] == "await_score"
    with pytest.raises(ValueError):
        book.answer_diagnosis_probe(case, CHILD, q["event_id"], "", state["version"], "bad-input")
    restarted = MistakeBookService(Database(db.path))
    assert restarted.diagnosis_state(case, CHILD)["version"] == state["version"]


def test_non_discriminating_predictions_never_form_pattern(flow):
    book, case = prepare(flow)
    state = propose(book, case)
    predictions = {h["hypothesis_id"]: "correct" for h in state["hypotheses"] if h["stance"] == "candidate"}
    q = book.start_diagnosis_probe(case, CHILD, SECOND, predictions, state["version"], "same-predictions")
    a = respond(book, case, q["event_id"])
    state = score(flow, book, case, a["event_id"])
    assert "non_discriminating_predictions" in state["assessment"]["insufficiency"]
    assert not state["assessment"]["support"]


def test_question_and_history_do_not_leak_answer_key(flow):
    book, case = prepare(flow)
    propose(book, case)
    state = ask(book, case)
    assert "expected_answer_json" not in json.dumps(state)
    assert "先算6×3=18" not in json.dumps(state, ensure_ascii=False)


def test_two_writers_on_same_version_cannot_lose_an_answer(flow):
    from concurrent.futures import ThreadPoolExecutor
    from home_study_agent.mistake_diagnosis import DiagnosisConflictError

    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)

    def write(answer):
        try:
            service = MistakeBookService(Database(db.path))
            service.answer_diagnosis_probe(case, CHILD, q["event_id"], answer, q["version"], answer)
            return "saved"
        except DiagnosisConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ["42", "90"])) == ["conflict", "saved"]
    assert book.diagnosis_state(case, CHILD)["version"] == 3


def test_unknown_only_probe_persists_but_cannot_claim_discrimination(flow):
    book, case = prepare(flow)
    p = book.propose_diagnosis_hypotheses(case, CHILD,
        [{"error_type": "unknown", "stance": "unknown", "explanation": "I do not know"}], 0, "unknown")
    q = book.start_diagnosis_probe(case, CHILD, SECOND, {}, p["version"], "unknown-probe")
    a = respond(book, case, q["event_id"])
    state = score(flow, book, case, a["event_id"])
    assert not state["assessment"]["support"]
    assert "non_discriminating_predictions" in state["assessment"]["insufficiency"]
    assert state["hypotheses"][0]["stance"] == "unknown"


def test_real_generated_source_scope_and_revoked_review(flow):
    from home_study_agent.variant_supply import VariantSupplyService

    class NoCloud:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError("Cloud calls forbidden")

    db, _, _, _ = flow
    book, case = prepare(flow)
    supply = VariantSupplyService(db, NoCloud())
    supply.enqueue_local(case, CHILD, "diagnosis-local")
    assert supply.process_local_pending()["jobs"][0]["status"] == "completed"
    with db.connect() as c:
        candidate = c.execute("SELECT candidate_id FROM variant_candidate WHERE mistake_id=? AND stage='near_transfer'", (case,)).fetchone()[0]
    approved = supply.qualify_local(candidate, CHILD, "diagnosis-qualified")
    item_id = approved["diagnostic_item_id"]
    with db.connect() as c:
        answer = json.loads(c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()[0])["exact_answers"][0]
    propose(book, case)
    q = ask(book, case, item_id)
    a = respond(book, case, q["event_id"], answer)
    state = score(flow, book, case, a["event_id"], item_id, answer)
    assert state["assessment"]["support"]
    _, other = prepare(flow)
    propose(book, other)
    with pytest.raises(ValueError):
        ask(book, other, item_id)
    with db.connect() as c:
        c.execute("UPDATE variant_candidate SET child_id='child-junior-3' WHERE candidate_id=?", (candidate,))
        c.commit()
    with pytest.raises(ValueError):
        ask(book, case, item_id)
    with db.connect() as c:
        c.execute("UPDATE variant_candidate SET child_id=?,review_action_id=NULL WHERE candidate_id=?", (CHILD, candidate))
        c.commit()
    state = book.diagnosis_state(case, CHILD)
    assert not state["assessment"]["support"]
    assert "question_source_unavailable" in state["assessment"]["insufficiency"]


def test_excluded_case_retains_trace_and_allows_withdrawing_diagnosis(flow):
    book, case = prepare(flow)
    p = propose(book, case)
    book.confirm(case, CHILD, "exclude", "exclude-case")
    assert book.diagnosis_state(case, CHILD)["next_action"]["kind"] == "blocked"
    state = retract(book, case, p["event_id"])
    assert len(state["history"]) == 2
    assert not state["hypotheses"]


def test_wrong_child_evidence_and_foreign_event_references_are_rejected(flow):
    db, learning, sid, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    receipt = learning.submit_diagnostic(sid, SECOND, "42", "foreign-score")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET child_id='child-junior-3' WHERE evidence_id=?", (receipt["evidence_id"],))
        c.commit()
    with pytest.raises(ValueError):
        book.attach_diagnosis_evidence(case, CHILD, a["event_id"], receipt["evidence_id"], a["version"], "bad-owner")
    _, other = prepare(flow)
    propose(book, other)
    with pytest.raises(ValueError):
        retract(book, other, q["event_id"])
    assert book.diagnosis_state(case, CHILD)["version"] == 3


def test_reading_empty_stream_does_not_install_schema(flow):
    db, _, _, _ = flow
    book, case = prepare(flow)
    assert book.diagnosis_state(case, CHILD)["version"] == 0
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='mistake_diagnosis_event'").fetchone()


def test_counterevidence_and_ambiguous_patterns_do_not_select_a_certain_cause(flow):
    book, case = prepare(flow, original=None)
    p = book.propose_diagnosis_hypotheses(case, CHILD, [
        {"error_type": "concept_gap", "stance": "candidate", "explanation": "Possible rule issue"},
        {"error_type": "reading_gap", "stance": "candidate", "explanation": "Possible interpretation issue"},
        {"error_type": "incidental", "stance": "candidate", "explanation": "Alternative"},
    ], 0, "ambiguous")
    for item, answer in [(SECOND, "90"), (ITEM, "42")]:
        state = book.diagnosis_state(case, CHILD)
        predictions = {h["hypothesis_id"]: "correct" if h["error_type"] == "incidental" else "incorrect"
                       for h in state["hypotheses"] if h["stance"] == "candidate"}
        q = book.start_diagnosis_probe(case, CHILD, item, predictions, state["version"], str(uuid4()))
        a = respond(book, case, q["event_id"], answer)
        state = score(flow, book, case, a["event_id"], item, answer)
    assert "ambiguous_hypotheses" in state["assessment"]["insufficiency"]
    assert state["next_action"]["kind"] == "additional_probe"
    assert {h["result"] for h in state["hypotheses"]} == {"supported_pattern", "counterevidence_pattern", "insufficient"}
    assert all(h["status"] == "candidate" for h in state["hypotheses"])


def test_mixed_observations_keep_hypotheses_open(flow):
    book, case = prepare(flow, original=None)
    propose(book, case)
    for item, answer in [(SECOND, "42"), (ITEM, "42")]:
        q = ask(book, case, item)
        a = respond(book, case, q["event_id"], answer)
        state = score(flow, book, case, a["event_id"], item, answer)
    assert {h["result"] for h in state["hypotheses"] if h["stance"] == "candidate"} == {"mixed"}
    assert state["assessment"]["status"] == "insufficient"
    assert state["next_action"]["kind"] == "additional_probe"


def test_remediation_completed_never_supplies_a_diagnosis_observation(flow):
    book, case = prepare(flow)
    p = propose(book, case)
    state = book.diagnosis_state(case, CHILD)
    assert state["assessment"]["support"] == []
    assert state["assessment"]["counterevidence"] == []
    assert state["version"] == p["version"]


def test_probe_answer_saved_before_score_and_failure_recovery(flow, monkeypatch):
    from home_study_agent.mistake_diagnosis import MistakeDiagnosisService

    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    q = ask(book, case)
    original_project = MistakeDiagnosisService.project

    def fail_after_insert(self, c, mistake_id, child_id):
        if c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event WHERE kind='answer'").fetchone()[0]:
            raise RuntimeError("Synthetic interrupted response")
        return original_project(self, c, mistake_id, child_id)

    monkeypatch.setattr(MistakeDiagnosisService, "project", fail_after_insert)
    with pytest.raises(RuntimeError):
        respond(book, case, q["event_id"])
    monkeypatch.setattr(MistakeDiagnosisService, "project", original_project)
    restarted = MistakeBookService(Database(db.path))
    state = restarted.diagnosis_state(case, CHILD)
    assert state["version"] == 2
    assert state["next_action"]["kind"] == "answer_probe"
    assert respond(restarted, case, q["event_id"])["next_action"]["kind"] == "await_score"


def test_completed_remediation_after_pattern_advances_to_gate_not_repeated_advice(flow):
    db, _, _, _ = flow
    book = MistakeBookService(db)
    case = book.capture(child_id=CHILD, knowledge_node_id="node-p4-math-arithmetic", subject="Math",
        title="Synthetic", prompt_summary="Original task", submitted_answer="wrong", source_type="manual",
        source_ref=None, confidence=1, client_action_id="full-diagnosis-flow")["mistake_id"]
    book.confirm(case, CHILD, "confirm", "full-flow-confirm")
    book.diagnose(case, CHILD, "unknown", "Cause not established", .99, "full-flow-diagnose")
    propose(book, case)
    for item, answer in [(SECOND, "42"), (ITEM, "90")]:
        q = ask(book, case, item)
        a = respond(book, case, q["event_id"], answer)
        state = score(flow, book, case, a["event_id"], item, answer)
    assert state["next_action"]["kind"] == "remediation"
    book.add_remediation(case, CHILD, "execution_check", "still_stuck", "Not clear yet", "stuck-after-pattern")
    assert book.diagnosis_state(case, CHILD)["next_action"]["kind"] == "remediation"
    book.add_remediation(case, CHILD, "representation_change", "completed", "Tried a different representation", "remediation-after-pattern")
    state = MistakeBookService(Database(db.path)).diagnosis_state(case, CHILD)
    assert state["next_action"]["kind"] == "mastery_verification"
    assert state["next_action"]["remediation_step_id"]
    assert state["assessment"]["causal_status"] == "unconfirmed"
    assert all(h["status"] == "candidate" for h in state["hypotheses"])
    assert book.get(case, CHILD)["mastery_gate"]["missing"] == ["near_transfer", "delayed_48h"]


def test_privacy_hard_delete_is_allowed_and_case_fk_cascades(flow):
    from home_study_agent.mistake_diagnosis import MistakeDiagnosisService

    db, _, _, _ = flow
    book, case = prepare(flow)
    p = propose(book, case)
    q = ask(book, case)
    respond(book, case, q["event_id"])
    _, other = prepare(flow)
    propose(book, other)
    with db.connect() as c:
        fks = c.execute("PRAGMA foreign_key_list(mistake_diagnosis_event)").fetchall()
        assert len(fks) == 4
        assert all(r["on_delete"] == "CASCADE" for r in fks)
        c.execute("DELETE FROM mistake_diagnosis_event WHERE mistake_id=?", (case,))
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event WHERE mistake_id=?", (other,)).fetchone()[0] == 1
        c.execute("DELETE FROM mistake_case WHERE mistake_id=?", (other,))
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event").fetchone()[0] == 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        c.commit()
    assert not hasattr(MistakeDiagnosisService(db), "delete")


def legacy_diagnosis_schema(c):
    c.execute("DROP TRIGGER IF EXISTS mistake_diagnosis_event_no_delete")
    c.execute("DROP TRIGGER IF EXISTS mistake_diagnosis_event_no_update")
    c.execute("DROP TABLE mistake_diagnosis_event")
    c.execute("""CREATE TABLE mistake_diagnosis_event (
        event_id TEXT PRIMARY KEY, mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id),
        child_id TEXT NOT NULL REFERENCES child_profile(child_id), version INTEGER NOT NULL,
        schema_version INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        request_json TEXT NOT NULL, reference_event_id TEXT REFERENCES mistake_diagnosis_event(event_id),
        supersedes_event_id TEXT REFERENCES mistake_diagnosis_event(event_id),
        client_action_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, UNIQUE(mistake_id,version))""")


def test_legacy_schema_migrates_without_losing_events_or_disabling_foreign_keys(flow):
    from home_study_agent.mistake_diagnosis import MistakeDiagnosisService

    db, _, _, _ = flow
    book, case = prepare(flow)
    p = propose(book, case)
    q = ask(book, case)
    a = respond(book, case, q["event_id"])
    score(flow, book, case, a["event_id"])
    with db.connect() as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM mistake_diagnosis_event ORDER BY version")]
        legacy_diagnosis_schema(c)
        c.executemany("INSERT INTO mistake_diagnosis_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", before)
        c.execute("""CREATE TRIGGER mistake_diagnosis_event_no_delete BEFORE DELETE ON mistake_diagnosis_event
            BEGIN SELECT RAISE(ABORT,'legacy append-only'); END""")
        c.commit()
    service = MistakeDiagnosisService(db)
    service.ensure_schema()
    service.ensure_schema()
    with db.connect() as c:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert [tuple(r) for r in c.execute("SELECT * FROM mistake_diagnosis_event ORDER BY version")] == before
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert all(r["on_delete"] == "CASCADE" for r in c.execute("PRAGMA foreign_key_list(mistake_diagnosis_event)"))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE mistake_diagnosis_event SET payload_json='{}'")
        c.execute("DELETE FROM mistake_diagnosis_event WHERE child_id=?", (CHILD,))
        c.commit()
    assert book.diagnosis_state(case, CHILD)["version"] == 0


def test_schema_migration_rolls_back_with_privacy_transaction(flow):
    from home_study_agent.mistake_diagnosis import MistakeDiagnosisService

    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    with db.connect() as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM mistake_diagnosis_event")]
        legacy_diagnosis_schema(c)
        c.executemany("INSERT INTO mistake_diagnosis_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", before)
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        MistakeDiagnosisService(db).ensure_schema(c)
        c.execute("DELETE FROM mistake_diagnosis_event WHERE child_id=?", (CHILD,))
        c.rollback()
        assert [tuple(r) for r in c.execute("SELECT * FROM mistake_diagnosis_event")] == before
        assert all(r["on_delete"] == "NO ACTION" for r in c.execute("PRAGMA foreign_key_list(mistake_diagnosis_event)"))
