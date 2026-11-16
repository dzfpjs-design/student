"""Candidate reduction is not review authority; downstream checks stay current."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from home_study_agent import school_open_reviews as reviews
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.writing_rubric import prepare_writing_review
from test_school_open_reviews import school_case, CHILD
from test_writing_calibrated_gate import authority
from test_written_score_evidence_authority import other_assessment


@pytest.mark.parametrize('blind', [
    {}, {'review_profile': 'general'}, {'review_profile': 'school_open_written_v1'},
    {'review_profile': 'school_open_written_v1', 'writing_evidence': {'binding_version': 'x'}},
    [], 'invalid json',
])
def test_drop_only_necessarily_ineligible_human_labels_keep_low_human_and_auto(blind):
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    try:
        c.executescript('''CREATE TABLE rubric_assessment(assessment_id TEXT,step_id TEXT,child_id TEXT,payload_json TEXT);
            CREATE TABLE rubric_review_event(event_id TEXT,assessment_id TEXT,revision INTEGER,operation TEXT,payload_json TEXT);''')
        valid = {'review_profile': 'school_open_written_v1', 'passed': False,
                 'writing_evidence': {'binding_version': 'x', 'task_point_evidence': []}}
        for aid, child in [('blind', 'child'), ('low-human', 'child'), ('automatic', 'child'),
                           ('withdrawn', 'child'), ('other-child', 'other')]:
            c.execute('INSERT INTO rubric_assessment VALUES(?,?,?,?)', (aid, aid, child, json.dumps({
                'source_evidence_id': aid, 'calibration_binding': {'protocol_id': 'unverified-candidate'}})))
        for aid, payload in [('blind', blind), ('low-human', valid), ('withdrawn', valid), ('other-child', valid)]:
            c.execute('INSERT INTO rubric_review_event VALUES(?,?,?,?,?)',
                (aid, aid, 1, 'approve', payload if isinstance(payload, str) else json.dumps(payload)))
        c.execute('INSERT INTO rubric_review_event VALUES(?,?,?,?,?)', ('withdraw', 'withdrawn', 2, 'retract', '{}'))
        c.commit()
        c.execute('BEGIN')
        changes = c.total_changes
        with reviews._ReviewCandidates(c, 'child') as index:
            assert set(index.rows) == {'low-human', 'automatic'}
        assert c.total_changes == changes and c.in_transaction
        c.rollback()
    finally:
        c.close()


def anchors(c, evidence):
    now = datetime.now(timezone.utc)
    raw = reviews._anchor(c, CHILD, evidence, now)
    with reviews._ReviewCandidates(c, CHILD) as index:
        indexed = reviews._anchor(c, CHILD, evidence, now, _candidates=index)
    assert raw == indexed
    return indexed


def blocked_anchors(c, evidence):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        reviews._anchor(c, CHILD, evidence, now)
    with reviews._ReviewCandidates(c, CHILD) as index:
        with pytest.raises(ValueError):
            reviews._anchor(c, CHILD, evidence, now, _candidates=index)


def test_cross_assessment_low_human_bad_original_and_retraction_do_not_fall_back(school_case, monkeypatch):
    db, client, headers = school_case[:3]
    evidence, automatic, _, (_, body) = authority(monkeypatch, school_case, 3)
    calibrated = RubricEvidenceService.calibrated_receipt

    def only_original(self, c, child, step, **kwargs):
        row = c.execute('SELECT assessment_id FROM rubric_assessment WHERE child_id=? AND step_id=?', (child, step)).fetchone()
        return calibrated(self, c, child, step, **kwargs) if row and row[0] == automatic['assessment_id'] else None

    monkeypatch.setattr(RubricEvidenceService, 'calibrated_receipt', only_original)
    with db.connect() as c:
        c.execute('BEGIN')
        assert anchors(c, evidence)['current']['assessment_id'] == automatic['assessment_id']
        c.rollback()
    human = other_assessment(school_case, evidence)
    with db.connect() as c:
        binding = prepare_writing_review(c, CHILD, human['assessment_id'])
    path = f'/v1/parent/children/{CHILD}/learning-rubrics/{human["assessment_id"]}/review'
    body = {**body, 'client_action_id': 'indexed-other-low-human',
        'writing_evidence': {**body['writing_evidence'], 'binding_version': binding['binding_version']},
        'dimensions': [{**d, 'score': 0} for d in body['dimensions']]}
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        anchor = anchors(c, evidence)
        assert anchor['current']['assessment_id'] == human['assessment_id']
        assert not anchor['score_decision']['passed']
        changed = c.execute('''UPDATE capture_artifact SET extracted_text='isolated damaged original'
            WHERE child_id=? AND artifact_type='writing_original'
            AND json_extract(details_json,'$.attempt_binding.evidence_id')=?''', (CHILD, evidence))
        assert changed.rowcount == 1
        blocked_anchors(c, evidence)
        c.rollback()
    response = client.post(path, headers=headers, json={'expected_revision': 1,
        'client_action_id': 'indexed-other-retract', 'operation': 'retract', 'reason': 'Synthetic human withdrawal'})
    assert response.status_code == 200, response.text
    with db.connect() as c:
        c.execute('BEGIN')
        with reviews._ReviewCandidates(c, CHILD) as index:
            assert [row['assessment_id'] for row in index.for_evidence(c, CHILD, evidence)] == [automatic['assessment_id']]
        blocked_anchors(c, evidence)
        assert c.total_changes == 0
        c.rollback()
