from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.exam_simulation import ExamSimulationError, ExamSimulationService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository


def _database(path: Path) -> tuple[Database, str]:
    database = Database(path)
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    session_id = "exam-session-primary"
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO daily_session(
                 session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
               ) VALUES(?, 'child-primary-4','active','study',datetime('now'),datetime('now'),'exam-session-action')""",
            (session_id,),
        )
        connection.commit()
    return database, session_id


def _fill_pet_blueprint(database: Database) -> None:
    service = ExamSimulationService(database)
    blueprint = service.blueprints("pet")[0]
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT di.diagnostic_item_id,di.knowledge_node_id,kn.subject,kn.title
               FROM diagnostic_item di JOIN knowledge_node kn
                 ON kn.knowledge_node_id=di.knowledge_node_id
               WHERE di.status='active' AND kn.status='active' AND kn.track='pet'"""
        ).fetchall()
        by_domain: dict[str, list[dict]] = {}
        for raw_row in rows:
            row = dict(raw_row)
            domain_id = service.specialized._mastery_domain("pet", row)
            if domain_id:
                by_domain.setdefault(domain_id, []).append(row)
        for section in blueprint["sections"]:
            prefixes = tuple(section["domain_prefixes"])
            seeds = [
                item for domain_id, items in sorted(by_domain.items())
                if domain_id.startswith(prefixes) for item in items[:1]
            ]
            assert seeds, section["section_id"]
            existing = sum(
                len(items) for domain_id, items in by_domain.items()
                if domain_id.startswith(prefixes)
            )
            for index in range(existing, int(section["item_target"])):
                seed = seeds[index % len(seeds)]
                item_id = f"pet-blueprint-{section['section_id']}-{index:03d}"
                connection.execute(
                    """INSERT INTO diagnostic_item(
                         diagnostic_item_id,knowledge_node_id,prompt,answer_type,
                         expected_answer_json,explanation,assessment_mode,difficulty,source_ref,status
                       ) SELECT ?,knowledge_node_id,prompt || ?,answer_type,expected_answer_json,
                                explanation,assessment_mode,difficulty,'test-blueprint-capacity','active'
                         FROM diagnostic_item WHERE diagnostic_item_id=?""",
                    (item_id, f" [卷型变体{index}]", seed["diagnostic_item_id"]),
                )
        connection.commit()


def test_full_competition_paper_is_domain_balanced_durable_and_idempotent(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "exam.db")
    service = ExamSimulationService(database)

    run = service.start(
        session_id, "competition_math", "数学", "full", 10, 45, "exam-start-competition-001"
    )

    assert run["status"] == "active"
    assert run["total_items"] == 10
    assert run["current_item"]["prompt"]
    with database.connect() as connection:
        domains = connection.execute(
            "SELECT COUNT(DISTINCT domain_id) FROM specialized_exam_item WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()[0]
    assert domains == 10
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(DISTINCT diagnostic_item_id) FROM specialized_exam_item WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()[0] == run["total_items"]
    assert service.start(
        session_id, "competition_math", "数学", "full", 10, 45, "exam-start-competition-001"
    )["run_id"] == run["run_id"]

    first_item = run["current_item"]
    answered = service.submit_answer(run["run_id"], "我不确定", "exam-answer-competition-001")
    replay = service.submit_answer(run["run_id"], "我不确定", "exam-answer-competition-001")
    assert answered["exam"]["answered_items"] == 1
    assert replay["attempt"]["attempt_id"] == answered["attempt"]["attempt_id"]
    assert replay["exam"]["answered_items"] == 1
    assert replay["exam"]["current_item"]["diagnostic_item_id"] != first_item["diagnostic_item_id"]

    paused = service.pause(run["run_id"])
    assert paused["status"] == "paused"
    reopened = ExamSimulationService(Database(database.path)).active_for_session(session_id)
    assert reopened["status"] == "paused" and reopened["answered_items"] == 1
    resumed = ExamSimulationService(Database(database.path)).resume(run["run_id"])
    assert resumed["status"] == "active"


@pytest.mark.parametrize("terminal", ["paused", "completed", "cancelled"])
def test_answer_replay_uses_original_slot_after_restart_and_state_change(tmp_path, terminal):
    database, session = _database(tmp_path / "replay-slot.db")
    service = ExamSimulationService(database)
    run = service.start(session, "competition_math", "数学", "practice", 2, 30, "replay-slot-start")
    first = service.submit_answer(run["run_id"], "我不确定", "replay-original-answer")
    if terminal == "paused":
        service.pause(run["run_id"])
    elif terminal == "cancelled":
        service.cancel(run["run_id"])
    else:
        service.submit_answer(run["run_id"], "仍需核实", "replay-second-answer")
    restarted = ExamSimulationService(Database(database.path))
    replay = restarted.submit_answer(run["run_id"], "我不确定", "replay-original-answer")
    assert replay["attempt"] == first["attempt"]
    assert replay["exam"]["status"] == terminal
    assert replay["exam"]["answered_items"] == (2 if terminal == "completed" else 1)
    with pytest.raises(ExamSimulationError):
        restarted.submit_answer(run["run_id"], "另一个答案", "replay-original-answer")


def test_legacy_completed_answer_replay_preserves_existing_receipt(tmp_path):
    database, session = _database(tmp_path / "legacy-replay.db")
    service = ExamSimulationService(database)
    run = service.start(session, "competition_math", "数学", "practice", 2, 30, "legacy-exam-start")
    first = service.submit_answer(run["run_id"], "我不确定", "legacy-exam-answer")
    with database.connect() as connection:
        connection.execute("UPDATE specialized_exam_item SET answer_action_id=NULL,answer_hash=NULL WHERE run_id=?", (run["run_id"],))
        connection.commit()
    replay = ExamSimulationService(Database(database.path)).submit_answer(run["run_id"], "我不确定", "legacy-exam-answer")
    assert replay["attempt"] == first["attempt"]
    assert replay["exam"]["answered_items"] == 1


@pytest.mark.parametrize("answer,action", [
    ("", "empty-exam-answer"),
    ("   ", "blank-exam-answer"),
    ("\t\n", "whitespace-exam-answer"),
    ("我不确定", ""),
    ("我不确定", "   "),
])
def test_invalid_answer_input_has_no_status_or_reservation_side_effects(tmp_path, monkeypatch, answer, action):
    database, session = _database(tmp_path / "invalid-exam-input.db")
    service = ExamSimulationService(database)
    run = service.start(session, "competition_math", "数学", "practice", 2, 30, "invalid-input-exam-start")
    status_calls = []
    status = service.status

    def record_status(run_id):
        status_calls.append(run_id)
        return status(run_id)

    monkeypatch.setattr(service, "status", record_status)
    with pytest.raises(ValueError):
        service.submit_answer(run["run_id"], answer, action)
    assert status_calls == []
    with database.connect() as connection:
        slots = connection.execute(
            "SELECT answer_action_id,answer_hash,attempt_id FROM specialized_exam_item WHERE run_id=?",
            (run["run_id"],),
        ).fetchall()
        assert all(tuple(slot) == (None, None, None) for slot in slots)
        assert connection.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == 0
    valid = service.submit_answer(run["run_id"], "我不确定", "valid-after-invalid-answer")
    assert valid["exam"]["answered_items"] == 1
    assert valid["attempt"]["diagnostic_item_id"] == run["current_item"]["diagnostic_item_id"]


def test_diagnostic_commit_before_exam_commit_recovers_bound_slot(tmp_path, monkeypatch):
    database, session = _database(tmp_path / "exam-crash.db")
    service = ExamSimulationService(database)
    run = service.start(session, "competition_math", "数学", "practice", 2, 30, "crash-exam-start")
    submit = service.learning.submit_diagnostic

    def crash_after_receipt(*args, **kwargs):
        submit(*args, **kwargs)
        raise OSError("synthetic crash after diagnostic commit")

    with monkeypatch.context() as patch:
        patch.setattr(service.learning, "submit_diagnostic", crash_after_receipt)
        with pytest.raises(OSError, match="synthetic crash"):
            service.submit_answer(run["run_id"], "我不确定", "crash-exam-answer")
    with database.connect() as connection:
        slot = connection.execute("SELECT * FROM specialized_exam_item WHERE run_id=? AND position=1", (run["run_id"],)).fetchone()
        assert slot["answer_action_id"] == "crash-exam-answer" and slot["attempt_id"] is None
        original = connection.execute("SELECT attempt_id FROM diagnostic_attempt WHERE client_action_id='crash-exam-answer'").fetchone()[0]
    restarted = ExamSimulationService(Database(database.path))
    restarted.recover_after_restart()
    restarted.resume(run["run_id"])
    with pytest.raises(ExamSimulationError):
        restarted.submit_answer(run["run_id"], "我不确定", "crash-different-action")
    recovered = restarted.submit_answer(run["run_id"], "我不确定", "crash-exam-answer")
    assert recovered["attempt"]["attempt_id"] == original
    assert recovered["exam"]["answered_items"] == 1
    assert recovered["attempt"]["diagnostic_item_id"] == run["current_item"]["diagnostic_item_id"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1


def test_actions_cannot_be_reused_across_runs_children_or_standalone_diagnostics(tmp_path):
    database, session = _database(tmp_path / "exam-action-owner.db")
    service = ExamSimulationService(database)
    run = service.start(session, "competition_math", "数学", "practice", 2, 30, "owner-exam-start")
    first = service.submit_answer(run["run_id"], "我不确定", "owned-answer-action")
    service.cancel(run["run_id"])
    second = service.start(session, "competition_math", "数学", "practice", 2, 30, "second-exam-start")
    with pytest.raises(ExamSimulationError):
        service.submit_answer(second["run_id"], "我不确定", "owned-answer-action")
    service.learning.submit_diagnostic(session, second["current_item"]["diagnostic_item_id"], "我不确定", "standalone-answer-action")
    with pytest.raises(ExamSimulationError):
        service.submit_answer(second["run_id"], "我不确定", "standalone-answer-action")
    service.cancel(second["run_id"])
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (session,))
        connection.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('owner-other-session','child-junior-3','active','study',datetime('now'),datetime('now'),'owner-other-session')""")
        connection.commit()
    with pytest.raises(ExamSimulationError):
        service.start("owner-other-session", "junior_exam", "数学", "practice", 2, 30, "owner-exam-start")
    other = service.start("owner-other-session", "junior_exam", "数学", "practice", 2, 30, "other-exam-start")
    with pytest.raises(ExamSimulationError):
        service.submit_answer(other["run_id"], "我不确定", "owned-answer-action")
    assert service.status(second["run_id"])["answered_items"] == 0
    assert service.status(other["run_id"])["answered_items"] == 0
    assert service.submit_answer(run["run_id"], "我不确定", "owned-answer-action")["attempt"] == first["attempt"]


@pytest.mark.parametrize("same_action", [True, False])
def test_concurrent_submissions_cannot_move_a_receipt_to_the_next_slot(tmp_path, monkeypatch, same_action):
    database, session = _database(tmp_path / "exam-concurrent.db")
    service = ExamSimulationService(database)
    run = service.start(session, "competition_math", "数学", "practice", 2, 30, "concurrent-exam-start")
    entered = threading.Event()
    release = threading.Event()
    both_entered = threading.Barrier(2) if same_action else None
    submit = service.learning.submit_diagnostic

    def blocked(*args, **kwargs):
        entered.set()
        if both_entered:
            both_entered.wait(timeout=10)
        assert release.wait(10)
        return submit(*args, **kwargs)

    monkeypatch.setattr(service.learning, "submit_diagnostic", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.submit_answer, run["run_id"], "我不确定", "concurrent-answer")
        assert entered.wait(10)
        try:
            if same_action:
                second = pool.submit(service.submit_answer, run["run_id"], "我不确定", "concurrent-answer")
            else:
                with pytest.raises(ExamSimulationError):
                    service.submit_answer(run["run_id"], "我不确定", "concurrent-other-answer")
        finally:
            release.set()
        result = first.result(timeout=15)
        if same_action:
            assert second.result(timeout=15)["attempt"]["attempt_id"] == result["attempt"]["attempt_id"]
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM specialized_exam_item WHERE run_id=? ORDER BY position", (run["run_id"],)).fetchall()
        assert rows[0]["attempt_id"] == result["attempt"]["attempt_id"]
        assert rows[1]["attempt_id"] is None and rows[1]["answer_action_id"] is None
        assert connection.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1


def test_distinct_items_sharing_a_source_reference_keep_distinct_slot_receipts(tmp_path):
    database, session = _database(tmp_path / "exam-shared-source.db")
    service = ExamSimulationService(database)
    run = service.start(session, "competition_math", "数学", "practice", 2, 30, "shared-source-exam-start")
    with database.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET source_ref='shared-test-bank' WHERE diagnostic_item_id IN (SELECT diagnostic_item_id FROM specialized_exam_item WHERE run_id=?)", (run["run_id"],))
        connection.commit()
    first = service.submit_answer(run["run_id"], "我不确定", "shared-source-answer-1")
    second = service.submit_answer(run["run_id"], "我不确定", "shared-source-answer-2")
    assert first["attempt"]["diagnostic_item_id"] != second["attempt"]["diagnostic_item_id"]
    assert first["attempt"]["attempt_id"] != second["attempt"]["attempt_id"]
    assert second["exam"]["answered_items"] == 2


def test_coverage_papers_expand_to_every_required_domain_and_expose_subject_sections(tmp_path: Path,monkeypatch) -> None:
    database, primary_session = _database(tmp_path / "exam-complete-coverage.db")
    service = ExamSimulationService(database)

    with pytest.raises(ExamSimulationError,match='EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED'):
        service.start(primary_session,'pet','英语','coverage',2,60,'exam-pet-no-typed-playback')
    # Inspect a historical structure without presenting it as delivered audio.
    with monkeypatch.context() as legacy:
        legacy.setattr(service,'_listening_item',lambda *args:False)
        pet = service.start(primary_session,'pet','英语','coverage',2,60,'exam-full-pet-coverage')
    assert service.status(pet['run_id'])['delivery_status']=='blocked'
    assert pet["total_items"] == 17
    assert pet["covered_domain_count"] == 17
    assert len(pet["sections"]) == 1
    assert {
        key: pet["sections"][0][key]
        for key in ("section_id", "subject", "total_items", "answered_items", "covered_domains", "status")
    } == {
        "section_id": "domain_coverage", "subject": "英语", "total_items": 17,
        "answered_items": 0, "covered_domains": 17, "status": "active",
    }
    assert pet["current_section"]["title"] == "能力域覆盖"
    service.cancel(pet["run_id"])

    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET status='completed',state='completed',ended_at=datetime('now') WHERE session_id=?",
            (primary_session,),
        )
        connection.execute(
            """INSERT INTO daily_session(
                 session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
               ) VALUES('exam-session-junior','child-junior-3','active','study',
                 datetime('now'),datetime('now'),'exam-junior-session-action')"""
        )
        connection.commit()
    with pytest.raises(ExamSimulationError,match='EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED'):
        service.start('exam-session-junior','junior_exam','','coverage',2,120,'exam-junior-no-typed-playback')
    with monkeypatch.context() as legacy:
        legacy.setattr(service,'_listening_item',lambda *args:False)
        junior = service.start('exam-session-junior','junior_exam','','coverage',2,120,'exam-full-junior-coverage')
    assert service.status(junior['run_id'])['delivery_status']=='blocked'
    assert junior["total_items"] == 35
    assert junior["covered_domain_count"] == 35
    assert {section["subject"] for section in junior["sections"]} == {
        "语文", "数学", "英语", "物理", "化学", "道德与法治", "历史",
    }
    assert sum(section["total_items"] for section in junior["sections"]) == 35


def test_versioned_blueprints_are_exposed_and_pet_full_refuses_fake_structure(tmp_path: Path, monkeypatch) -> None:
    database, session_id = _database(tmp_path / "exam-blueprint.db")
    service = ExamSimulationService(database)

    blueprints = service.blueprints("pet")
    assert len(blueprints) == 1
    pet = blueprints[0]
    assert pet["authority"] == "Cambridge University Press & Assessment"
    assert pet["blueprint_id"] == "cambridge-b1-preliminary-for-schools-current-v2"
    assert pet["version"] == "current-v2"
    assert [(section["section_id"], section["item_target"]) for section in pet["sections"]] == [
        ("reading", 32), ("writing", 2), ("listening", 25), ("speaking", 4),
    ]
    speaking = next(section for section in pet["sections"] if section["section_id"] == "speaking")
    assert speaking["duration_minutes"] == 12
    assert speaking["operational_duration_basis"] == "pair_upper_bound"
    assert speaking["duration_variants"] == [
        {"candidate_count": 2, "min_minutes": 10, "max_minutes": 12, "label": "两人组"},
        {"candidate_count": 3, "min_minutes": 15, "max_minutes": 17, "label": "三人组"},
    ]

    with pytest.raises(ExamSimulationError, match='EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED'):
        service.start(session_id, "pet", "英语", "full", 2, 30, "exam-pet-strict-source")
    assert service.active_for_session(session_id) is None
    try:
        # Inspect blueprint counts independently of the new original-audio source gate.
        with monkeypatch.context() as legacy:
            legacy.setattr(service, '_listening_item', lambda *args: False)
            service.start(session_id, "pet", "英语", "full", 2, 30, "exam-pet-strict-blueprint")
    except ExamSimulationError as error:
        message = str(error)
        assert "Reading需32题" in message
        assert "Listening需25题" in message
        assert "能力域覆盖测评" in message
    else:
        raise AssertionError("题库不满足官方卷型时不得生成伪整卷")


def test_nanjing_full_paper_persists_2026_blueprint_snapshot_and_reference_boundary(tmp_path: Path) -> None:
    database, primary_session = _database(tmp_path / "exam-nanjing-blueprint.db")
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET status='completed',state='completed',ended_at=datetime('now') WHERE session_id=?",
            (primary_session,),
        )
        connection.execute(
            """INSERT INTO daily_session(
                 session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
               ) VALUES('exam-blueprint-junior','child-junior-3','active','study',
                 datetime('now'),datetime('now'),'exam-blueprint-junior-action')"""
        )
        connection.commit()
    service = ExamSimulationService(database)
    run = service.start(
        "exam-blueprint-junior", "junior_exam", "数学", "full", 6, 30,
        "exam-nanjing-math-full",
    )

    assert run["blueprint_id"] == "nanjing-2026-junior-exam-数学"
    assert run["blueprint_version"] == "nanjing-2026"
    assert run["time_limit_seconds"] == 120 * 60
    assert run["blueprint"]["sections"][0]["max_score"] == 120
    assert "参考分" in run["blueprint"]["score_boundary"]
    reopened = ExamSimulationService(Database(database.path)).status(run["run_id"])
    assert reopened["blueprint"] == run["blueprint"]


def test_restart_pauses_exam_and_timeout_records_partial_domain_evidence(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "exam-timeout.db")
    service = ExamSimulationService(database)
    run = service.start(
        session_id, "competition_math", "数学", "practice", 4, 5, "exam-start-clock-practice-001"
    )
    service.submit_answer(run["run_id"], "I do not know yet", "exam-answer-pet-practice-001")

    with database.connect() as connection:
        connection.execute(
            "UPDATE specialized_exam_run SET active_since=datetime('now','-10 minutes') WHERE run_id=?",
            (run["run_id"],),
        )
        connection.execute(
            """UPDATE specialized_exam_section_state
               SET active_since=datetime('now','-10 minutes')
               WHERE run_id=? AND status='active'""",
            (run["run_id"],),
        )
        connection.commit()
    timed_out = service.status(run["run_id"])

    assert timed_out["status"] == "timed_out"
    assert timed_out["result"]["answered_items"] == 1
    assert timed_out["result"]["total_items"] == 4
    assert timed_out["result"]["domain_results"] == {}
    assert timed_out["result"]["score"] is None
    assert timed_out["result"]["score_status"] == 'needs_review'
    roadmap = service.specialized.roadmap("child-primary-4", "competition_math")
    assert roadmap["whole_exam_analysis"]["latest_exam"] is None

    second = service.start(
        session_id, "competition_math", "数学", "practice", 4, 5, "exam-start-clock-practice-002"
    )
    assert service.recover_after_restart() == 1
    assert ExamSimulationService(Database(database.path)).status(second["run_id"])["status"] == "paused"


def test_legacy_pet_full_keeps_independent_section_clocks_without_claiming_audio_delivery(tmp_path: Path,monkeypatch) -> None:
    database, session_id = _database(tmp_path / "exam-pet-section-clocks.db")
    _fill_pet_blueprint(database)
    service = ExamSimulationService(database)
    with pytest.raises(ExamSimulationError,match='EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED'):
        service.start(session_id,'pet','英语','full',63,132,'exam-pet-missing-typed-playback')
    with monkeypatch.context() as legacy:
        legacy.setattr(service,'_listening_item',lambda *args:False)
        run = service.start(session_id,'pet','英语','full',63,132,'exam-pet-section-clocks')
    assert service.status(run['run_id'])['delivery_status']=='blocked'
    assert run["total_items"] == 63
    assert run["time_limit_seconds"] == 132 * 60
    assert run["current_section"]["section_id"] == "reading"
    assert run["current_section"]["time_limit_seconds"] == 45 * 60

    expected_transitions = [
        ("reading", "-46 minutes", "writing", 33),
        ("writing", "-46 minutes", "listening", 35),
        ("listening", "-31 minutes", "speaking", 60),
    ]
    for old_section, offset, next_section, next_position in expected_transitions:
        with database.connect() as connection:
            connection.execute(
                "UPDATE specialized_exam_run SET active_since=datetime('now',?) WHERE run_id=?",
                (offset, run["run_id"]),
            )
            connection.execute(
                """UPDATE specialized_exam_section_state SET active_since=datetime('now',?)
                   WHERE run_id=? AND section_id=?""",
                (offset, run["run_id"], old_section),
            )
            connection.commit()
        advanced = service.status(run["run_id"])
        assert advanced["status"] == "active"
        assert advanced["current_section"]["section_id"] == next_section
        assert advanced["current_position"] == next_position
        assert next(item for item in advanced["sections"] if item["section_id"] == old_section)["status"] == "timed_out"

    with database.connect() as connection:
        connection.execute(
            "UPDATE specialized_exam_run SET active_since=datetime('now','-13 minutes') WHERE run_id=?",
            (run["run_id"],),
        )
        connection.execute(
            """UPDATE specialized_exam_section_state SET active_since=datetime('now','-13 minutes')
               WHERE run_id=? AND section_id='speaking'""",
            (run["run_id"],),
        )
        connection.commit()
    finished = service.status(run["run_id"])
    assert finished["status"] == "timed_out"
    assert finished["answered_items"] == 0
    assert finished["result"]["total_items"] == 63
    assert finished['result']['score'] is None and finished['result']['score_status']=='needs_review'
    assert 'weighted_reference_percentage' not in finished['result']
    assert set(finished["result"]["section_results"]) == {
        "reading", "writing", "listening", "speaking",
    }
    assert all(item["status"] == "timed_out" for item in finished["sections"])


def test_full_paper_refuses_uncovered_domains_instead_of_claiming_completeness(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "exam-gap.db")
    with database.connect() as connection:
        connection.execute("DELETE FROM diagnostic_item WHERE diagnostic_item_id LIKE 'diag-oly-logic%'")
        connection.commit()

    try:
        ExamSimulationService(database).start(
            session_id, "competition_math", "数学", "full", 10, 45, "exam-start-gap-001"
        )
    except ExamSimulationError as error:
        assert "competition_math.logic" in str(error)
    else:
        raise AssertionError("能力域缺失时不得伪装成整卷")


def test_exam_simulation_http_flow_is_reachable_from_the_product_app(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "exam-api.db",
        data_encryption_key=b"e" * 32,
        maintenance_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        blueprints = client.get("/v1/exam-blueprints", params={"track": "junior_exam"})
        assert blueprints.status_code == 200
        assert {item["subject"] for item in blueprints.json()["blueprints"]} == {
            "语文", "数学", "英语", "物理", "化学", "道德与法治", "历史",
        }
        database = Database(settings.database_path)
        with database.connect() as connection:
            connection.execute(
                """INSERT INTO daily_session(
                     session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
                   ) VALUES('exam-api-session','child-junior-3','active','study',datetime('now'),datetime('now'),'exam-api-session-action')"""
            )
            connection.commit()
        started = client.post(
            "/v1/sessions/exam-api-session/exam-simulations",
            json={
                "track": "junior_exam", "subject": "数学", "mode": "full",
                "requested_items": 6, "time_limit_minutes": 60,
                "client_action_id": "exam-api-start-001",
            },
        )
        assert started.status_code == 200
        run = started.json()
        assert run["total_items"] == 6 and run["current_item"]["prompt"]
        active = client.get("/v1/sessions/exam-api-session/exam-simulations/active")
        assert active.status_code == 200 and active.json()["exam"]["run_id"] == run["run_id"]
        paused = client.post(
            f"/v1/exam-simulations/{run['run_id']}/control", json={"command": "pause"}
        )
        assert paused.status_code == 200 and paused.json()["status"] == "paused"


def test_voice_pet_full_readiness_gap_is_spoken_without_starting_a_fake_exam(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "exam-voice-readiness.db",
        data_encryption_key=b"r" * 32,
        maintenance_enabled=False,
        agent_runtime_enabled=True,
    )
    session_id = "exam-voice-readiness-session"
    with TestClient(create_app(settings)) as client:
        database = Database(settings.database_path)
        with database.connect() as connection:
            connection.execute(
                """INSERT INTO daily_session(
                     session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
                   ) VALUES(?, 'child-primary-4','active','study',datetime('now'),datetime('now'),
                     'exam-voice-readiness-session-action')""",
                (session_id,),
            )
            connection.commit()
        turn = client.post(
            f"/v1/sessions/{session_id}/agent-turn",
            json={"transcript": "开始PET模拟卷", "client_action_id": "exam-pet-readiness-turn"},
        ).json()
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code == 200
        execution = executed.json()["execution"]
        assert execution["result"]["status"] == "not_ready"
        assert "原播放要求" in execution["response_text"]
        assert "没有开始计时" in execution["response_text"]
        assert ExamSimulationService(database).active_for_session(session_id) is None


def test_exam_history_is_exported_and_deleted_with_learning_scope(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "exam-lifecycle.db")
    exam = ExamSimulationService(database).start(
        session_id, "competition_math", "数学", "practice", 4, 10, "exam-lifecycle-start"
    )
    ExamSimulationService(database).submit_answer(
        exam["run_id"], "I am not sure", "exam-lifecycle-answer"
    )
    lifecycle = PrivacyLifecycleService(database, tmp_path)
    lifecycle.initialize()

    export = lifecycle.export_child("child-primary-4")
    payload = json.loads(Path(export["path"]).read_text(encoding="utf-8"))
    preview = lifecycle.deletion_preview("child-primary-4")
    deleted = lifecycle.delete_child_scope(
        "child-primary-4", "learning", "确认删除", "exam-lifecycle-delete"
    )

    assert len(payload["specialized_exam_runs"]) == 1
    assert len(payload["specialized_exam_items"]) == 4
    assert len(payload["specialized_exam_sections"]) == 1
    assert preview["specialized_exams"] == 1
    assert deleted["status"] == "completed"
    assert lifecycle.deletion_preview("child-primary-4")["specialized_exams"] == 0


def test_voice_led_exam_owns_dialogue_and_recovers_after_app_restart(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "exam-voice.db",
        data_encryption_key=b"v" * 32,
        maintenance_enabled=False,
        agent_runtime_enabled=True,
    )
    session_id = "exam-voice-session"

    def speak_and_execute(client: TestClient, text: str, action_id: str) -> dict:
        turn = client.post(
            f"/v1/sessions/{session_id}/agent-turn",
            json={"transcript": text, "client_action_id": action_id},
        )
        assert turn.status_code == 200, turn.text
        payload = turn.json()
        assert payload["runtime_execution_location"] == "server", payload
        executed = client.post(f"/v1/agent-turns/{payload['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        return executed.json()["execution"]

    with TestClient(create_app(settings)) as client:
        database = Database(settings.database_path)
        with database.connect() as connection:
            connection.execute(
                """INSERT INTO daily_session(
                     session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id
                   ) VALUES(?, 'child-junior-3','active','study',datetime('now'),datetime('now'),'exam-voice-session-action')""",
                (session_id,),
            )
            connection.commit()
        started = speak_and_execute(client, "开始数学模拟卷", "exam-voice-start-action")
        assert started["capability"] == "exam.start"
        assert started["result"]["status"] == "active"
        first = speak_and_execute(client, "我选A", "exam-voice-answer-01")
        assert first["capability"] == "exam.submit"
        assert first["result"]["exam"]["answered_items"] == 1

    with TestClient(create_app(settings)) as restarted:
        paused = restarted.get(f"/v1/sessions/{session_id}/exam-simulations/active").json()["exam"]
        assert paused["status"] == "paused"
        ignored = restarted.post(
            f"/v1/sessions/{session_id}/agent-turn",
            json={"transcript": "我刚才喝了一口水", "client_action_id": "exam-voice-paused-talk"},
        ).json()
        assert ignored["intent"] == "supportive_chat"
        assert ignored["action"]["type"] == "none"
        still_paused = restarted.get(f"/v1/sessions/{session_id}/exam-simulations/active").json()["exam"]
        assert still_paused["status"] == "paused"
        assert still_paused["answered_items"] == 1
        resumed = speak_and_execute(restarted, "继续模拟卷", "exam-voice-resume-action")
        assert resumed["capability"] == "exam.resume"
        for index in range(2, 7):
            result = speak_and_execute(
                restarted, "这题我暂时不会", f"exam-voice-answer-{index:02d}"
            )
        assert result["result"]["exam"]["status"] == "completed"
        assert result["result"]["exam"]["result"]["answered_items"] == 6
