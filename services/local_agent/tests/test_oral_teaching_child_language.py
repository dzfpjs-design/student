"""Child-facing words do not relax the original audio/assessment gate."""
from test_pet_audio_rubric_review import prepared
from test_pet_media_evidence import CHILD, media
from test_oral_teaching import low, project


def test_neutral_self_check_asks_child_without_internal_scoring_language(media):
    db, service, assessment, body, prompt = prepared(media)
    result = project(db, media[2], prompt)
    point = result['task_points'][0]['task_point']
    assert result['spoken_feedback'] == f'我们核对一下：{point}。你想补充一句吗？'
    assert result['feedback'] == [] and not result['mastery_claim_allowed']
    assert not result['skill_scoring_allowed']
    assert all(p['status'] == 'not_assessed' for p in result['task_points'])


def test_low_review_says_only_actual_specific_suggestion_not_certification_notice(media):
    db, service, assessment, body, prompt = prepared(media)
    review = low(body)
    service.review(CHILD, assessment['assessment_id'], **review)
    result = project(db, media[2], prompt)
    assert result['spoken_feedback'] == review['dimensions'][0]['rationale']
    assert not result['mastery_claim_allowed'] and not result['skill_scoring_allowed']
    assert result['next_action']['independent_check'] is False
