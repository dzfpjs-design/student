"""Immutable journal order, not UUID ordering, selects the latest review."""
from datetime import datetime, timezone, timedelta

import pytest

from home_study_agent import learning_rubric
from home_study_agent.school_open_reviews import _anchor
from test_school_open_reviews import school_case, answer_and_review, CHILD
from test_school_spoken_reviews import spoken_review
from test_written_score_evidence_authority import other_assessment


@pytest.mark.parametrize('school_case', ['spoken','public'], indirect=True)
def test_same_time_later_human_correction_and_retraction_override_prior_score(school_case, monkeypatch):
    db, client, headers, sid = school_case[:4]
    with db.connect() as c:
        import json
        spoken = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
            (school_case[4]['diagnostic_item_id'],)).fetchone()[0])['response_mode'] == 'spoken_explanation'
    if spoken:
        scored, assessment, path, body, _ = spoken_review(school_case, monkeypatch, approve=False, score=3)
    else:
        _, scored, assessment, path, body = answer_and_review(school_case, submit=False, score=3)
    fixed = datetime.now(timezone.utc) + timedelta(minutes=1)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    def review(url, payload, event_id):
        with monkeypatch.context() as clock:
            clock.setattr(learning_rubric, 'datetime', Clock)
            clock.setattr(learning_rubric, 'uuid4', lambda: event_id)
            response = client.post(url, headers=headers, json=payload)
            assert response.status_code == 200, response.text
    review(path, body, 'ffffffff-ffff-ffff-ffff-ffffffffffff')
    latest = other_assessment(school_case, scored['attempt']['evidence_id'])
    latest_path = f'/v1/parent/children/{CHILD}/learning-rubrics/{latest["assessment_id"]}/review'
    corrected = {**body, 'client_action_id':'same-time-correct',
        'dimensions':[{**d,'score':0} for d in body['dimensions']]}
    if not spoken:
        from home_study_agent.writing_rubric import prepare_writing_review
        with db.connect() as c:
            binding = prepare_writing_review(c, CHILD, latest['assessment_id'])
        corrected['writing_evidence'] = {**body['writing_evidence'], 'binding_version':binding['binding_version']}
    review(latest_path, corrected, '11111111-1111-1111-1111-111111111111')
    with db.connect() as c:
        selected = _anchor(c, CHILD, scored['attempt']['evidence_id'], fixed)
        assert selected['current']['assessment_id'] == latest['assessment_id']
        assert selected['score_decision']['passed'] is False
    review(latest_path, dict(operation='retract', expected_revision=1,
        client_action_id='same-time-retract', reason='Withdraw the latest reasoning review'),
        '00000000-0000-0000-0000-000000000000')
    with db.connect() as c:
        with pytest.raises(ValueError):
            _anchor(c, CHILD, scored['attempt']['evidence_id'], fixed)
