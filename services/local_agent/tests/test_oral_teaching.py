"""Synthetic original PCM and real rubric services; not speech-quality validation."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from test_pet_audio_rubric_review import prepared
from test_pet_media_evidence import CHILD, media, change
from test_teaching_context_audio import facts


def project(db, sid, prompt, **kwargs):
    from home_study_agent.oral_teaching import project_oral_teaching
    with db.connect() as c:
        c.execute('BEGIN')
        before = list(c.iterdump())
        changes = c.total_changes
        result = project_oral_teaching(c, CHILD, sid, prompt['prompt_id'],
                                      prompt['item_version'], **kwargs)
        assert c.in_transaction and c.total_changes == changes
        assert list(c.iterdump()) == before
        return result


def low(body, revision=0):
    return {**body, 'expected_revision': revision, 'client_action_id': 'oral-low-' + str(revision),
        'dimensions': [{**d, 'score': 0,
            'rationale': 'Please reconnect the two ideas before trying the whole answer again.'}
            for d in body['dimensions']]}


def test_candidate_is_neutral_task_self_check_not_missing_points_or_speech_score(media):
    db, service, assessment, body, prompt = prepared(media)
    result = project(db, media[2], prompt)
    assert result['status'] == 'content_self_check'
    assert result['task_points']
    assert all(p['status'] == 'not_assessed' for p in result['task_points'])
    assert result['feedback'] == [] and not result['mastery_claim_allowed']
    assert result['next_action']['kind'] == 'self_check_original_task'
    assert result['next_action']['task_point'] in [p['task_point'] for p in result['task_points']]
    assert not result['skill_scoring_allowed']
    assert result['response_authority'] == 'unverified_transcript_not_heard_audio'
    assert result['local_only'] and not result['cloud_transmission_allowed']


def test_low_review_concrete_feedback_restart_retract_no_old_success_fallback(media):
    db, service, assessment, body, prompt = prepared(media)
    before = facts(db)
    service.review(CHILD, assessment['assessment_id'], **body)
    high = project(db, media[2], prompt)
    service.review(CHILD, assessment['assessment_id'], **low(body, 1))
    reopened = Database(db.path, media_root=media[4].parent)
    result = project(reopened, media[2], prompt)
    assert result['status'] == 'reviewed_audio_feedback'
    assert result['review_ref']['review_revision'] == 2
    assert all(f['score'] == 0 for f in result['feedback'])
    assert 'reconnect the two ideas' in result['spoken_feedback']
    assert result['next_action']['kind'] == 'retry_original_supported'
    assert result['next_action']['requires_new_prompt_and_capture']
    assert not result['next_action']['independent_check']
    assert not result['skill_scoring_allowed'] and not result['mastery_claim_allowed']
    assert result['feedback_version'] != high['feedback_version']
    assert project(reopened, media[2], prompt) == result
    assert all(f['audio_observation']['end_frame'] == 16000 for f in result['feedback'] if f['audio_observation'])
    service.review(CHILD, assessment['assessment_id'], expected_revision=2, client_action_id='retract',
        reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Review withdrawn')
    withdrawn = project(reopened, media[2], prompt)
    assert withdrawn['status'] == 'content_self_check' and withdrawn['feedback'] == []
    assert withdrawn['feedback_version'] != result['feedback_version']
    assert withdrawn['review_ref'] is None
    assert facts(db) == before


@pytest.mark.parametrize('fault', ['source', 'audio', 'transcript', 'text_only', 'future', 'expired', 'cancel'])
def test_invalid_authority_never_replays_feedback(media, fault):
    db, service, assessment, body, prompt = prepared(media)
    service.review(CHILD, assessment['assessment_id'], **low(body))
    valid = project(db, media[2], prompt)
    assert valid['status'] == 'reviewed_audio_feedback'
    kwargs = {}
    if fault == 'source':
        change(db, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
    elif fault == 'audio':
        media[4].write_bytes(b'changed synthetic bytes')
    elif fault == 'transcript':
        change(db, "UPDATE voice_input SET transcript='different original' WHERE voice_input_id='voice'")
    elif fault == 'cancel':
        change(db, "UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?", (prompt['prompt_id'],))
    elif fault == 'text_only':
        from test_pet_media_evidence import review_rubric
        review_rubric(service, assessment, revision=1)
    elif fault == 'future':
        kwargs['as_of'] = datetime.fromisoformat(service.get(CHILD, assessment['assessment_id'])['review']['created_at']) - timedelta(microseconds=1)
    else:
        kwargs['as_of'] = datetime.now(timezone.utc) + timedelta(days=31)
    result = project(db, media[2], prompt, **kwargs)
    assert result['status'] != 'reviewed_audio_feedback'
    assert result['feedback'] == [] and result['review_ref'] is None
    assert result['feedback_version'] != valid['feedback_version']
    assert not result['skill_scoring_allowed']


def test_exact_scope_and_borrowed_rollback_do_not_write_or_commit(media):
    from home_study_agent.oral_teaching import project_oral_teaching, OralTeachingScopeError
    db, service, assessment, body, prompt = prepared(media)
    service.review(CHILD, assessment['assessment_id'], **body)
    before = project(db, media[2], prompt)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for child, sid, version in [('child-junior-3', media[2], prompt['item_version']),
            (CHILD, 'wrong-session', prompt['item_version']), (CHILD, media[2], 'old-version')]:
            with pytest.raises(OralTeachingScopeError):
                project_oral_teaching(c, child, sid, prompt['prompt_id'], version)
        c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
        assert project_oral_teaching(c, CHILD, media[2], prompt['prompt_id'], prompt['item_version'])['status'] == 'unavailable'
        assert c.in_transaction
        c.rollback()
    assert project(db, media[2], prompt) == before


def test_tutor_exposure_keeps_review_detail_but_not_independent_credit(media, monkeypatch):
    from home_study_agent import repository
    from home_study_agent.repository import StudyRepository
    db, service, assessment, body, prompt = prepared(media)
    service.review(CHILD, assessment['assessment_id'], **body)
    with db.connect() as c:
        at = c.execute('SELECT occurred_at FROM learning_evidence WHERE evidence_id=?',
            (assessment['source_evidence_id'],)).fetchone()[0]
    monkeypatch.setattr(repository, 'utc_now', lambda: (datetime.fromisoformat(at)-timedelta(seconds=1)).isoformat())
    StudyRepository(db).save_tutor_turn(media[2], None, '英语', prompt['item']['prompt'],
        'Synthetic explanation of the original answer', 'test', 'test', 'oral-exposure')
    result = project(db, media[2], prompt)
    assert result['status'] == 'reviewed_audio_feedback' and result['feedback']
    assert not result['independent_observed'] and not result['skill_scoring_allowed']
    assert result['next_action']['independent_check'] is False


def test_projection_requires_owner_transaction_and_does_not_begin_one(media):
    from home_study_agent.oral_teaching import project_oral_teaching
    db, service, assessment, body, prompt = prepared(media)
    with db.connect() as c:
        assert not c.in_transaction
        with pytest.raises(ValueError, match='ORAL_TEACHING_READ_TRANSACTION_REQUIRED'):
            project_oral_teaching(c, CHILD, media[2], prompt['prompt_id'], prompt['item_version'])
        assert not c.in_transaction and c.total_changes == 0
