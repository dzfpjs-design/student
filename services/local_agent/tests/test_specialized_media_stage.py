"""Real isolated media/answer/review journals must reach current stage consumers."""
import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.specialized_stages import project_specialized_stages
from test_pet_media_evidence import media, CHILD, change
from test_pet_scoped_listening import twice
from test_pet_audio_rubric_review import prepared


def node(db, node_id):
    with db.connect() as c:
        before = c.total_changes
        result = project_specialized_stages(c, CHILD, 'pet')
        assert c.total_changes == before
    return next(n for d in result['domains'] for n in d['nodes'] if n['knowledge_node_id'] == node_id)


def test_original_listening_delivery_answer_reaches_stage_and_recomputes_after_withdrawal(media):
    twice(media)
    db = Database(media[0].path, media_root=media[4].parent)
    answer = DiagnosticPromptService(db).answer(media[2], prompt_id=media[3]['prompt_id'],
        item_version=media[3]['item_version'], answer='in front of the library', client_action_id='stage-answer')
    with db.connect() as c:
        target = c.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?',
                           (answer['attempt']['diagnostic_item_id'],)).fetchone()[0]
    current = node(db, target)
    assert current['qualified_stage'] == 'baseline', current
    assert current['gates']['baseline']['evidence_refs'] == [answer['attempt']['evidence_id']]
    assert node(Database(db.path, media_root=media[4].parent), target)['qualified_stage'] == 'baseline'
    change(db, "UPDATE capture_artifact SET status='deleted' WHERE artifact_id='audio'")
    assert node(db, target)['qualified_stage'] is None


@pytest.mark.parametrize('fault', ['none', 'retract', 'bytes', 'voice', 'low_score', 'text_only'])
def test_original_human_audio_review_reaches_stage_without_certifying_text_or_missing_sources(media, fault):
    db, rubric, snapshot, body, prompt = prepared(media)
    rubric.review(CHILD, snapshot['assessment_id'], **body)
    target = snapshot['knowledge_node_id'] if 'knowledge_node_id' in snapshot else 'node-pet-speaking-picture'
    current = node(db, target)
    assert current['qualified_stage'] == 'baseline', current
    assert current['gates']['baseline']['review_refs']
    assert not current['mastery_claim_allowed']
    if fault == 'none':
        assert node(Database(db.path, media_root=media[4].parent), target)['qualified_stage'] == 'baseline'
        return
    if fault == 'retract':
        rubric.review(CHILD, snapshot['assessment_id'], expected_revision=1, client_action_id='retract',
            reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Withdraw audio judgment')
    elif fault == 'bytes':
        media[4].write_bytes(b'changed original')
    elif fault == 'voice':
        change(db, "UPDATE voice_input SET transcript='a different original answer'")
    else:
        update = {**body, 'expected_revision': 1, 'client_action_id': fault}
        if fault == 'low_score':
            update['dimensions'] = [{**d, 'score': 0} for d in body['dimensions']]
        else:
            update.pop('review_profile'); update.pop('audio_evidence')
            update['dimensions'] = body['dimensions'][:3]
        rubric.review(CHILD, snapshot['assessment_id'], **update)
    assert node(db, target)['qualified_stage'] is None
