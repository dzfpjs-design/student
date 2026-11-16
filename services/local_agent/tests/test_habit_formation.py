from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from home_study_agent.behavior_model import BehaviorModelService
from home_study_agent.behavior_model.habit_formation import (
    current_prompt_policy,
    habit_formation_state,
    synchronize_habit_formation,
)
from home_study_agent.bounded_experiment_protocol import protocol_state
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_behavior_observation_coverage import managed


CHILD = "child-a"
EXPERIMENT = "habit-a"
START = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def _insert_experiment(service: BehaviorModelService, *, child_id=CHILD, status="active") -> None:
    with service._connect() as connection:
        connection.execute(
            """INSERT INTO habit_experiment(
                 experiment_id,child_id,target_state,title,child_action,success_signal,
                 duration_days,baseline_json,status,proposed_at,started_at,ends_at
               ) VALUES(?,?,?,?,?,?,30,?,?,?,?,?)""",
            (
                EXPERIMENT, child_id, "blocked_candidate", "圈出卡点再求助",
                "先圈出最不确定的一步", "在合格机会中独立说清卡点",
                json.dumps({"feature": "stall_count"}), status,
                START.isoformat(), START.isoformat() if status == "active" else None,
                (START + timedelta(days=45)).isoformat() if status == "active" else None,
            ),
        )
        connection.commit()


def _assessment(
    opportunities: int, *, status="insufficient_evidence", bounded=False,
    contexts=1, success_days=0,
):
    days = [(START + timedelta(days=index)).date().isoformat() for index in range(success_days)]
    strata = {
        f"context-{index}": {"success_days": days if index == 0 else []}
        for index in range(contexts)
    }
    result = {
        "status": status,
        "direction": "improving" if bounded else "not_yet_clear",
        "bounded_goal_success": bounded,
        "missing_evidence": [] if bounded else ["independent_repetition"],
        "baseline": {"opportunities": 5},
        "current": {
            "opportunities": opportunities,
            "independent_successes": max(0, opportunities - 1),
            "strata": strata,
        },
        "goal_version": f"goal-{opportunities}-{status}-{contexts}-{success_days}",
    }
    return lambda *_args, **_kwargs: result


def test_full_prompt_fade_is_evidence_gated_restart_safe_and_recoverable(tmp_path) -> None:
    path = tmp_path / "behavior.db"
    service = BehaviorModelService(path)
    _insert_experiment(service)
    sequence = [
        (0, "insufficient_evidence", False, 1, 0, "baseline", "L0"),
        (5, "insufficient_evidence", False, 1, 0, "cue", "L3"),
        (6, "insufficient_evidence", False, 1, 0, "practice", "L3"),
        (7, "repeated_observed_behavior", False, 1, 1, "reinforce", "L3"),
        (8, "bounded_confirmed_behavior", True, 1, 2, "fade", "L2"),
        (9, "bounded_confirmed_behavior", True, 2, 2, "transfer", "L1"),
        (10, "bounded_confirmed_behavior", True, 2, 3, "maintain", "L0"),
    ]
    with service._connect() as connection:
        for day, (opportunities, status, bounded, contexts, success_days, stage, cue) in enumerate(sequence):
            state = synchronize_habit_formation(
                connection, CHILD, EXPERIMENT, START + timedelta(days=day),
                assessment_provider=_assessment(
                    opportunities, status=status, bounded=bounded,
                    contexts=contexts, success_days=success_days,
                ),
            )
            assert (state["stage"], state["cue_level"]) == (stage, cue)
            if stage == "baseline":
                baseline_policy = current_prompt_policy(connection, CHILD, "blocked_candidate")
                assert baseline_policy["suppress_voice"] is True
                assert baseline_policy["suppression_reason"] == "habit_baseline_observation_no_prompt"
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM habit_formation_event WHERE experiment_id=?", (EXPERIMENT,)
        ).fetchone()[0] == len(sequence)
        policy = current_prompt_policy(connection, CHILD, "blocked_candidate")
        assert policy["suppress_voice"] is True
        assert policy["suppression_reason"] == "habit_maintenance_no_prompt"
        protocol = protocol_state(
            connection, source_kind="habit_experiment", source_id=EXPERIMENT
        )
        assert protocol["status"] == "active"
        assert protocol["outcome_status"] in {"unknown", "stable", "improving"}

    restarted = BehaviorModelService(path)
    with restarted._connect() as connection:
        before = habit_formation_state(connection, CHILD, EXPERIMENT)
        unchanged = synchronize_habit_formation(
            connection, CHILD, EXPERIMENT, START + timedelta(days=8),
            assessment_provider=_assessment(
                10, status="bounded_confirmed_behavior", bounded=True,
                contexts=2, success_days=3,
            ),
        )
        assert unchanged["revision"] == before["revision"]
        recovered = synchronize_habit_formation(
            connection, CHILD, EXPERIMENT, START + timedelta(days=9),
            assessment_provider=_assessment(11),
        )
        assert (recovered["stage"], recovered["cue_level"]) == ("recover", "L1")
        faded = synchronize_habit_formation(
            connection, CHILD, EXPERIMENT, START + timedelta(days=10),
            assessment_provider=_assessment(
                12, status="bounded_confirmed_behavior", bounded=True,
                contexts=2, success_days=4,
            ),
        )
        assert (faded["stage"], faded["cue_level"]) == ("fade", "L2")
        connection.commit()
        row = connection.execute(
            "SELECT event_id FROM habit_formation_event ORDER BY event_seq LIMIT 1"
        ).fetchone()
        try:
            connection.execute(
                "UPDATE habit_formation_event SET reason='rewritten' WHERE event_id=?", (row["event_id"],)
            )
        except sqlite3.IntegrityError as error:
            assert "immutable" in str(error)
        else:
            raise AssertionError("habit formation history was mutable")
    history = restarted.habit_formation_history(CHILD, EXPERIMENT)
    assert history["state"]["stage"] == "fade"
    assert history["events"][-1]["to_cue_level"] == "L2"
    assert history["cloud_transmission_allowed"] is False


def test_decline_and_burden_stop_do_not_create_a_habit_claim(tmp_path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    _insert_experiment(service, status="proposed")
    declined = service.respond_habit_experiment(
        EXPERIMENT, CHILD, False, START.isoformat(), "decline-habit-a"
    )
    assert declined["formation"]["stage"] == "declined"
    assert declined["formation"]["cue_level"] == "L0"
    assert declined["formation"]["habit_success"] is False

    other = BehaviorModelService(tmp_path / "burden.db")
    _insert_experiment(other)
    with other._connect() as connection:
        for index in range(2):
            connection.execute(
                """INSERT INTO intervention(
                     intervention_id,child_id,session_id,decided_at,candidate_state,message,
                     feedback,feedback_at,intervention_type,status
                   ) VALUES(?,?,?,?,?,?,?,?,?,'active')""",
                (
                    f"declined-{index}", CHILD, "session", (START + timedelta(minutes=index)).isoformat(),
                    "blocked_candidate", "提醒", "declined",
                    (START + timedelta(minutes=index)).isoformat(), "contextual_habit_ask_first",
                ),
            )
        stopped = synchronize_habit_formation(
            connection, CHILD, EXPERIMENT, START + timedelta(days=1),
            assessment_provider=_assessment(5),
        )
        connection.commit()
        assert (stopped['stage'], stopped['cue_level']) == ('pending_stop', 'L0')
        stopped = synchronize_habit_formation(
            connection, CHILD, EXPERIMENT, START + timedelta(days=1),
            assessment_provider=_assessment(5), complete_auto_stop=True,
        )
        connection.commit()
        assert (stopped["stage"], stopped["cue_level"]) == ("stopped", "L0")
        assert connection.execute(
            "SELECT status FROM habit_experiment WHERE experiment_id=?", (EXPERIMENT,)
        ).fetchone()[0] == "stopped"
        assert connection.execute(
            "SELECT COUNT(*) FROM intervention WHERE status='withdrawn'"
        ).fetchone()[0] == 2
        protocol = protocol_state(
            connection, source_kind="habit_experiment", source_id=EXPERIMENT
        )
        assert protocol["status"] == "rolled_back"
        assert protocol["outcome_status"] == "worsening"


def test_formation_state_is_scoped_to_one_child(tmp_path) -> None:
    service = BehaviorModelService(tmp_path / "behavior.db")
    _insert_experiment(service)
    with service._connect() as connection:
        synchronize_habit_formation(
            connection, CHILD, EXPERIMENT, START,
            assessment_provider=_assessment(0),
        )
        try:
            habit_formation_state(connection, "child-b", EXPERIMENT)
        except KeyError:
            pass
        else:
            raise AssertionError("habit formation leaked across children")


def test_formation_history_exports_and_all_history_deletion_is_durable(managed, tmp_path) -> None:
    database, service = managed[:2]
    child_id = 'child-primary-4'
    _insert_experiment(service, child_id=child_id)
    with service._connect() as connection:
        synchronize_habit_formation(
            connection, child_id, EXPERIMENT, START,
            assessment_provider=_assessment(0),
        )
        connection.commit()

    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    payload = json.loads(
        Path(privacy.export_child(child_id)['path']).read_text(encoding='utf-8')
    )
    assert len(payload['habit_formation_states']) == 1
    assert len(payload['habit_formation_events']) == 1
    assert len(payload['bounded_experiment_protocols']) == 1
    assert len(payload['bounded_experiment_events']) >= 3
    preview = privacy.deletion_preview(child_id)
    assert preview['behavior_records'] >= 2

    deleted = privacy.delete_child_scope(
        child_id, 'all_history', '确认删除', 'delete-habit-formation-history'
    )
    assert deleted['status'] == 'completed'
    with database.connect() as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM habit_formation_state WHERE child_id=?', (child_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            'SELECT COUNT(*) FROM habit_formation_event WHERE child_id=?', (child_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            'SELECT COUNT(*) FROM bounded_experiment_protocol WHERE child_id=?', (child_id,)
        ).fetchone()[0] == 0
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []

    restarted = PrivacyLifecycleService(database, tmp_path)
    restarted.initialize()
    assert restarted.deletion_preview(child_id)['behavior_records'] == 0
