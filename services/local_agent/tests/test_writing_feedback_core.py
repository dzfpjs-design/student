"""Candidate teaching is not mastery certification; no network or private data."""
import asyncio

import pytest

from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from test_formative_evaluator import evaluation
from test_formative_evaluation_projection import projection, writing


def test_candidate_feedback_survives_execution_and_read_only_recovery(evaluation):
    _, _, service, _, calls, turn = evaluation
    result = asyncio.run(service.evaluate(turn))
    assert result['evaluation']['certification_status'] == 'candidate'
    for returned in (result, service.execution_status(turn)['result'], asyncio.run(service.evaluate(turn))):
        assert returned['response_text'].count('Make the meeting time easier to find.') == 1, returned['evaluation'].get('feedback_reason')
        assert 'Revise the meeting time sentence.' not in returned['response_text']
    assert len(calls) == 1


@pytest.mark.parametrize('claim', ['You have mastered writing.', '你已经完全掌握了写作。'])
def test_candidate_speech_does_not_repeat_mastery_claim_or_unbound_question(claim):
    response = LearningOutcomeEvaluator._response_text({
        'formative_only': True, 'formative_feedback_kind': 'written', 'objective_update_applied': True,
        'mastery_gate': {'status': 'unknown'},
        'spoken_feedback': claim + ' Make the meeting time easier to find.',
        'next_check': 'Write a new letter about PRIVATE_UNBOUND_TASK.',
    })
    assert 'Make the meeting time easier to find.' in response
    assert claim not in response and 'PRIVATE_UNBOUND_TASK' not in response


@pytest.mark.parametrize('fault', ['privacy', 'source', 'original', 'cancel', 'answer'])
def test_completed_feedback_rechecks_its_original_source_before_speaking(evaluation, fault):
    writing, scope, service, _, calls, turn = evaluation
    asyncio.run(service.evaluate(turn))
    with writing[0].connect() as c:
        if fault == 'privacy':
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        elif fault == 'source':
            c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id='diag-pet-writing-1'")
        elif fault == 'original':
            c.execute("UPDATE capture_artifact SET status='withdrawn'")
        elif fault == 'answer':
            c.execute("UPDATE diagnostic_attempt SET submitted_answer='replacement answer'")
        else:
            c.execute("UPDATE agent_objective SET status='cancelled' WHERE objective_id=?", (scope['objective_id'],))
        c.commit()
    result = service.execution_status(turn)['result']
    assert result['evaluation']['feedback_status'] == 'blocked'
    assert 'Make the meeting time easier to find.' not in result['response_text']
    assert 'Revise the meeting time sentence.' not in result['response_text']
    assert len(calls) == 1


def test_postcommit_revocation_preserves_assessment_but_suppresses_delivery(evaluation, monkeypatch):
    writing, _, service, _, calls, turn = evaluation
    def revoke_after_commit(*args, **kwargs):
        with writing[0].connect() as c:
            assert c.execute("SELECT state FROM learning_evaluation_execution").fetchone()[0] == 'completed'
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            c.commit()
    monkeypatch.setattr(service, '_complete_retention_task', revoke_after_commit)
    result = asyncio.run(service.evaluate(turn))
    assert result['evaluation']['feedback_status'] == 'blocked'
    assert 'Make the meeting time easier to find.' not in result['response_text']
    assert len(calls) == 1
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1


def test_specific_feedback_keeps_numbers_and_deduplicates_without_inventing_a_task():
    text = 'Change 1.30 pm to 13.30 for consistency.'
    response = LearningOutcomeEvaluator._response_text({
        'formative_only': True, 'formative_feedback_kind': 'written',
        'objective_update_applied': True, 'mastery_gate': {'status': 'unknown'},
        'spoken_feedback': text + ' ' + text, 'next_check': 'PRIVATE_UNBOUND_TASK',
    })
    assert response.count(text) == 1
    assert 'PRIVATE_UNBOUND_TASK' not in response
