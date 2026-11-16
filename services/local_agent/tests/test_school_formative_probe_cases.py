"""Fixed authored probe contracts. No model or network calls."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'eval'))
from school_formative_cases import SUPPORTED_CRITERIA,cases,compare,compare_review


def test_probe_suites_are_disjoint_and_dimensions_are_real():
    regression,heldout=cases('regression'),cases('heldout')
    assert not {c['task_slug'] for c in regression}&{c['task_slug'] for c in heldout}
    all_cases=regression+heldout
    assert len({c['case_id'] for c in all_cases})==len(all_cases)==12
    assert set(SUPPORTED_CRITERIA)=={c['case_id'] for c in all_cases}
    for case in all_cases:
        assert set(case['payload'])=={'question','response','rubric'}
        allowed={d['dimension_id'] for d in case['payload']['rubric']['dimensions']}
        assert set(case['expected_pass'])|set(case['expected_fail'])<=allowed
        assert not set(case['expected_pass'])&set(case['expected_fail'])


@pytest.mark.parametrize('suite',['regression','heldout'])
def test_compare_rejects_false_pass_and_false_failure(suite):
    for case in cases(suite):
        dimensions=[dict(dimension_id=d['dimension_id'],score=1 if d['dimension_id'] in case['expected_fail'] else 2)
            for d in case['payload']['rubric']['dimensions']]
        assert compare(case,dict(rubric_dimensions=dimensions))['passed']
        key=(case['expected_pass']+case['expected_fail'])[0]
        next(d for d in dimensions if d['dimension_id']==key)['score']=0 if key in case['expected_pass'] else 3
        assert not compare(case,dict(rubric_dimensions=dimensions))['passed']


@pytest.mark.parametrize('suite',['regression','heldout'])
def test_compare_review_requires_exact_criterion_support(suite):
    for case in cases(suite):
        expected=set(SUPPORTED_CRITERIA[case['case_id']])
        dimensions=[dict(dimension_id=d['dimension_id'],score=1 if d['dimension_id'] in case['expected_fail'] else 2)
            for d in case['payload']['rubric']['dimensions']]
        checks=[dict(criterion_index=index,status='supported' if index in expected else 'not_addressed')
            for index,_ in enumerate(case['payload']['rubric']['task_points'])]
        evaluation=dict(rubric_dimensions=dimensions,criterion_review={'checks':checks})
        assert compare_review(case,evaluation)['passed']
        flipped=0 if 0 not in expected else next((i for i in range(len(checks)) if i not in expected),None)
        if flipped is None:
            checks[0]['status']='not_addressed'
        else:
            checks[flipped]['status']='supported'
        assert not compare_review(case,evaluation)['passed']
