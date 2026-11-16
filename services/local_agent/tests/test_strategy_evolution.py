from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository, utc_now
from home_study_agent.strategy_evolution import (
    CANDIDATE_CONFIG,
    CANDIDATE_VERSION,
    INDEPENDENT_VERSION,
    InvalidStrategyTransitionError,
    STUCK_VERSION,
    StrategyEvidenceInsufficientError,
    StrategyEvolutionService,
)


CHILD = "child-junior-3"
OTHER = "child-primary-4"


@pytest.fixture
def service(tmp_path):
    database = Database(tmp_path / "strategy-test.db")
    database.initialize()
    StudyRepository(database).seed_family()
    with database.connect() as connection:
        for child in (CHILD, OTHER):
            connection.execute(
                """INSERT INTO daily_session(session_id, child_id, status, state,
                started_at, last_checkpoint_at, client_action_id)
                VALUES (?, ?, 'completed', 'study', ?, ?, ?)""",
                (child, child, utc_now(), utc_now(), child),
            )
        connection.commit()
    return StrategyEvolutionService(database)


def turn(service, *, child=CHILD, level=1, safety="passed", context=None, created_at=None):
    turn_id = str(uuid4())
    with service.database.connect() as connection:
        connection.execute(
            """INSERT INTO tutor_turn(turn_id, session_id, child_id, subject, question,
            answer, status, hint_level, safety_status, created_at, client_action_id)
            VALUES (?, ?, ?, 'math', 'fixture question', 'fixture hint', 'completed', ?, ?, ?, ?)""",
            (turn_id, child, child, level, safety, created_at or utc_now(), turn_id),
        )
        connection.commit()
    if context:
        service.record_tutor_turn(turn_id, child, context, safety)
    return turn_id


def feedback(service, turn_id, outcome, *, child=CHILD, notify=True):
    result = StudyRepository(service.database).save_tutor_feedback(
        turn_id, "fixture response", outcome, str(uuid4())
    )
    if notify:
        service.record_tutor_feedback(turn_id, outcome)
    return result


def baseline(service, outcomes=("understood",) * 3, *, level=1, child=CHILD):
    return [feedback(service, turn(service, child=child, level=level), outcome, child=child)
            for outcome in outcomes]


def shadow(service, child=CHILD):
    baseline(service, child=child)
    candidate = service.ensure_candidate(child)
    experiment_id = candidate["experiment_id"]
    service.run_replay(experiment_id)
    service.start_shadow(experiment_id)
    return experiment_id


def active(service, child=CHILD):
    experiment_id = shadow(service, child)
    context = service.tutor_context(child, 1)
    for _ in range(3):
        turn(service, context=context, child=child)
    service.activate(experiment_id)
    return experiment_id


@pytest.mark.parametrize("outcomes,level,expected,reason", [
    (("still_stuck",) * 3, 1, STUCK_VERSION, "repeated_self_report_still_stuck"),
    (("understood",) * 3, 3, INDEPENDENT_VERSION, "possible_hint_dependency_not_verified"),
    (("understood",) * 3, 1, CANDIDATE_VERSION, "insufficient_differentiating_evidence"),
])
def test_selects_explainable_bounded_candidate(service, outcomes, level, expected, reason):
    sources = baseline(service, outcomes, level=level)
    result = service.maybe_propose(CHILD)
    assert result["candidate_version"] == expected
    selection = result["candidate_config"]["selection"]
    assert selection["reason"] == reason
    assert selection["effectiveness_status"] == "unverified"
    assert {ref["source_ref"] for ref in selection["signals"]["signal_refs"]} == {
        source["feedback_id"] for source in sources
    }
    assert "fixture response" not in json.dumps(selection)
    assert service.ensure_candidate(CHILD)["experiment_id"] == result["experiment_id"]


def test_cold_start_and_unscored_answers_cannot_pass_replay(service):
    baseline(service, ("answer_attempt",) * 4)
    assert service.maybe_propose(CHILD) is None
    candidate = service.ensure_candidate(CHILD, actor="parent")
    with pytest.raises(StrategyEvidenceInsufficientError) as error:
        service.run_replay(candidate["experiment_id"])
    metrics = error.value.metrics
    assert metrics["tutor_feedback_count"] == 0
    assert metrics["tutor_understood_rate"] is None
    assert metrics["feedback_signals"]["unscored_answer_attempt_count"] == 4
    assert {ref["evidence_kind"] for ref in metrics["feedback_signals"]["signal_refs"]} == {"unscored_answer_attempt"}


def test_baseline_is_latest_per_turn_safe_recent_and_child_isolated(service):
    baseline(service, ("still_stuck",) * 3, child=OTHER)
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    for options in ({"created_at": old}, {"level": 6}, {"safety": "blocked_replaced"}):
        feedback(service, turn(service, **options), "still_stuck")
    one = turn(service, level=3)
    for _ in range(5):
        feedback(service, one, "still_stuck")
    final = feedback(service, one, "understood")
    assert service.maybe_propose(CHILD) is None
    signals = service.ensure_candidate(CHILD)["candidate_config"]["selection"]["signals"]
    assert signals["feedback_count"] == 1
    assert signals["still_stuck_count"] == 0
    assert signals["signal_refs"][0]["source_ref"] == final["feedback_id"]


def test_shadow_is_applicability_not_execution_or_gain(service):
    experiment_id = shadow(service)
    context = service.tutor_context(CHILD, 2)
    assert context["prompt_extension"] is None
    first = turn(service, context=context, level=2)
    feedback(service, first, "understood")
    for _ in range(4):
        service.record_tutor_turn(first, CHILD, context, "passed")
    with pytest.raises(StrategyEvidenceInsufficientError):
        service.activate(experiment_id)
    for _ in range(2):
        turn(service, context=context)
    result = service.activate(experiment_id)
    metrics = result["shadow_metrics"]
    assert metrics["eligible_turns"] == 3
    assert metrics["candidate_executed_turns"] == 0
    assert metrics["learning_gain"] is None
    assert metrics["feedback_attribution"] == "baseline_only"
    assert result["active_metrics"]["candidate_execution_verified"] is False
    assert result["replay_metrics"]["evaluation_kind"] == "baseline_review_not_candidate_replay"
    assert result["activation_semantics"] == "parent_authorized_trial_not_effectiveness_approval"
    service.record_tutor_turn(first, CHILD, service.tutor_context(CHILD, 2), "passed")
    assert service.get(experiment_id)["active_metrics"]["eligible_turns"] == 0


def test_active_unscored_attempts_are_not_failures_and_feedback_corrections_persist(service):
    experiment_id = active(service)
    context = service.tutor_context(CHILD, 1)
    ids = [turn(service, context=context) for _ in range(4)]
    for turn_id in ids:
        feedback(service, turn_id, "answer_attempt")
    metrics = service.get(experiment_id)["active_metrics"]
    assert metrics["candidate_executed_turns"] == 4
    assert metrics["self_report_count"] == 0
    assert metrics["understood_rate"] is None
    assert service.get(experiment_id)["status"] == "active"
    feedback(service, ids[0], "still_stuck")
    latest = feedback(service, ids[0], "understood")
    service.record_tutor_feedback(ids[0], "still_stuck")
    restarted = StrategyEvolutionService(Database(service.database.path))
    metrics = restarted.get(experiment_id)["active_metrics"]
    assert metrics["feedback_count"] == 4
    assert metrics["self_report_count"] == 1
    assert metrics["understood_rate"] == 1.0
    assert metrics["learning_gain"] is None
    with service.database.connect() as connection:
        event = connection.execute(
            "SELECT * FROM strategy_evaluation_event WHERE source_ref = ? AND event_type = 'tutor_understood'",
            (ids[0],),
        ).fetchone()
    assert json.loads(event["summary_json"])["feedback_id"] == latest["feedback_id"]
    assert event["value"] == 1.0


def test_feedback_callback_without_persisted_feedback_is_not_evidence(service):
    experiment_id = active(service)
    turn_id = turn(service, context=service.tutor_context(CHILD, 1))
    service.record_tutor_feedback(turn_id, "understood")
    assert service.get(experiment_id)["active_metrics"]["feedback_count"] == 0


@pytest.mark.parametrize("patch", [
    {"changes_safety_policy": True}, {"changes_privacy_policy": True},
    {"changes_answer_boundary": True}, {"changes_family_rules": True},
    {"max_short_sentences": "3"}, {"max_short_sentences": -1},
    {"allowed_hint_levels": [True, 2, 3]}, {"prompt_extension": "untrusted"},
])
def test_mutated_config_cannot_enter_trial_or_emit_prompt(service, patch):
    experiment_id = shadow(service)
    with service.database.connect() as connection:
        connection.execute("UPDATE strategy_experiment SET candidate_config_json = ? WHERE experiment_id = ?",
                           (json.dumps({**CANDIDATE_CONFIG, **patch}), experiment_id))
        connection.commit()
    assert service.tutor_context(CHILD, 1) is None
    with pytest.raises(StrategyEvidenceInsufficientError):
        service.activate(experiment_id)


def test_unknown_version_and_version_config_mismatch_fail_closed(service):
    experiment_id = shadow(service)
    for version in ("unknown", STUCK_VERSION):
        with service.database.connect() as connection:
            connection.execute("UPDATE strategy_experiment SET candidate_version = ? WHERE experiment_id = ?",
                               (version, experiment_id))
            connection.commit()
        assert service.tutor_context(CHILD, 1) is None


def test_forged_cross_child_missing_and_ineligible_turns_do_not_count(service):
    experiment_id = shadow(service)
    context = service.tutor_context(CHILD, 1)
    for turn_id in ("missing", turn(service, child=OTHER), turn(service, level=6)):
        service.record_tutor_turn(turn_id, CHILD, context, "passed")
    wrong_version = {**context, "strategy_version": STUCK_VERSION}
    service.record_tutor_turn(turn(service), CHILD, wrong_version, "passed")
    assert service.get(experiment_id)["shadow_metrics"]["eligible_turns"] == 0


def test_shadow_safety_replacement_blocks_activation(service):
    experiment_id = shadow(service)
    context = service.tutor_context(CHILD, 1)
    for _ in range(3):
        turn(service, context=context)
    turn(service, context=context, safety="blocked_replaced")
    with pytest.raises(StrategyEvidenceInsufficientError) as error:
        service.activate(experiment_id)
    assert error.value.metrics["policy_replacements"] == 1


@pytest.mark.parametrize("trigger", ["safety", "self_report_regression", "parent"])
def test_rollback_is_durable_and_stops_candidate_prompt(service, trigger):
    experiment_id = active(service)
    context = service.tutor_context(CHILD, 1)
    assert context["prompt_extension"]
    if trigger == "safety":
        turn(service, context=context, safety="answer_leak_replaced")
    elif trigger == "parent":
        service.rollback(experiment_id, "parent requested stop")
    else:
        for _ in range(3):
            feedback(service, turn(service, context=context), "still_stuck")
    restarted = StrategyEvolutionService(Database(service.database.path))
    result = restarted.get(experiment_id)
    assert result["status"] == "rolled_back"
    assert result["effectiveness_status"] == "unverified"
    assert restarted.tutor_context(CHILD, 1) is None
    assert result["transitions"][-1]["actor"] == ("parent" if trigger == "parent" else "system")
    assert result["phase_metrics"] == result["active_metrics"]


def test_legacy_config_json_is_compatible_and_metadata_never_enters_prompt(service):
    experiment_id = active(service)
    with service.database.connect() as connection:
        connection.execute("UPDATE strategy_experiment SET candidate_config_json = ? WHERE experiment_id = ?",
                           (json.dumps(CANDIDATE_CONFIG), experiment_id))
        connection.commit()
    original = service.tutor_context(CHILD, 1)["prompt_extension"]
    with service.database.connect() as connection:
        connection.execute("UPDATE strategy_experiment SET candidate_config_json = ? WHERE experiment_id = ?",
                           (json.dumps({**CANDIDATE_CONFIG, "selection": {"prompt": "untrusted"}}), experiment_id))
        connection.commit()
    assert service.tutor_context(CHILD, 1)["prompt_extension"] == original
    assert service.tutor_context(CHILD, 4) is None
    assert service.tutor_context(OTHER, 1) is None


def test_active_feedback_is_not_recycled_as_next_baseline(service):
    experiment_id = active(service)
    context = service.tutor_context(CHILD, 1)
    for _ in range(2):
        feedback(service, turn(service, level=3, context=context), "understood")
    service.rollback(experiment_id, "end trial")
    result = service.ensure_candidate(CHILD)
    assert result["candidate_version"] == CANDIDATE_VERSION
    assert result["candidate_config"]["selection"]["signals"]["feedback_count"] == 3


@pytest.fixture
def learning_flow(service, monkeypatch):
    from home_study_agent import learning_state, repository, strategy_evolution
    from home_study_agent.learning_state import LearningStateService

    clock = [datetime.now(timezone.utc) - timedelta(days=5)]
    for module in (learning_state, repository, strategy_evolution):
        monkeypatch.setattr(module, "utc_now", lambda: clock[0].isoformat())
    learning = LearningStateService(service.database)
    learning.seed_catalog()
    with service.database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='active' WHERE child_id=?", (OTHER,))
        connection.commit()
    return service, learning, clock


def scoped_turn(service, clock):
    turn_id = turn(service, child=OTHER, created_at=clock[0].isoformat())
    with service.database.connect() as connection:
        item = connection.execute(
            "SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'"
        ).fetchone()
        connection.execute("UPDATE tutor_turn SET question=?, subject=? WHERE turn_id=?",
                           (item["prompt"], "数学", turn_id))
        connection.commit()
    service.record_tutor_turn(turn_id, OTHER, service.tutor_context(OTHER, 1), "passed")
    clock[0] += timedelta(minutes=1)
    return turn_id


def selected_learning_trial(learning_flow, stage="active"):
    service, learning, clock = learning_flow
    source = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-1", "wrong", "selected-source")
    clock[0] += timedelta(minutes=1)
    candidate = service.maybe_propose(OTHER)
    experiment_id = candidate["experiment_id"]
    assert candidate["candidate_version"] == STUCK_VERSION
    if stage != "draft":
        service.run_replay(experiment_id)
    if stage in {"shadow", "active"}:
        service.start_shadow(experiment_id)
        for _ in range(3):
            turn(service, child=OTHER, context=service.tutor_context(OTHER, 1), created_at=clock[0].isoformat())
    if stage == "active":
        service.activate(experiment_id)
    return experiment_id, source, candidate["candidate_config"]["selection"]


@pytest.mark.parametrize("stage", ["draft", "replay_passed", "shadow", "active"])
@pytest.mark.parametrize("mutation", ["withdrawn", "retired", "score", "hint", "question_identity"])
def test_invalid_selection_basis_blocks_every_trial_stage(learning_flow, stage, mutation):
    from home_study_agent.mistake_book import MistakeBookService

    service, _, clock = learning_flow
    eid, source, original_selection = selected_learning_trial(learning_flow, stage)
    if stage == "active":
        scoped_turn(service, clock)
    if mutation == "withdrawn":
        mistakes = MistakeBookService(service.database)
        case = mistakes.capture_from_attempt(source)
        mistakes.confirm(case["mistake_id"], OTHER, "exclude", "exclude-selected-source")
    else:
        with service.database.connect() as connection:
            if mutation == "retired":
                connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
            elif mutation == "score":
                connection.execute("UPDATE diagnostic_attempt SET submitted_answer='90',correctness=1 WHERE evidence_id=?", (source["evidence_id"],))
                connection.execute("UPDATE learning_evidence SET correctness=1 WHERE evidence_id=?", (source["evidence_id"],))
            elif mutation == "hint":
                connection.execute("UPDATE learning_evidence SET hint_level=2,independent=0 WHERE evidence_id=?", (source["evidence_id"],))
            else:
                connection.execute("UPDATE diagnostic_item SET stimulus_text='changed question context' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
            connection.commit()
    if mutation in {"score", "hint"}:
        from home_study_agent.learning_obligations import project_child_mastery_evidence
        with service.database.connect() as connection:
            projection = project_child_mastery_evidence(connection, OTHER)
        assert source["evidence_id"] in {r["evidence_id"] for p in projection.values() for r in p["effective_evidence"]}
    if stage == "active":
        assert service.tutor_context(OTHER, 1) is None
    else:
        advance = {"draft": service.run_replay, "replay_passed": service.start_shadow, "shadow": service.activate}[stage]
        with pytest.raises(InvalidStrategyTransitionError, match="selection_basis_invalidated"):
            advance(eid)
    service = StrategyEvolutionService(Database(service.database.path))
    result = service.get(eid)
    assert result["status"] == "rolled_back" and result["selection_basis_invalidated"] is True
    assert result["candidate_config"]["selection"] == original_selection
    invalidations = [t for t in result["transitions"] if t["metrics"].get("reason_code") == "selection_basis_invalidated"]
    assert len(invalidations) == 1
    assert invalidations[0]["from_status"] == stage and invalidations[0]["actor"] == "system"
    assert service.tutor_context(OTHER, 1) is None
    if mutation == "retired":
        with service.database.connect() as connection:
            connection.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
            connection.commit()
        assert service.get(eid)["status"] == "rolled_back"
        assert service.tutor_context(OTHER, 1) is None
    with pytest.raises(InvalidStrategyTransitionError):
        service.activate(eid)


@pytest.mark.parametrize("change", ["none", "new_success", "window_expiry"])
def test_valid_selection_is_not_revoked_for_missing_effect_or_natural_learning_change(learning_flow, monkeypatch, change):
    from home_study_agent import strategy_evolution
    from home_study_agent.learning_feedback import build_learning_feedback

    service, learning, clock = learning_flow
    feedback_now = [datetime.now(timezone.utc)]
    if change == "window_expiry":
        clock[0] = feedback_now[0] - timedelta(days=89)
        monkeypatch.setattr(strategy_evolution, "build_learning_feedback",
                            lambda connection, child_id: build_learning_feedback(connection, child_id, now=feedback_now[0]))
    eid, source, selection = selected_learning_trial(learning_flow)
    if change == "new_success":
        learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", "42", "later-success")
    elif change == "window_expiry":
        clock[0] += timedelta(days=2)
        feedback_now[0] += timedelta(days=2)
        with service.database.connect() as connection:
            assert service._reliable_learning(connection, OTHER)["nodes"] == []
    result = service.get(eid)
    assert result["status"] == "active" and result["selection_basis_invalidated"] is False
    assert result["candidate_config"]["selection"] == selection
    assert result["active_metrics"]["learning_evidence"]["status"] == "insufficient_learning_evidence"
    assert service.tutor_context(OTHER, 1)["strategy_version"] == STUCK_VERSION


@pytest.mark.parametrize("mutation", ["feedback_owner", "turn_owner", "session_owner", "outcome", "hint", "missing", "correction"])
def test_self_report_selection_requires_original_owned_receipt_not_a_score(service, mutation):
    eid = active(service)
    original = service.get(eid)["candidate_config"]["selection"]
    fact = original["basis"]["self_reports"][0]
    assert fact["evidence_kind"] == "child_self_report" and "correctness" not in fact
    if mutation == "correction":
        feedback(service, fact["turn_id"], "still_stuck")
    else:
        with service.database.connect() as connection:
            if mutation == "feedback_owner":
                connection.execute("UPDATE tutor_feedback SET child_id=? WHERE feedback_id=?", (OTHER, fact["source_ref"]))
            elif mutation == "turn_owner":
                connection.execute("UPDATE tutor_turn SET child_id=? WHERE turn_id=?", (OTHER, fact["turn_id"]))
            elif mutation == "session_owner":
                connection.execute("UPDATE daily_session SET child_id=? WHERE session_id=?", (OTHER, fact["session_id"]))
            elif mutation == "outcome":
                connection.execute("UPDATE tutor_feedback SET outcome='still_stuck' WHERE feedback_id=?", (fact["source_ref"],))
            elif mutation == "hint":
                connection.execute("UPDATE tutor_turn SET hint_level=3 WHERE turn_id=?", (fact["turn_id"],))
            else:
                connection.execute("DELETE FROM tutor_feedback WHERE feedback_id=?", (fact["source_ref"],))
            connection.commit()
    result = service.refresh_active_learning(CHILD)[0]
    assert result["status"] == "rolled_back"
    assert result["learning_evidence_status"] == "selection_basis_invalidated"
    assert service.get(eid)["candidate_config"]["selection"] == original
    assert StrategyEvolutionService(Database(service.database.path)).tutor_context(CHILD, 1) is None


def test_removing_mutable_selection_metadata_cannot_hide_withdrawn_basis(learning_flow):
    service, _, _ = learning_flow
    eid, _, _ = selected_learning_trial(learning_flow)
    with service.database.connect() as connection:
        row = connection.execute("SELECT candidate_config_json FROM strategy_experiment WHERE experiment_id=?", (eid,)).fetchone()
        config = json.loads(row[0])
        config.pop("selection")
        connection.execute("UPDATE strategy_experiment SET candidate_config_json=? WHERE experiment_id=?", (json.dumps(config), eid))
        connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
        connection.commit()
    assert service.tutor_context(OTHER, 1) is None
    assert service.get(eid)["selection_basis_invalidated"] is True


def test_legacy_learning_aggregate_cannot_reconstruct_original_selection_facts(learning_flow):
    service, _, _ = learning_flow
    eid, _, _ = selected_learning_trial(learning_flow, "shadow")
    with service.database.connect() as connection:
        row = connection.execute("SELECT transition_id,metrics_json FROM strategy_transition_event WHERE experiment_id=? AND from_status IS NULL", (eid,)).fetchone()
        old_selection = json.loads(row["metrics_json"])
        old_selection.pop("basis")
        connection.execute("UPDATE strategy_transition_event SET metrics_json=? WHERE transition_id=?", (json.dumps(old_selection), row["transition_id"]))
        connection.commit()
    with pytest.raises(InvalidStrategyTransitionError, match="selection_basis_invalidated"):
        service.activate(eid)
    result = service.get(eid)
    assert result["transitions"][-1]["metrics"]["invalid_sources"][0]["reason"] == "original_learning_fact_snapshot_missing"


def alias_question(learning, monkeypatch, source_id):
    from home_study_agent import learning_state

    original = next(item for item in (*learning_state.ITEMS, *learning_state.EXTENDED_ITEMS)
                    if item[0] == source_id)
    alias_id = "alias-" + source_id
    # Catalog fixture only; validation, scoring and persisted receipts stay real.
    monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS, (alias_id, *original[1:])))
    learning.seed_catalog()
    return alias_id, original[4]["exact_answers"][0]


@pytest.mark.parametrize("correct", [False, True])
def test_taught_question_alias_is_not_a_new_check(learning_flow, monkeypatch, correct):
    service, learning, clock = learning_flow
    experiment_id = active(service, OTHER)
    turn_id = scoped_turn(service, clock)
    alias_id, answer = alias_question(learning, monkeypatch, "diag-p4-arithmetic-closed-1")
    attempt = learning.submit_diagnostic(OTHER, alias_id, answer if correct else "wrong", "alias-check")
    from home_study_agent.learning_obligations import project_mastery_evidence, question_identity
    with service.database.connect() as connection:
        item = dict(connection.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?", (alias_id,)).fetchone())
        projection = project_mastery_evidence(connection, OTHER, item["knowledge_node_id"])
    receipt = next(r for r in projection["effective_evidence"] if r["evidence_id"] == attempt["evidence_id"])
    assert receipt["question_identity"] == question_identity(item)
    assert receipt["independent"] == 0
    assert turn_id in {r["turn_id"] for r in receipt["tutor_refs"]}
    result = service.get(experiment_id)
    assert result["status"] == "active"
    assert result["active_metrics"]["learning_evidence"]["status"] == "insufficient_learning_evidence"
    assert result["active_metrics"]["learning_evidence"]["checks"] == []


@pytest.mark.parametrize("history_kind", ["answer", "tutor"])
def test_new_id_cannot_bypass_same_question_history(learning_flow, monkeypatch, history_kind):
    service, learning, clock = learning_flow
    original_id = "diag-p4-arithmetic-closed-2"
    if history_kind == "answer":
        learning.submit_diagnostic(OTHER, original_id, "wrong", "historical-question")
    else:
        prior_turn = turn(service, child=OTHER, created_at=clock[0].isoformat())
        with service.database.connect() as connection:
            prompt = connection.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?", (original_id,)).fetchone()[0]
            connection.execute("UPDATE tutor_turn SET subject='数学',question=? WHERE turn_id=?", (prompt, prior_turn))
            connection.commit()
    clock[0] += timedelta(days=2)
    experiment_id = active(service, OTHER)
    scoped_turn(service, clock)
    alias_id, _ = alias_question(learning, monkeypatch, original_id)
    attempt = learning.submit_diagnostic(OTHER, alias_id, "wrong", "historical-alias-check")
    with service.database.connect() as connection:
        observation = service._reliable_learning(connection, OTHER)
    assert attempt["evidence_id"] in {ref for node in observation["nodes"] for ref in node["evidence_refs"]}
    result = StrategyEvolutionService(Database(service.database.path)).get(experiment_id)
    assert result["status"] == "active"
    assert result["active_metrics"]["learning_evidence"]["checks"] == []
    assert result["active_metrics"]["learning_evidence"]["status"] == "insufficient_learning_evidence"


@pytest.mark.parametrize("correct", [False, True])
def test_same_identity_alias_source_binds_delayed_check_not_retention(learning_flow, monkeypatch, correct):
    service, learning, clock = learning_flow
    experiment_id = active(service, OTHER)
    turn_id = scoped_turn(service, clock)
    alias_id, answer = alias_question(learning, monkeypatch, "diag-p4-arithmetic-closed-1")
    first = learning.submit_diagnostic(OTHER, alias_id, answer, "alias-baseline")
    clock[0] += timedelta(days=2)
    with service.database.connect() as connection:
        review = connection.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'",
                                    (first["evidence_id"],)).fetchone()
        rubric = connection.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
                                    (review["diagnostic_item_id"],)).fetchone()[0]
    answer = json.loads(rubric)["exact_answers"][0]
    attempt = learning.submit_diagnostic(OTHER, review["diagnostic_item_id"], answer if correct else "wrong", "alias-delayed", review["review_task_id"])
    result = service.list_for_child(OTHER)[0]
    assert result["experiment_id"] == experiment_id
    assert result["status"] == ("active" if correct else "rolled_back")
    metrics = result["active_metrics"]["learning_evidence"]
    assert metrics["qualified_check_count"] == 1
    check = metrics["checks"][0]
    assert check["check_kind"] == "delayed_check"
    assert check["question_identity"] == check["exposure_question_identity"]
    assert check["evidence_ref"] == attempt["evidence_id"]
    assert check["source_evidence_ref"] == first["evidence_id"]
    assert check["turn_id"] == turn_id
    assert check["strategy_version"] == result["candidate_version"]
    assert check["retention_claim_allowed"] is False
    assert metrics["learning_gain"] is None
    with service.database.connect() as connection:
        observation = service._reliable_learning(connection, OTHER)["nodes"][0]
    assert observation["retention_success_count"] == 0


def test_qualified_answer_observations_select_candidate_without_self_report(learning_flow):
    service, learning, _ = learning_flow
    attempt = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-1", "12", "wrong-baseline")
    candidate = service.maybe_propose(OTHER)
    selection = candidate["candidate_config"]["selection"]
    assert candidate["candidate_version"] == STUCK_VERSION
    assert selection["evidence_kind"] == "qualified_diagnostic_answer"
    assert selection["learning_observations"][0]["evidence_refs"] == [attempt["evidence_id"]]
    replay = service.run_replay(candidate["experiment_id"])
    assert replay["replay_metrics"]["tutor_feedback_count"] == 0
    assert replay["effectiveness_status"] == "unverified"


@pytest.mark.parametrize("check", ["new_item", "delayed"])
def test_saying_understood_does_not_hide_bound_learning_failure(learning_flow, check):
    service, learning, clock = learning_flow
    experiment_id = active(service, OTHER)
    turn_id = scoped_turn(service, clock)
    for _ in range(3):
        feedback(service, turn_id, "understood", child=OTHER)
    assert service.get(experiment_id)["status"] == "active"
    if check == "new_item":
        attempt = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", "wrong", "new-check")
    else:
        first = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-1", "90", "initial-check")
        clock[0] += timedelta(days=2)
        with service.database.connect() as connection:
            review = connection.execute(
                "SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'",
                (first["evidence_id"],),
            ).fetchone()
        attempt = learning.submit_diagnostic(OTHER, review["diagnostic_item_id"], "wrong",
                                             "delayed-check", review["review_task_id"])
    # The next existing tutor entry evaluates persisted receipts before emitting a prompt.
    assert service.tutor_context(OTHER, 1) is None
    result = StrategyEvolutionService(Database(service.database.path)).get(experiment_id)
    assert result["status"] == "rolled_back"
    metrics = result["active_metrics"]["learning_evidence"]
    assert metrics["status"] == "learning_check_failed"
    assert metrics["failed_check_count"] == 1
    assert metrics["checks"][0]["evidence_ref"] == attempt["evidence_id"]
    assert metrics["checks"][0]["strategy_version"] == result["candidate_version"]
    assert metrics["checks"][0]["turn_id"] == turn_id
    assert metrics["learning_gain"] is None
    assert result["active_metrics"]["understood_rate"] == 1.0
    assert result["transitions"][-1]["actor"] == "system"


@pytest.mark.parametrize("mismatch", ["unbound", "historical", "wrong_version", "wrong_child", "unreliable_score"])
def test_unrelated_or_unqualified_failure_cannot_be_attributed(learning_flow, mismatch):
    service, learning, clock = learning_flow
    if mismatch == "historical":
        learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", "wrong", "old-check")
        clock[0] += timedelta(days=1)
    experiment_id = active(service, OTHER)
    if mismatch != "unbound":
        turn_id = scoped_turn(service, clock)
    else:
        turn_id = turn(service, child=OTHER, context=service.tutor_context(OTHER, 1))
    if mismatch != "historical":
        attempt = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", "wrong", "unqualified-check")
    with service.database.connect() as connection:
        if mismatch == "wrong_version":
            connection.execute("UPDATE tutor_turn SET strategy_version='different' WHERE turn_id=?", (turn_id,))
        elif mismatch == "wrong_child":
            connection.execute("UPDATE diagnostic_attempt SET child_id=? WHERE evidence_id=?", (CHILD, attempt["evidence_id"]))
        elif mismatch == "unreliable_score":
            connection.execute("UPDATE learning_evidence SET confidence=.1 WHERE evidence_id=?", (attempt["evidence_id"],))
        connection.commit()
    result = service.get(experiment_id)
    assert result["status"] == "active"
    assert result["active_metrics"]["learning_evidence"]["status"] == "insufficient_learning_evidence"


def test_successful_bound_check_is_observation_not_gain(learning_flow):
    service, learning, clock = learning_flow
    experiment_id = active(service, OTHER)
    scoped_turn(service, clock)
    with service.database.connect() as connection:
        item = connection.execute(
            "SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id='diag-p4-arithmetic-closed-2'"
        ).fetchone()
    answer = json.loads(item["expected_answer_json"])["exact_answers"][0]
    learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", answer, "successful-new-check")
    result = service.get(experiment_id)
    assert result["status"] == "active"
    metrics = result["active_metrics"]["learning_evidence"]
    assert metrics["status"] == "observed_checks_not_causal_gain"
    assert metrics["qualified_check_count"] == 1
    assert metrics["learning_gain"] is None
    assert metrics["mastery_claim_allowed"] is False


def test_later_experiment_cannot_inherit_earlier_version_checks(learning_flow):
    service, learning, clock = learning_flow
    first = active(service, OTHER)
    scoped_turn(service, clock)
    service.rollback(first, "parent stopped first trial")
    clock[0] += timedelta(hours=1)
    second = active(service, OTHER)
    learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", "wrong", "after-first-trial")
    for experiment_id in (first, second):
        metrics = service.get(experiment_id)["active_metrics"]["learning_evidence"]
        assert metrics["status"] == "insufficient_learning_evidence"
        assert metrics["checks"] == []
    assert service.get(second)["status"] == "active"


@pytest.mark.parametrize("correct", [False, True])
def test_owned_review_plan_binds_delayed_check_without_claiming_retention(learning_flow, correct):
    service, learning, clock = learning_flow
    original = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-1", "12", "before-strategy")
    clock[0] += timedelta(days=2)
    experiment_id = active(service, OTHER)
    with service.database.connect() as connection:
        review = connection.execute(
            "SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'",
            (original["evidence_id"],),
        ).fetchone()
        connection.execute(
            """INSERT INTO daily_plan(plan_id, child_id, session_id, plan_date, status,
            total_minutes, generator, version, created_at, client_action_id)
            VALUES ('review-plan', ?, ?, '2026-09-01', 'active', 10, 'fixture', 1, ?, 'review-plan')""",
            (OTHER, OTHER, clock[0].isoformat()),
        )
        connection.execute(
            """INSERT INTO plan_item(plan_item_id, plan_id, subject, title, expected_minutes,
            priority, status, position, source_type, source_ref)
            VALUES ('review-plan-item', 'review-plan', 'math', 'review', 10, 1, 'pending', 1, 'due_review', ?)""",
            (review["review_task_id"],),
        )
        connection.commit()
    turn_id = turn(service, child=OTHER, created_at=clock[0].isoformat())
    with service.database.connect() as connection:
        connection.execute("UPDATE tutor_turn SET plan_item_id='review-plan-item' WHERE turn_id=?", (turn_id,))
        connection.commit()
    service.record_tutor_turn(turn_id, OTHER, service.tutor_context(OTHER, 1), "passed")
    clock[0] += timedelta(minutes=1)
    with service.database.connect() as connection:
        expected = connection.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
                                      (review["diagnostic_item_id"],)).fetchone()[0]
    answer = json.loads(expected)["exact_answers"][0] if correct else "wrong"
    learning.submit_diagnostic(OTHER, review["diagnostic_item_id"], answer, "review-after-strategy", review["review_task_id"])
    result = service.list_for_child(OTHER)[0]
    assert result["experiment_id"] == experiment_id
    assert result["status"] == ("active" if correct else "rolled_back")
    check = result["active_metrics"]["learning_evidence"]["checks"][0]
    assert check["check_kind"] == "delayed_check"
    assert check["retention_claim_allowed"] is False
    with service.database.connect() as connection:
        observation = service._reliable_learning(connection, OTHER)["nodes"][0]
    assert observation["retention_success_count"] == 0
    assert observation["status"] != "retention_observed"


def test_repeat_preserves_first_qualified_success_without_transfer_or_retention_claim(learning_flow):
    service, learning, clock = learning_flow
    experiment_id = active(service, OTHER)
    scoped_turn(service, clock)
    with service.database.connect() as connection:
        expected = connection.execute(
            "SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id='diag-p4-arithmetic-closed-2'"
        ).fetchone()[0]
    answer = json.loads(expected)["exact_answers"][0]
    first = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", answer, "first-qualified")
    clock[0] += timedelta(minutes=10)
    repeated = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", answer, "practice-qualified")
    with service.database.connect() as connection:
        observation = service._reliable_learning(connection, OTHER)["nodes"][0]
    assert observation["status"] == "independent_observed"
    assert observation["independent_correct"] == 1
    assert observation["independent_distinct_items"] == 1
    assert observation["retention_success_count"] == 0
    assert observation["mastery_claim_allowed"] is False
    metrics = service.get(experiment_id)["active_metrics"]["learning_evidence"]
    expected_refs = {first["evidence_id"]}
    assert set(observation["evidence_refs"]) == expected_refs
    assert {check["evidence_ref"] for check in metrics["checks"]} == expected_refs
    with service.database.connect() as connection:
        assert connection.execute("SELECT 1 FROM diagnostic_attempt WHERE evidence_id=?", (repeated["evidence_id"],)).fetchone()
    assert all(check["independent"] is None for check in metrics["checks"])
    assert metrics["learning_gain"] is None
    assert metrics["mastery_claim_allowed"] is False


def test_recomputed_score_mismatch_removes_trial_observation(learning_flow):
    service, learning, clock = learning_flow
    experiment_id = active(service, OTHER)
    scoped_turn(service, clock)
    with service.database.connect() as connection:
        item = connection.execute(
            "SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id='diag-p4-arithmetic-closed-2'"
        ).fetchone()
    answer = json.loads(item["expected_answer_json"])["exact_answers"][0]
    attempt = learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", answer, "invalidated-check")
    assert service.get(experiment_id)["active_metrics"]["learning_evidence"]["qualified_check_count"] == 1
    with service.database.connect() as connection:
        connection.execute("UPDATE diagnostic_attempt SET submitted_answer='changed' WHERE evidence_id=?", (attempt["evidence_id"],))
        connection.commit()
    assert service.get(experiment_id)["active_metrics"]["learning_evidence"]["status"] == "insufficient_learning_evidence"
    assert service.get(experiment_id)["status"] == "active"


def test_maintenance_refresh_is_child_scoped_idempotent_and_persistent(learning_flow):
    service, learning, clock = learning_flow
    experiment_id = active(service, OTHER)
    scoped_turn(service, clock)
    learning.submit_diagnostic(OTHER, "diag-p4-arithmetic-closed-2", "wrong", "maintenance-check")
    assert service.refresh_active_learning(CHILD) == []
    with service.database.connect() as connection:
        assert connection.execute("SELECT status FROM strategy_experiment WHERE experiment_id=?", (experiment_id,)).fetchone()[0] == "active"
    assert service.refresh_active_learning() == [{
        "experiment_id": experiment_id, "child_id": OTHER,
        "status": "rolled_back", "learning_evidence_status": "learning_check_failed",
    }]
    assert service.refresh_active_learning() == []
    assert StrategyEvolutionService(Database(service.database.path)).get(experiment_id)["status"] == "rolled_back"


def test_unchanged_refresh_does_not_churn_updated_at(learning_flow):
    service, _, clock = learning_flow
    experiment_id = active(service, OTHER)
    first = service.get(experiment_id)
    clock[0] += timedelta(minutes=1)
    service.refresh_active_learning()
    assert service.get(experiment_id)["updated_at"] == first["updated_at"]


def test_all_strategy_mutations_keep_http_parent_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HSC_DATA_DIR", str(tmp_path / "import-state"))
    monkeypatch.setenv("HSC_DATABASE_PATH", str(tmp_path / "import-state" / "import.db"))
    monkeypatch.setenv("HSC_MAINTENANCE_ENABLED", "0")
    from test_app import client_for

    with client_for(tmp_path) as client:
        token = client.post("/v1/parent/setup", json={"pin": "6842"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        candidate_url = f"/v1/children/{CHILD}/strategy-experiments/candidate"
        assert client.post(candidate_url).status_code == 401
        candidate = client.post(candidate_url, headers=headers).json()
        for action in ("replay", "shadow", "activate", "rollback"):
            response = client.post(f"/v1/strategy-experiments/{candidate['experiment_id']}/{action}",
                                   json={"reason": "child cannot authorize"} if action == "rollback" else None)
            assert response.status_code == 401
        result = client.get(f"/v1/children/{CHILD}/strategy-experiments", headers=headers).json()[0]
        assert result["status"] == "draft"
        assert result["effectiveness_status"] == "unverified"
        protocol_url = f"/v1/children/{CHILD}/bounded-experiments"
        assert client.get(protocol_url).status_code == 401
        protocol = client.get(protocol_url, headers=headers).json()
        assert protocol["items"][0]["source_id"] == candidate["experiment_id"]
        assert protocol["items"][0]["outcome_status"] == "unknown"
        assert protocol["local_only"] is True
        assert protocol["cloud_transmission_allowed"] is False
