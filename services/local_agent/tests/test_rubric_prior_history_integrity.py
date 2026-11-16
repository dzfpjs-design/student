import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from test_learning_outcome_mastery_gate import gate, flow, CHILD, SECOND
from test_learning_rubric_review import review
from test_learning_feedback_loop import OTHER


@pytest.mark.parametrize('payload', ['{broken', '[]', 'null', '{"question": []}',
    '{"question": "Calculate something", "responded_at": "not-a-time"}'])
@pytest.mark.parametrize('prior_child', [CHILD, OTHER])
def test_bad_prior_history_preserves_score_and_child_isolation(gate, payload, prior_child):
    first, _, _ = gate.run(local=True)
    prior_id = first['evaluation']['rubric_assessment']['assessment_id']
    # Inside the repetition window, unknown prior identity remains conservative.
    # Owned answers proven outside it are covered by test_rubric_prior_time_recovery.
    current, _, _ = gate.run(local=True, item=SECOND, answer='42', days=.5)
    snapshot = current['evaluation']['rubric_assessment']
    service = RubricEvidenceService(gate.db)
    review(service, snapshot)
    with gate.db.connect() as c:
        c.execute('BEGIN')
        before = service.reviewed_receipt(c, CHILD, snapshot['step_id'], now=gate.clock[0])
        assert before and before['correctness'] == 1 and before['independent']
        # Simulate corrupt legacy storage; restore the normal immutable guard
        # before reading through production code.
        guard = c.execute("SELECT sql FROM sqlite_master WHERE name='rubric_assessment_no_update'").fetchone()[0]
        c.execute('DROP TRIGGER rubric_assessment_no_update')
        c.execute('UPDATE rubric_assessment SET payload_json=?,child_id=? WHERE assessment_id=?', (payload, prior_child, prior_id))
        c.execute(guard)
        c.commit()
    # Reopen the durable store, not the evaluator's previous response cache.
    with gate.db.connect() as c:
        c.execute('BEGIN')
        receipt = service.reviewed_receipt(c, CHILD, snapshot['step_id'], now=gate.clock[0])
        assert receipt and receipt['correctness'] == 1
        assert receipt['independent'] is (prior_child != CHILD)
        assert c.execute('SELECT payload_json FROM rubric_assessment WHERE assessment_id=?', (prior_id,)).fetchone()[0] == payload
