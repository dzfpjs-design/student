"""Real original-writing records, injected score-authority boundary only.

These consumer tests do not supply deployed calibration or prove model quality.
The authority provider itself has separate real-journal calibration tests.
"""
from datetime import datetime, timezone

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.writing_rubric import writing_review_gate
from test_school_open_reviews import school_case, answer_and_review, CHILD


def authority(monkeypatch, case, score):
    freeze = RubricEvidenceService.freeze
    def bind(self, c, record, evaluation, call_id, now, **kwargs):
        return freeze(self, c, {**record, 'calibration': {'assessment_binding': {
            'protocol_id':'synthetic-consumer-authority'}}}, evaluation, call_id, now, **kwargs)
    # Simulated host admission is installed before the immutable score is saved.
    # Never rewrite the journal or disable its immutability trigger for a test.
    with monkeypatch.context() as host:
        host.setattr(RubricEvidenceService, 'freeze', bind)
        _, answered, current, path, body = answer_and_review(case, submit=False)
    evidence = answered['attempt']['evidence_id']
    calls = []
    receipt = dict(assessment_id=current['assessment_id'], review_revision=0,
        evidence_id='calibrated:synthetic-authority', score_status='calibrated_model',
        correctness=int(score >= 2), independent=True, hint_level=0,
        occurred_at=current['responded_at'], diagnostic_item_id=answered['attempt']['diagnostic_item_id'],
        knowledge_node_id=None, criterion_evidence=[], question_identity='synthetic-consumer-identity',
        criterion_mapping_status='calibrated_dimension_exact',
        score_authority='calibrated_model_original_response',
        calibration_receipt=dict(score_qualified=True, source_evidence_id=evidence,
            assessment_id=current['assessment_id'], diagnostic_item_id=answered['attempt']['diagnostic_item_id'],
            passed=score >= 2, certificate_event_id='synthetic-certificate',
            dimensions=[{**d, 'score':score, 'response_quotes':[current['response']]} for d in current['dimensions']]))

    def qualified(self, connection, child, step, *, now, **kwargs):
        requested = kwargs.get('assessment_id')
        if requested and requested != current['assessment_id']:
            return None
        row = connection.execute('''SELECT assessment_id FROM rubric_assessment
            WHERE step_id=? AND child_id=? AND assessment_id=?''',
            (step, child, current['assessment_id'])).fetchone()
        assert row and child == CHILD
        if connection.execute('SELECT 1 FROM rubric_review_event WHERE assessment_id=?', (row[0],)).fetchone():
            return None
        calls.append(step)
        return receipt

    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', qualified)
    return evidence, receipt, calls, (path, body)


@pytest.mark.parametrize('score', [0, 3])
def test_original_written_calibrated_score_reaches_gate(school_case, monkeypatch, score):
    db = school_case[0]
    evidence, receipt, calls, _ = authority(monkeypatch, school_case, score)
    with db.connect() as c:
        c.execute('BEGIN')
        result = writing_review_gate(c, CHILD, evidence, as_of=datetime.now(timezone.utc).isoformat())
        assert result['status'] == 'calibrated_model', result
        assert result['skill_scoring_allowed'] is (score >= 2)
        assert result['assessment_id'] == receipt['assessment_id']
        assert result['review_revision'] == 0
        assert c.total_changes == 0
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?',(evidence,)).fetchone()[0] is None
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['other_evidence', 'other_item', 'dimension', 'score', 'duplicate', 'not_independent'])
def test_score_qualification_cannot_substitute_original_scope_or_independence(school_case, monkeypatch, change):
    db = school_case[0]
    evidence, receipt, _, _ = authority(monkeypatch, school_case, 3)
    proof = receipt['calibration_receipt']
    if change == 'other_evidence': proof['source_evidence_id'] = 'other'
    if change == 'other_item': proof['diagnostic_item_id'] = 'other'
    if change == 'dimension': proof['dimensions'][0]['criterion'] = 'Unrelated skill'
    if change == 'score': proof['dimensions'][0]['score'] = 0
    if change == 'duplicate': proof['dimensions'][1] = proof['dimensions'][0]
    if change == 'not_independent': receipt['independent'] = False
    with db.connect() as c:
        c.execute('BEGIN')
        result = writing_review_gate(c, CHILD, evidence)
        assert result['skill_scoring_allowed'] is False
        assert result['status'] == ('calibrated_model' if change == 'not_independent' else 'unknown')
        assert writing_review_gate(c, 'child-primary-4', evidence)['status'] == 'unknown'
        assert c.total_changes == 0


def test_real_human_decision_and_retraction_are_not_replaced_by_model(school_case, monkeypatch):
    db, client, headers = school_case[:3]
    evidence, _, calls, (path, body) = authority(monkeypatch, school_case, 3)
    for d in body['dimensions']: d['score'] = 0
    assert client.post(path, headers=headers, json=body).status_code == 200
    with db.connect() as c:
        result = writing_review_gate(c, CHILD, evidence)
        assert result['status'] == 'reviewed_human' and not result['skill_scoring_allowed']
    assert not calls
    revoked = client.post(path, headers=headers, json=dict(operation='retract', expected_revision=1,
        client_action_id='withdraw-synthetic-review', reason='Synthetic correction'))
    assert revoked.status_code == 200, revoked.text
    with db.connect() as c:
        assert writing_review_gate(c, CHILD, evidence)['status'] == 'unknown'
    assert not calls


def test_calibrated_original_creates_review_and_withdrawal_cancels_without_rewriting_answer(school_case, monkeypatch):
    from home_study_agent.school_open_reviews import reconcile_school_reviews
    db, client, headers, sid, baseline, near = school_case[:6]
    evidence, receipt, _, _ = authority(monkeypatch, school_case, 3)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result = reconcile_school_reviews(c, CHILD)
        assert len(result['created']) == 1, result
        row = dict(c.execute('SELECT * FROM review_task WHERE review_task_id=?', (result['created'][0],)).fetchone())
        assert row['diagnostic_item_id'] == near['diagnostic_item_id']
        assert row['interval_label'] == 'school_open_near'
        c.commit()
    assert client.school_reopen().get(f'/v1/sessions/{sid}').status_code == 200
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_school_reviews(c, CHILD)['retained'] == [row['review_task_id']]
        c.commit()
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *args, **kwargs: None)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        cancelled = reconcile_school_reviews(c, CHILD)
        assert cancelled['cancelled'] == [row['review_task_id']]
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?',(evidence,)).fetchone()[0] is None
        c.commit()


@pytest.mark.parametrize('withdraw_first', [False, True])
def test_real_parent_review_replaces_calibrated_obligation_without_reviving_it(school_case, monkeypatch, withdraw_first):
    from home_study_agent.school_open_reviews import reconcile_school_reviews, validate_review_source
    db, client, headers = school_case[:3]
    evidence, receipt, _, (path, body) = authority(monkeypatch, school_case, 3)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old_id = reconcile_school_reviews(c, CHILD)['created'][0]
        c.commit()
    if withdraw_first:
        monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', lambda *a, **k: None)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            assert reconcile_school_reviews(c, CHILD)['cancelled'] == [old_id]
            c.commit()
    reviewed = client.post(path, headers=headers, json=body)
    assert reviewed.status_code == 200, reviewed.text
    with db.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (evidence,))]
        pending = [r for r in rows if r['status'] == 'pending']
        assert len(pending) == 1, rows
        new = pending[0]
        assert new['review_task_id'] != old_id
        assert next(r for r in rows if r['review_task_id'] == old_id)['status'] == 'cancelled'
        validate_review_source(c, CHILD, new)
    assert client.post(path, headers=headers, json=body).status_code == 200
    client = client.school_reopen()
    headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin':'8264'}).json()['parent_token']}
    assert client.post(path, headers=headers, json=body).status_code == 200
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_school_reviews(c, CHILD)['created'] == []
        assert reconcile_school_reviews(c, 'child-primary-4')['created'] == []
        assert c.execute('SELECT COUNT(*) FROM review_task WHERE source_evidence_id=?', (evidence,)).fetchone()[0] == 2
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?', (evidence,)).fetchone()[0] is None
        c.rollback()


def test_successor_insert_failure_rolls_back_cancel_and_review_same_key_recovers(school_case, monkeypatch):
    import sqlite3
    from home_study_agent.school_open_reviews import reconcile_school_reviews
    db, client, headers = school_case[:3]
    evidence, _, _, (path, body) = authority(monkeypatch, school_case, 3)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = reconcile_school_reviews(c, CHILD)['created'][0]
        c.execute("CREATE TRIGGER fail_successor BEFORE INSERT ON review_task WHEN NEW.predecessor_review_task_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'successor disk fault'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='successor disk fault'):
        client.post(path, headers=headers, json=body)
    with db.connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (old,)).fetchone()[0] == 'pending'
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_successor')
        c.commit()
    assert client.post(path, headers=headers, json=body).status_code == 200
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM review_task WHERE source_evidence_id=? AND status='pending'", (evidence,)).fetchone()[0] == 1
