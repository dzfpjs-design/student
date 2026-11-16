"""Stage qualification from disposable, real SQLite sources and scoring receipts."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_program import SpecializedProgramService
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService


CHILD = "child-primary-4"
OTHER = "child-junior-3"
NODE = "node-oly-counting"
DOMAIN = "competition_math.counting"
SEED = "diag-oly-counting-1"


@pytest.fixture
def case(tmp_path, monkeypatch):
    from home_study_agent import learning_state, repository, variant_supply, mistake_book
    db = Database(tmp_path / "stages.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    clock = [datetime.now(timezone.utc) - timedelta(days=12)]
    monkeypatch.setattr(learning_state, "utc_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(repository, "utc_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(mistake_book, "utc_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(variant_supply, "_now", lambda: clock[0].isoformat())
    sid = repo.start_session(CHILD, "stage-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    return db, learning, sid, clock


def progress(case, track="competition_math"):
    return SpecializedProgramService(case[0]).stage_progress(CHILD, track, as_of=case[3][0].isoformat())


def node(case):
    return next(d for d in progress(case)["domains"] if d["domain_id"] == DOMAIN)["nodes"][0]


def baseline(case):
    supply = SpecializedDiagnosticSupplyService(case[0])
    supply.enqueue_local(SEED, CHILD, "baseline-source", count=1)
    supply.process_local_pending()
    candidate = supply.list(CHILD)["items"][0]
    item = supply.review(candidate["candidate_id"], "approve", "Checked synthetic source", "baseline-review")["diagnostic_item_id"]
    case[3][0] = datetime.now(timezone.utc)
    return item


def answer(case, item_id, *, hint=0, correct=True, review=None):
    with case[0].connect() as c:
        item = c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()
        text = json.loads(item[0]).get("exact_answers", ["no exact rule"])[0] if correct else "wrong"
    return case[1].submit_diagnostic(case[2], item_id, text, str(uuid4()), review_task_id=review, hint_level=hint)


def test_cold_stage_read_is_pure_and_pet_four_skills_are_separate(case):
    with case[0].connect() as c:
        before = "\n".join(c.iterdump())
    result = progress(case, "pet")
    assert result["current_stage"] == "baseline"
    assert set(result["skills"]) == {"listening", "speaking", "reading", "writing"}
    assert all(row["qualified_stage"] is None for row in result["domains"])
    assert result["mastery_claim_allowed"] is False
    assert result["certification_claim_allowed"] is False
    with case[0].connect() as c:
        assert "\n".join(c.iterdump()) == before


def test_parent_high_scores_cannot_skip_stages_or_create_due_tasks(case):
    service = SpecializedProgramService(case[0])
    service.record_assessment(CHILD, "competition_math", "数学", "parent_reference", {DOMAIN: 1}, "high")
    assert node(case)["current_stage"] == "baseline"
    assert node(case)["qualified_stage"] is None


def test_real_exact_baseline_cannot_become_transfer_from_repetition(case):
    item = baseline(case)
    first = answer(case, item)
    assert node(case)["qualified_stage"] == "baseline"
    assert node(case)["current_stage"] == "near_transfer"
    case[3][0] += timedelta(days=2)
    answer(case, item)
    result = node(case)
    assert result["qualified_stage"] == "baseline"
    assert result["gates"]["baseline"]["evidence_refs"] == [first["evidence_id"]]
    assert result["next_action"]["execution_status"] == "blocked"


@pytest.mark.parametrize("hint,correct", [(1, True), (0, False)])
def test_hint_or_failure_does_not_qualify_baseline(case, hint, correct):
    answer(case, baseline(case), hint=hint, correct=correct)
    result = node(case)
    assert result["qualified_stage"] is None
    assert result["status"] == "recheck_needed"


def test_source_withdrawal_and_restart_revoke_stage_not_history(case):
    item = baseline(case)
    receipt = answer(case, item)
    before = progress(case)
    with case[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (item,))
        c.commit()
    result = progress(case)
    assert node(case)["qualified_stage"] is None
    assert result["evidence_fingerprint"] != before["evidence_fingerprint"]
    assert SpecializedProgramService(Database(case[0].path)).stage_progress(CHILD, "competition_math",
        as_of=case[3][0].isoformat()) == result
    with case[0].connect() as c:
        assert c.execute("SELECT count(*) FROM diagnostic_attempt WHERE evidence_id=?", (receipt["evidence_id"],)).fetchone()[0] == 1


def test_stage_projection_rejects_foreign_track_and_naive_or_future_evidence(case):
    service = SpecializedProgramService(case[0])
    with pytest.raises(ValueError):
        service.stage_progress(OTHER, "pet")
    with pytest.raises(ValueError):
        service.stage_progress(CHILD, "pet", as_of="2026-09-07T00:00:00")
    item = baseline(case)
    answer(case, item)
    result = service.stage_progress(CHILD, "competition_math", as_of=(case[3][0] - timedelta(seconds=1)).isoformat())
    assert next(d for d in result["domains"] if d["domain_id"] == DOMAIN)["qualified_stage"] is None


def test_reference_pet_keyword_answer_cannot_certify_writing_or_listening(case):
    for item, text in (("diag-pet-writing-organization-baseline", "Last week I visited a museum because it was fun and then I played."),
                       ("diag-pet-listening-gist-baseline", "swim sports centre")):
        case[1].submit_diagnostic(case[2], item, text, str(uuid4()))
    result = progress(case, "pet")
    assert result["skills"]["writing"]["qualified_stage"] is None
    assert result["skills"]["listening"]["qualified_stage"] is None
    assert "listening_delivery_receipt_unavailable" in result["skills"]["listening"]["blockers"]


def test_borrowed_connection_sees_uncommitted_withdrawal_without_writes(case):
    from home_study_agent.specialized_stages import project_specialized_stages
    item = baseline(case)
    answer(case, item)
    with case[0].connect() as c:
        before = project_specialized_stages(c, CHILD, "competition_math", as_of=case[3][0].isoformat())
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (item,))
        changes = c.total_changes
        after = project_specialized_stages(c, CHILD, "competition_math", as_of=case[3][0].isoformat())
        assert after["evidence_fingerprint"] != before["evidence_fingerprint"]
        assert c.in_transaction and c.total_changes == changes
        c.rollback()


def reviewed_variants(case, *, duplicate_far=False):
    """Persist synthetic reviewed sources, then use real promotion and scoring.

    This tests the source contract, not a model's ability to design far transfer.
    No generation worker or network is invoked.
    """
    from home_study_agent.mistake_book import MistakeBookService
    from home_study_agent.variant_supply import VariantSupplyService
    from home_study_agent.local_supply import item_snapshot, validate_item
    book = MistakeBookService(case[0])
    mistake = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject="数学", title="Synthetic counting",
        prompt_summary="Synthetic seed reference", submitted_answer="wrong", source_type="manual", source_ref=None,
        confidence=1, client_action_id="stage-mistake", diagnostic_item_id=SEED)["mistake_id"]
    book.confirm(mistake, CHILD, "confirm", "stage-confirm")
    book.diagnose(mistake, CHILD, "unknown", "Unverified hypothesis", .9, "stage-diagnose")
    supply = VariantSupplyService(case[0])
    job = supply.enqueue(mistake, CHILD, "synthetic-stage-sources")
    with case[0].connect() as c:
        c.execute("UPDATE variant_generation_job SET source_snapshot_json=? WHERE job_id=?",
            (json.dumps(item_snapshot(validate_item(c, CHILD, SEED))), job["job_id"]))
        for stage, prompt, correct in (
            ("near_transfer", "There are 4 roads to B and 7 roads from B to C. How many routes through B?", "28"),
            ("far_transfer", "A code uses one of 3 letters followed by one of 8 digits. How many distinct codes?", "24"),
        ):
            if duplicate_far and stage == "far_transfer":
                prompt = "There are 4 roads to B and  7 roads from B to C. How many routes through B?"
                correct = "28"
            supply._store_candidate(c, job, {"knowledge_node_id": NODE}, {
                "stage": stage, "prompt": prompt, "answer_type": "text",
                "expected_answer": {"local_rule": "local_exact_v1", "exact_answers": [correct]},
                "explanation": "Multiply the independent choices.", "difficulty": 2,
                "validation": {"passed": True, "ruleset": "synthetic_import_fixture_v1"}})
        c.execute("UPDATE variant_generation_job SET status='completed' WHERE job_id=?", (job["job_id"],))
        c.commit()
    return {r["stage"]: supply.review(r["candidate_id"], "approve", "Checked stage, source and exact answer",
        "review:" + r["candidate_id"])["diagnostic_item_id"] for r in supply.list(CHILD)["items"]}


def chain(case):
    base = baseline(case)
    b = answer(case, base)
    items = reviewed_variants(case)
    case[3][0] += timedelta(minutes=1)
    near = answer(case, items["near_transfer"])
    case[3][0] += timedelta(minutes=1)
    far = answer(case, items["far_transfer"])
    return base, items, b, near, far


def test_reviewed_stage_chain_and_real_due_receipt_can_observe_retention(case):
    base, items, b, near, far = chain(case)
    current = node(case)
    assert current["qualified_stage"] == "far_transfer"
    assert current["current_stage"] == "delayed_retention"
    assert current["gates"]["near_transfer"]["evidence_refs"] == [near["evidence_id"]]
    assert current["next_action"]["execution_status"] == "waiting_due"
    with case[0].connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='7d'",
                           (far["evidence_id"],)).fetchone()
    assert review is not None
    case[3][0] += timedelta(days=7)
    retained = answer(case, review["diagnostic_item_id"], review=review["review_task_id"])
    current = node(case)
    assert current["qualified_stage"] == "delayed_retention"
    assert current["status"] == "retention_observed"
    assert current["gates"]["delayed_retention"]["evidence_refs"] == [retained["evidence_id"]]
    assert progress(case)["qualified_stage"] is None  # Other competition domains are unknown.


def test_two_day_review_or_unbound_late_answer_cannot_certify_retention(case):
    _, items, _, _, far = chain(case)
    with case[0].connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (far["evidence_id"],)).fetchone()
    case[3][0] += timedelta(days=2)
    answer(case, review["diagnostic_item_id"], review=review["review_task_id"])
    assert node(case)["qualified_stage"] == "far_transfer"
    case[3][0] += timedelta(days=6)
    answer(case, items["far_transfer"])
    assert node(case)["qualified_stage"] == "far_transfer"


def test_far_before_near_cannot_be_reused_as_unseen_transfer(case):
    answer(case, baseline(case))
    items = reviewed_variants(case)
    case[3][0] += timedelta(minutes=1)
    answer(case, items["far_transfer"])
    case[3][0] += timedelta(minutes=1)
    answer(case, items["near_transfer"])
    case[3][0] += timedelta(days=2)
    answer(case, items["far_transfer"])
    assert node(case)["qualified_stage"] == "near_transfer"


def test_late_counterevidence_downgrades_prior_chain_and_withdrawal_rebuilds(case):
    from home_study_agent.mistake_book import MistakeBookService
    _, items, _, _, _ = chain(case)
    case[3][0] += timedelta(days=2)
    failed = answer(case, items["far_transfer"], correct=False)
    assert node(case)["qualified_stage"] is None
    book = MistakeBookService(case[0])
    mistake = book.capture_from_attempt(failed)
    book.confirm(mistake["mistake_id"], CHILD, "exclude", "withdraw-late-failure")
    assert node(case)["qualified_stage"] == "far_transfer"


@pytest.mark.parametrize("mutation", ["review_withdrawn", "stage_changed", "answer_changed", "source_expired", "evidence_expired"])
def test_stage_sources_dates_and_score_revisions_are_recomputed(case, mutation):
    _, items, _, near, _ = chain(case)
    assert node(case)["qualified_stage"] == "far_transfer"
    before = progress(case)["evidence_fingerprint"]
    with case[0].connect() as c:
        if mutation == "review_withdrawn":
            c.execute("UPDATE variant_candidate SET status='rejected' WHERE diagnostic_item_id=?", (items["near_transfer"],))
        elif mutation == "stage_changed":
            c.execute("UPDATE variant_candidate SET stage='discrimination' WHERE diagnostic_item_id=?", (items["near_transfer"],))
        elif mutation == "answer_changed":
            c.execute("UPDATE diagnostic_attempt SET submitted_answer='wrong' WHERE evidence_id=?", (near["evidence_id"],))
        elif mutation == "source_expired":
            c.execute("UPDATE knowledge_node SET source_ref='expired-stage-source' WHERE knowledge_node_id=?", (NODE,))
            c.execute("""INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
                source_ref,license_status,review_status,content_hash,imported_at,effective_to)
                VALUES('expired-stage-source',?,'test','Synthetic','数学','四年级',
                'expired-stage-source','local','approved','v1',?,'2000-01-01')""", (CHILD, case[3][0].isoformat()))
        else:
            case[3][0] += timedelta(days=91)
        c.commit()
    assert node(case)["qualified_stage"] != "far_transfer"
    assert progress(case)["evidence_fingerprint"] != before


def test_old_tutor_explanation_is_still_prior_exposure_for_new_transfer(case):
    answer(case, baseline(case))
    items = reviewed_variants(case)
    with case[0].connect() as c:
        question = c.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?", (items["near_transfer"],)).fetchone()[0]
    StudyRepository(case[0]).save_tutor_turn(case[2], None, "数学", question, "Synthetic explanation", "test", "test", "stage-hint")
    case[3][0] += timedelta(days=2)
    answer(case, items["near_transfer"])
    assert node(case)["qualified_stage"] == "baseline"


def test_roadmap_and_action_expose_current_stage_without_reference_skip(case):
    answer(case, baseline(case))
    service = SpecializedProgramService(case[0])
    roadmap = service.roadmap(CHILD, "competition_math")
    row = next(r for r in roadmap["domains"] if r["domain_id"] == DOMAIN)
    assert row["stage_progress"]["current_stage"] == "near_transfer"
    action = service._next_action("competition_math", row, 0)
    assert action["stage_constraint"]["current_stage"] == "near_transfer"
    assert action["execution_status"] == "blocked"
    assert action["diagnostic_item_id"] is None
    assert roadmap["stage_progress"]["mastery_claim_allowed"] is False


def test_same_grade_other_child_private_sources_do_not_enter_stage_inventory(case):
    item = baseline(case)
    with case[0].connect() as c:
        c.execute("UPDATE child_profile SET grade='四年级' WHERE child_id=?", (OTHER,))
        c.commit()
    service = SpecializedProgramService(case[0])
    before = service.stage_progress(OTHER, "competition_math", as_of=case[3][0].isoformat())
    assert item not in json.dumps(before)
    answer(case, item)
    with case[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (item,))
        c.commit()
    assert service.stage_progress(OTHER, "competition_math", as_of=case[3][0].isoformat()) == before


def test_report_reference_fingerprint_tracks_reviewed_stage_metadata(case, monkeypatch):
    from home_study_agent.specialized_program import report_reference_fingerprint
    from home_study_agent import specialized_program
    _, items, _, _, _ = chain(case)
    monkeypatch.setattr(specialized_program, "_now", lambda: case[3][0].isoformat())
    with case[0].connect() as c:
        before = report_reference_fingerprint(c, CHILD)
        c.execute("UPDATE variant_candidate SET stage='discrimination' WHERE diagnostic_item_id=?", (items["near_transfer"],))
        assert report_reference_fingerprint(c, CHILD) != before
        c.rollback()


def test_missing_or_near_deadline_never_shortens_stage_delay(case):
    from home_study_agent.goal_program import GoalProgramService
    # Initialize the existing goal store only; no new goal/certificate store.
    goal = GoalProgramService(case[0])
    goal.ensure_schema()
    _, _, _, _, _ = chain(case)
    before = progress(case)
    with case[0].connect() as c:
        row = c.execute("SELECT program_id FROM goal_program WHERE child_id=? AND track='competition_math_path'", (CHILD,)).fetchone()
    if row is None:
        goal.cycles(CHILD)
    with case[0].connect() as c:
        c.execute("UPDATE goal_program SET target_horizon_json=? WHERE child_id=? AND track='competition_math_path'",
            (json.dumps({"target_date": case[3][0].date().isoformat()}), CHILD))
        c.commit()
    after = progress(case)
    assert before["target_date_status"] == "clarification_required"
    assert after["target_date"] == case[3][0].date().isoformat()
    assert after["domains"] == before["domains"]
    assert after["policy"]["automatic_pressure_increase"] is False


def test_repeat_read_and_readonly_database_never_consume_questions(case):
    from home_study_agent.specialized_stages import project_specialized_stages
    chain(case)
    before = progress(case)
    c = sqlite3.connect(case[0].path.as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN")
        assert project_specialized_stages(c, CHILD, "competition_math", as_of=case[3][0].isoformat()) == before
        assert c.in_transaction and c.total_changes == 0
    finally:
        c.close()
    assert progress(case) == before


def test_ambiguous_specialized_transfer_label_cannot_be_far_transfer(case):
    supply = SpecializedDiagnosticSupplyService(case[0])
    # The existing discriminant template really supports a generic transfer.
    # Competition templates intentionally reject it; do not make a vacuous test.
    supply.enqueue_local("diag-j3-discriminant-1", OTHER, "generic-transfer")
    supply.process_local_pending()
    candidates = [c for c in supply.list(OTHER)["items"] if c["stage"] == "transfer"]
    assert candidates
    with case[0].connect() as c:
        c.execute("UPDATE daily_session SET state='completed',status='completed' WHERE session_id=?", (case[2],))
        c.commit()
    sid = StudyRepository(case[0]).start_session(OTHER, "junior-generic-transfer")["session_id"]
    with case[0].connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    for candidate in candidates:
        item = supply.review(candidate["candidate_id"], "approve", "Reviewed generic exercise", "generic-review")["diagnostic_item_id"]
        case[3][0] = datetime.now(timezone.utc)
        answer((case[0], case[1], sid, case[3]), item)
    result = SpecializedProgramService(case[0]).stage_progress(OTHER, "junior_exam", as_of=case[3][0].isoformat())
    assert all(d["qualified_stage"] != "far_transfer" for d in result["domains"])


def test_retention_receipt_withdrawal_removes_only_retention_qualification(case):
    from home_study_agent.mistake_book import MistakeBookService
    _, _, _, _, far = chain(case)
    with case[0].connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='7d'", (far["evidence_id"],)).fetchone()
    case[3][0] += timedelta(days=7)
    receipt = answer(case, review["diagnostic_item_id"], review=review["review_task_id"])
    assert node(case)["qualified_stage"] == "delayed_retention"
    book = MistakeBookService(case[0])
    # Excluding a captured response uses the same existing evidence withdrawal path.
    captured = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject="数学", title="Synthetic withdrawn receipt",
        prompt_summary="Retest to retract", submitted_answer="24", source_type="diagnostic",
        source_ref=receipt["attempt_id"], confidence=1, client_action_id="capture-retention",
        diagnostic_item_id=review["diagnostic_item_id"], source_evidence_id=receipt["evidence_id"])
    book.confirm(captured["mistake_id"], CHILD, "exclude", "exclude-retention")
    assert node(case)["qualified_stage"] == "far_transfer"


def test_different_ids_for_equivalent_original_cannot_fill_two_transfer_gates(case):
    answer(case, baseline(case))
    items = reviewed_variants(case, duplicate_far=True)
    assert items["near_transfer"] != items["far_transfer"]
    case[3][0] += timedelta(minutes=1)
    answer(case, items["near_transfer"])
    case[3][0] += timedelta(days=2)
    answer(case, items["far_transfer"])
    assert node(case)["qualified_stage"] == "near_transfer"


def test_stage_projection_survives_actual_process_restart(case):
    import os
    import subprocess
    import sys
    chain(case)
    expected = progress(case)
    script = """import json,sys
from home_study_agent.database import Database
from home_study_agent.specialized_program import SpecializedProgramService
print(json.dumps(SpecializedProgramService(Database(sys.argv[1])).stage_progress(sys.argv[2], 'competition_math', as_of=sys.argv[3])))
"""
    process = subprocess.run([sys.executable, "-c", script, str(case[0].path), CHILD, case[3][0].isoformat()],
        env=os.environ.copy(), capture_output=True, text=True, check=True)
    assert json.loads(process.stdout) == expected
