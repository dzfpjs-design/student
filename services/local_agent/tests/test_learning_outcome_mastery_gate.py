import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from home_study_agent import learning_outcome_evaluator as module
from home_study_agent import learning_state
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from home_study_agent.model_gateway import ModelResult
from home_study_agent.repository import StudyRepository
from test_mistake_gate_reliability import flow, CHILD, ITEM, SECOND


CRITERIA = ["Return the correct integer", "Answer without hints"]


class Gateway:
    coverage = None

    async def complete(self, request):
        context = json.loads(request.messages[1]["content"])
        return ModelResult(content=json.dumps({
            "understanding": "independent", "confidence": .95,
            "evidence_summary": "Correct answer", "spoken_feedback": "Answer recorded",
            "next_check": "Try another question",
            "criterion_evidence": self.coverage if self.coverage is not None else [
                {"criterion": criterion, "response_quote": context["child_response"]}
                for criterion in CRITERIA],
        }), provider="test", model="test", latency_ms=1)


@pytest.fixture
def gate(flow, monkeypatch):
    db, learning, sid, clock = flow
    monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS,
        ("gate-third", "node-p4-math-arithmetic", "Calculate 6*(3+4).", "text",
         {"keywords": ["42"], "local_rule": "local_exact_v1", "exact_answers": ["42"]},
         "Parentheses first.", 1)))
    learning.seed_catalog()
    clock[0] -= timedelta(days=10)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", Clock)
    gateway = Gateway()
    evaluator = LearningOutcomeEvaluator(db, StudyRepository(db), gateway,
        SimpleNamespace(model_provider="test", model_name="test"))
    objective_id = str(uuid4())
    with db.connect() as c:
        c.execute("""INSERT INTO agent_objective VALUES (?,?,?,'learning',?,?,
            'diagnose','active',?,?,NULL,NULL)""",
            (objective_id, CHILD, sid, "Arithmetic", json.dumps(CRITERIA), clock[0].isoformat(), clock[0].isoformat()))
        c.commit()

    def run(item=ITEM, answer="90", phase="diagnose", days=0, hint=0, receipt=True, adapter=None, subject="数学", local=False):
        clock[0] += timedelta(days=days)
        evidence = learning.submit_diagnostic(sid, item, answer, str(uuid4()), hint_level=hint) if receipt else None
        prepared = adapter(evidence) if adapter else None
        if local and prepared is None:
            from home_study_agent.learning_rubric import RubricEvidenceService
            prepared = RubricEvidenceService(db).diagnostic_candidate_input(CHILD, evidence["evidence_id"])
        turn, plan, step = str(uuid4()), str(uuid4()), str(uuid4())
        now = clock[0].isoformat()
        with db.connect() as c:
            prompt = c.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?", (item,)).fetchone()[0]
            c.execute("UPDATE agent_objective SET status='active',phase=? WHERE objective_id=?", (phase, objective_id))
            c.execute("""INSERT INTO agent_turn (agent_turn_id,child_id,session_id,transcript,intent,
                response_text,action_json,orchestrator_version,created_at,client_action_id)
                VALUES (?,?,?,?,'learning.evaluate_response','','{}','test',?,?)""", (turn, CHILD, sid, answer, now, turn))
            if local:
                c.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?",
                    (json.dumps({"local_only": True, "cloud_transmission_allowed": False}), turn))
            c.execute("""INSERT INTO agent_plan VALUES (?,?,?,?,?,'test','{}','{}','planned',?,?)""",
                      (plan, objective_id, CHILD, sid, turn, now, now))
            c.execute("""INSERT INTO agent_step VALUES (?,?,0,'learning.evaluate_response','1',
                'local_model','medium',?,'[]','planned',?,NULL,NULL,?,?)""",
                (step, plan, json.dumps(prepared["arguments"] if prepared else
                    {"response": answer, "subject": subject, "question_asked": prompt}), step, now, now))
            c.commit()
        result = evaluator.prepare_local_evaluation(turn) if local else asyncio.run(evaluator.evaluate(turn))
        return result, evidence, turn

    return SimpleNamespace(db=db, evaluator=evaluator, run=run, gateway=gateway,
                           clock=clock, objective=objective_id, learning=learning, sid=sid)


def test_legacy_rubric_alone_cannot_upgrade_even_with_forged_phase(gate):
    result, _, _ = gate.run(phase="retention_check", receipt=False)
    assert result["evaluation"]["outcome_level"] == "understood_now"
    assert result["evaluation"]["mastery_gate"]["status"] == "unknown"
    assert "已掌握" not in result["response_text"]


@pytest.mark.parametrize("phase", ["transfer_check", "retention_check"])
def test_same_question_phase_change_is_not_a_new_outcome(gate, phase):
    gate.run()
    result, _, _ = gate.run(phase=phase)
    assert result["evaluation"]["outcome_level"] not in {"transferred", "retained"}


def test_distinct_question_on_same_day_cannot_skip_delay(gate):
    gate.run()
    result, _, _ = gate.run(SECOND, "42", "transfer_check")
    assert result["evaluation"]["outcome_level"] == "independent"
    assert "transfer_delay_not_met" in result["evaluation"]["mastery_gate"]["missing"]


@pytest.mark.parametrize("hint", [1, 5])
def test_independent_model_label_cannot_override_recorded_hints(gate, hint):
    result, _, _ = gate.run(hint=hint)
    assert result["evaluation"]["outcome_level"] == "understood_now"


@pytest.mark.parametrize("coverage", [[], [{"criterion": "Invented", "response_quote": "90"}],
    [{"criterion": CRITERIA[0], "response_quote": "not the answer"}],
    [{"criterion": CRITERIA[0], "response_quote": "90"}]])
def test_criteria_require_complete_traceable_coverage(gate, coverage):
    gate.gateway.coverage = coverage
    result, _, _ = gate.run()
    assert result["evaluation"]["outcome_level"] == "understood_now"


def test_different_question_after_24h_transfers_but_early_retention_does_not(gate):
    first, _, _ = gate.run()
    second, _, _ = gate.run(SECOND, "42", "transfer_check", days=1)
    assert first["evaluation"]["outcome_level"] == "independent"
    assert second["evaluation"]["outcome_level"] == "transferred"
    with gate.db.connect() as c:
        c.execute("UPDATE agent_commitment SET due_at=?", (gate.clock[0].isoformat(),))
        c.commit()
    third, _, _ = gate.run(ITEM, "90", "retention_check", days=1)
    assert third["evaluation"]["outcome_level"] != "retained"


def third_item(gate):
    return "gate-third", "42"


def test_retention_uses_distinct_identity_actual_delay_and_survives_restart(gate):
    gate.run()
    gate.run(SECOND, "42", "transfer_check", days=1)
    item, answer = third_item(gate)
    result, _, turn = gate.run(item, answer, "retention_check", days=3)
    assert result["evaluation"]["outcome_level"] == "retained"
    restarted = LearningOutcomeEvaluator(gate.db, StudyRepository(gate.db), gate.gateway, gate.evaluator.settings)
    assert restarted.execution_status(turn)["result"] == result
    assert restarted.current_mastery_gate(gate.objective)["outcome_level"] == "retained"


@pytest.mark.parametrize("change", ["review", "confidence", "hint", "reference", "criteria", "model", "answer"])
def test_replay_recomputes_transitive_basis_without_rewriting_history(gate, change):
    first, evidence, _ = gate.run()
    second, _, turn = gate.run(SECOND, "42", "transfer_check", days=1)
    assert second["evaluation"]["outcome_level"] == "transferred"
    if change == "review":
        LearningOutcomeProjection(gate.db).review_outcome(CHILD, first["evaluation"]["outcome_id"],
            decision="revoke", reason="Source corrected", client_action_id=str(uuid4()))
    else:
        with gate.db.connect() as c:
            if change == "confidence":
                c.execute("UPDATE learning_evidence SET confidence=.1 WHERE evidence_id=?", (evidence["evidence_id"],))
            elif change == "hint":
                c.execute("UPDATE learning_evidence SET hint_level=1 WHERE evidence_id=?", (evidence["evidence_id"],))
            elif change == "reference":
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
            elif change == "model":
                c.execute("UPDATE model_call SET verification_status='rejected'")
            elif change == "answer":
                c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','changed')")
            else:
                c.execute("UPDATE agent_objective SET success_criteria_json=? WHERE objective_id=?",
                          (json.dumps(["New objective criterion"]), gate.objective))
            c.commit()
    current = gate.evaluator.current_mastery_gate(gate.objective)
    assert current["outcome_level"] not in {"transferred", "retained"}
    replay = asyncio.run(gate.evaluator.evaluate(turn))
    assert replay["evaluation"]["outcome_level"] not in {"transferred", "retained"}
    with gate.db.connect() as c:
        assert c.execute("SELECT outcome_level FROM agent_outcome WHERE outcome_id=?",
                         (second["evaluation"]["outcome_id"],)).fetchone()[0] == "transferred"


def test_same_content_alias_after_24h_is_still_not_transfer(gate, monkeypatch):
    seed = next(row for row in learning_state.ITEMS if row[0] == ITEM)
    monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS, ("gate-alias", *seed[1:])))
    gate.learning.seed_catalog()
    gate.run()
    result, _, _ = gate.run("gate-alias", "90", "transfer_check", days=1)
    assert result["evaluation"]["outcome_level"] != "transferred"


def test_retention_cannot_reuse_any_question_in_its_basis(gate):
    gate.run()
    gate.run(SECOND, "42", "transfer_check", days=1)
    result, _, _ = gate.run(ITEM, "90", "retention_check", days=3)
    assert result["evaluation"]["outcome_level"] != "retained"


def test_partial_criteria_can_accumulate_but_cannot_skip_fresh_transfer_coverage(gate):
    gate.gateway.coverage = [{"criterion": CRITERIA[0], "response_quote": "90"}]
    first, _, _ = gate.run()
    gate.gateway.coverage = [{"criterion": CRITERIA[1], "response_quote": "42"}]
    second, _, _ = gate.run(SECOND, "42", "transfer_check", days=1)
    assert first["evaluation"]["outcome_level"] == "understood_now"
    assert second["evaluation"]["outcome_level"] == "independent"
    assert second["evaluation"]["mastery_gate"]["basis_outcome_ids"] == [first["evaluation"]["outcome_id"]]


def test_old_completed_envelope_is_unknown_not_a_retention_authority(gate):
    result, _, turn = gate.run(receipt=False)
    with gate.db.connect() as c:
        c.execute("DELETE FROM learning_evaluation_execution")
        c.execute("DELETE FROM agent_outcome")
        legacy = {**result["evaluation"], "outcome_level": "retained", "objective_phase": "completed"}
        legacy.pop("mastery_gate")
        c.execute("UPDATE agent_step SET result_json=?", (json.dumps(legacy),))
        c.commit()
    status = gate.evaluator.execution_status(turn)
    assert status["result"]["evaluation"]["outcome_level"] == "understood_now"
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] == "understood_now"


def test_retained_replay_after_basis_revocation_never_completes_a_task(gate, monkeypatch):
    first, _, _ = gate.run()
    gate.run(SECOND, "42", "transfer_check", days=1)
    result, _, turn = gate.run(*third_item(gate), "retention_check", days=3)
    assert result["evaluation"]["outcome_level"] == "retained"
    LearningOutcomeProjection(gate.db).review_outcome(CHILD, first["evaluation"]["outcome_id"],
        decision="revoke", reason="Basis withdrawn", client_action_id=str(uuid4()))
    def forbidden(*args, **kwargs):
        pytest.fail("Revoked retained result must not complete a task")
    monkeypatch.setattr(gate.evaluator, "_complete_retention_task", forbidden)
    before = gate.evaluator.current_mastery_gate(gate.objective)
    assert before["outcome_level"] != "retained"
    assert asyncio.run(gate.evaluator.evaluate(turn))["evaluation"]["outcome_level"] != "retained"
    assert gate.evaluator.recover_ready(turn)["evaluation"]["outcome_level"] != "retained"
    assert gate.evaluator.current_mastery_gate(gate.objective) == before


def test_read_only_gate_does_not_repair_or_initialize_tables(gate, monkeypatch):
    gate.run()
    original = gate.db.connect
    from contextlib import contextmanager

    @contextmanager
    def readonly():
        with original() as c:
            c.execute("PRAGMA query_only=ON")
            yield c

    monkeypatch.setattr(gate.db, "connect", readonly)
    assert gate.evaluator.current_mastery_gate(gate.objective)["outcome_level"] == "independent"


def test_recovery_revalidates_evidence_changed_while_model_was_running(gate, monkeypatch):
    original = gate.evaluator._persist

    def crash(*args, **kwargs):
        raise RuntimeError("Simulated process failure before local commit")

    monkeypatch.setattr(gate.evaluator, "_persist", crash)
    with pytest.raises(module.LearningEvaluationUnknownError):
        gate.run()
    with gate.db.connect() as c:
        c.execute("UPDATE learning_evidence SET hint_level=1")
        turn = c.execute("SELECT agent_turn_id FROM agent_plan").fetchone()[0]
        c.commit()
    monkeypatch.setattr(gate.evaluator, "_persist", original)
    result = gate.evaluator.recover_ready(turn)
    assert result["evaluation"]["outcome_level"] == "understood_now"
    assert "response_evidence_not_current_independent_correct" in result["evaluation"]["mastery_gate"]["missing"]


def test_cross_child_receipt_does_not_bind_to_this_answer(gate, monkeypatch):
    original = gate.evaluator._bind_response

    def other_child(c, record):
        return original(c, {**record, "child_id": "another-child"})

    monkeypatch.setattr(gate.evaluator, "_bind_response", other_child)
    result, _, _ = gate.run()
    assert result["evaluation"]["mastery_gate"]["status"] == "unknown"


def test_model_recording_time_does_not_replace_actual_transfer_time(gate):
    gate.run()
    _, evidence, _ = gate.run(SECOND, "42", "transfer_check", days=1)
    with gate.db.connect() as c:
        c.execute("UPDATE agent_outcome SET created_at=?", ((gate.clock[0] - timedelta(days=100)).isoformat(),))
        c.commit()
    third, _, _ = gate.run(*third_item(gate), "retention_check", days=2.99)
    assert third["evaluation"]["outcome_level"] != "retained"
    assert evidence["evidence_id"]
