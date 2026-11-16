import json

from home_study_agent.mistake_graph_semantic import parse_semantic_assessments


def test_semantic_assessment_requires_unique_consistent_grounded_choice():
    options = [{'ref': 'C1', 'label': '推断等高三角形面积比与底边比的关系'},
               {'ref': 'C2', 'label': '计算三角形面积'}]
    source = '等高两个三角形的面积比是多少'
    value = {'graph_assessments': [
        {'ref': 'C1', 'verdict': 'same_skill', 'confidence': .96,
         'source_quote': '等高两个三角形的面积比', 'label_quote': '等高三角形面积比', 'missing_qualifiers': []},
        {'ref': 'C2', 'verdict': 'different', 'confidence': .9,
         'source_quote': '', 'label_quote': '', 'missing_qualifiers': ['等高', '面积比']},
    ]}
    assert parse_semantic_assessments(json.dumps(value, ensure_ascii=False), source, options) == 'C1'
    for edit in ('unknown_ref', 'duplicate_ref', 'both_match', 'low_confidence', 'invented_quote', 'missing_qualifier', 'uncertain'):
        data = json.loads(json.dumps(value, ensure_ascii=False))
        first, second = data['graph_assessments']
        if edit == 'unknown_ref': first['ref'] = 'C9'
        if edit == 'duplicate_ref': second['ref'] = 'C1'
        if edit == 'both_match': second.update(verdict='same_skill', source_quote='三角形的面积比', label_quote='三角形面积', missing_qualifiers=[])
        if edit == 'low_confidence': first['confidence'] = .6
        if edit == 'invented_quote': first['source_quote'] = '面积恰好相等'
        if edit == 'missing_qualifier': first['missing_qualifiers'] = ['等高']
        if edit == 'uncertain': second['verdict'] = 'uncertain'
        assert parse_semantic_assessments(json.dumps(data, ensure_ascii=False), source, options) is None, edit
    assert parse_semantic_assessments('not-json', source, options) is None
    broad = {'graph_assessments': [{'ref': 'C2', 'verdict': 'same_skill', 'confidence': .99,
             'source_quote': '等高两个三角形的面积比', 'label_quote': '三角形面积', 'missing_qualifiers': []}]}
    assert parse_semantic_assessments(json.dumps(broad, ensure_ascii=False), source, [options[1]]) is None
