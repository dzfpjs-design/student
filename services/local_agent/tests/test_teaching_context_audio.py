"""Original-audio reviews drive bound teaching, never new scoring facts."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.repository import StudyRepository
from home_study_agent.teaching_context import build_teaching_context
from test_pet_audio_rubric_review import prepared
from test_pet_media_evidence import CHILD, media


def facts(db):
    with db.connect() as c:
        return {table: [dict(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('learning_evidence', 'diagnostic_attempt')}


def support(db, media, prompt):
    result = ChildModelService(db).tutor_controls(CHILD, 1, None, {}, session_id=media[2],
        subject='英语', question=prompt['item']['prompt'],
        prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
    assert result['audit']['question_target']['status'] == 'resolved'
    assert result['audit']['cloud_transmission_allowed'] is False
    return result


def restart(db, media):
    return Database(db.path, media_root=media[4].parent)


def test_original_audio_review_corrected_low_then_retracted_changes_bound_controls_after_restart(media):
    db, rubric, assessment, body, prompt = prepared(media)
    original = facts(db)
    evidence = assessment['source_evidence_id']
    unreviewed = support(db, media, prompt)['audit']['teaching_context']
    assert unreviewed['learning']['status'] == 'unknown'
    rubric.review(CHILD, assessment['assessment_id'], **body)
    high = support(restart(db, media), media, prompt)
    high_context = high['audit']['teaching_context']
    assert high_context['learning']['status'] == 'independent_observed'
    assert high_context['learning']['evidence_refs'] == [evidence]
    assert high_context['learning']['review_refs'][0]['review_revision'] == 1
    assert high_context['learning']['score_authority'] == 'versioned_human_original_audio_review_not_reference_score'
    assert high['audit']['controls']['explanation_pacing'] == 'standard_layered'
    assert not high_context['mastery_claim_allowed']

    rubric.review(CHILD, assessment['assessment_id'], **{**body, 'expected_revision': 1,
        'client_action_id': 'teaching-audio-low',
        'dimensions': [{**dimension, 'score': 0} for dimension in body['dimensions']]})
    low = support(restart(db, media), media, prompt)
    low_context = low['audit']['teaching_context']
    assert low_context['learning']['status'] == 'recheck_needed'
    assert low_context['learning']['evidence_refs'] == [evidence]
    assert low_context['learning']['review_refs'][0]['review_revision'] == 2
    assert low_context['source_version'] != high_context['source_version']
    assert low['audit']['controls']['explanation_pacing'] == 'short_step_then_restate'
    assert low['audit']['decisions']['explanation_pacing']['source'] == 'reliable_feedback'
    assert low['audit']['decisions']['explanation_pacing']['evidence_refs'] == [evidence]
    with db.connect() as c:
        transcript = c.execute("SELECT transcript FROM voice_input WHERE voice_input_id='voice'").fetchone()[0]
    for private_value in (transcript, str(media[4]), evidence, assessment['assessment_id']):
        assert private_value not in low['prompt_extension']

    RubricEvidenceService(restart(db, media)).review(CHILD, assessment['assessment_id'],
        expected_revision=2, client_action_id='teaching-audio-retract', reviewer_id='parent',
        reviewer_role='parent', operation='retract', reason='Synthetic original-audio review withdrawn')
    withdrawn = support(restart(db, media), media, prompt)
    withdrawn_context = withdrawn['audit']['teaching_context']
    assert withdrawn_context['learning']['status'] == 'unknown'
    assert withdrawn_context['learning']['evidence_refs'] == []
    assert withdrawn_context['source_version'] != low_context['source_version']
    assert withdrawn['audit']['controls']['explanation_pacing'] == 'standard_layered'
    assert withdrawn['audit']['decisions']['explanation_pacing']['source'] == 'default'
    assert facts(db) == original


@pytest.mark.parametrize('fault', ['missing_media_scope', 'changed_audio', 'changed_transcript', 'text_only_review'])
def test_changed_audio_authority_never_reuses_old_pass_for_teaching(media, fault):
    db, rubric, assessment, body, prompt = prepared(media)
    rubric.review(CHILD, assessment['assessment_id'], **body)
    before = support(db, media, prompt)['audit']['teaching_context']
    assert before['learning']['status'] == 'independent_observed'
    original = facts(db)
    reopened = restart(db, media)
    if fault == 'missing_media_scope':
        reopened = Database(db.path)
    elif fault == 'changed_audio':
        media[4].write_bytes(b'synthetic changed original audio')
    elif fault == 'changed_transcript':
        with db.connect() as c:
            c.execute("UPDATE voice_input SET transcript='Synthetic corrected transcript' WHERE voice_input_id='voice'")
            c.commit()
    else:
        update = {**body, 'expected_revision': 1, 'client_action_id': 'teaching-text-only'}
        update.pop('review_profile')
        update.pop('audio_evidence')
        update['dimensions'] = body['dimensions'][:3]
        rubric.review(CHILD, assessment['assessment_id'], **update)
    result = support(reopened, media, prompt)
    context = result['audit']['teaching_context']
    assert context['learning']['status'] == 'unknown'
    assert context['learning']['evidence_refs'] == []
    assert context['source_version'] != before['source_version']
    assert result['audit']['decisions']['explanation_pacing']['source'] == 'default'
    assert facts(db) == original


def test_original_audio_review_keeps_tutor_exposure_and_readonly_expiry(media, monkeypatch):
    db, rubric, assessment, body, prompt = prepared(media)
    rubric.review(CHILD, assessment['assessment_id'], **body)
    original = facts(db)
    assert support(db, media, prompt)['audit']['teaching_context']['learning']['status'] == 'independent_observed'
    with db.connect() as c:
        row = c.execute('SELECT occurred_at,knowledge_node_id FROM learning_evidence WHERE evidence_id=?',
                        (assessment['source_evidence_id'],)).fetchone()
    before_answer = (datetime.fromisoformat(row['occurred_at']) - timedelta(seconds=1)).isoformat()
    from home_study_agent import repository
    with monkeypatch.context() as clock:
        clock.setattr(repository, 'utc_now', lambda: before_answer)
        StudyRepository(db).save_tutor_turn(media[2], None, '英语', prompt['item']['prompt'],
            'Synthetic explanation of the original speaking task', 'test', 'test', 'audio-teaching-exposure')
    result = support(restart(db, media), media, prompt)
    learning = result['audit']['teaching_context']['learning']
    assert learning['status'] == 'supported_only'
    assert learning['review_refs'][0]['assessment_id'] == assessment['assessment_id']
    assert result['audit']['controls']['explanation_pacing'] == 'short_step_then_restate'
    with db.connect() as c:
        c.execute('BEGIN')
        before = '\n'.join(c.iterdump())
        expired = build_teaching_context(c, CHILD, session_id=media[2], subject='英语',
            knowledge_node_id=row['knowledge_node_id'], task_type='diagnostic',
            now=datetime.now(timezone.utc) + timedelta(days=31))
        assert expired['learning']['status'] == 'unknown'
        assert expired['learning']['reason'] == 'evidence_expired'
        assert '\n'.join(c.iterdump()) == before
    assert facts(db) == original
