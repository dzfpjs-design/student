"""An explicit reviewed ordered tuple contract, never number extraction."""
import json
import pytest
from home_study_agent.local_supply import score_local_item, LocalSupplyError
from home_study_agent.curriculum_objective_source import parse, exact_rubric


def rubric(**changes):
    value=dict(local_rule='local_exact_v1',answer_format='reviewed_spoken_numeric_tuple_v1',
        numeric_kind='integer',tuple_size=2,exact_answers=['3,4'])
    value.update(changes)
    return value


@pytest.mark.parametrize('answer',['3,4','3，4','三、四。','三和四','三与四'])
def test_explicit_integer_tuple_scores_whole_clear_spoken_pair(answer):
    value=score_local_item({'expected_answer_json':json.dumps(rubric()),'explanation':'reviewed synthetic original'},answer)
    assert value and value[0]==1


@pytest.mark.parametrize('answer',['三四','34','3.4','三或四','三、四、五','不是三、四','她说三、四','“三、四”','答案三、四，解释是这样','先别提交三、四'])
def test_ambiguous_mixed_or_quoted_pair_has_no_reliable_score(answer):
    assert score_local_item({'expected_answer_json':json.dumps(rubric()),'explanation':'synthetic'},answer) is None


@pytest.mark.parametrize('answer',['四、三','一、二'])
def test_order_and_wrong_values_remain_actual_incorrect_answer(answer):
    assert score_local_item({'expected_answer_json':json.dumps(rubric()),'explanation':'synthetic'},answer)[0]==0


def test_source_requires_explicit_tuple_metadata_and_preserves_legacy_exact_text():
    source=dict(schema='curriculum_objective_source_v1',subject='数学',grade='初三',knowledge_node_id='synthetic-node',
        assessment_mode='objective',answer_type='text',prompt='按从小到大说两个根。',expected_answer=rubric())
    checked=parse(json.dumps(source));assert exact_rubric(checked)==rubric()
    bad={**source,'expected_answer':{**rubric(),'tuple_size':3}}
    with pytest.raises(LocalSupplyError):parse(json.dumps(bad))
    legacy={**source,'expected_answer':dict(local_rule='local_exact_v1',exact_answers=['3,4'])}
    old=exact_rubric(parse(json.dumps(legacy)))
    assert old['answer_format']=='reviewed_exact_text_v1'
    assert score_local_item({'expected_answer_json':json.dumps(old),'explanation':'synthetic'},'三、四。')[0]==0


def test_explicit_rational_pair_and_unknown_tuple_do_not_fall_back_to_keywords():
    from home_study_agent.learning_state import LearningStateService
    item=dict(expected_answer_json=json.dumps(rubric(numeric_kind='rational',exact_answers=['1/2,3/4'])),
        answer_type='text',assessment_mode='objective',explanation='synthetic approved reference')
    assert score_local_item(item,'二分之一、四分之三。')[0]==1
    invalid={**rubric(), 'keywords':['三']}
    item['expected_answer_json']=json.dumps(invalid)
    assert LearningStateService._score_answer(item,'三四')[0] is None
