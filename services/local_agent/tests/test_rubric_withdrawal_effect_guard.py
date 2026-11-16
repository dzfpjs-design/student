"""A human score correction cannot overtake final report submission."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from test_learning_rubric_review import CHILD, candidate, gate, flow, review
from test_school_open_reviews import CHILD as SCHOOL_CHILD, school_case


def test_human_retraction_waits_for_external_submission(gate):
    snapshot, _, _ = candidate(gate)
    service = RubricEvidenceService(gate.db)
    review(service, snapshot, key='synthetic-review')
    started = Event()

    def retract():
        started.set()
        return review(service, snapshot, revision=1, operation='retract', key='synthetic-retract')

    with ThreadPoolExecutor(max_workers=1) as pool:
        with gate.db.external_effect_guard():
            future = pool.submit(retract)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=.3)
            assert service.get(CHILD, snapshot['assessment_id'])['score_status'] == 'reviewed_human'
        assert future.result(timeout=5)['score_status'] == 'withdrawn'


@pytest.mark.parametrize('entry', ['ledger', 'candidate', 'resolve'])
def test_paper_reconciliation_waits_before_withdrawing_stale_score(school_case, monkeypatch, entry):
    from home_study_agent.answer_attempt_ledger import AnswerAttemptLedger
    from test_paper_subjective_scoring import paper_request_for_assessment
    from test_writing_calibrated_gate import authority

    database = school_case[0]
    _, automatic, _, _ = authority(monkeypatch, school_case, 0)
    scoring, request, _ = paper_request_for_assessment(database, automatic['assessment_id'], suffix='guard-ledger')
    decision = scoring.resolve(request['request_id'], SCHOOL_CHILD, 'guard-ledger-resolve')
    assert decision['correctness'] == 0
    # Simulate a source correction that happened before the list was opened.
    with database.connect() as c:
        c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','corrected original answer')")
        c.commit()
    started = Event()
    def fetch():
        started.set()
        if entry == 'ledger':
            return AnswerAttemptLedger(database).list_for_child(SCHOOL_CHILD)
        if entry == 'candidate':
            return scoring.for_candidate(request['candidate_id'], SCHOOL_CHILD)
        return scoring.resolve(request['request_id'], SCHOOL_CHILD, 'guard-ledger-resolve')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.external_effect_guard():
            future = pool.submit(fetch)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=.3)
            with database.connect() as c:
                assert c.execute("SELECT status FROM paper_scoring_decision_status_event WHERE decision_id=? ORDER BY rowid DESC LIMIT 1", (decision['decision_id'],)).fetchone()[0] == 'qualified'
        future.result(timeout=10)
    with database.connect() as c:
        assert c.execute("SELECT status FROM paper_scoring_decision_status_event WHERE decision_id=? ORDER BY rowid DESC LIMIT 1", (decision['decision_id'],)).fetchone()[0] == 'withdrawn'


def test_new_paper_answer_does_not_wait_for_report_submission(school_case, monkeypatch):
    from test_paper_subjective_scoring import paper_request_for_assessment
    from test_writing_calibrated_gate import authority

    database = school_case[0]
    _, automatic, _, _ = authority(monkeypatch, school_case, 0)
    scoring, request, _ = paper_request_for_assessment(database, automatic['assessment_id'], suffix='guard-new')
    with ThreadPoolExecutor(max_workers=1) as pool, database.external_effect_guard():
        result = pool.submit(scoring.resolve, request['request_id'], SCHOOL_CHILD, 'guard-new-resolve').result(timeout=5)
        assert result['correctness'] == 0
