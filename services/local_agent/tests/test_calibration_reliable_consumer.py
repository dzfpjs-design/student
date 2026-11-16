"""Synthetic fixed-policy corpus proves consumer wiring, not real calibration."""
from datetime import timedelta

from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.rubric_calibration import RubricCalibrationService
from test_mistake_gate_reliability import flow, CHILD
from test_rubric_calibration import Corpus


def test_calibrated_score_consumer_retains_independence_unknown_and_rechecks_withdrawal(flow, monkeypatch):
    assert callable(getattr(RubricEvidenceService, 'calibrated_receipt', None)), 'No current calibrated-score consumer'
    corpus = Corpus(flow, monkeypatch, count=200)
    corpus.freeze()
    for i in range(200):
        aid, labels = corpus.score(i, score=i // 50, gold=i // 50)
        corpus.service.record_case(CHILD, corpus.pid, assessment_id=aid, blind_receipt_ids=labels,
            expected_revision=i+1, client_action_id='case-' + str(i))
    qualified = corpus.service.evaluate(CHILD, corpus.pid, expected_revision=201, client_action_id='qualify')
    assert qualified['current']['score_qualified']
    fresh, _ = corpus.score(200, score=3)
    with corpus.db.connect() as c:
        step = c.execute('SELECT step_id FROM rubric_assessment WHERE assessment_id=?', (fresh,)).fetchone()[0]
        receipt = corpus.rubrics.calibrated_receipt(c, CHILD, step, now=corpus.clock[0],
                                                   calibration_service=corpus.service)
        assert receipt and receipt['score_status'] == 'calibrated_model'
        assert receipt['correctness'] == 1 and not receipt['independent']
        assert receipt['criterion_evidence'] == [], 'Dimension scores cannot certify arbitrary goal criteria'
        assert receipt['score_authority'] == 'calibrated_model_original_response'
        assert c.total_changes == 0
        assert corpus.rubrics.calibrated_receipt(c, 'child-junior-3', step, now=corpus.clock[0],
                                                calibration_service=corpus.service) is None
    reopened = RubricCalibrationService(corpus.db, authority_resolver=corpus.resolve, clock=lambda: corpus.clock[0])
    with corpus.db.connect() as c:
        assert corpus.rubrics.calibrated_receipt(c, CHILD, step, now=corpus.clock[0],
                                               calibration_service=reopened)['evidence_id'] == receipt['evidence_id']
    corpus.service.revoke(CHILD, corpus.pid, expected_revision=202, client_action_id='withdraw')
    with corpus.db.connect() as c:
        assert corpus.rubrics.calibrated_receipt(c, CHILD, step, now=corpus.clock[0] + timedelta(seconds=1),
                                               calibration_service=reopened) is None
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 201
