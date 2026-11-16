from __future__ import annotations

import sqlite3

from home_study_agent.bounded_experiment_protocol import (
    ensure_protocol,
    list_for_child,
    protocol_state,
    transition_protocol,
)
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository


NOW = "2026-09-28T12:00:00+00:00"


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "bounded-protocol.db")
    database.initialize()
    StudyRepository(database).seed_family()
    return database


def _draft(connection, source_id="source-a", child_id="child-primary-4", variable="prompt_pacing"):
    return ensure_protocol(
        connection, child_id=child_id, domain="teaching_strategy",
        source_kind="strategy_experiment", source_id=source_id,
        primary_variable=variable, baseline_version="baseline-v1",
        candidate_version="candidate-v1", status="draft",
        metrics={"sample_size": 0}, now=NOW,
        reason="create one-variable candidate",
    )


def test_insufficient_samples_stay_unknown_and_burden_rolls_back(tmp_path) -> None:
    database = _database(tmp_path)
    with database.connect() as connection:
        state = _draft(connection)
        assert state["outcome_status"] == "unknown"
        for index, target in enumerate(("replay_passed", "shadow"), start=1):
            state = transition_protocol(
                connection, source_kind="strategy_experiment", source_id="source-a",
                target=target, metrics={"sample_size": index},
                now=f"2026-09-28T12:0{index}:00+00:00", reason=target,
            )
            assert state["outcome_status"] == "unknown"
        rolled_back = transition_protocol(
            connection, source_kind="strategy_experiment", source_id="source-a",
            target="active",
            metrics={"sample_size": 3, "refusal_count": 2, "burden_events": 0},
            now="2026-09-28T12:03:00+00:00", reason="activate",
        )
        assert rolled_back["status"] == "rolled_back"
        assert rolled_back["outcome_status"] == "worsening"
        assert rolled_back["causal_effect_claimed"] is False
        first = connection.execute(
            "SELECT event_id FROM bounded_experiment_event ORDER BY event_seq LIMIT 1"
        ).fetchone()
        try:
            connection.execute(
                "UPDATE bounded_experiment_event SET reason='rewrite' WHERE event_id=?",
                (first["event_id"],),
            )
        except sqlite3.IntegrityError as error:
            assert "immutable" in str(error)
        else:
            raise AssertionError("bounded experiment event was mutable")
        connection.commit()

    with database.connect() as restarted:
        state = protocol_state(
            restarted, source_kind="strategy_experiment", source_id="source-a"
        )
        assert state["status"] == "rolled_back"
        assert state["local_only"] is True
        assert state["cloud_transmission_allowed"] is False


def test_one_open_primary_variable_and_child_isolation(tmp_path) -> None:
    database = _database(tmp_path)
    with database.connect() as connection:
        _draft(connection)
        try:
            _draft(connection, source_id="source-b")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("two open experiments changed the same primary variable")
        _draft(
            connection, source_id="source-c", child_id="child-junior-3",
            variable="prompt_pacing",
        )
        primary = list_for_child(connection, "child-primary-4")
        junior = list_for_child(connection, "child-junior-3")
        assert [item["source_id"] for item in primary] == ["source-a"]
        assert [item["source_id"] for item in junior] == ["source-c"]
