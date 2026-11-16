"""Read-only-main audit: authored school math and real parent review ASGI."""
import json
from datetime import datetime, timezone

from test_school_open_reviews import school_case, answer_and_review, CHILD


def test_qualified_low_school_original_reaches_child_feedback(school_case, tmp_path):
    from home_study_agent.learning_feedback import build_learning_feedback
    from home_study_agent.school_open_reviews import _anchor
    from home_study_agent.strategy_evolution import StrategyEvolutionService

    db, client, headers = school_case[:3]
    prompt, saved, assessment, _, _ = answer_and_review(school_case, score=0)
    evidence_id = saved['attempt']['evidence_id']
    with db.connect() as c:
        now = datetime.now(timezone.utc)
        anchor = _anchor(c, CHILD, evidence_id, now)
        assert anchor['score_decision']['passed'] is False
        assert anchor['score_decision']['status'] == 'reviewed_human'
        assert anchor['receipt']['independent'] is True
        reviews = [dict(r) for r in c.execute(
            'SELECT review_task_id,status,interval_label FROM review_task WHERE source_evidence_id=?',
            (evidence_id,))]
        assert len(reviews) == 1 and reviews[0]['status'] == 'pending'
        feedback = build_learning_feedback(c, CHILD, now)
    response = client.get(f'/v1/parent/children/{CHILD}/longitudinal-model', headers=headers)
    assert response.status_code == 200, response.text
    model = response.json()
    strategy = StrategyEvolutionService(db).maybe_propose(CHILD)
    result = {
        'child_id': CHILD, 'prompt_id': prompt['prompt_id'],
        'evidence_id': evidence_id, 'assessment_id': assessment['assessment_id'],
        'score_decision': anchor['score_decision'], 'reviews': reviews,
        'learning_feedback': feedback,
        'model_learning': model.get('learning', model.get('payload', {}).get('learning')),
        'model_keys': list(model), 'strategy_candidate': strategy,
        'boundary': 'synthetic authored original and human review; ASGI/SQLite, no external model',
    }
    (tmp_path / 'consumer-result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
    assert any(evidence_id in node['evidence_refs'] for node in feedback['nodes']), (
        'Current qualified low school original schedules a review, but is absent from '
        'the shared child/planning/strategy feedback consumer; this is observation, not mastery.')
