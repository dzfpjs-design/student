from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from home_study_agent import learning_state, mistake_book, repository
from home_study_agent.database import Database
from home_study_agent.learning_obligations import project_child_mastery_evidence
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService, InvalidMistakeStateError, MistakeReviewNotDueError
from home_study_agent.repository import StudyRepository


CHILD = "child-primary-4"
NODE = "node-p4-math-arithmetic"
ITEM = "diag-p4-arithmetic-closed-1"
SECOND = "diag-p4-arithmetic-closed-2"


def build_flow(tmp_path, monkeypatch, *, initialize=None):
    db = Database(tmp_path / "gate.db")
    if initialize is None:
        db.initialize()
    else:
        initialize(db.path)
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    clock = [datetime.now(timezone.utc)]
    for module in (mistake_book, learning_state, repository):
        monkeypatch.setattr(module, "utc_now", lambda: clock[0].isoformat())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(mistake_book, "datetime", Clock)
    sid = repo.start_session(CHILD, "gate-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    return db, learning, sid, clock


@pytest.fixture
def flow(tmp_path, monkeypatch):
    return build_flow(tmp_path, monkeypatch)


def prepare(flow, error="unknown", *, node=NODE, original=ITEM):
    db, _, _, _ = flow
    book = MistakeBookService(db)
    case = book.capture(child_id=CHILD, knowledge_node_id=node, subject="Math", title="Synthetic",
        prompt_summary=str(uuid4()), submitted_answer="wrong", source_type="manual", source_ref=None,
        confidence=1, client_action_id=str(uuid4()), diagnostic_item_id=original)["mistake_id"]
    book.confirm(case, CHILD, "confirm", str(uuid4()))
    book.diagnose(case, CHILD, error, "Unverified hypothesis", .99, str(uuid4()))
    book.add_remediation(case, CHILD, "active_recall", "completed", None, str(uuid4()))
    return book, case


def submit(book, case, stage="near_transfer", item=SECOND, answer="42", hint=0, action=None):
    return book.submit_attempt(case, CHILD, stage, item, answer, hint, action or str(uuid4()))


def projection(flow):
    db, _, _, clock = flow
    with db.connect() as c:
        c.execute("PRAGMA query_only=ON")
        return project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat()).get(NODE, {})


@pytest.mark.parametrize("error", list(mistake_book.GATE_REQUIREMENTS))
def test_candidate_hypothesis_cannot_reduce_unknown_gate(flow, error):
    book, case = prepare(flow, error)
    detail = book.get(case, CHILD)
    assert detail["diagnosis"]["status"] == "candidate"
    assert all(detail["mastery_gate"]["requirements"].get(stage, 0) >= count
               for stage, count in mistake_book.GATE_REQUIREMENTS["unknown"].items())


def test_candidate_incidental_repair_alone_does_not_certify(flow):
    book, case = prepare(flow, "incidental")
    result = submit(book, case, "repair_check")
    assert result["status"] != "mastered"
    assert result["mastery_gate"]["missing"] == ["near_transfer", "delayed_48h"]


def test_normal_reference_answer_is_saved_but_not_certified(flow):
    db, _, _, _ = flow
    book, case = prepare(flow, node="node-p4-english-phonics-discrimination", original="diag-p4-english-phonics-3")
    result = submit(book, case, item="diag-p4-english-phonics-1", answer="bag")
    assert result["attempt_result"]["correctness"] == 1  # Keep supported reference scoring.
    assert result["attempt_result"]["independent"] is False
    assert result["mastery_gate"]["satisfied"] == {}
    assert result["attempt_result"]["score_status"] == "needs_review"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM mistake_attempt").fetchone()[0] == 1
        assert not project_child_mastery_evidence(c, CHILD)["node-p4-english-phonics-discrimination"]["effective_evidence"]


@pytest.mark.parametrize("mutation", [
    "UPDATE learning_evidence SET confidence=.1",
    "UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-2'",
    "UPDATE diagnostic_item SET stimulus_asset='changed' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-2'",
    "UPDATE mistake_attempt SET submitted_answer='wrong'",
    "UPDATE mistake_attempt SET child_id='child-junior-3'",
])
def test_history_gate_recomputes_from_current_receipts_readonly(flow, mutation):
    db, _, _, _ = flow
    book, case = prepare(flow)
    assert submit(book, case)["mastery_gate"]["satisfied"] == {"near_transfer": 1}
    with db.connect() as c:
        c.execute(mutation)
        c.commit()
    assert not projection(flow)["effective_evidence"]
    with Database(db.path).connect() as c:
        c.execute("PRAGMA query_only=ON")
        changes = c.total_changes
        gate = MistakeBookService(Database(db.path))._gate_summary(c, case)
        assert gate["satisfied"] == {}
        assert gate["missing"][0] == "near_transfer"
        assert c.total_changes == changes


def test_completed_history_withdrawal_reopens_read_views_without_erasing_receipts(flow):
    db, _, sid, clock = flow
    book, case = prepare(flow)
    submit(book, case)
    clock[0] += timedelta(hours=49)
    assert submit(book, case, "delayed_48h")["status"] == "mastered"
    with db.connect() as c:
        source = c.execute("SELECT evidence_id FROM mistake_attempt WHERE mistake_id=? AND stage='near_transfer'", (case,)).fetchone()[0]
    withdrawal = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject="Math", title="Correction",
        prompt_summary="Withdraw this evidence", submitted_answer="wrong", source_type="manual", source_ref=None,
        confidence=1, client_action_id="withdrawal-case", source_evidence_id=source)
    book.confirm(withdrawal["mistake_id"], CHILD, "exclude", "withdrawal")
    with db.connect() as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM mistake_attempt ORDER BY rowid")]
    restarted = MistakeBookService(Database(db.path))
    assert restarted.get(case, CHILD)["status"] != "mastered"
    assert restarted.next_activity(case, CHILD)["stage"] == "near_transfer"
    assert restarted.list(CHILD, "mastered")["items"] == []
    assert restarted.list(CHILD)["counts"]["mastered"] == 0
    assert restarted.current_for_session(sid)["mistake_id"] == case
    with db.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM mistake_attempt ORDER BY rowid")] == before


def alias(flow, monkeypatch, item_id, original=SECOND):
    seed = next(row for row in learning_state.ITEMS if row[0] == original)
    monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS, (item_id, *seed[1:])))
    flow[1].seed_catalog()
    return item_id


def test_normal_diagnostic_exposure_cannot_certify_alias_transfer(flow, monkeypatch):
    _, learning, sid, clock = flow
    renamed = alias(flow, monkeypatch, "gate-alias")
    learning.submit_diagnostic(sid, SECOND, "42", "earlier-diagnostic")
    clock[0] += timedelta(seconds=1)
    book, case = prepare(flow)
    result = submit(book, case, item=renamed)
    assert result["mastery_gate"]["satisfied"] == {}
    assert result["attempt_result"]["independent"] is False
    assert len(projection(flow)["effective_evidence"]) == 1


def test_old_same_question_aliases_cannot_count_as_two_transfer_items(flow, monkeypatch):
    db, _, _, clock = flow
    renamed = alias(flow, monkeypatch, "gate-spaced-alias")
    book, case = prepare(flow, "concept_gap")
    submit(book, case)
    clock[0] += timedelta(hours=25)
    submit(book, case, item=renamed)
    assert len(projection(flow)["effective_evidence"]) == 2
    gate = book.get(case, CHILD)["mastery_gate"]
    assert gate["satisfied"] == {"near_transfer": 1}
    assert gate["missing"][0] == "near_transfer"


def test_qualified_delayed_reuse_and_hints_keep_original_boundaries(flow):
    _, _, _, clock = flow
    book, case = prepare(flow)
    submit(book, case)
    with pytest.raises(MistakeReviewNotDueError):
        submit(book, case, "delayed_48h")
    clock[0] += timedelta(hours=49)
    hinted = submit(book, case, "delayed_48h", hint=1)
    assert hinted["status"] != "mastered"
    clock[0] += timedelta(hours=49)
    assert submit(book, case, "delayed_48h")["status"] == "mastered"


def test_legacy_reference_flags_do_not_restore_score_authority(flow):
    db, _, _, _ = flow
    book, case = prepare(flow, node="node-p4-english-phonics-discrimination", original="diag-p4-english-phonics-3")
    submit(book, case, item="diag-p4-english-phonics-1", answer="bag")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=1,independent=1")
        c.execute("UPDATE mistake_attempt SET independent=1")
        c.commit()
        assert not project_child_mastery_evidence(c, CHILD)["node-p4-english-phonics-discrimination"]["effective_evidence"]
    assert book.get(case, CHILD)["mastery_gate"]["satisfied"] == {}


def test_unqualified_reference_failures_do_not_trigger_remediation_diagnosis(flow):
    book, case = prepare(flow, node="node-p4-english-phonics-discrimination", original="diag-p4-english-phonics-3")
    for item in ("diag-p4-english-phonics-1", "diag-p4-english-phonics-2"):
        result = submit(book, case, item=item, answer="wrong")
    assert result["status"] == "near_transfer"


def test_legacy_delayed_success_must_be_spaced_from_current_immediate_basis(flow):
    db, _, _, clock = flow
    book, case = prepare(flow)
    submit(book, case)
    immediate = clock[0] + timedelta(hours=24)
    clock[0] += timedelta(hours=49)
    assert submit(book, case, "delayed_48h")["status"] == "mastered"
    with db.connect() as c:
        source = c.execute("SELECT evidence_id FROM mistake_attempt WHERE stage='near_transfer'").fetchone()[0]
        c.execute("UPDATE learning_evidence SET occurred_at=? WHERE evidence_id=?", (immediate.isoformat(), source))
        c.execute("UPDATE mistake_attempt SET created_at=? WHERE evidence_id=?", (immediate.isoformat(), source))
        c.commit()
    assert len(projection(flow)["effective_evidence"]) == 2
    detail = book.get(case, CHILD)
    assert detail["mastery_gate"]["missing"] == ["delayed_48h"]
    assert detail["status"] == "fragile"
    with pytest.raises(MistakeReviewNotDueError):
        submit(book, case, "delayed_48h")


def test_original_question_alias_is_not_transfer_even_without_a_prior_receipt(flow, monkeypatch):
    renamed = alias(flow, monkeypatch, "gate-original-alias", original=ITEM)
    book, case = prepare(flow)
    result = submit(book, case, item=renamed, answer="90")
    assert result["mastery_gate"]["satisfied"] == {}


def test_next_activity_skips_aliases_seen_in_other_diagnostic_sessions(flow, monkeypatch):
    _, learning, sid, clock = flow
    alias(flow, monkeypatch, "000-gate-alias")
    learning.submit_diagnostic(sid, SECOND, "42", "exposed")
    clock[0] += timedelta(days=2)
    book, case = prepare(flow)
    assert book.next_activity(case, CHILD)["kind"] == "blocked"


def test_http_receipts_restart_and_current_gate_agree(flow):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings

    db, _, _, clock = flow
    book, case = prepare(flow)
    config = Settings(data_dir=db.path.parent, database_path=db.path, data_encryption_key=b"0" * 32,
                      maintenance_enabled=False)
    payload = {"child_id": CHILD, "stage": "near_transfer", "diagnostic_item_id": SECOND,
               "answer": "42", "hint_level": 0, "client_action_id": "http-gate-answer"}
    with TestClient(create_app(config)) as client:
        result = client.post(f"/v1/mistakes/{case}/attempts", json=payload)
        assert result.status_code == 200, result.text[:300]
        assert result.json()["mastery_gate"]["satisfied"] == {"near_transfer": 1}
        replay = client.post(f"/v1/mistakes/{case}/attempts", json=payload)
        assert replay.status_code == 200
        assert replay.json()["mastery_gate"] == result.json()["mastery_gate"]
    clock[0] += timedelta(hours=49)
    with TestClient(create_app(config)) as client:
        result = client.post(f"/v1/mistakes/{case}/attempts", json={**payload, "stage": "delayed_48h", "client_action_id": "http-delayed"})
        assert result.status_code == 200, result.text[:300]
        assert result.json()["status"] == "mastered"
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id=?", (SECOND,))
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM mistake_attempt").fetchone()[0] == 2
    assert not projection(flow)["effective_evidence"]
    assert MistakeBookService(Database(db.path)).get(case, CHILD)["mastery_gate"]["satisfied"] == {}


def test_legacy_candidate_incidental_gate_is_read_upgraded(flow):
    db, _, _, _ = flow
    book, case = prepare(flow, "incidental")
    submit(book, case, "repair_check")
    with db.connect() as c:
        c.execute("UPDATE mistake_mastery_gate SET requirements_json=?,delayed_48h_due_at=NULL WHERE mistake_id=?", ('{"repair_check":1}', case))
        c.execute("UPDATE mistake_case SET status='mastered' WHERE mistake_id=?", (case,))
        c.commit()
        c.execute("PRAGMA query_only=ON")
        detail = book._detail(c, case)
    assert detail["recorded_status"] == "mastered"
    assert detail["status"] == "near_transfer"
    assert detail["mastery_gate"]["missing"] == ["near_transfer", "delayed_48h"]


def test_real_local_variant_aliases_and_review_source_invalidation(flow):
    from home_study_agent.local_supply import validate_item
    from home_study_agent.learning_obligations import question_identity
    from home_study_agent.variant_supply import VariantSupplyService

    class NoCloud:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError("Cloud calls forbidden")

    db, _, _, clock = flow
    supply = VariantSupplyService(db, NoCloud())
    cases, items = [], []
    for seed in (ITEM, SECOND):
        book, case = prepare(flow, original=seed)
        cases.append(case)
        supply.enqueue_local(case, CHILD, "local:" + seed)
        assert supply.process_local_pending()["jobs"][0]["status"] == "completed"
        with db.connect() as c:
            candidate = c.execute("SELECT candidate_id FROM variant_candidate WHERE mistake_id=? AND stage='near_transfer'", (case,)).fetchone()[0]
        approved = supply.qualify_local(candidate, CHILD, "qualify:" + seed)
        with db.connect() as c:
            items.append(validate_item(c, CHILD, approved["diagnostic_item_id"]))
    assert items[0]["diagnostic_item_id"] != items[1]["diagnostic_item_id"]
    assert question_identity(items[0]) == question_identity(items[1])
    clock[0] += timedelta(seconds=2)
    for index in (0, 1):
        item = items[index]
        result = submit(book, cases[index], item=item["diagnostic_item_id"], answer=json.loads(item["expected_answer_json"])["exact_answers"][0])
        assert result["mastery_gate"]["satisfied"] == ({"near_transfer": 1} if index == 0 else {})
        clock[0] += timedelta(seconds=1)
    assert len(projection(flow)["effective_evidence"]) == 1
    with db.connect() as c:
        c.execute("UPDATE variant_candidate SET review_action_id=NULL WHERE mistake_id=?", (cases[0],))
        c.commit()
    assert not projection(flow)["effective_evidence"]
    assert book.get(cases[0], CHILD)["mastery_gate"]["satisfied"] == {}


def test_changed_old_source_does_not_make_a_spaced_alias_new_transfer(flow, monkeypatch):
    db, learning, sid, clock = flow
    renamed = alias(flow, monkeypatch, "unchanged-alias")
    learning.submit_diagnostic(sid, SECOND, "wrong", "old-exposure")
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id=?", (SECOND,))
        c.commit()
    clock[0] += timedelta(hours=25)
    book, case = prepare(flow)
    result = submit(book, case, item=renamed)
    assert len(projection(flow)["effective_evidence"]) == 1
    assert result["mastery_gate"]["satisfied"] == {}
