from datetime import timedelta
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_mistake_gate_reliability import CHILD, ITEM, SECOND, alias, flow, prepare, submit
from test_parent_coaching_feedback_semantics import material


def mastered(flow):
    db, learning, sid, clock = flow
    clock[0] -= timedelta(days=8)
    source = learning.submit_diagnostic(sid, ITEM, "12", str(uuid4()))
    book, case = prepare(flow)
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET source_evidence_id=? WHERE mistake_id=?", (source["evidence_id"], case))
        c.commit()
    submit(book, case)
    clock[0] += timedelta(hours=49)
    assert submit(book, case, "delayed_48h")["status"] == "mastered"
    return book, case


def finish_report(flow):
    db, _, sid, _ = flow
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repo.complete_with_reflection(sid, "", "", "okay", "", True, str(uuid4()))
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    return repo, repo.save_daily_report(sid, summary, coaching)


def invalidate(flow, case, change):
    db = flow[0]
    with db.connect() as c:
        if change == "withdrawal":
            evidence = c.execute("SELECT evidence_id FROM mistake_attempt WHERE mistake_id=? AND stage='near_transfer'", (case,)).fetchone()[0]
        elif change == "confidence":
            c.execute("UPDATE learning_evidence SET confidence=.1 WHERE evidence_id IN (SELECT evidence_id FROM mistake_attempt WHERE mistake_id=?)", (case,))
        elif change == "assistance":
            c.execute("UPDATE mistake_attempt SET hint_level=1,independent=0 WHERE mistake_id=?", (case,))
        else:
            c.execute("UPDATE diagnostic_item SET prompt='changed reference' WHERE diagnostic_item_id=?", (SECOND,))
        c.commit()
    if change == "withdrawal":
        book = MistakeBookService(db)
        withdrawal = book.capture(child_id=CHILD, knowledge_node_id="node-p4-math-arithmetic", subject="Math", title="Correction",
            prompt_summary="Withdraw transfer evidence", submitted_answer="wrong", source_type="manual", source_ref=None,
            confidence=1, client_action_id=str(uuid4()), source_evidence_id=evidence)
        book.confirm(withdrawal["mistake_id"], CHILD, "exclude", str(uuid4()))


def plan(flow, case):
    db, _, sid, _ = flow
    repo = StudyRepository(Database(db.path))
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        c.execute("UPDATE family_rule SET latest_end_time=NULL WHERE child_id=?", (CHILD,))
        c.commit()
    next_sid = repo.start_session(CHILD, str(uuid4()))["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (next_sid,))
        c.commit()
    result = repo.generate_plan(next_sid, str(uuid4()))
    return [item for item in result["plan_items"] if item["source_type"] == "mistake_review" and item["source_ref"] == case]


@pytest.mark.parametrize("change", ["withdrawal", "confidence", "assistance", "reference"])
def test_report_revalidates_current_gate_even_when_its_own_session_fingerprint_is_unchanged(flow, change):
    book, case = mastered(flow)
    repo, report = finish_report(flow)
    repo.save_parent_report_feedback(report["report_id"], True, 2, 1, "Keep actual action feedback")
    before = report["evidence"]["reliable_learning_fingerprint"]
    invalidate(flow, case, change)
    current = book.get(case, CHILD)
    assert current["recorded_status"] == "mastered" and current["status"] != "mastered"
    restored = StudyRepository(Database(flow[0].path)).get_daily_report(report["report_id"])
    item = next(item for item in restored["evidence_detail"]["mistakes"] if item.get("mistake_id") == case)
    assert item["status"] == current["status"]
    assert item["mastery_gate"] == current["mastery_gate"]
    assert restored["evidence"]["reliable_learning_fingerprint"] == before
    assert restored["summary"] != report["summary"]
    assert any("需继续处理" in fact and "Synthetic" in fact for fact in restored["summary"]["facts"])
    assert restored["feedback"]["executed"] and restored["feedback"]["effectiveness"] == 2
    with flow[0].connect() as c:
        assert c.execute("SELECT status FROM mistake_case WHERE mistake_id=?", (case,)).fetchone()[0] == "mastered"


@pytest.mark.parametrize("change", ["withdrawal", "confidence", "assistance", "reference"])
def test_plan_reincludes_historically_mastered_case_using_current_read_projection(flow, change):
    book, case = mastered(flow)
    invalidate(flow, case, change)
    assert book.get(case, CHILD)["mastery_gate"]["missing"][0] == "near_transfer"
    assert len(plan(flow, case)) == 1


def test_valid_mastery_stays_out_of_plan(flow):
    _, case = mastered(flow)
    assert plan(flow, case) == []


@pytest.mark.parametrize("due", [False, True])
def test_plan_obeys_the_projected_missing_stage_due_date(flow, due):
    flow[3][0] -= timedelta(days=8)
    book, case = prepare(flow)
    submit(book, case)
    flow[3][0] += timedelta(hours=49 if due else 1)
    with flow[0].connect() as c:
        c.execute("UPDATE mistake_case SET status='mastered' WHERE mistake_id=?", (case,))
        c.commit()
    assert book.get(case, CHILD)["mastery_gate"]["missing"] == ["delayed_48h"]
    assert bool(plan(flow, case)) is due


def test_consumer_projection_is_read_only_and_child_scoped(flow):
    _, case = mastered(flow)
    invalidate(flow, case, "confidence")
    repo = StudyRepository(flow[0])
    with flow[0].connect() as c:
        c.execute("PRAGMA query_only=ON")
        before = c.total_changes
        projected = repo._current_mistake_projection(c, CHILD, {case})
        assert projected[0]["recorded_status"] == "mastered"
        assert projected[0]["status"] == "near_transfer"
        assert repo._current_mistake_projection(c, "child-junior-3", {case}) == []
        assert c.total_changes == before


def test_legacy_report_without_current_gate_fingerprint_is_rebuilt(flow):
    _, case = mastered(flow)
    repo, report = finish_report(flow)
    with flow[0].connect() as c:
        manifest = {key: value for key, value in report["evidence"].items() if key != "mistake_mastery_fingerprint"}
        c.execute("UPDATE daily_report SET evidence_json=?,summary_json=? WHERE report_id=?",
                  (json.dumps(manifest), json.dumps({"headline": "stale mastery"}), report["report_id"]))
        c.commit()
    invalidate(flow, case, "confidence")
    restored = repo.get_daily_report(report["report_id"])
    assert restored["summary"]["headline"] != "stale mastery"
    assert restored["evidence"]["mistake_mastery_fingerprint"]
    assert any("需继续处理" in fact and "Synthetic" in fact for fact in restored["summary"]["facts"])


def test_same_question_aliases_do_not_hide_an_unmet_transfer_gate_from_plan(flow, monkeypatch):
    flow[3][0] -= timedelta(days=8)
    renamed = alias(flow, monkeypatch, "consumer-alias")
    book, case = prepare(flow, "concept_gap")
    submit(book, case)
    flow[3][0] += timedelta(hours=25)
    submit(book, case, item=renamed)
    with flow[0].connect() as c:
        c.execute("UPDATE mistake_case SET status='mastered' WHERE mistake_id=?", (case,))
        c.commit()
    assert book.get(case, CHILD)["mastery_gate"]["satisfied"] == {"near_transfer": 1}
    assert len(plan(flow, case)) == 1


@pytest.mark.parametrize("score_status", [None, "needs_review", "practice_only"])
def test_correctness_alone_cannot_be_reported_as_verified_feedback(score_status):
    evidence = material()
    evidence["knowledge_evidence"] = [{"knowledge_title": "Unqualified", "correctness": 1, "independent": True, "score_status": score_status}]
    summary, _ = build_daily_report(evidence)
    assert not any("已核验作答" in fact for fact in summary["facts"])
    assert any("未计入可靠学习判断" in fact for fact in summary["facts"])


def test_verified_assisted_feedback_does_not_claim_independence():
    evidence = material()
    evidence["knowledge_evidence"] = [{"knowledge_title": "Assisted", "correctness": 1, "independent": True, "hint_level": 1, "score_status": "verified_local"}]
    summary, _ = build_daily_report(evidence)
    assert any("1条已核验作答" in fact for fact in summary["facts"])
    assert not any("其中1条为独立作答" in fact for fact in summary["facts"])


def test_report_does_not_hide_a_stale_mastery_label_with_an_open_gate():
    evidence = material()
    evidence["mistakes"] = [{"title": "Unfinished transfer", "status": "mastered", "mastery_gate": {"missing": ["near_transfer"]}}]
    summary, _ = build_daily_report(evidence)
    assert any("需继续处理" in fact for fact in summary["facts"])
