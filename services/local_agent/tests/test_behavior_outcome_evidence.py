import json

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent


def observe(service, at, *, child="child-1", kind="work.stall", payload=None, missing=()):
    event = ObservableEvent(
        child_id=child, session_id=f"session-{child}", occurred_at=at,
        event_type=kind, source="plan" if kind.startswith("task.") else "local_vision", confidence=.95,
        payload=payload or {}, missing_sources=missing,
    )
    service.ingest(event)
    return event.event_id


def experiment(service):
    ids = []
    for day in (20, 21, 22):
        for minute in (0, 10, 20, 30):
            ids.append(observe(service, f"2026-08-{day}T10:{minute:02d}:00Z"))
    pending = service.pending_habit_experiment("child-1")["experiment"]
    active = service.respond_habit_experiment(
        pending["experiment_id"], "child-1", True, "2026-08-23T00:00:00Z", "accept-habit",
    )
    return active, ids


def test_shorter_observation_same_event_rate_is_not_habit_improvement(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    experiment(service)
    # Same 1 event / 10 minutes, but a shorter observed span and fewer events.
    for day in (23, 24, 25):
        for minute in (0, 10):
            observe(service, f"2026-08-{day}T10:{minute:02d}:00Z")
    outcome = service.habit_experiments("child-1", "2026-08-26T00:00:00Z")["items"][0]["outcome"]
    assert outcome["direction"] == "not_yet_clear"
    assert outcome["status"] == "insufficient_evidence"
    assert outcome["current"]["event_rate"] is None


def test_duration_and_task_minutes_are_not_observation_coverage(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    for day in (20, 21, 22):
        observe(service, f"2026-08-{day}T10:00:00Z", payload={"duration_ms": 60_000})
        observe(service, f"2026-08-{day}T11:00:00Z", kind="task.finished", payload={"actual_minutes": 60})
    baseline = service.baseline("child-1")
    assert baseline["status"] == "insufficient_evidence"
    assert baseline["coverage"]["observed_seconds"] is None
    assert baseline["coverage"]["task_opportunities"] is None
    assert baseline["coverage"]["recorded_task_minutes"] == 180
    assert baseline["habit_trends"]["stall_count"]["direction"] == "not_yet_clear"


def test_habit_uses_exact_window_and_recomputes_completed_evidence_after_retraction(tmp_path):
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    active, ids = experiment(service)
    observe(service, "2026-08-30T00:00:00Z")  # Exactly at exclusive end.
    current = observe(service, "2026-08-24T10:00:00Z")
    before = service.habit_experiments("child-1", "2026-08-31T00:00:00Z")["items"][0]
    assert before["outcome"]["current"]["event_count"] == 1
    service.retract_event(current, "child-1", "retract-current", "incorrect")
    service.retract_event(ids[0], "child-1", "retract-baseline", "incorrect")
    restarted = BehaviorModelService(path)
    after = restarted.habit_experiments("child-1", "2026-09-01T00:00:00Z")["items"][0]
    assert after["experiment_id"] == active["experiment_id"]
    assert after["status"] == "completed"
    assert after["closed_at"] == before["closed_at"]
    assert after["outcome"]["current"]["event_count"] == 0
    assert after["outcome"]["baseline"]["event_count"] == 11
    assert after["outcome"]["direction"] == "not_yet_clear"
    with restarted._connect() as db:
        saved = json.loads(db.execute("SELECT outcome_json FROM habit_experiment").fetchone()[0])
    assert saved == after["outcome"]


def test_missing_and_other_child_data_cannot_fill_habit_coverage(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    experiment(service)
    observe(service, "2026-08-24T10:00:00Z", missing=("local_vision",))
    for day in (24, 25, 26):
        observe(service, f"2026-08-{day}T10:00:00Z", child="child-2", kind="task.finished")
    outcome = service.habit_experiments("child-1", "2026-08-29T00:00:00Z")["items"][0]["outcome"]
    assert outcome["direction"] == "not_yet_clear"
    assert outcome["current"]["missing_event_count"] == 1
    assert outcome["current"]["task_finished_count"] == 0


def effects(service, state, *, child="child-1", count=4, probability=.9):
    sources = []
    for offset in range(count):
        day = 20 + offset
        sources.append(observe(service, f"2026-08-{day}T10:00:00Z", child=child))
        decision = service.decide_intervention(child, f"session-{child}", f"2026-08-{day}T10:01:00Z")
        assert decision["status"] == "suggested"
        service.record_feedback(decision["intervention_id"], True, f"2026-08-{day}T10:02:00Z")
        if state is not None:
            service.evaluate_effect(
                decision["intervention_id"], state, probability, f"2026-08-{day}T10:04:00Z",
                child, f"session-{child}", f"effect-{child}-{offset}",
            )
    return sources


def test_opposite_effects_change_real_support_choice_without_increasing_interventions(tmp_path):
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    effects(service, "writing", child="child-1")
    effects(service, "blocked_candidate", child="child-2")
    service = BehaviorModelService(path)
    for child in ("child-1", "child-2"):
        observe(service, "2026-08-24T10:00:00Z", child=child)
    helpful = service.decide_intervention("child-1", "session-child-1", "2026-08-24T10:01:00Z")
    unhelpful = service.decide_intervention("child-2", "session-child-2", "2026-08-24T10:01:00Z")
    assert helpful["strategy"] == "default_ask_first"
    helpful_policy = service.personalization("child-1")["items"][0]
    assert helpful_policy["effect_status"] == "recovery_observed"
    assert helpful_policy["cooldown_minutes"] == 10 and helpful_policy["daily_cap"] == 3
    assert unhelpful["strategy"] == "low_frequency_ask_first"
    assert helpful["message"] != unhelpful["message"]
    policy = service.personalization("child-2")["items"][0]
    assert policy["effect_status"] == "limited_recovery"
    assert policy["cooldown_minutes"] >= 10 and policy["daily_cap"] <= 3
    assert policy["effect_evidence_refs"]
    service.record_feedback(unhelpful["intervention_id"], True, "2026-08-24T10:02:00Z")
    observe(service, "2026-08-24T10:12:00Z", child="child-2")
    assert service.decide_intervention("child-2", "session-child-2", "2026-08-24T10:13:00Z")["reason"] == "personalized_cooldown"


@pytest.mark.parametrize("state,count,probability", [(None, 4, .9), ("blocked_candidate", 3, .9), ("blocked_candidate", 4, .3), ("unknown", 4, .9)])
def test_missing_or_weak_effects_do_not_become_negative_evidence(tmp_path, state, count, probability):
    service = BehaviorModelService(tmp_path / "behavior.db")
    effects(service, state, count=count, probability=probability)
    policy = service.personalization("child-1")["items"][0]
    assert policy["strategy"] == "default_ask_first"
    assert policy["effect_status"] == "insufficient_evidence"


def test_effect_retraction_and_parent_reset_remove_adaptation(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    sources = effects(service, "blocked_candidate")
    assert service.personalization("child-1")["items"][0]["adjusted"]
    service.retract_event(sources[0], "child-1", "retract-effect", "incorrect")
    policy = BehaviorModelService(service.path).personalization("child-1")["items"][0]
    assert policy["strategy"] == "default_ask_first"
    assert policy["effect_samples"] == 3
    assert service.personalization("child-2")["items"] == []
    policy = service.reset_personalization("child-1", "2026-08-25T00:00:00Z")["items"][0]
    assert policy["effect_samples"] == 0


@pytest.mark.parametrize("at", ["2026-08-20T09:00:00Z", "invalid", "2026-08-20T10:04:00"])
def test_effect_timestamp_must_follow_decision_with_timezone(tmp_path, at):
    service = BehaviorModelService(tmp_path / "behavior.db")
    observe(service, "2026-08-20T10:00:00Z")
    decision = service.decide_intervention("child-1", "session-child-1", "2026-08-20T10:01:00Z")
    with pytest.raises(ValueError):
        service.evaluate_effect(decision["intervention_id"], "writing", .9, at, "child-1", "session-child-1", "bad-time")


def test_habit_acceptance_replay_cannot_expose_another_child(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    active, _ = experiment(service)
    with pytest.raises((KeyError, ValueError)):
        service.respond_habit_experiment(active["experiment_id"], "child-2", True, "2026-08-23T00:00:00Z", "accept-habit")


def test_report_projection_cannot_publish_legacy_improving_or_raw_event_ids(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    active, ids = experiment(service)
    with service._connect() as db:
        db.execute("UPDATE habit_experiment SET status='completed', outcome_json=? WHERE experiment_id=?",
                   (json.dumps({"direction": "improving"}), active["experiment_id"]))
        db.commit()
    report = service.report_safe_context("child-1")
    assert report["habit_experiments"][0]["outcome"]["direction"] == "not_yet_clear"
    assert not any(event_id in json.dumps(report) for event_id in ids)


def test_missing_followups_prevent_selection_bias(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    effects(service, "blocked_candidate")
    for day in (24, 25):
        observe(service, f"2026-08-{day}T10:00:00Z")
        decision = service.decide_intervention("child-1", "session-child-1", f"2026-08-{day}T10:01:00Z")
        service.record_feedback(decision["intervention_id"], True, f"2026-08-{day}T10:02:00Z")
    # Simulate two historic same-support opportunities without follow-up.
    with service._connect() as db:
        db.execute("UPDATE intervention SET intervention_type='default_ask_first'")
        db.commit()
    policy = service.personalization("child-1")["items"][0]
    assert policy["effect_samples"] == 4
    assert policy["effect_opportunities"] == 6
    assert policy["effect_status"] == "insufficient_evidence"
    assert policy["strategy"] == "default_ask_first"


@pytest.mark.parametrize("defect", ["late", "before_feedback", "malformed", "missing_source", "shadow", "same_day", "different_support"])
def test_unqualified_effect_records_cannot_drive_adaptation(tmp_path, defect):
    service = BehaviorModelService(tmp_path / "behavior.db")
    effects(service, "blocked_candidate")
    with service._connect() as db:
        rows = db.execute("SELECT * FROM intervention ORDER BY decided_at").fetchall()
        for row in rows:
            outcome = json.loads(row["outcome_json"])
            if defect in {"late", "before_feedback", "same_day"}:
                if defect == "late":
                    outcome["observed_at"] = row["decided_at"][:10] + "T11:00:00Z"
                elif defect == "before_feedback":
                    outcome["observed_at"] = row["decided_at"]
                else:
                    outcome["observed_at"] = "2026-08-20T10:04:00Z"
                    db.execute("UPDATE intervention SET decided_at='2026-08-20T10:01:00Z',feedback_at='2026-08-20T10:02:00Z' WHERE intervention_id=?", (row["intervention_id"],))
                db.execute("UPDATE intervention SET outcome_json=? WHERE intervention_id=?", (json.dumps(outcome), row["intervention_id"]))
            elif defect == "malformed":
                db.execute("UPDATE intervention SET outcome_json='not-json'")
            elif defect == "missing_source":
                db.execute("DELETE FROM observable_event WHERE event_id=?", (row["source_event_id"],))
            elif defect == "shadow":
                db.execute("UPDATE observable_event SET payload_json=?", (json.dumps({"shadow": True}),))
            else:
                db.execute("UPDATE intervention SET intervention_type='low_frequency_ask_first'")
        db.commit()
    policy = service.personalization("child-1")["items"][0]
    assert policy["effect_status"] == "insufficient_evidence"
    assert policy["strategy"] == "default_ask_first"


def test_stopped_habit_keeps_stop_boundary_but_retracts_its_evidence(tmp_path):
    service = BehaviorModelService(tmp_path / "behavior.db")
    active, _ = experiment(service)
    event_id = observe(service, "2026-08-24T10:00:00Z")
    stopped = service.stop_habit_experiment(active["experiment_id"], "child-1", "2026-08-24T10:01:00Z", "stop-habit")
    assert stopped["outcome"]["current"]["event_count"] == 1
    observe(service, "2026-08-24T11:00:00Z")
    service.retract_event(event_id, "child-1", "retract-stopped", "incorrect")
    replay = next(item for item in BehaviorModelService(service.path).habit_experiments("child-1")["items"]
                  if item["experiment_id"] == active["experiment_id"])
    assert replay["status"] == "stopped"
    assert replay["outcome"]["current"]["event_count"] == 0
    assert replay["outcome"]["direction"] == "not_yet_clear"
    assert replay["closed_at"] == stopped["closed_at"]
