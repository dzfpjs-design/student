"""Blind calibration labels are not assignments or child-model observations."""
import pytest
from datetime import datetime, timezone

from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.learning_feedback import build_learning_feedback
from test_school_open_reviews import school_case, CHILD
from test_school_spoken_reviews import spoken_review
from contextlib import nullcontext


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
@pytest.mark.parametrize('legacy', [False, True])
def test_oral_holdout_blind_label_does_not_schedule_child_practice(school_case, monkeypatch, legacy):
    original = RubricEvidenceService.freeze
    def freeze(self,c,record,evaluation,call,now,**kwargs):
        return original(self,c,{**record,'calibration':{'status':'bound','mode':'holdout',
            'assessment_binding':{'protocol_id':'synthetic-holdout'}}},evaluation,call,now,**kwargs)
    with monkeypatch.context() as host:
        host.setattr(RubricEvidenceService,'freeze',freeze)
        saved, assessment, path, body, _ = spoken_review(school_case,monkeypatch,approve=False)
    db, client, headers = school_case[:3]
    body.update(independent=False,hint_level=1)
    # Reproduce the previously admitted label through the actual review writer,
    # then restore current validation without rewriting any immutable event.
    with (monkeypatch.context() if legacy else nullcontext()) as old:
        if legacy:
            old.setattr('home_study_agent.school_spoken_score.calibration_only', lambda _:False)
        response = client.post(path,headers=headers,json=body)
        assert response.status_code == 200,response.text
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        from home_study_agent.school_open_reviews import reconcile_school_reviews
        result = reconcile_school_reviews(c,CHILD)
        assert len(result['cancelled']) == int(legacy)
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',
            (assessment['assessment_id'],)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM review_task WHERE source_evidence_id=? AND status='pending'",
            (saved['attempt']['evidence_id'],)).fetchone()[0] == 0
        assert build_learning_feedback(c,CHILD)['nodes'] == []
        assert RubricEvidenceService(db).reviewed_receipt(c,CHILD,assessment['step_id'],
            now=datetime.now(timezone.utc)) is None
        c.commit()
