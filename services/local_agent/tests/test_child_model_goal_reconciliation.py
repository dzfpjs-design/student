from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.family_clock import family_today
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_goal_program import exact_goal_item, goal_db


CHILD = "child-junior-3"
OTHER = "child-primary-4"


def _rows(database, table, child_id=None):
    with database.connect() as connection:
        where = " WHERE child_id=?" if child_id else ""
        return sorted(
            (dict(row) for row in connection.execute(
                f"SELECT * FROM {table}{where}", (child_id,) if child_id else ()
            )),
            key=lambda row: repr(sorted(row.items())),
        )


def _answer(database, item):
    session = StudyRepository(database).start_session(CHILD, str(uuid4()))["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    return LearningStateService(database).submit_diagnostic(session, item[0], item[1], str(uuid4()))


def test_snapshot_reconciles_existing_goal_before_freezing_and_parent_noop_is_stable(goal_db, exact_goal_item):
    goals = GoalProgramService(goal_db)
    baseline = goals.cycles(CHILD)[0]
    goals.cycles(OTHER)
    other_before = _rows(goal_db, "goal_cycle", OTHER)
    _answer(goal_db, exact_goal_item)
    attempts = _rows(goal_db, "diagnostic_attempt")

    # Reopening services leaves the persisted goal stale until the snapshot transaction.
    reopened = Database(goal_db.path)
    first = ChildModelService(reopened).snapshot(CHILD)
    cycle = GoalProgramService(reopened).cycles(CHILD)[0]
    assert cycle["cycle_id"] == baseline["cycle_id"]
    assert cycle["learning_outcome"]["status"] == "independent_observed"
    assert cycle["learning_outcome"]["goal_attainment"] == "not_established"
    rows = _rows(goal_db, "goal_cycle")
    second = ChildModelService(reopened).snapshot(CHILD)
    GoalProgramService(reopened).cycles(CHILD)
    third = ChildModelService(reopened).snapshot(CHILD)

    assert first["snapshot_id"] == second["snapshot_id"] == third["snapshot_id"]
    assert first["evidence_fingerprint"] == third["evidence_fingerprint"]
    assert _rows(goal_db, "goal_cycle") == rows
    assert _rows(goal_db, "goal_cycle", OTHER) == other_before
    assert _rows(goal_db, "diagnostic_attempt") == attempts
    with goal_db.connect() as connection:
        assert connection.execute(
            "SELECT is_current FROM child_model_snapshot WHERE snapshot_id=?", (first["snapshot_id"],)
        ).fetchone()[0] == 1


def test_borrowed_refresh_never_commits_or_rolls_back_the_callers_transaction(goal_db, exact_goal_item):
    goals = GoalProgramService(goal_db)
    goals.cycles(CHILD)
    goals.cycles(OTHER)
    _answer(goal_db, exact_goal_item)
    before = _rows(goal_db, "goal_cycle")
    programs = _rows(goal_db, "goal_program")
    with goal_db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        refreshed = goals.refresh_existing_cycles(connection, CHILD, as_of=datetime.now(timezone.utc))
        assert refreshed[0]["learning_outcome"]["status"] == "independent_observed"
        assert connection.in_transaction
        assert [dict(row) for row in connection.execute("SELECT * FROM goal_cycle WHERE child_id=?", (CHILD,))] != [
            row for row in before if row["child_id"] == CHILD
        ]
        connection.rollback()
    assert _rows(goal_db, "goal_cycle") == before
    assert _rows(goal_db, "goal_program") == programs


def test_borrowed_refresh_requires_a_transaction(goal_db):
    goals = GoalProgramService(goal_db)
    goals.cycles(CHILD)
    with goal_db.connect() as connection:
        with pytest.raises(RuntimeError, match="caller transaction"):
            goals.refresh_existing_cycles(connection, CHILD, as_of=datetime.now(timezone.utc))


def test_snapshot_does_not_seed_goals_or_create_missing_cycles(goal_db):
    goals = GoalProgramService(goal_db)
    goals.ensure_schema()
    programs = _rows(goal_db, "goal_program")
    cycles = _rows(goal_db, "goal_cycle")
    ChildModelService(goal_db).snapshot(CHILD)
    assert _rows(goal_db, "goal_program") == programs == []
    assert _rows(goal_db, "goal_cycle") == cycles == []
    goals.seed_defaults()
    programs = _rows(goal_db, "goal_program")
    ChildModelService(goal_db).snapshot(CHILD)
    assert _rows(goal_db, "goal_program") == programs
    assert _rows(goal_db, "goal_cycle") == []


def test_snapshot_only_refreshes_existing_current_week_cycles(goal_db, exact_goal_item):
    goals = GoalProgramService(goal_db)
    today = family_today()
    goals.cycles(CHILD, reference_date=today - timedelta(days=7))
    goals.cycles(CHILD, reference_date=today + timedelta(days=7))
    before = _rows(goal_db, "goal_cycle")
    _answer(goal_db, exact_goal_item)
    ChildModelService(goal_db).snapshot(CHILD)
    assert _rows(goal_db, "goal_cycle") == before


def test_genuine_new_formal_answer_after_freeze_still_supersedes_the_snapshot(goal_db, exact_goal_item):
    GoalProgramService(goal_db).cycles(CHILD)
    first = ChildModelService(goal_db).snapshot(CHILD)
    _answer(goal_db, exact_goal_item)
    current = ChildModelService(goal_db).snapshot(CHILD)
    assert current["snapshot_id"] != first["snapshot_id"]
    assert current["evidence_fingerprint"] != first["evidence_fingerprint"]
    with goal_db.connect() as connection:
        assert connection.execute(
            "SELECT is_current FROM child_model_snapshot WHERE snapshot_id=?", (first["snapshot_id"],)
        ).fetchone()[0] == 0
