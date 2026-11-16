import pytest

from home_study_agent.learning_grade_scope import allows_learning_grade
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import LocalSupplyError, validate_item
from home_study_agent.child_model import ChildModelService
from home_study_agent.repository import StudyRepository
from test_variant_supply import _prepared


@pytest.mark.parametrize('grade,band,track,allowed', [
    ('初三', '四年级', 'school', True), ('六年级', '四年级', 'school', True),
    ('四年级', '四年级-PET', 'pet', True), ('初三', '初三', 'school', True),
    ('四年级', '初三', 'school', False), ('四年级', '五年级', 'school', False),
    ('初三', '四年级-PET', 'pet', False), ('初三', '四年级', 'competition', False),
    ('未知', '四年级', 'school', False), ('', '初三', 'school', False),
])
def test_learning_grade_scope(grade, band, track, allowed):
    assert allows_learning_grade(grade, band, track) is allowed


def test_foundation_item_is_selectable_but_not_a_cross_child_permission(tmp_path):
    db, _, _ = _prepared(tmp_path)
    with db.connect() as c:
        iid = c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id='node-p4-english-phonics-discrimination' LIMIT 1").fetchone()[0]
        assert validate_item(c, 'child-junior-3', iid)['diagnostic_item_id'] == iid
        assert LearningStateService._eligible_diagnostic(c, 'child-junior-3', iid)
        with pytest.raises(LocalSupplyError):
            validate_item(c, 'child-primary-4', 'diag-j3-discriminant-1')
        assert not LearningStateService._eligible_diagnostic(c, 'child-primary-4', 'diag-j3-discriminant-1')


def test_foundation_feedback_reaches_tutor_without_borrowing_other_child_evidence(tmp_path):
    db, _, _ = _prepared(tmp_path)
    repo, learning = StudyRepository(db), LearningStateService(db)
    receipts = {}
    for child, answer in [('child-primary-4', '17'), ('child-junior-3', '90')]:
        if receipts:
            with db.connect() as c:
                c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
                c.commit()
        sid = repo.start_session(child, 'foundation-session:' + child)['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        receipts[child] = learning.submit_diagnostic(sid, 'diag-p4-arithmetic-closed-1', answer,
                                                     'foundation-answer:' + child)
    model = ChildModelService(db)
    model.ensure_schema()
    controls = model.tutor_controls('child-junior-3', 1, None, {}, session_id=sid,
        subject='数学', knowledge_node_id='node-p4-math-arithmetic', task_type='diagnostic')
    state = controls['audit']['teaching_context']['learning']
    assert state['status'] == 'independent_observed'
    assert state['evidence_refs'] == [receipts['child-junior-3']['evidence_id']]
    assert receipts['child-primary-4']['evidence_id'] not in state['evidence_refs']
