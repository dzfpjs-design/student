"""Structural checks do not stand in for model semantic quality tests."""
from copy import deepcopy

import pytest

from home_study_agent.formative_criterion_review import VERSION,normalize,validate_saved


@pytest.fixture
def example():
    payload={'question':'Explain the initial amount and each group.',
        'response':'There are 48. I checked the total. Both groups together get 9.',
        'rubric':{'task_points':['Initial amount is 48 and checked.','Two groups each receive 9.'],
            'dimensions':[{'dimension_id':'reasoning','criterion':'Explain the groups.'}]}}
    raw=dict(version=VERSION,confidence=.8,next_check='Try a different number of groups.',
        rubric_dimensions=[dict(dimension_id='reasoning',score=1,response_quotes=['Both groups together get 9.'],rationale='Group size was confused.')],
        criterion_checks=[
            dict(criterion_index=0,status='supported',response_quotes=['There are 48.','I checked the total.'],rationale='Initial amount and check given.'),
            dict(criterion_index=1,status='contradicted',response_quotes=['Both groups together get 9.'],rationale='Each was interpreted as total.')],
        feedback=dict(acknowledgement='The original amount is correct.',focus_index=1,question='What does each mean?'))
    return payload,raw


def test_only_supported_checks_create_coverage_and_quote_span_is_real(example):
    payload,raw=example
    result=normalize(payload,raw)
    assert result['understanding']=='incorrect'
    assert result['criterion_evidence']==[{'criterion':payload['rubric']['task_points'][0],
        'response_quote':'There are 48. I checked the total.'}]
    assert result['spoken_feedback']=='The original amount is correct. What does each mean?'
    validate_saved(payload,result)


@pytest.mark.parametrize('fault',['missing','duplicate','bool_index','wrong_index','empty_positive','unseen_quote',
    'joined_quote','unseen_status','list_status','focus_supported','focus_absent','hidden_authority','legacy_fields'])
def test_invalid_or_conflicting_review_is_not_silently_accepted(example,fault):
    payload,raw=example
    if fault=='missing':raw['criterion_checks'].pop()
    elif fault=='duplicate':raw['criterion_checks'][1]['criterion_index']=0
    elif fault=='bool_index':raw['criterion_checks'][0]['criterion_index']=False
    elif fault=='wrong_index':raw['criterion_checks'][1]['criterion_index']=5
    elif fault=='empty_positive':raw['criterion_checks'][0]['response_quotes']=[]
    elif fault=='unseen_quote':raw['criterion_checks'][1]['response_quotes']=['One group gets 9.']
    elif fault=='joined_quote':raw['criterion_checks'][0]['response_quotes']=['There are 48.Both groups together get 9.']
    elif fault=='unseen_status':raw['criterion_checks'][0]['status']='mastered'
    elif fault=='list_status':raw['criterion_checks'][0]['status']=[]
    elif fault=='focus_supported':raw['feedback']['focus_index']=0
    elif fault=='focus_absent':raw['feedback']['focus_index']=None
    elif fault=='hidden_authority':raw['automatic_certification']=True
    elif fault=='legacy_fields':raw['criterion_evidence']=[]
    with pytest.raises(ValueError,match='FORMATIVE_CHECK_'):normalize(payload,raw)


def test_correct_complete_answer_has_no_repeat_question(example):
    payload,raw=example
    raw['criterion_checks'][1]['status']='supported'
    raw['feedback'].update(focus_index=None,question=None)
    result=normalize(payload,raw)
    assert result['understanding']=='independent'
    assert result['spoken_feedback']==raw['feedback']['acknowledgement']
    raw['feedback'].update(focus_index=0,question='Explain the same thing again.')
    with pytest.raises(ValueError,match='REDUNDANT_QUESTION'):normalize(payload,raw)


@pytest.mark.parametrize('complete',[False,True])
def test_spoken_delivery_does_not_append_unpublished_next_check(example,complete):
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    payload,raw=example
    if complete:
        raw['criterion_checks'][1]['status']='supported'
        raw['feedback'].update(focus_index=None,question=None)
    result=normalize(payload,raw)
    result.update(formative_only=True,formative_feedback_kind=None,objective_update_applied=True,
        mastery_gate={'status':'unknown'},objective_phase='remediate')
    assert LearningOutcomeEvaluator._response_text(result)==result['spoken_feedback']


@pytest.mark.parametrize('field',['criterion_evidence','spoken_feedback','understanding','criterion_review'])
def test_saved_review_cannot_disagree_with_derived_feedback_or_coverage(example,field):
    payload,raw=example
    result=normalize(payload,raw)
    if field=='criterion_evidence':
        result[field].append({'criterion':payload['rubric']['task_points'][1],'response_quote':payload['response']})
    elif field=='spoken_feedback':result[field]='You got everything right.'
    elif field=='understanding':result[field]='independent'
    else:
        result[field]=deepcopy(result[field])
        result[field]['checks'][1]['status']='supported'
    with pytest.raises(ValueError,match='FORMATIVE_CHECK_'):validate_saved(payload,result)


def test_not_addressed_is_empty_evidence_and_incorrect_has_priority(example):
    payload,raw=example
    raw['criterion_checks'][0].update(status='not_addressed',response_quotes=[])
    assert normalize(payload,raw)['criterion_evidence']==[]
    raw['feedback']['focus_index']=0
    with pytest.raises(ValueError,match='FOCUS'):normalize(payload,raw)
