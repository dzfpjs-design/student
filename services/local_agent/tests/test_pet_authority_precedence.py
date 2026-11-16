"""Real original/review journals; calibration receipt alone is explicitly injected."""
import json
from datetime import timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.writing_rubric import writing_review_gate
from home_study_agent.writing_stage_reviews import apply_writing_stage_review
from test_pet_calibrated_writing_consumers import calibrated
from test_writing_response_origin import CHILD
from test_writing_stage_reviews import writing_chain
from test_writing_stage_review_consumers import due_answer, review_due


def test_late_automatic_assessment_cannot_complete_after_other_assessment_human_retraction(
        writing_chain, monkeypatch, tmp_path):
    db, task, prompt, text, answered, clock = due_answer(writing_chain, monkeypatch)
    service, automatic, _, _ = calibrated(monkeypatch, writing_chain,
        prepare=lambda: (RubricEvidenceService(db), *review_due(
            db, writing_chain[1], prompt, text, answered)))
    human, human_body = review_due(db, writing_chain[1], prompt, text, answered, score=0)
    assert automatic['assessment_id'] != human['assessment_id']
    assert automatic['source_evidence_id'] == human['source_evidence_id'] == answered['attempt']['evidence_id']
    # The parent disputes independence; this decision must not complete execution.
    service.review(CHILD, human['assessment_id'], **{
        **human_body, 'client_action_id': 'other-assessment-assisted', 'independent': False})
    service.review(CHILD, human['assessment_id'], expected_revision=1,
        client_action_id='other-assessment-retracted', reviewer_id='parent', reviewer_role='parent',
        operation='retract', reason='Withdraw this original-answer judgment')
    with Database(db.path).connect() as c:
        original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?',
            (answered['attempt']['evidence_id'],)).fetchone())
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',
            (task['review_task_id'],)).fetchone()[0] == 'pending'
        now = clock.now(timezone.utc).isoformat()
        assert writing_review_gate(c, CHILD, automatic['source_evidence_id'], as_of=now)['status'] == 'unknown'
        c.execute('BEGIN IMMEDIATE')
        result = None
        rejected = None
        try:
            result = apply_writing_stage_review(c, CHILD, automatic['assessment_id'], as_of=now)
        except ValueError as error:
            rejected = str(error)
            c.rollback()
        else:
            c.commit()
    with Database(db.path).connect() as c:
        status = c.execute('SELECT status FROM review_task WHERE review_task_id=?',
            (task['review_task_id'],)).fetchone()[0]
        after = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?',
            (answered['attempt']['evidence_id'],)).fetchone())
        evidence = dict(database=str(db.path), automatic_assessment_id=automatic['assessment_id'],
            human_assessment_id=human['assessment_id'], source_evidence_id=automatic['source_evidence_id'],
            gate=writing_review_gate(c, CHILD, automatic['source_evidence_id'], as_of=now),
            status=status, result=result, rejected=rejected, original_unchanged=after == original,
            synthetic_calibration_boundary=True, model_http_verified=False)
        (tmp_path/'authority-precedence-result.json').write_text(json.dumps(evidence, indent=2))
        assert after == original
        assert status == 'pending', evidence
        assert not result or result['completed_review_task_id'] is None


def test_two_automatic_assessments_are_not_a_unique_completion_authority(writing_chain, monkeypatch, tmp_path):
    db, task, prompt, text, answered, clock = due_answer(writing_chain, monkeypatch)
    authorities = {}
    for _ in range(2):
        _, assessment, _, receipt = calibrated(monkeypatch, writing_chain,
            prepare=lambda: (RubricEvidenceService(db), *review_due(db, writing_chain[1], prompt, text, answered)))
        authorities[assessment['assessment_id']] = receipt
    assert len(authorities) == 2
    def receipts(self, c, child, step, **kwargs):
        row = c.execute('SELECT assessment_id FROM rubric_assessment WHERE child_id=? AND step_id=?', (child, step)).fetchone()
        if row and not c.execute('SELECT 1 FROM rubric_review_event WHERE assessment_id=?', (row[0],)).fetchone():
            return authorities.get(row[0])
        return None
    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', receipts)
    with db.connect() as c:
        now = clock.now(timezone.utc).isoformat()
        gate = writing_review_gate(c, CHILD, answered['attempt']['evidence_id'], as_of=now)
        assert gate['status'] == 'unknown'
        c.execute('BEGIN IMMEDIATE')
        try:
            result = apply_writing_stage_review(c, CHILD, next(iter(authorities)), as_of=now)
        except ValueError as error:
            c.rollback()
            result = {'rejected': str(error)}
        else:
            c.commit()
    with Database(db.path).connect() as c:
        status = c.execute('SELECT status FROM review_task WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0]
        (tmp_path/'ambiguous-authority-result.json').write_text(json.dumps(
            dict(database=str(db.path), status=status, gate=gate, result=result, synthetic_calibration_boundary=True), indent=2))
        assert status == 'pending'


@pytest.mark.parametrize('human_decision', ['retract', 'low_score'])
def test_other_assessment_actual_parent_http_receipt_and_restart(writing_chain, monkeypatch, tmp_path, human_decision):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.writing_stage_reviews import validate_writing_review_result

    class NoCloud:
        requests = 0
        async def complete(self, *args, **kwargs):
            self.requests += 1
            raise AssertionError('No model request is allowed in this authority-consumer test')

    db, task, prompt, text, answered, clock = due_answer(writing_chain, monkeypatch)
    _, automatic, _, _ = calibrated(monkeypatch, writing_chain,
        prepare=lambda: (RubricEvidenceService(db), *review_due(db, writing_chain[1], prompt, text, answered)))
    human, body = review_due(db, writing_chain[1], prompt, text, answered, score=0)
    assert human['assessment_id'] != automatic['assessment_id']
    assert human['source_evidence_id'] == automatic['source_evidence_id']
    payload = {k:v for k,v in body.items() if k not in {'reviewer_id', 'reviewer_role'}}
    payload.update(client_action_id='actual-parent-second-assessment', independent=human_decision == 'low_score')
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False,
        session_token='isolated-pet-authority-http')
    gateway = NoCloud()
    local = {'X-HSC-Session': settings.session_token}
    path = f'/v1/parent/children/{CHILD}/learning-rubrics/{human["assessment_id"]}'
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        auth = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'})
        assert auth.status_code == 200, auth.text
        headers = {**local, 'X-HSC-Parent': auth.json()['parent_token']}
        assert client.post(path+'/review', headers=local, json=payload).status_code in {401,403}
        assert client.post(path+'/review', headers=headers, json={**payload,'reviewer_role':'teacher'}).status_code == 422
        approved = client.post(path+'/review', headers=headers, json=payload)
        assert approved.status_code == 200, approved.text
        replay = client.post(path+'/review', headers=headers, json=payload)
        assert replay.status_code == 200 and replay.json()['event'] == approved.json()['event']
        if human_decision == 'retract':
            payload = dict(expected_revision=1, client_action_id='actual-parent-second-retract',
                operation='retract', reason='Withdraw this original-answer judgment')
            withdrawn = client.post(path+'/review', headers=headers, json=payload)
            assert withdrawn.status_code == 200, withdrawn.text
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        auth = client.post('/v1/parent/unlock', headers=local, json={'pin':'2468'})
        assert auth.status_code == 200, auth.text
        headers = {**local, 'X-HSC-Parent': auth.json()['parent_token']}
        read = client.get(path, headers=headers)
        assert read.status_code == 200, read.text
        assert client.get(path.replace(CHILD,'child-junior-3'), headers=headers).status_code in {404,409}
        with db.connect() as c:
            now = clock.now(timezone.utc).isoformat()
            if human_decision == 'low_score':
                result = validate_writing_review_result(c, child_id=CHILD, session_id=writing_chain[1],
                    review_task_id=task['review_task_id'], prompt_id=prompt['prompt_id'],
                    evidence_id=answered['attempt']['evidence_id'], as_of=now)
                assert result['performed'] and result['passed'] is False and not result['mastery_claim_allowed']
                assert result['score_status'] == 'reviewed_human' and result['assessment_id'] == human['assessment_id']
            else:
                assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 'pending'
                c.execute('BEGIN IMMEDIATE')
                try:
                    result = apply_writing_stage_review(c, CHILD, automatic['assessment_id'], as_of=now)
                except ValueError as error:
                    c.rollback()
                    result = {'rejected':str(error)}
                else:
                    c.commit()
                status = c.execute('SELECT status FROM review_task WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0]
                (tmp_path/'http-authority-result.json').write_text(json.dumps(
                    dict(database=str(db.path),status=status,result=result,synthetic_calibration_boundary=True),indent=2))
                assert status == 'pending'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (task['review_task_id'],)).fetchone()[0] == 1
    assert gateway.requests == 0
