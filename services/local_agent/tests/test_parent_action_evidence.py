from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_parent_coaching_feedback_semantics import CHILD, OTHER, report, repository, session


def service(repo):
    from home_study_agent.parent_action_evidence import ParentActionEvidenceService
    return ParentActionEvidenceService(repo.database)


def submit(repo, saved, **changes):
    snapshot = saved["parent_action"]
    args = dict(executed=True, effectiveness=2, stress_rating=1, notes_summary="Parent observed result",
                action_snapshot_id=snapshot["snapshot_id"], client_action_id=str(uuid4()),
                expected_revision=0, executed_at=snapshot["created_at"])
    args.update(changes)
    return repo.save_parent_report_feedback(saved["report_id"], **args)


def test_report_generation_freezes_content_target_timing_and_version(repository):
    saved = report(repository)
    snapshot = saved["parent_action"]
    assert snapshot["binding_status"] == "bound"
    assert snapshot["target"] == {"actor": "parent", "child_id": CHILD}
    assert snapshot["coaching"] == saved["parent_coaching"]
    assert snapshot["timing"]["when"] == saved["parent_coaching"]["action"]["when"]
    assert snapshot["version"] == 1
    restarted = StudyRepository(Database(repository.database.path))
    assert restarted.get_daily_report(saved["report_id"])["parent_action"] == snapshot
    with repository.database.connect() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE parent_action_snapshot SET version=2")


def test_feedback_is_idempotent_revisable_and_retractable_without_erasing_history(repository):
    saved = report(repository)
    first = submit(repository, saved, client_action_id="first")
    assert submit(repository, saved, client_action_id="first") == first
    assert first["revision"] == 1 and first["binding_status"] == "bound"
    assert first["report_id"] == saved["report_id"]
    assert first["executed_at"] == saved["parent_action"]["created_at"]
    assert service(repository).history(CHILD, saved["parent_action"]["snapshot_id"]) == [first]
    second = submit(repository, saved, expected_revision=1, client_action_id="correct", effectiveness=4)
    assert second["revision"] == 2
    restored = StudyRepository(Database(repository.database.path))
    evidence = restored.report_evidence(session(restored))
    assert evidence["coaching_feedback"][0]["effectiveness"] == 4
    revoked = submit(restored, saved, expected_revision=2, client_action_id="retract", operation="retract",
                     reason="Wrong recollection", executed=None, executed_at=None, effectiveness=None, stress_rating=None)
    assert revoked["operation"] == "retract" and revoked["revision"] == 3
    assert revoked["report_id"] == saved["report_id"] and revoked["executed_at"] is None
    assert restored.report_evidence(session(restored))["coaching_feedback"] == []
    assert service(restored).history(CHILD, saved["parent_action"]["snapshot_id"]) == [first, second, revoked]


def test_unknown_legacy_feedback_is_preserved_but_never_backfilled(repository):
    saved = report(repository)
    receipt = repository.save_parent_report_feedback(saved["report_id"], True, 5, 1, "Old report-only feedback")
    assert receipt["binding_status"] == "unknown"
    evidence = repository.report_evidence(session(repository))
    assert evidence["coaching_feedback"] == []
    assert evidence["legacy_coaching_feedback"][0]["binding_status"] == "unknown"
    _, coaching = build_daily_report(evidence)
    assert coaching["adaptation"]["average_effectiveness"] is None
    assert service(repository).history(CHILD, saved["parent_action"]["snapshot_id"]) == []


def test_legacy_write_cannot_impersonate_an_existing_bound_receipt(repository):
    saved = report(repository)
    bound = submit(repository, saved)
    legacy = repository.save_parent_report_feedback(saved["report_id"], True, 5, 5, "Unbound report feedback")
    assert legacy["binding_status"] == "unknown" and legacy["feedback_id"] != bound["feedback_id"]
    current = service(repository).get(CHILD, saved["parent_action"]["snapshot_id"])
    assert current["feedback"] == bound


def test_report_get_and_history_preserve_explicit_execution_time_separate_from_recording_time(repository, monkeypatch):
    from home_study_agent import parent_action_evidence
    saved = report(repository)
    executed_at = saved["parent_action"]["created_at"]
    recorded_at = datetime.fromisoformat(executed_at) + timedelta(hours=2)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return recorded_at.astimezone(tz) if tz else recorded_at.replace(tzinfo=None)
    monkeypatch.setattr(parent_action_evidence, "datetime", Clock)
    receipt = submit(repository, saved)
    assert receipt["executed_at"] == executed_at
    assert receipt["created_at"] == recorded_at.isoformat()
    restarted = StudyRepository(Database(repository.database.path))
    assert restarted.get_daily_report(saved["report_id"])["feedback"] == receipt
    assert service(restarted).history(CHILD, saved["parent_action"]["snapshot_id"]) == [receipt]


def test_missing_historical_snapshot_is_not_created_by_read_or_feedback(repository):
    saved = report(repository)
    with repository.database.connect() as c:
        c.execute("DELETE FROM parent_action_snapshot WHERE report_id=?", (saved["report_id"],))
        c.commit()
    restarted = StudyRepository(Database(repository.database.path))
    assert restarted.get_daily_report(saved["report_id"])["parent_action"]["binding_status"] == "unknown"
    restarted.save_parent_report_feedback(saved["report_id"], True, 5, 1, "Cannot identify old advice")
    with repository.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM parent_action_snapshot").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [
    {"expected_revision": 1}, {"expected_revision": True}, {"executed": "true"},
    {"executed_at": None}, {"executed_at": "2020-01-01"},
    {"executed_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    {"executed": False}, {"effectiveness": True}, {"effectiveness": 6},
    {"operation": "retract", "reason": ""},
])
def test_invalid_or_stale_feedback_is_rejected_atomically(repository, changes):
    saved = report(repository)
    with pytest.raises(ValueError):
        submit(repository, saved, **changes)
    assert service(repository).history(CHILD, saved["parent_action"]["snapshot_id"]) == []


def test_key_reuse_does_not_change_payload_and_cross_report_or_child_binding_is_rejected(repository):
    saved = report(repository)
    submit(repository, saved, client_action_id="same-key")
    with pytest.raises(ValueError):
        submit(repository, saved, client_action_id="same-key", effectiveness=4)
    other = report(repository, child=OTHER)
    with pytest.raises(ValueError):
        submit(repository, {**saved, "report_id": other["report_id"]})
    same_child = report(repository)
    with pytest.raises(ValueError):
        submit(repository, {**saved, "report_id": same_child["report_id"]})


def test_concurrent_revisions_have_one_winner(repository):
    saved = report(repository)
    def write(value):
        try:
            return submit(StudyRepository(Database(repository.database.path)), saved, effectiveness=value)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [2, 4]))
    assert sum(item is not None for item in results) == 1
    assert len(service(repository).history(CHILD, saved["parent_action"]["snapshot_id"])) == 1


def test_concurrent_identical_commands_replay_one_durable_event(repository):
    saved = report(repository)
    def write(_):
        return submit(StudyRepository(Database(repository.database.path)), saved, client_action_id="shared-command")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [1, 2]))
    assert results[0] == results[1]
    assert service(repository).history(CHILD, saved["parent_action"]["snapshot_id"]) == [results[0]]


def test_replaying_an_old_receipt_cannot_restore_retracted_effects(repository):
    saved = report(repository)
    first = submit(repository, saved, client_action_id="original-command")
    submit(repository, saved, expected_revision=1, operation="retract", reason="Withdraw result",
           executed=None, executed_at=None, effectiveness=None, stress_rating=None)
    assert submit(repository, saved, client_action_id="original-command") == first
    assert repository.report_evidence(session(repository))["coaching_feedback"] == []


def test_report_and_action_snapshot_commit_atomically(repository, monkeypatch):
    from home_study_agent.parent_action_evidence import ParentActionEvidenceService
    def fail(*args):
        raise ValueError("isolated freeze failure")
    monkeypatch.setattr(ParentActionEvidenceService, "freeze", fail)
    with pytest.raises(ValueError, match="isolated freeze failure"):
        report(repository)
    with repository.database.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM daily_report").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM parent_action_snapshot").fetchone()[0] == 0


def test_report_rebuild_creates_new_action_but_feedback_remains_bound_to_seen_version(repository):
    saved = report(repository)
    old = saved["parent_action"]
    with repository.database.connect() as c:
        c.execute("UPDATE daily_reflection SET next_action='A newly corrected action' WHERE session_id=?", (saved["session_id"],))
        c.execute("UPDATE daily_report SET evidence_json='{}' WHERE report_id=?", (saved["report_id"],))
        c.commit()
    rebuilt = repository.get_daily_report(saved["report_id"])
    assert rebuilt["parent_action"]["snapshot_id"] != old["snapshot_id"]
    assert rebuilt["parent_action"]["version"] == 2
    assert rebuilt["parent_action"]["coaching"]["action"]["what"] == "A newly corrected action"
    receipt = submit(repository, saved)
    assert receipt["action_snapshot_id"] == old["snapshot_id"]
    assert service(repository).get(CHILD, old["snapshot_id"])["coaching"] == old["coaching"]
    assert service(repository).history(CHILD, rebuilt["parent_action"]["snapshot_id"]) == []


def test_explicit_not_executed_never_contributes_effects(repository):
    saved = report(repository)
    submit(repository, saved, executed=False, executed_at=None, effectiveness=None, stress_rating=None)
    _, coaching = build_daily_report(repository.report_evidence(session(repository)))
    assert coaching["adaptation"]["average_effectiveness"] is None
    assert coaching["adaptation"]["feedback_count"] == 0


@pytest.mark.parametrize("delete_from", ["parent_action_snapshot", "daily_report"])
def test_authorized_hard_delete_cascades_all_revisions_and_preserves_other_child(repository, delete_from):
    saved = report(repository)
    submit(repository, saved)
    submit(repository, saved, expected_revision=1, effectiveness=4)
    foreign = report(repository, child=OTHER)
    foreign_feedback = submit(repository, foreign)
    with repository.database.connect() as c:
        c.execute(f"DELETE FROM {delete_from} WHERE report_id=?", (saved["report_id"],))
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM parent_action_snapshot WHERE child_id=?", (CHILD,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM parent_action_feedback_event WHERE child_id=?", (CHILD,)).fetchone()[0] == 0
    assert service(repository).history(OTHER, foreign["parent_action"]["snapshot_id"]) == [foreign_feedback]


def test_upgrade_removes_own_old_delete_guards_but_keeps_update_guards(repository):
    saved = report(repository)
    submit(repository, saved)
    with repository.database.connect() as c:
        for table in ("parent_action_snapshot", "parent_action_feedback_event"):
            c.execute(f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'old guard'); END")
        c.commit()
    restarted = service(StudyRepository(Database(repository.database.path)))
    restarted.initialize()
    restarted.initialize()
    with repository.database.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'parent_action%no_delete'").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE parent_action_feedback_event SET revision=8")
        c.execute("DELETE FROM parent_action_snapshot WHERE child_id=?", (CHILD,))
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM parent_action_feedback_event").fetchone()[0] == 0
