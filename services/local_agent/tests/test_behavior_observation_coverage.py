"""Synthetic, isolated coverage contracts; no family database or saved frames."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import cv2
import numpy as np
import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.behavior_model.app_integration import BehaviorAppIntegration
from home_study_agent.behavior_model.outcome_evidence import task_context_snapshot
from home_study_agent.database import Database
from home_study_agent.observation_engine import ObservationEngine
from home_study_agent.repository import StudyRepository


ZERO = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def at(seconds):
    return (ZERO + timedelta(seconds=seconds)).isoformat()


def context(task="task-1", subject="math", version=1):
    return task_context_snapshot({
        "plan_item_id": task, "subject": subject, "task_type": "written",
        "cognitive_load": "medium", "source_type": "school_homework", "plan_version": version,
    })


def sample(sequence, seconds, *, epoch="epoch-1", child="child-1", task=None, good=True, state="present"):
    return {
        "schema_version": "observation_coverage_v1", "child_id": child, "session_id": f"session-{child}",
        "stream_epoch": epoch, "sequence": sequence, "captured_at": at(seconds),
        "capture_monotonic_ms": int(seconds * 1000), "received_at": at(seconds),
        "quality": {"observation_ready": good, "scene_quality": "ready" if good else "too_dark",
                    "person_baseline": "established", "shadow": False, "analysis_skipped": False},
        "task_context": context() if task is None else task,
        "state": state, "confidence": .9, "model_version": "opencv_rules_v1",
        "analysis_id": f"analysis-{child}-{epoch}-{sequence}",
        "producer_epoch": "producer-1",
        "state_event_id": None,
    }


def begin(service, epoch="epoch-1", child="child-1", seconds=0):
    return service.begin_observation_stream(child, f"session-{child}", stream_epoch=epoch,
                                            at=at(seconds), client_action_id=f"begin-{child}-{epoch}")


def put(service, payload):
    return service.record_observation_sample(payload["child_id"], payload["session_id"], payload)


def coverage(service, start=0, end=20, child="child-1"):
    return service.observation_coverage(child, start_at=at(start), end_at=at(end))


def test_continuous_samples_produce_real_clipped_denominator_and_restart(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    ids = [put(service, sample(i, i * 2))["event_id"] for i in range(4)]
    result = coverage(BehaviorModelService(service.path), start=1, end=5)
    assert result["observed_seconds"] == 4
    assert result["task_opportunities"] == 1
    assert result["strata"][0]["sample_opportunities"] >= 2
    assert set(ids) <= set(result["evidence_refs"])
    assert result["measurement_basis"] == "qualified_sample_intervals"
    assert service.replay("child-1")["states"] == []


def test_no_tail_extrapolation_after_silence_stop_or_epoch_change(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    put(service, sample(0, 0))
    put(service, sample(1, 2))
    assert coverage(service)["observed_seconds"] == 2
    assert "unobserved_stream_time" in coverage(service)["unknown_reasons"]
    service.stop_observation_stream("child-1", "session-child-1", stream_epoch="epoch-1", sequence=2,
                                    reason="paused", at=at(3), client_action_id="stop-1")
    put(service, sample(3, 4))
    begin(service, epoch="epoch-2", seconds=6)
    put(service, sample(0, 6, epoch="epoch-2"))
    put(service, sample(4, 7))  # Late old connection must not replace the new one.
    put(service, sample(1, 8, epoch="epoch-2"))
    assert coverage(service)["observed_seconds"] == 4
    assert "inactive_epoch" in coverage(service)["unknown_reasons"]


@pytest.mark.parametrize("defect", ["gap", "quality", "sequence_gap", "out_of_order", "clock_jump", "stale", "context", "context_version", "unknown_context", "producer_restart", "shadow", "skipped"])
def test_discontinuities_do_not_bridge_unobserved_time(tmp_path, defect):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    put(service, sample(0, 0))
    middle = sample(1, 2)
    last = sample(2, 4)
    if defect == "gap":
        middle = sample(1, 10)
        last = sample(2, 12)
    elif defect == "quality":
        middle["quality"]["scene_quality"] = "too_dark"
    elif defect == "sequence_gap":
        middle["sequence"], last["sequence"] = 3, 5
    elif defect == "out_of_order":
        middle["sequence"], last["sequence"] = 2, 1
    elif defect == "clock_jump":
        middle["capture_monotonic_ms"] = 100_000
    elif defect == "stale":
        middle["received_at"] = at(15)
    elif defect == "context":
        middle["task_context"] = context("another-task")
    elif defect == "context_version":
        middle["task_context"] = context(version=2)
    elif defect == "producer_restart":
        middle["producer_epoch"] = "restarted-engine"
    elif defect == "shadow":
        middle["quality"]["shadow"] = True
    elif defect == "skipped":
        middle["quality"]["analysis_skipped"] = True
    else:
        middle["task_context"] = None
    put(service, middle)
    put(service, last)
    result = coverage(service)
    assert result["observed_seconds"] == (4 if defect == "shadow" else 2 if defect == "gap" else 0)
    if defect == "shadow":
        assert result["behavior_observed_seconds"] == 0
    else:
        assert result["unknown_reasons"]


def test_duplicate_is_payload_bound_and_retraction_never_bridges_a_hole(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    saved = [put(service, sample(i, i * 2)) for i in range(4)]
    assert put(service, sample(1, 2))["idempotent"] is True
    altered = sample(1, 2, good=False)
    with pytest.raises(ValueError, match="conflict"):
        put(service, altered)
    service.retract_event(saved[1]["event_id"], "child-1", "retract-sample", "wrong quality")
    assert coverage(BehaviorModelService(service.path))["observed_seconds"] == 2
    assert put(service, sample(1, 2))["idempotent"] is True
    assert coverage(service)["observed_seconds"] == 2


def test_retracted_start_cannot_restore_previous_connection(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    opening = begin(service)
    put(service, sample(0, 0))
    put(service, sample(1, 2))
    service.retract_event(opening["event_id"], "child-1", "retract-open", "invalid stream")
    assert coverage(service)["observed_seconds"] == 0


def test_overlapping_sessions_do_not_double_count_and_children_are_isolated(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    for child in ("child-1", "child-2"):
        begin(service, child=child)
        for i in range(3):
            put(service, sample(i, i * 2, child=child))
    assert coverage(service)["observed_seconds"] == 4
    assert coverage(service, child="child-2")["observed_seconds"] == 4
    with pytest.raises(ValueError):
        service.record_observation_sample("child-2", "session-child-2", sample(3, 6))
    service.begin_observation_stream("child-1", "parallel", stream_epoch="other", at=at(0), client_action_id="parallel-open")
    for i in range(3):
        payload = sample(i, i * 2, epoch="other")
        payload["session_id"] = "parallel"
        put(service, payload)
    assert coverage(service)["observed_seconds"] <= 4


def test_legacy_events_never_supply_coverage_or_pollute_new_numerator(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    service.ingest(ObservableEvent(child_id="child-1", session_id="session-child-1", occurred_at=at(1),
                                   event_type="work.stall", source="local_vision", confidence=.9,
                                   payload={"duration_ms": 100_000, "actual_minutes": 60}))
    assert coverage(service)["status"] == "unknown"
    assert coverage(service)["observed_seconds"] is None
    begin(service)
    put(service, sample(0, 0))
    put(service, sample(1, 2))
    assert coverage(service)["strata"][0]["event_counts"].get("work.stall", 0) == 0


def test_shorter_exposure_with_same_episode_rate_is_stable_not_improving(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    for i in range(11):
        put(service, sample(i, i * 2, state="paused_like" if i in (1, 6) else "present"))
    begin(service, epoch="epoch-2", seconds=30)
    for i in range(6):
        put(service, sample(i, 30 + i * 2, epoch="epoch-2", state="paused_like" if i == 1 else "present"))
    from home_study_agent.behavior_model.outcome_evidence import compare_coverage
    result = compare_coverage(coverage(service, end=20), coverage(service, start=30, end=40), "work.stall")
    assert result["baseline_rate_per_hour"] == result["current_rate_per_hour"]
    assert result["rate_direction"] == "stable"
    assert result["direction"] == "not_yet_clear"


@pytest.fixture
def managed(tmp_path):
    database = Database(tmp_path / "agent.db")
    database.initialize()
    return seed_managed_observation(database)


def seed_managed_observation(database):
    """Seed the original public fixture through its already initialized Database."""
    StudyRepository(database).seed_family()
    with database.connect() as db:
        db.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('session','child-primary-4','active','study',?,?,'session-action')", (at(0), at(0)))
        db.execute("INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id) VALUES('plan','child-primary-4','session','2026-09-01','active',20,'test',1,?,'plan-action')", (at(0),))
        db.execute("INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position) VALUES('task','plan','math','exercise',20,1,'active',0)")
        db.execute("UPDATE family_rule SET observation_mode='gentle' WHERE child_id='child-primary-4'")
        db.commit()
    service = BehaviorModelService(database)
    service.initialize()
    return database, service, ObservationEngine(database), BehaviorAppIntegration(service, database)


def test_engine_builder_uses_own_quality_time_and_db_context_without_media(managed, monkeypatch):
    database, service, engine, integration = managed
    monkeypatch.setattr("home_study_agent.behavior_model.app_integration._now", lambda: at(0))
    integration.begin_observation_stream("session", stream_epoch="epoch", client_action_id="open-managed")
    frame = np.tile(np.array([80, 180], dtype=np.uint8), (240, 160))
    encoded = cv2.imencode(".png", frame)[1].tobytes()
    def classify(gray, brightness, sharpness, motion, tracker, now):
        tracker.face_seen_at = now
        return "present", .9, "synthetic qualification"
    monkeypatch.setattr(engine, "_classify", classify)
    for sequence in (0, 1):
        engine._now = lambda: ZERO + timedelta(seconds=sequence * 2)
        analysis = engine.analyze("session", encoded)
        payload = engine.build_coverage_sample("session", stream_epoch="epoch", sequence=sequence,
                    captured_at=at(sequence * 2), capture_monotonic_ms=sequence * 2000,
                    analysis=analysis, plan_item_id="task")
        altered_analysis = deepcopy(analysis)
        altered_analysis["observation_ready"] = False
        assert engine.build_coverage_sample("session", stream_epoch="epoch", sequence=sequence,
                    captured_at=at(sequence * 2), capture_monotonic_ms=sequence * 2000,
                    analysis=altered_analysis, plan_item_id="task")["quality"]["observation_ready"] is True
        assert payload["received_at"] == at(sequence * 2)
        assert payload["task_context"]["context_version"]
        assert "frame" not in json.dumps(payload) and "metrics" not in payload
        integration.record_coverage_sample("session", payload)
    result = service.observation_coverage("child-primary-4", start_at=at(0), end_at=at(2))
    assert result["observed_seconds"] == 2
    with pytest.raises(ValueError):
        engine.build_coverage_sample("session", stream_epoch="epoch", sequence=2, captured_at=at(4),
                                     capture_monotonic_ms=4000, analysis={"observation_ready": True})
    monkeypatch.setattr("home_study_agent.behavior_model.app_integration._now", lambda: at(3))
    integration.stop_observation_stream("session", stream_epoch="epoch", sequence=2,
                                        reason="paused", client_action_id="stop-managed")


def test_missing_context_stays_unknown_in_real_engine_entry(managed, monkeypatch):
    database, service, engine, integration = managed
    monkeypatch.setattr("home_study_agent.behavior_model.app_integration._now", lambda: at(0))
    integration.begin_observation_stream("session", stream_epoch="empty-task", client_action_id="open-no-task")
    with database.connect() as db:
        db.execute("UPDATE plan_item SET status='completed'")
        db.commit()
    frame = cv2.imencode(".png", np.full((200, 200, 3), 100, dtype=np.uint8))[1].tobytes()
    engine._now = lambda: ZERO
    analysis = engine.analyze("session", frame)
    payload = engine.build_coverage_sample("session", stream_epoch="empty-task", sequence=0,
                    captured_at=at(0), capture_monotonic_ms=0, analysis=analysis)
    assert payload["task_context"] is None
    integration.record_coverage_sample("session", payload)
    assert service.observation_coverage("child-primary-4", start_at=at(0), end_at=at(5))["status"] == "unknown"
    engine.release("session")
    with pytest.raises(ValueError):
        engine.build_coverage_sample("session", stream_epoch="empty-task", sequence=1,
                    captured_at=at(2), capture_monotonic_ms=2000, analysis=analysis)


@pytest.mark.parametrize("change", [{"frame_base64": "not-allowed"}, {"sequence": True}, {"capture_monotonic_ms": -1}, {"received_at": "2026-09-01T10:00:00"}])
def test_invalid_capture_contract_is_rejected_without_persisting_sample(tmp_path, change):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    payload = sample(0, 0)
    payload.update(change)
    with pytest.raises(ValueError):
        put(service, payload)
    assert not any(item["event_type"] == "observation.sample" for item in service.replay("child-1")["events"])


def test_source_deletion_is_not_bridged_and_generic_ingest_cannot_forge_coverage(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    saved = [put(service, sample(i, i * 2)) for i in range(4)]
    with service._connect() as db:
        db.execute("DELETE FROM observable_event WHERE event_id=?", (saved[1]["event_id"],))
        db.commit()
    assert coverage(service)["observed_seconds"] == 2
    with pytest.raises(ValueError, match="trusted"):
        service.ingest(ObservableEvent(child_id="child-1", session_id="session-child-1", occurred_at=at(1),
                                      event_type="work.stall", source="observation_coverage", confidence=.9))


def test_one_analysis_cannot_be_reused_as_multiple_sample_opportunities(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    first = sample(0, 0)
    put(service, first)
    second = sample(1, 2)
    second["analysis_id"] = first["analysis_id"]
    with pytest.raises(ValueError, match="analysis"):
        put(service, second)


def test_normalized_rate_can_decrease_but_goal_success_stays_unknown(tmp_path):
    from home_study_agent.behavior_model.outcome_evidence import compare_coverage
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    for i in range(11):
        put(service, sample(i, i * 2, state="paused_like" if i in (1, 6) else "present"))
    begin(service, epoch="epoch-2", seconds=30)
    for i in range(11):
        put(service, sample(i, 30 + i * 2, epoch="epoch-2", state="paused_like" if i == 1 else "present"))
    result = compare_coverage(coverage(service, end=20), coverage(service, start=30, end=50), "work.stall")
    assert result["rate_direction"] == "lower"
    assert result["current_rate_per_hour"] == result["baseline_rate_per_hour"] / 2
    assert result["direction"] == "not_yet_clear"
    assert "goal_behavior_confirmation" in result["missing_evidence"]


def test_different_contexts_and_unsupported_observations_do_not_compare(tmp_path):
    from home_study_agent.behavior_model.outcome_evidence import compare_coverage
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    for i in range(3):
        put(service, sample(i, i * 2, state="paused_like"))
    begin(service, epoch="epoch-2", seconds=10)
    for i in range(3):
        put(service, sample(i, 10 + i * 2, epoch="epoch-2", task=context("task-2", "english")))
    prior, current = coverage(service, end=4), coverage(service, start=10, end=14)
    assert compare_coverage(prior, current, "work.stall")["rate_direction"] == "not_yet_clear"
    assert compare_coverage(prior, prior, "attention.diversion")["baseline_rate_per_hour"] is None


def test_habit_consumer_recomputes_real_denominator_and_redacts_report_sources(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    for i in range(6):
        put(service, sample(i, i * 2, state="paused_like" if i == 1 else "present"))
    begin(service, epoch="epoch-2", seconds=20)
    saved = [put(service, sample(i, 20 + i * 2, epoch="epoch-2", state="paused_like" if i == 1 else "present")) for i in range(6)]
    # An already-consented isolated experiment, not fabricated family evidence.
    with service._connect() as db:
        db.execute("""INSERT INTO habit_experiment(experiment_id,child_id,target_state,title,child_action,success_signal,
                   duration_days,baseline_json,status,proposed_at,started_at,ends_at)
                   VALUES('habit','child-1','blocked_candidate','test','test','test',7,?,'active',?,?,?)""",
                   (json.dumps({"feature": "stall_count"}), at(10), at(20), at(30)))
        db.commit()
    first = service.habit_experiments("child-1", now=at(31))["items"][0]["outcome"]
    assert first["current"]["observed_seconds"] == 10
    assert first["current"]["event_count"] == 1
    assert first["current"]["event_rate"] == .1
    service.retract_event(saved[2]["event_id"], "child-1", "retract-habit-sample", "bad coverage")
    restarted = BehaviorModelService(service.path)
    second = restarted.habit_experiments("child-1", now=at(31))["items"][0]["outcome"]
    assert second["current"]["observed_seconds"] == 6
    report = json.dumps(restarted.report_safe_context("child-1"))
    assert saved[1]["event_id"] not in report
    assert '"child_id"' not in report


def test_shadow_can_establish_exposure_baseline_without_behavior_claims(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    for day in range(3):
        begin(service, epoch=f"quiet-{day}", seconds=day * 86400)
        for i in range(4):
            payload = sample(i, day * 86400 + i * 2, epoch=f"quiet-{day}", state="paused_like",
                             task=context(f"task-{day}"))
            payload["quality"]["shadow"] = True
            put(service, payload)
        service.stop_observation_stream("child-1", "session-child-1", stream_epoch=f"quiet-{day}", sequence=4,
                        reason="ended", at=at(day * 86400 + 6), client_action_id=f"end-quiet-{day}")
    result = coverage(service, end=2 * 86400 + 6)
    assert result["status"] == "measured"
    assert result["observed_seconds"] == 18
    assert result["task_opportunities"] == 3
    assert result["behavior_observed_seconds"] == 0
    assert result["behavior_status"] == "unknown"
    assert result["strata"][0]["rates_per_hour"] == {}
    assert result["strata"][0]["event_counts"] == {}
    baseline = service.baseline("child-1")
    assert baseline["status"] == "coverage_only"
    assert baseline["days_observed"] == 3
    assert baseline["coverage"]["observed_seconds"] == 18
    assert service.replay("child-1")["states"] == []
    assert service.decide_intervention("child-1", "session-child-1", at(2 * 86400 + 6))["status"] == "no_action"
    assert service.personalization("child-1")["items"] == []


def test_shadow_exposure_cannot_dilute_gentle_event_rate_or_fake_a_zero_baseline(tmp_path):
    from home_study_agent.behavior_model.outcome_evidence import compare_coverage
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    for i in range(11):
        payload = sample(i, i * 2, state="paused_like" if i in (1, 6) else "present")
        payload["quality"]["shadow"] = i >= 4
        put(service, payload)
    result = coverage(service, end=20)
    assert result["observed_seconds"] == 20
    assert result["behavior_observed_seconds"] == 6
    assert result["strata"][0]["rates_per_hour"]["work.stall"] == 600
    assert result["strata"][0]["event_counts"]["work.stall"] == 1
    assert compare_coverage(result, result, "work.stall")["rate_direction"] == "not_yet_clear"
    shadow = coverage(service, start=10, end=20)
    assert compare_coverage(shadow, result, "work.stall")["baseline_rate_per_hour"] is None


def test_unqualified_shadow_does_not_contribute_exposure(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    for i in range(3):
        payload = sample(i, i * 2, good=False)
        payload["quality"]["shadow"] = True
        put(service, payload)
    assert coverage(service, end=4)["observed_seconds"] == 0


def test_shadow_contract_through_engine_and_integration_remains_non_intervening(managed, monkeypatch):
    database, service, engine, integration = managed
    monkeypatch.setattr("home_study_agent.behavior_model.app_integration._now", lambda: at(0))
    with database.connect() as db:
        db.execute("UPDATE family_rule SET observation_mode='shadow'")
        db.commit()
    integration.begin_observation_stream("session", stream_epoch="quiet", client_action_id="quiet-open")
    encoded = cv2.imencode(".png", np.tile(np.array([80, 180], dtype=np.uint8), (240, 160)))[1].tobytes()
    def classify(gray, brightness, sharpness, motion, tracker, now):
        tracker.face_seen_at = now
        return "paused_like", .9, "synthetic quiet candidate"
    monkeypatch.setattr(engine, "_classify", classify)
    for sequence in (0, 1):
        engine._now = lambda: ZERO + timedelta(seconds=sequence * 2)
        analysis = engine.analyze("session", encoded)
        assert integration.observation("session", analysis) is None
        assert not integration.can_offer_intervention("session", analysis)
        payload = engine.build_coverage_sample("session", stream_epoch="quiet", sequence=sequence,
                    captured_at=at(sequence * 2), capture_monotonic_ms=sequence * 2000, analysis=analysis)
        integration.record_coverage_sample("session", payload)
    result = service.observation_coverage("child-primary-4", start_at=at(0), end_at=at(2))
    assert result["observed_seconds"] == 2
    assert result["behavior_observed_seconds"] == 0
    assert service.replay("child-primary-4")["states"] == []


def test_original_observation_correction_invalidates_sample_numerator_not_camera_quality(tmp_path):
    service = BehaviorModelService(tmp_path / "coverage.db")
    begin(service)
    service.ingest(ObservableEvent(child_id="child-1", session_id="session-child-1", occurred_at=at(0),
                    event_id="observation:state-1", event_type="work.stall", source="local_vision", confidence=.9))
    for i in range(3):
        payload = sample(i, i * 2, state="paused_like")
        payload["state_event_id"] = "state-1"
        put(service, payload)
    assert coverage(service, end=4)["strata"][0]["event_counts"]["work.stall"] == 1
    service.retract_event("observation:state-1", "child-1", "source-correction", "thinking, not stalled")
    result = coverage(BehaviorModelService(service.path), end=4)
    assert result["observed_seconds"] == 4
    assert result["behavior_observed_seconds"] == 0
    assert result["strata"][0]["event_counts"] == {}
