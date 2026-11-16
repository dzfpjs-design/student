"""Retrieval scale and source selection; never access the family database."""
from types import SimpleNamespace
import socket

import pytest

from home_study_agent import document_independent_check, document_target_hypothesis
from home_study_agent.retrieval.local_semantic import LocalSemanticMatcher
from home_study_agent.retrieval import local_semantic
from test_local_semantic import installed_models


def test_real_model_compares_sources_after_the_old_128_boundary(installed_models, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('Inference went online'))
    matcher = LocalSemanticMatcher(installed_models)
    documents = [f'第{i}课：观察花的颜色，记录植物生长的变化。' for i in range(136)]
    documents.append('整数四则运算顺序：先括号、后乘除再加减。计算(24+6)×3。')
    result = matcher.scores('先求24与6的和，再将所得结果乘3', documents)
    assert result['status'] == 'ready', result
    assert len(result['scores']) == len(documents)
    assert result['scores'].index(max(result['scores'])) == 136
    assert result['scores'][-1] >= .60


def test_semantics_compares_every_qualified_variant_before_node_deduplication(monkeypatch):
    sources = {
        'a': dict(diagnostic_item_id='a', knowledge_node_id='n1', title='运算', prompt='词面优先但不是最佳题型'),
        'b': dict(diagnostic_item_id='b', knowledge_node_id='n1', title='运算', prompt='真正匹配的另一种题型'),
        'c': dict(diagnostic_item_id='c', knowledge_node_id='n2', title='单位', prompt='其他知识内容'),
        'd': dict(diagnostic_item_id='d', knowledge_node_id='n2', title='单位', prompt='另一道同节点题'),
    }
    rows = [dict(diagnostic_item_id=k, prompt=v['prompt'], subject='math') for k,v in sources.items()]
    rows.append(dict(diagnostic_item_id='withdrawn', prompt='不可用来源', subject='math'))
    seen = []
    def scores(query, documents):
        seen.extend(documents)
        return {'status':'ready', 'scores':[.61 if '词面' in s else .91 if '真正' in s else .72 for s in documents]}
    connection = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchall=lambda:rows),
        local_semantic_matcher=SimpleNamespace(scores=scores))
    monkeypatch.setattr(document_independent_check, '_qualified', lambda c, child, key:sources.get(key))
    monkeypatch.setattr(document_independent_check, 'original_version', lambda state:'original')
    packet = ({'child_id':'synthetic-child'}, {}, {}, {}, {'confirmed_text':'请理解这道不重复的题'}, {'subject':'math'})
    proposal = document_target_hypothesis.candidates(connection, packet)
    assert len(seen) == 4
    assert [v['item_id'] for v in proposal['options']] == ['b','c']
    assert len({v['node_id'] for v in proposal['options']}) == 2
    assert proposal['alignment_verified'] is False
    assert proposal['mastery_update_allowed'] is False
    assert proposal['retrieval']['source_count'] == 4


@pytest.mark.parametrize('failure', ['late_timeout', 'late_model_failure', 'last_batch_timeout'])
def test_no_partial_semantic_scores_escape_a_failed_scan(tmp_path, monkeypatch, failure):
    import numpy as np
    matcher = LocalSemanticMatcher(tmp_path)
    monkeypatch.setattr(matcher, '_load', lambda:'ready')
    matcher._tokenizer = SimpleNamespace(encode_batch=lambda texts:[
        SimpleNamespace(ids=[1], attention_mask=[1], type_ids=[0]) for _ in texts])
    calls = []
    clock = [0.0]
    monkeypatch.setattr(local_semantic, 'monotonic', lambda:clock[0])
    def run(outputs, inputs):
        calls.append(len(inputs['input_ids']))
        if len(calls) == 2:
            if failure == 'late_model_failure':
                raise RuntimeError('No partial ranking is authoritative')
            clock[0] = 6.0
        hidden = np.zeros((calls[-1], 1, 512))
        hidden[:,:,0] = 1
        return [hidden]
    matcher._session = SimpleNamespace(run=run)
    documents = ['synthetic reference'] * (12 if failure == 'last_batch_timeout' else 30)
    result = matcher.scores('synthetic query', documents)
    assert len(calls) == 2
    assert 'scores' not in result
    assert result['status'] == ('inference_failed' if failure == 'late_model_failure' else 'time_budget')
    assert matcher.status()['last_inference_status'] == result['status']
