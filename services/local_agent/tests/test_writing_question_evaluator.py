"""Published real source -> purpose permission -> existing evaluator, no network."""
import asyncio
import json

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.formative_evaluation_projection import build_formative_projection
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator, LearningEvaluationUnknownError, InvalidLearningEvaluationError
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_response_origin import WritingOriginalService
from test_formative_evaluation_projection import objective
from test_formative_evaluator import evaluation
from test_writing_question_authorization import published_question, grant, CHILD

TEXT = 'Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance because the music was cheerful. Would you like to come with me to our next concert in June? Best wishes, Mia.'


@pytest.fixture
def projection(published_question):
    db,_,_,published,_ = published_question
    sid = StudyRepository(db).start_session(CHILD,'nonseed-writing')['session_id']
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid,track='pet',preferred_item_id=published['diagnostic_item_id'],client_action_id='ask')['prompt']
    originals = WritingOriginalService(db)
    original = originals.capture_native_response(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],text=TEXT,client_action_id='original')
    scored = prompts.answer(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=TEXT,
        writing_original_artifact_id=original['artifact_id'],client_action_id='answer')
    scope = objective(db,sid,prompt,scored['attempt'])
    return (db,sid,prompts,prompt,originals),scope


def test_reviewed_nonseed_evaluator_requires_explicit_scope_and_persists_minimal_formative_result(evaluation,published_question):
    case,scope,evaluator,gateway,calls,turn = evaluation
    db = case[0]
    with db.connect() as c:
        assert build_formative_projection(c,**scope)['status']=='local_required'
    grant(published_question)
    with db.connect() as c:
        projection = build_formative_projection(c,**scope)
        assert projection['status']=='ready_for_runtime_checks',projection
        assert c.total_changes==0
    result = asyncio.run(evaluator.evaluate(turn))
    assert result['evaluation']['formative_only'] and not result['evaluation']['automatic_certification']
    assert len(calls)==1
    payload = json.loads(calls[0].messages[1]['content'])
    assert set(payload)=={'question','response','rubric'} and payload['response']==TEXT
    assert payload['rubric']['task_points'] and 'exact_answers' not in json.dumps(payload)
    assert not any(value in json.dumps(calls[0].messages) for value in (
        CHILD,scope['prompt_id'],'source_snapshot_json','knowledge_node_id','standard_quote','CC0','license_basis'))
    restarted = LearningOutcomeEvaluator(db,StudyRepository(db),gateway,evaluator.settings)
    assert asyncio.run(restarted.evaluate(turn))==result and len(calls)==1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0]==0


@pytest.mark.parametrize('fault',['revoke','consent','source','curriculum','mixed_local'])
@pytest.mark.parametrize('when',['before_dispatch','before_persist','response_ready'])
def test_nonseed_evaluator_revalidates_permission_and_source_at_every_boundary(evaluation,published_question,monkeypatch,fault,when):
    case,scope,evaluator,gateway,calls,turn = evaluation
    db,service,_,published,privacy = published_question
    grant(published_question)
    def invalidate():
        if fault=='revoke': service.revoke(CHILD,diagnostic_item_id=published['diagnostic_item_id'],expected_revision=1,
            actor_id='authenticated-parent',client_action_id='revoke')
        elif fault=='consent': privacy.update_preferences({'cloud_model_text_allowed':False})
        else:
            with db.connect() as c:
                if fault=='source': c.execute("UPDATE knowledge_source SET review_status='quarantined'")
                elif fault=='curriculum': c.execute("UPDATE curriculum_standard_page SET content_text='changed private scope'")
                else: c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.unrelated',json('{\"local_only\":true,\"text\":\"private\"}')) WHERE agent_turn_id=?",(turn,))
                c.commit()
    if when=='before_dispatch': invalidate()
    elif when=='before_persist': gateway.before_return=invalidate
    else:
        def crash(*args): raise RuntimeError('synthetic response-ready crash')
        monkeypatch.setattr(evaluator,'_persist',crash)
    with pytest.raises((InvalidLearningEvaluationError,LearningEvaluationUnknownError)):
        asyncio.run(evaluator.evaluate(turn))
    if when=='response_ready':
        invalidate()
        restarted = LearningOutcomeEvaluator(db,StudyRepository(db),gateway,evaluator.settings)
        with pytest.raises((InvalidLearningEvaluationError,LearningEvaluationUnknownError)): restarted.recover_ready(turn)
    assert len(calls)==(0 if when=='before_dispatch' else 1)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
