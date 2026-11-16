import asyncio
import json
import pytest
from home_study_agent.model_gateway import ModelResult
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_supply import PaperScoringAuthorityDraftService
from home_study_agent.paper_scoring_source_match import PaperScoringSourceMatchService
from home_study_agent.paper_scoring_source_authority import PaperScoringSourceAuthorityService
from home_study_agent.paper_subjective_rubric import PaperSubjectiveRubricService, extract_spec
from home_study_agent.repository import StudyRepository
from test_paper_scoring_source_match import add_local_page
from test_paper_scoring_supply import pending_request


class SubjectiveGateway:
    configured = True
    async def complete(self, request):
        return ModelResult(json.dumps(dict(subject='数学', answer_mode='subjective',
            task_points=['列出数量关系', '解释计算依据'], source_search_terms=['计算整数', '评分标准'],
            confidence=.99, requires_external_source=True)), 'fixture', 'fixture', 1, 8, 8)


def prepared(tmp_path):
    db, _, request = pending_request(tmp_path)
    asyncio.run(PaperScoringAuthorityDraftService(db, SubjectiveGateway(), StudyRepository(db), lambda: True).process_next())
    resource_id, _ = add_local_page(db, text=f"{request['question_text']}\n评分标准：\n1. 列出准确的数量关系（2分）\n2. 计算结果与单位正确（1分）")
    assert PaperScoringSourceMatchService(db).process_next()['status'] == 'candidate'
    return db, request, resource_id


def test_source_rubric_freezes_once_without_inventing_score_and_restarts(tmp_path):
    from home_study_agent.database import Database
    db, request, _ = prepared(tmp_path)
    assert PaperScoringSourceAuthorityService(db).process_next()['status'] == 'calibration_pending'
    scoring = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
    evidence = scoring['authority_supply']['latest_evidence']
    artifact_id = evidence['frozen_rubric_artifact_id']
    assert artifact_id and evidence['criterion_count'] == 2
    assert scoring['decision_id'] is None
    assert PaperScoringService(db).resolve(request['request_id'], 'child', 'never-self-certify') is None
    service = PaperSubjectiveRubricService(Database(db.path, runtime_mode=db.runtime_mode))
    spec = service.current('child', artifact_id)
    assert spec['status'] == 'calibration_pending' and spec['maximum_score'] == 3
    assert spec['score_authority_granted'] is False and spec['cloud_transmission_allowed'] is False
    obligation = scoring['authority_supply']['obligation_id']
    source = PaperScoringSourceMatchService(db).valid_candidates(obligation)[0]
    assert service.freeze('child', request['request_id'], source)['artifact_id'] == artifact_id
    with pytest.raises(ValueError, match='UNKNOWN'):
        service.current('another-child', artifact_id)


def test_source_mutation_withdraws_frozen_spec_and_forged_source_is_rejected(tmp_path):
    db, request, resource_id = prepared(tmp_path)
    PaperScoringSourceAuthorityService(db).process_next()
    scoring = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
    aid = scoring['authority_supply']['latest_evidence']['frozen_rubric_artifact_id']
    service = PaperSubjectiveRubricService(db)
    source = PaperScoringSourceMatchService(db).valid_candidates(scoring['authority_supply']['obligation_id'])[0]
    with pytest.raises(ValueError, match='SOURCE_CHANGED'):
        service.freeze('child', request['request_id'], {**source, 'text': 'model-generated rubric'})
    with db.connect() as c:
        c.execute('UPDATE kg_segment SET text=? WHERE resource_id=?', ('changed source', resource_id)); c.commit()
    assert service.current('child', aid)['status'] == 'source_withdrawn'


@pytest.mark.parametrize('tail', ['参考答案：80', '评分标准：\n很好（2分）\n很好（2分）', '评分标准：\n列式正确（-1分）', '评分标准：\n列式正确（2分）\n别的题答案：B'])
def test_unverifiable_or_ambiguous_criteria_never_freeze(tail):
    with pytest.raises(ValueError):
        extract_spec('计算十个整数的和', dict(text='计算十个整数的和\n' + tail, ocr_confidence=.99, source_version='v1', candidate_id='source'))
