"""Current teaching must consume the same reviewed originals as stage planning."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.teaching_context import build_teaching_context
from test_writing_rubric import prepared
from test_writing_response_origin import writing
from test_pet_media_evidence import review_rubric
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, CHILD


def support(db, sid, **kwargs):
    return ChildModelService(db).tutor_controls(CHILD, 1, None, {}, session_id=sid,
        subject='英语', knowledge_node_id='node-pet-writing-email', task_type='diagnostic', **kwargs)


def test_reviewed_writing_changes_teaching_and_retraction_recomputes_current_support(writing_chain):
    db, sid = writing_chain[:2]
    assessment, scored, body = complete_transfer_chain(writing_chain)
    with db.connect() as c:
        before = [dict(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY evidence_id')]
    accepted = support(db, sid)
    learning = accepted['audit']['teaching_context']['learning']
    assert learning['status'] == 'independent_observed'
    assert learning['evidence_refs'] == [scored['attempt']['evidence_id']]
    assert accepted['audit']['controls']['explanation_pacing'] == 'standard_layered'
    assert not accepted['audit']['teaching_context']['mastery_claim_allowed']

    reviews = RubricEvidenceService(db)
    reviews.review(CHILD, assessment['assessment_id'], **{**body, 'expected_revision': 1,
        'client_action_id': 'correct-writing-for-teaching',
        'dimensions': [{**dimension, 'score': 0} for dimension in body['dimensions']]})
    corrected = support(Database(db.path), sid)
    context = corrected['audit']['teaching_context']
    assert context['learning']['status'] == 'recheck_needed'
    assert context['learning']['evidence_refs'] == [scored['attempt']['evidence_id']]
    assert context['source_version'] != accepted['audit']['teaching_context']['source_version']
    assert corrected['audit']['controls']['explanation_pacing'] == 'short_step_then_restate'
    assert 'Dear Ben' not in corrected['prompt_extension']
    assert scored['attempt']['evidence_id'] not in corrected['prompt_extension']

    reviews.review(CHILD, assessment['assessment_id'], expected_revision=2,
        client_action_id='retract-writing-for-teaching', reviewer_id='parent', reviewer_role='parent',
        operation='retract', reason='The synthetic review needs verification')
    withdrawn = support(Database(db.path), sid)
    assert scored['attempt']['evidence_id'] not in withdrawn['audit']['teaching_context']['learning']['evidence_refs']
    assert withdrawn['audit']['teaching_context']['source_version'] != context['source_version']
    with db.connect() as c:
        assert [dict(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY evidence_id')] == before


@pytest.mark.parametrize('fault', ['candidate_only', 'generic_review', 'original_withdrawn', 'other_child_original', 'future_review'])
def test_unreliable_or_changed_writing_cannot_choose_teaching_support(writing, fault, monkeypatch):
    db, sid = writing[:2]
    service, snapshot, body = prepared(writing)
    if fault == 'generic_review':
        review_rubric(service, snapshot)
    elif fault == 'future_review':
        from home_study_agent import learning_rubric
        future = datetime.now(timezone.utc) + timedelta(days=1)

        class LaterClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future.astimezone(tz) if tz else future.replace(tzinfo=None)

        # Preserve the immutable journal: append under a simulated future clock.
        with monkeypatch.context() as patch:
            patch.setattr(learning_rubric, 'datetime', LaterClock)
            service.review(CHILD, snapshot['assessment_id'], **body)
    elif fault != 'candidate_only':
        service.review(CHILD, snapshot['assessment_id'], **body)
        with db.connect() as c:
            if fault == 'original_withdrawn':
                c.execute("UPDATE capture_artifact SET status='withdrawn'")
            elif fault == 'other_child_original':
                c.execute("UPDATE capture_artifact SET child_id='child-junior-3'")
            c.commit()
    result = support(Database(db.path), sid)
    assert result['audit']['teaching_context']['learning']['status'] == 'unknown'
    assert result['audit']['teaching_context']['learning']['evidence_refs'] == []
    assert result['audit']['controls']['explanation_pacing'] == 'standard_layered'


def test_bound_original_question_uses_review_but_expired_feedback_stays_unknown(writing):
    db, sid, _, prompt, _ = writing
    service, snapshot, body = prepared(writing)
    service.review(CHILD, snapshot['assessment_id'], **{**body, 'hint_level': 1, 'independent': False})
    result = support(db, sid, question=prompt['item']['prompt'],
        prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
    context = result['audit']['teaching_context']
    assert context['learning']['status'] == 'supported_only'
    assert context['learning']['review_refs'][0]['assessment_id'] == snapshot['assessment_id']
    assert result['audit']['controls']['explanation_pacing'] == 'short_step_then_restate'
    with db.connect() as c:
        expired = build_teaching_context(c, CHILD, session_id=sid, subject='英语',
            knowledge_node_id='node-pet-writing-email', task_type='diagnostic',
            now=datetime.now(timezone.utc) + timedelta(days=31))
    assert expired['learning']['status'] == 'unknown'
    assert expired['learning']['reason'] == 'evidence_expired'


def test_published_source_withdrawal_removes_its_teaching_evidence(writing_chain):
    db, sid, _, _, supply, _, far = writing_chain
    _, scored, _ = complete_transfer_chain(writing_chain)
    before = support(db, sid)['audit']['teaching_context']
    supply.withdraw(CHILD, far['candidate_id'], actor='parent', note='Withdraw the synthetic source',
        action_id='withdraw-teaching-source', expected_version=4)
    after = support(Database(db.path), sid)['audit']['teaching_context']
    assert scored['attempt']['evidence_id'] not in after['learning']['evidence_refs']
    assert before['source_version'] != after['source_version']
    with db.connect() as c:
        other = build_teaching_context(c, 'child-junior-3', subject='英语',
            knowledge_node_id='node-pet-writing-email', task_type='diagnostic')
    assert other['status'] == 'invalid_context'
    assert other['learning']['evidence_refs'] == []
