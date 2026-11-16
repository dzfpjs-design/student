from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.database import Database
from home_study_agent.family_clock import family_time


def event(event_type: str, time: str, **kwargs) -> ObservableEvent:
    return ObservableEvent(child_id="child-1", session_id="session-1", occurred_at=time,
                           event_type=event_type, source="local.sensor", confidence=.9, **kwargs)


def test_behavior_event_state_intervention_replay_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    service.ingest(event("task.started", "2026-08-28T10:00:00Z"))
    service.ingest(event("work.stall", "2026-08-28T10:02:00Z", missing_sources=("app_interaction",), alternative_explanations=("The task may intentionally be difficult.",)))
    decision = service.decide_intervention("child-1", "session-1", "2026-08-28T10:03:00Z")
    assert decision["status"] == "suggested"
    assert service.record_feedback(decision["intervention_id"], False, "2026-08-28T10:04:00Z")["feedback"] == "declined"
    service.evaluate_effect(decision["intervention_id"], "writing", .8, "2026-08-28T10:07:00Z", "child-1", "session-1", "effect-replay-001")
    restarted = BehaviorModelService(path)
    assert restarted.decide_intervention("child-1", "session-1", "2026-08-28T10:05:00Z") == {"status": "suppressed", "reason": "cooldown"}
    replay = restarted.replay("child-1", "session-1")
    assert [state["state"] for state in replay["states"]] == ["task_started", "blocked_candidate"]
    assert replay["states"][1]["evidence"]["missing_sources"] == ["app_interaction"]
    assert replay["interventions"][0]["feedback"] == "declined"
    assert "difficult" in replay["states"][1]["alternatives"][0]


def test_behavior_frequency_cap_baseline_and_invalid_event(tmp_path: Path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    for day in range(1, 4):
        for index in range(day):
            service.ingest(event("interaction.switch", f"2026-08-0{day}T10:0{index}:00Z"))
    baseline = service.baseline("child-1")
    assert baseline["status"] == "insufficient_evidence"
    for index in range(3):
        service.ingest(event("work.stall", f"2026-08-04T1{index}:00:00Z"))
        decision = service.decide_intervention("child-1", "session-1", f"2026-08-04T1{index}:01:00Z")
        if decision["status"] == "suggested":
            service.record_feedback(decision["intervention_id"], True, f"2026-08-04T1{index}:02:00Z")
    service.ingest(event("work.stall", "2026-08-04T12:59:00Z"))
    assert service.decide_intervention("child-1", "session-1", "2026-08-04T13:00:00Z")["status"] == "suppressed"
    invalid = ObservableEvent(child_id="child-1", occurred_at="2026-08-04T13:00:00Z", event_type="emotion.happy", source="camera", confidence=.9)
    try:
        service.ingest(invalid)
    except ValueError as error:
        assert "Unsupported" in str(error)
    else:
        raise AssertionError("diagnostic/emotion event was accepted")


def test_behavior_daily_features_trends_and_legacy_schema_migration(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE daily_feature(child_id TEXT NOT NULL, day TEXT NOT NULL, switch_count INTEGER NOT NULL, help_count INTEGER NOT NULL, missing_observation_count INTEGER NOT NULL, PRIMARY KEY(child_id, day))")
    service = BehaviorModelService(path)
    for day in range(1, 7):
        service.ingest(event("presence.seated", f"2026-08-0{day}T10:00:00Z"))
        for index in range(day):
            service.ingest(event("work.writing", f"2026-08-0{day}T10:{index + 1:02d}:00Z"))
        if day >= 4:
            service.ingest(event("work.stall", f"2026-08-0{day}T11:00:00Z", missing_sources=("audio",)))
            service.ingest(event("plan.deviation", f"2026-08-0{day}T11:01:00Z"))

    baseline = BehaviorModelService(path).baseline("child-1")

    assert baseline["status"] == "insufficient_evidence"
    assert baseline["features"][-1]["writing_count"] == 6
    assert baseline["features"][-1]["stall_count"] == 1
    assert baseline["feature_baselines"]["plan_deviation_count"]["mean"] == 0.5
    assert baseline["count_trends"]["writing_count"]["direction"] == "increasing"
    assert baseline["habit_trends"]["writing_count"]["direction"] == "not_yet_clear"
    assert baseline["interpretation_boundary"].startswith("Counts summarize")


def test_cross_day_evidence_proposes_one_consent_based_habit_experiment_and_persists_outcome(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    service.ingest(event("work.stall", "2026-08-20T10:00:00Z", event_id="habit-stall-1"))
    service.ingest(event("work.stall", "2026-08-20T10:05:00Z", event_id="habit-stall-2"))
    assert service.pending_habit_experiment("child-1")["status"] == "none"

    service.ingest(event("work.stall", "2026-08-21T10:00:00Z", event_id="habit-stall-3"))
    proposed = service.pending_habit_experiment("child-1")
    assert proposed["status"] == "proposed"
    assert proposed["experiment"]["title"] == "圈出卡点再求助"
    assert proposed["experiment"]["baseline"]["evidence_day_count"] == 2

    active = service.respond_habit_experiment(
        proposed["experiment"]["experiment_id"], "child-1", True,
        "2026-08-21T10:01:00Z", "habit-response-001",
    )
    assert active["status"] == "active"
    assert active["ends_at"].startswith("2026-08-28")
    replay = BehaviorModelService(path).respond_habit_experiment(
        active["experiment_id"], "child-1", True,
        "2026-08-21T10:01:00Z", "habit-response-001",
    )
    assert replay["experiment_id"] == active["experiment_id"]

    completed = BehaviorModelService(path).habit_experiments(
        "child-1", "2026-08-29T10:00:00Z"
    )["items"][0]
    assert completed["status"] == "completed"
    assert completed["outcome"]["observed_days"] == 0
    assert completed["outcome"]["status"] == "insufficient_evidence"
    assert "人格" in service.habit_experiments("child-1")["boundary"]


def test_child_correction_retracts_evidence_and_decayed_evidence_does_not_prompt(tmp_path: Path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    service.ingest(event("work.stall", "2026-08-28T10:00:00Z", event_id="observation:state-1"))
    assert service.retract_event("observation:state-1", "child-1", "correction-1", "我在心算")["status"] == "retracted"
    replay = service.replay("child-1", "session-1")
    assert replay["states"][0]["retracted"]["correction_id"] == "correction-1"
    assert service.decide_intervention("child-1", "session-1", "2026-08-28T10:01:00Z") == {
        "status": "suppressed", "reason": "child_corrected_recent_observation"
    }
    service.ingest(event("work.stall", "2026-08-28T09:00:00Z", event_id="old-stall"))
    assert service.decide_intervention("child-1", "session-1", "2026-08-28T10:31:00Z")["status"] == "no_action"


def test_manual_observable_event_keeps_its_source_without_media_payload(tmp_path: Path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    service.ingest(ObservableEvent(
        child_id="child-1", session_id="session-1", event_id="manual-help-1",
        occurred_at="2026-08-28T10:00:00Z", event_type="help.request",
        source="child_self_report", confidence=1.0, source_refs=("voice-action-1",),
        missing_sources=("local_vision",), payload={"model_version": "manual_event_v1", "channel": "voice"},
        alternative_explanations=("A request for help does not establish a stable ability or emotional state.",),
    ))

    replay = BehaviorModelService(tmp_path / "behavior.db").replay("child-1", "session-1")
    assert replay["events"][0]["source"] == "child_self_report"
    assert replay["events"][0]["payload"] == {"model_version": "manual_event_v1", "channel": "voice"}
    assert "audio" not in repr(replay["events"][0]["payload"]).lower()


def test_retraction_action_is_payload_bound_and_returns_the_original_correction(tmp_path: Path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    service.ingest(event("work.stall", "2026-08-28T10:00:00Z", event_id="retract-target"))
    first = service.retract_event("retract-target", "child-1", "correction-action-1", "我在心算")
    replay = service.retract_event("retract-target", "child-1", "correction-action-1", "我在心算")
    assert first["idempotent"] is False
    assert replay == {"status": "retracted", "idempotent": True, "correction": first["correction"]}
    try:
        service.retract_event("retract-target", "child-1", "correction-action-1", "不是走神")
    except ValueError as error:
        assert "conflicts" in str(error)
    else:
        raise AssertionError("a changed correction payload was accepted")


def test_effect_action_is_payload_bound_and_replays_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    service.ingest(event("work.stall", "2026-08-28T10:00:00Z", event_id="effect-stall"))
    decision = service.decide_intervention("child-1", "session-1", "2026-08-28T10:01:00Z")
    first = service.evaluate_effect(decision["intervention_id"], "writing", .8, "2026-08-28T10:02:00Z", "child-1", "session-1", "effect-action-001")
    replay = BehaviorModelService(path).evaluate_effect(decision["intervention_id"], "writing", .8, "2026-08-28T10:02:00Z", "child-1", "session-1", "effect-action-001")
    assert first["idempotent"] is False and replay["idempotent"] is True
    try:
        service.evaluate_effect(decision["intervention_id"], "reading", .8, "2026-08-28T10:03:00Z", "child-1", "session-1", "effect-action-001")
    except ValueError as error:
        assert "conflicts" in str(error)
    else:
        raise AssertionError("a changed effect payload was accepted")
    try:
        service.evaluate_effect(decision["intervention_id"], "writing", .8, "2026-08-28T10:03:00Z", "child-1", "session-1", "effect-action-001")
    except ValueError as error:
        assert "conflicts" in str(error)
    else:
        raise AssertionError("a changed effect timestamp was accepted")
    service.retract_event("effect-stall", "child-1", "effect-withdraw-001", "孩子在心算")
    assert service.evaluate_effect(decision["intervention_id"], "writing", .8, "2026-08-28T10:02:00Z", "child-1", "session-1", "effect-action-001")["idempotent"] is True


def test_effect_action_is_scoped_and_requires_a_client_action_id(tmp_path: Path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    now = datetime.now(timezone.utc)
    service.ingest(ObservableEvent(child_id="child-1", session_id="shared-session", event_id="effect-a", occurred_at=now.isoformat(), event_type="work.stall", source="local_vision", confidence=.95))
    service.ingest(ObservableEvent(child_id="child-2", session_id="shared-session", event_id="effect-b", occurred_at=(now + timedelta(minutes=30)).isoformat(), event_type="work.stall", source="local_vision", confidence=.95))
    first = service.decide_intervention("child-1", "shared-session", (now + timedelta(minutes=1)).isoformat())
    second = service.decide_intervention("child-2", "shared-session", (now + timedelta(minutes=31)).isoformat())
    service.evaluate_effect(first["intervention_id"], "writing", .8, (now + timedelta(minutes=2)).isoformat(), "child-1", "shared-session", "effect-scoped-001")
    try:
        service.evaluate_effect(second["intervention_id"], "writing", .8, (now + timedelta(minutes=32)).isoformat(), "child-2", "shared-session", "effect-scoped-001")
    except ValueError as error:
        assert "conflicts" in str(error)
    else:
        raise AssertionError("an effect action id was reused across interventions")
    try:
        service.evaluate_effect(first["intervention_id"], "writing", .8, now.isoformat(), "child-2", "shared-session", "effect-other-child")
    except KeyError:
        pass
    else:
        raise AssertionError("an effect was replayed across children")
    try:
        service.evaluate_effect(second["intervention_id"], "writing", .8, now.isoformat(), "child-2", "shared-session", "")
    except ValueError as error:
        assert "required" in str(error)
    else:
        raise AssertionError("an unkeyed effect overwrite was accepted")


def test_main_database_imports_legacy_behavior_sidecar_once_and_survives_restart(tmp_path: Path) -> None:
    legacy_path = tmp_path / "behavior-model.db"
    legacy = BehaviorModelService(legacy_path)
    legacy.ingest(event("work.stall", "2026-08-28T10:00:00Z", event_id="legacy-stall"))

    database = Database(tmp_path / "agent.db")
    database.initialize()
    managed = BehaviorModelService(database, legacy_path=legacy_path)
    managed.initialize()
    assert managed.replay("child-1", "session-1")["events"][0]["event_id"] == "legacy-stall"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM behavior_legacy_migration").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM observable_event").fetchone()[0] == 1

    restarted = BehaviorModelService(database, legacy_path=legacy_path)
    restarted.initialize()
    assert len(restarted.replay("child-1", "session-1")["events"]) == 1
    assert not legacy_path.exists()
    assert legacy_path.with_name("behavior-model.db.migrated").is_file()


def test_legacy_sidecar_cannot_revive_after_delete_or_old_primary_restore(tmp_path: Path) -> None:
    legacy_path = tmp_path / "behavior-model.db"
    BehaviorModelService(legacy_path).ingest(event("work.stall", "2026-08-28T10:00:00Z", event_id="legacy-delete"))
    database = Database(tmp_path / "agent.db")
    database.initialize()
    old_primary = tmp_path / "pre-migration-agent.db"
    with database.connect() as source, sqlite3.connect(old_primary) as destination:
        source.backup(destination)
    migrated = BehaviorModelService(database, legacy_path=legacy_path)
    migrated.initialize()
    assert migrated.replay("child-1", "session-1")["events"]
    # The privacy lifecycle owns main-record deletion; archive purging only
    # prevents a legacy source from restoring it later.
    with database.connect() as connection:
        connection.execute("DELETE FROM probabilistic_state WHERE child_id = 'child-1'")
        connection.execute("DELETE FROM observable_event WHERE child_id = 'child-1'")
        connection.commit()
    migrated.purge_legacy_child("child-1")
    assert not BehaviorModelService(database, legacy_path=legacy_path).replay("child-1", "session-1")["events"]

    # A restored database from before the marker cannot re-import the archived child.
    with sqlite3.connect(old_primary) as source, database.connect() as destination:
        source.backup(destination)
    restored = BehaviorModelService(Database(database.path), legacy_path=legacy_path)
    restored.initialize()
    assert restored.replay("child-1", "session-1")["events"] == []


def test_legacy_migration_conflict_stays_recoverable_until_the_next_restart(tmp_path: Path) -> None:
    legacy_path = tmp_path / "behavior-model.db"
    BehaviorModelService(legacy_path).ingest(event("work.stall", "2026-08-28T10:00:00Z", event_id="legacy-conflict"))
    database = Database(tmp_path / "agent.db")
    database.initialize()
    target = BehaviorModelService(database, legacy_path=legacy_path)
    target.ingest(event("work.reading", "2026-08-28T10:00:00Z", event_id="legacy-conflict"))
    try:
        target.initialize()
    except ValueError as error:
        assert "migration conflict" in str(error).lower()
    else:
        raise AssertionError("conflicting legacy data was silently marked migrated")
    assert legacy_path.is_file()
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM behavior_legacy_migration").fetchone()[0] == 0
        connection.execute("DELETE FROM probabilistic_state WHERE event_id = 'legacy-conflict'")
        connection.execute("DELETE FROM observable_event WHERE event_id = 'legacy-conflict'")
        connection.execute("DELETE FROM daily_feature WHERE child_id = 'child-1'")
        connection.commit()
    recovered = BehaviorModelService(database, legacy_path=legacy_path)
    recovered.initialize()
    assert recovered.replay("child-1", "session-1")["events"][0]["event_type"] == "work.stall"
    assert legacy_path.with_name("behavior-model.db.migrated").is_file()


def test_report_safe_context_is_authoritative_opaque_and_range_filtered(tmp_path: Path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    family_day = family_time(now).date()
    service.ingest(ObservableEvent(child_id="child-1", session_id="session-1", event_id="report-active", occurred_at=now.isoformat(), event_type="work.stall", source="local_vision", confidence=.9, payload={"model_version": "behavior-local-v2"}))
    decision = service.decide_intervention("child-1", "session-1", (now + timedelta(minutes=1)).isoformat())
    service.record_feedback(decision["intervention_id"], True, (now + timedelta(minutes=2)).isoformat(), client_action_id="report-feedback-001", child_id="child-1", session_id="session-1")
    context = service.report_safe_context("child-1", "session-1", family_day, family_day)
    assert context["behavior_context_authoritative"] is True
    hypothesis = context["behavior_model_summary"]["active_hypotheses"][0]
    assert hypothesis["model_version"] == "behavior-local-v2"
    assert hypothesis["evidence_ref"].startswith("behavior-hypothesis:") and "report-active" not in repr(context)
    feedback = context["behavior_intervention_feedback"][0]
    assert feedback["kind"] == "intervention_accepted"
    assert feedback["evidence_ref"].startswith("behavior-feedback:") and decision["intervention_id"] not in repr(context)
    assert service.report_safe_context("child-2", "session-1", family_day, family_day) == {
        "behavior_context_authoritative": True,
        "behavior_intervention_feedback": [],
        "habit_experiments": [],
        "behavior_model_summary": {
            "active_hypotheses": [], "withdrawn_intervention_count": 0, "retracted_hypothesis_count": 0,
            "uncertainty": "仅汇总本地可观察候选与反馈；不作情绪、人格或能力推断。",
        },
    }


def test_report_safe_context_tolerates_a_bad_historical_payload(tmp_path: Path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    family_day = family_time(now).date()
    service.ingest(ObservableEvent(child_id="child-1", session_id="session-1", event_id="bad-payload", occurred_at=now.isoformat(), event_type="work.stall", source="local_vision", confidence=.9))
    with service._connect() as db:
        db.execute("UPDATE observable_event SET payload_json = '{not-json' WHERE event_id = 'bad-payload'")
        db.commit()
    context = service.report_safe_context("child-1", "session-1", family_day, family_day)
    assert context["behavior_model_summary"]["active_hypotheses"][0]["model_version"] == "not_recorded"


def test_reappeared_sidecar_is_quarantined_and_audited_without_reimport(tmp_path: Path) -> None:
    legacy_path = tmp_path / "behavior-model.db"
    BehaviorModelService(legacy_path).ingest(event("work.stall", "2026-08-28T10:00:00Z", event_id="legacy-original"))
    database = Database(tmp_path / "agent.db")
    database.initialize()
    service = BehaviorModelService(database, legacy_path=legacy_path)
    service.initialize()
    assert legacy_path.with_name("behavior-model.db.migrated").is_file()
    # A new file at the historical path must be retained for repair, never
    # silently unlinked or folded into the already-completed migration.
    BehaviorModelService(legacy_path).ingest(event("work.stall", "2026-08-28T10:30:00Z", event_id="legacy-new"))
    BehaviorModelService(database, legacy_path=legacy_path).initialize()
    quarantines = list(tmp_path.glob("behavior-model.db.quarantine-*"))
    assert len(quarantines) == 1 and not legacy_path.exists()
    assert {row["event_id"] for row in service.replay("child-1", "session-1")["events"]} == {"legacy-original"}
    with database.connect() as connection:
        audit = connection.execute("SELECT action, summary_json FROM audit_event WHERE action = 'behavior_legacy_sidecar_quarantined'").fetchone()
    assert audit and audit["action"] == "behavior_legacy_sidecar_quarantined" and "quarantine_file" in audit["summary_json"]


def test_parent_retraction_is_limited_to_local_observation_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    service.ingest(ObservableEvent(child_id="child-1", session_id="session-1", event_id="vision-stall", occurred_at="2026-08-27T10:00:00Z", event_type="work.stall", source="local_vision", confidence=.9))
    state = service.replay("child-1")["states"][0]
    assert service.retract_observation_state(state["state_id"], "child-1", "parent-001", "正在心算")["status"] == "retracted"
    replay = BehaviorModelService(path).replay("child-1")
    assert replay["states"][0]["retracted"]["note"] == "正在心算"
    assert replay["states"][0]["effective_probability"] < replay["states"][0]["probability"]
    service.ingest(ObservableEvent(child_id="child-1", session_id="session-1", event_id="plan-task", occurred_at="2026-08-28T10:02:00Z", event_type="task.started", source="plan", confidence=.9))
    non_observation = service.replay("child-1")["states"][-1]
    try:
        service.retract_observation_state(non_observation["state_id"], "child-1", "parent-002", "不是视觉观察")
    except ValueError:
        pass
    else:
        raise AssertionError("plan evidence was retracted as an observation")


def test_feedback_personalization_stays_gentle_then_lowers_frequency_and_resets(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    for day in range(20, 24):
        at = f"2026-08-{day}T10:00:00Z"
        service.ingest(event("work.stall", at, event_id=f"stall-{day}"))
        decision = service.decide_intervention("child-1", "session-1", f"2026-08-{day}T10:01:00Z")
        assert decision["strategy"] == "default_ask_first"
        service.record_feedback(decision["intervention_id"], day == 20, f"2026-08-{day}T10:02:00Z")
        if day == 20:
            service.evaluate_effect(decision["intervention_id"], "writing", .8, f"2026-08-{day}T10:05:00Z", "child-1", "session-1", "effect-policy-001")
    service.ingest(event("work.stall", "2026-08-24T10:00:00Z", event_id="stall-adjusted"))
    decision = service.decide_intervention("child-1", "session-1", "2026-08-24T10:01:00Z")
    assert decision["strategy"] == "low_frequency_ask_first"
    assert "降低提醒频率" in " ".join(decision["explanation"])
    restarted = BehaviorModelService(path)
    policy = restarted.personalization("child-1")["items"][0]
    assert policy["sample_size"] == 4 and policy["declined"] == 3 and policy["helpful_effects"] == 1
    assert restarted.personalization("child-2")["items"] == []
    reset = restarted.reset_personalization("child-1", "2026-08-24T11:00:00Z")
    assert reset["items"][0]["sample_size"] == 0 and reset["items"][0]["strategy"] == "default_ask_first"


def test_pending_feedback_recovers_by_session_then_expires_without_cross_child_leak(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    service.ingest(event("work.stall", "2026-08-28T10:00:00Z"))
    decision = service.decide_intervention("child-1", "session-1", "2026-08-28T10:01:00Z")
    pending = BehaviorModelService(path).pending_feedback("child-1", "session-1", "2026-08-28T10:02:00Z")
    assert pending["status"] == "pending" and pending["intervention"]["intervention_id"] == decision["intervention_id"]
    assert service.pending_feedback("child-2", "session-1", "2026-08-28T10:02:00Z") == {"status": "no_pending"}
    assert service.pending_feedback("child-1", "session-1", "2026-08-28T10:17:00Z")["status"] == "expired"
    assert service.record_feedback(decision["intervention_id"], True, "2026-08-28T10:02:00Z", client_action_id="reply-001", child_id="child-1", session_id="session-1")["feedback"] == "accepted"
    assert service.record_feedback(decision["intervention_id"], True, "2026-08-28T10:02:00Z", client_action_id="reply-001", child_id="child-1", session_id="session-1")["idempotent"] is True
    try:
        service.record_feedback(decision["intervention_id"], False, "2026-08-28T10:02:00Z", client_action_id="reply-001", child_id="child-1", session_id="session-1")
    except ValueError as error:
        assert "already" in str(error)
    else:
        raise AssertionError("changed payload reused the feedback action id")
    service.ingest(event("work.stall", "2026-08-29T10:00:00Z", event_id="second-feedback-stall"))
    second = service.decide_intervention("child-1", "session-1", "2026-08-29T10:01:00Z")
    try:
        service.record_feedback(second["intervention_id"], True, "2026-08-29T10:02:00Z", client_action_id="reply-001", child_id="child-1", session_id="session-1")
    except ValueError as error:
        assert "different" in str(error)
    else:
        raise AssertionError("feedback action id leaked across interventions")


def test_correction_withdraws_derived_prompt_persists_stop_and_excludes_daily_summary(tmp_path: Path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    source_event_id = "vision-paused-001"
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    family_day = family_time(observed_at).date()
    prompted_at = observed_at + timedelta(minutes=1)
    after_correction = prompted_at + timedelta(minutes=1)
    service.ingest(ObservableEvent(
        child_id="child-1", session_id="session-1", event_id=source_event_id,
        occurred_at=observed_at.isoformat(), event_type="work.stall", source="local_vision", confidence=.95,
        source_refs=("derived-vision-state",), missing_sources=("child_self_report",),
        alternative_explanations=("The child may be calculating mentally.",),
    ))
    first = service.decide_intervention("child-1", "session-1", prompted_at.isoformat())
    assert first["status"] == "suggested"
    # Replaying the same derived source cannot create a second state or prompt.
    service.ingest(ObservableEvent(
        child_id="child-1", session_id="session-1", event_id=source_event_id,
        occurred_at=observed_at.isoformat(), event_type="work.stall", source="local_vision", confidence=.95,
    ))
    assert service.retract_event(source_event_id, "child-1", "child-correction-001", "我在心算，不是走神")["status"] == "retracted"

    restarted = BehaviorModelService(path)
    replay = restarted.replay("child-1", "session-1")
    assert len(replay["states"]) == 1
    assert replay["interventions"][0]["status"] == "withdrawn"
    assert replay["interventions"][0]["withdrawal_reason"] == "corrected_observation"
    assert restarted.pending_feedback("child-1", "session-1", after_correction.isoformat()) == {"status": "no_pending"}
    assert restarted.decide_intervention("child-1", "session-1", after_correction.isoformat()) == {
        "status": "suppressed", "reason": "child_corrected_recent_observation"
    }
    summary = restarted.daily_report_evidence("child-1", "session-1", family_day.isoformat())
    assert summary["retracted_hypothesis_count"] == 1
    assert summary["active_candidates"] == []
    safe = restarted.report_safe_context("child-1", "session-1", family_day, family_day)
    assert safe["behavior_context_authoritative"] is True
    assert safe["behavior_intervention_feedback"] == []
    assert safe["behavior_model_summary"]["retracted_hypothesis_count"] == 1
    assert summary["withdrawn_intervention_count"] == 1
    assert restarted.daily_report_evidence("child-2", "session-1", family_day.isoformat())["event_count"] == 0
