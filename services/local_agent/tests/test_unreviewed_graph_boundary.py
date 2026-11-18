"""Unreviewed graph content is a hypothesis, never review authority."""
import json

from home_study_agent.mistake_graph_study import study_targets
from home_study_agent.mistake_learning import analysis_context_key, analysis_for_graph
from home_study_agent.mistake_topic_learning import _graph_hint


def graph(primary='approved', node='approved', relation='approved'):
    return {'status': 'matched', 'graph_version': 'synthetic-v1',
            'primary': {'key': 'main', 'title': '主知识点', 'review_status': primary},
            'prerequisites': [{'key': 'pre', 'title': '前置知识点', 'review_status': node,
                               'relation_review_status': relation}]}


def test_unreviewed_primary_and_edges_do_not_drive_review_or_persisted_checks():
    route={'provider':'deepseek','endpoint':'https://synthetic.invalid/v1','model':'deepseek-synthetic'}
    for status in ('unreviewed', 'needs_merge', 'needs_correction', 'unknown'):
        assert study_targets(graph(primary=status)) == []
        for candidate in (graph(node=status), graph(relation=status)):
            assert [x['kind'] for x in study_targets(candidate)] == ['primary']
            old = {'analysis_json': json.dumps({'prerequisite_checks': [{'title': '前置知识点', 'question': '如何判断？'}]}),
                   'analysis_context_key': analysis_context_key(candidate, route)}
            assert analysis_for_graph(old, candidate,route)['prerequisite_checks'] == []
    candidate = graph()
    assert [x['kind'] for x in study_targets(candidate)] == ['prerequisite', 'primary']
    old = {'analysis_json': json.dumps({'prerequisite_checks': [{'title': '前置知识点', 'question': '如何判断？'}]}),
           'analysis_context_key': analysis_context_key(candidate, route)}
    assert len(analysis_for_graph(old, candidate,route)['prerequisite_checks']) == 1


def test_cloud_right_alone_does_not_authorize_unreviewed_teaching_hints():
    def allow(g):
        return ([{'ref': 'C1', 'label': g['alternatives'][0]['title']}], {})
    for status in ('unreviewed', 'needs_merge', 'needs_correction', 'unknown'):
        candidate = graph(primary=status)
        assert 'knowledge_point' not in _graph_hint(candidate, candidate['primary'], (), allow)
        for candidate in (graph(node=status), graph(relation=status)):
            assert _graph_hint(candidate, candidate['primary'], (), allow)['prerequisite_hypotheses'] == []
    candidate = graph()
    assert _graph_hint(candidate, candidate['primary'], (), allow)['knowledge_point'] == '主知识点'
    assert len(_graph_hint(candidate, candidate['primary'], (), allow)['prerequisite_hypotheses']) == 1


def test_no_route_cannot_qualify_independent_analysis():
    from home_study_agent.mistake_learning import independent_analysis_context_key
    candidate = graph(primary='unreviewed')
    row = {'analysis_json': json.dumps({'knowledge_points': [{'label': '不能确证来源', 'description': '旧分析'}]}),
           'analysis_context_key': independent_analysis_context_key(None)}
    assert analysis_for_graph(row, candidate, None) is None


def test_unreviewed_primary_cannot_reuse_old_graph_bound_analysis():
    route={'provider':'deepseek','endpoint':'https://synthetic.invalid/v1','model':'deepseek-synthetic'}
    candidate=graph(primary='unreviewed')
    old={'analysis_json':json.dumps({'summary':'不可信旧图谱标签','prerequisite_checks':[]}),
         'analysis_context_key':analysis_context_key(candidate,route)}
    assert analysis_for_graph(old,candidate,route) is None
