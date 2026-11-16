"""Corrupt unrelated journals do not grant or suppress original-answer authority."""
from datetime import datetime, timezone
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.writing_score import current_written_score
from home_study_agent.writing_rubric import writing_review_gate
from test_school_open_reviews import school_case, answer_and_review, CHILD
from test_written_score_evidence_authority import other_assessment
from test_writing_calibrated_gate import authority


def corrupt_history(c, assessment_id, payload, child_id=CHILD):
    # Isolated on-disk corruption injection; restore the exact production guard
    # before exercising any reader. Matches the prior-history regression fixture.
    guard = c.execute("SELECT sql FROM sqlite_master WHERE name='rubric_assessment_no_update'").fetchone()[0]
    c.execute('DROP TRIGGER rubric_assessment_no_update')
    try:
        c.execute('UPDATE rubric_assessment SET payload_json=?,child_id=? WHERE assessment_id=?',
            (payload, child_id, assessment_id))
    finally:
        c.execute(guard)


@pytest.mark.parametrize('owner', ['same_child', 'other_child'])
@pytest.mark.parametrize('corruption', ['invalid_json', 'invalid_shape'])
def test_unrelated_corrupt_assessment_is_isolated(school_case, monkeypatch, owner, corruption):
    db = school_case[0]
    target_eid, target, _, _ = authority(monkeypatch, school_case, 3)
    _, answered, other, _, _ = answer_and_review(school_case, key='unrelated', submit=False)
    eid = answered['attempt']['evidence_id']
    assert eid != target_eid
    with db.connect() as c:
        now = datetime.now(timezone.utc)
        before = current_written_score(c, CHILD, target['assessment_id'], now=now)
        gate = writing_review_gate(c, CHILD, target_eid, as_of=now.isoformat())
        bad = '{' if corruption == 'invalid_json' else json.dumps({'source_evidence_id': eid})
        corrupt_history(c, other['assessment_id'], bad,
            CHILD if owner == 'same_child' else 'child-primary-4')
        c.commit()
    with Database(db.path).connect() as c:
        assert current_written_score(c, CHILD, target['assessment_id'], now=now) == before
        assert writing_review_gate(c, CHILD, target_eid, as_of=now.isoformat()) == gate
        assert c.total_changes == 0


@pytest.mark.parametrize('corruption', ['invalid_json', 'missing_source', 'wrong_source', 'invalid_shape'])
def test_identifiable_current_source_corruption_blocks_older_automatic_authority(school_case, monkeypatch, corruption):
    db = school_case[0]
    eid, target, _, _ = authority(monkeypatch, school_case, 3)
    other = other_assessment(school_case, eid)
    bad = {'invalid_json': '{', 'missing_source': '{}',
           'wrong_source': json.dumps({'source_evidence_id': 'unrelated-corrupt-claim'}),
           'invalid_shape': json.dumps({'source_evidence_id': eid})}[corruption]
    with db.connect() as c:
        corrupt_history(c, other['assessment_id'], bad)
        c.commit()
    with Database(db.path).connect() as c:
        with pytest.raises(ValueError, match='SCORE_HISTORY_CORRUPT'):
            current_written_score(c, CHILD, target['assessment_id'], now=datetime.now(timezone.utc))
        assert writing_review_gate(c, CHILD, eid)['status'] == 'unknown'
        assert c.total_changes == 0
