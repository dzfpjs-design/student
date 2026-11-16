"""Offline inference integrity and bounded inputs, no family material."""
from pathlib import Path
import os
import socket
import json
import shutil
from time import perf_counter

import pytest

from home_study_agent.retrieval.local_semantic import LocalSemanticMatcher, MODEL_DIR, FILES, REVISION


def test_missing_and_changed_assets_do_not_trigger_a_download(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('Inference attempted a network connection'))
    matcher = LocalSemanticMatcher(tmp_path)
    assert matcher.scores('计算顺序', ['整数混合运算'])['status'] == 'missing_assets'
    root = tmp_path / MODEL_DIR
    root.mkdir()
    for name in FILES:
        (root / name).write_bytes(b'not a model')
    result = matcher.scores('计算顺序', ['整数混合运算'])
    assert result['status'] == 'invalid_assets' and 'scores' not in result
    assert matcher.status()['last_inference_status'] == 'invalid_assets'


@pytest.mark.parametrize('query,documents',[('', ['a']), ('a', []), ('a', {}), ('a' * 4001, ['b']), ('a', [None])])
def test_invalid_or_unbounded_input_cannot_reach_inference(tmp_path,query,documents,monkeypatch):
    matcher = LocalSemanticMatcher(tmp_path)
    monkeypatch.setattr(matcher, '_load', lambda: pytest.fail('Unbounded input reached model loading'))
    assert matcher.scores(query, documents)['status'] == 'input_limit'


@pytest.fixture
def installed_models():
    value = os.environ.get('HSC_TEST_SEMANTIC_MODELS')
    if not value:
        pytest.skip('Explicit local model assets required; never auto-download')
    return Path(value)


def test_runtime_and_package_asset_pins_agree():
    manifest=json.loads((Path(__file__).parents[1]/'semantic-model-manifest.json').read_text())
    assert manifest['model_dir']==MODEL_DIR and manifest['revision']==REVISION
    assert {name:(v['minimum_bytes'],v['sha256']) for name,v in manifest['files'].items()}==FILES


def test_real_cpu_semantics_survives_paraphrase_and_rejects_unrelated_text(installed_models,monkeypatch,tmp_path):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('Local model went online'))
    matcher = LocalSemanticMatcher(installed_models)
    documents = [
        '整数四则运算顺序：先括号、后乘除再加减。例：计算(24+6)×3。',
        '分数与小数：十分之一、百分之一的等值表示。例：把百分之三十写成小数。',
        '一元二次方程判别式：通过b²-4ac的正负判断不同实数根的个数。',
        '欧姆定律：电压、电流与电阻的关系，利用U=IR计算电路中的电流。',
    ]
    cases = [('先求24与6的和，然后将所得结果乘3',0),
             ('百分之三十和零点三是一样多的吗',1),
             ('不解二次方程，怎样知道它有两个不同的根',2),
             ('一个电阻两端接了6伏电压，阻值3欧，经过它的电流多大',3)]
    measurements=[]
    for query, expected in cases:
        start=perf_counter()
        result = matcher.scores(query, documents)
        measurements.append(round((perf_counter()-start)*1000,2))
        assert result['status'] == 'ready',result
        assert result['model_revision'] == REVISION
        assert result['scores'].index(max(result['scores'])) == expected,result
        assert result['scores'][expected]>=.60,result
    assert matcher._session.get_providers()==['CPUExecutionProvider']
    (tmp_path/'semantic-cpu-measurements.json').write_text(json.dumps({'milliseconds':measurements,'sources':len(documents),
        'queries':len(cases),'threads':2,'network_connections':0,'revision':REVISION}))
    unrelated = matcher.scores('小明出生在江苏南京，只回答一个整数。', documents)
    assert max(unrelated['scores']) < .60,unrelated
    long_input = matcher.scores('计算' * 600, documents)
    assert long_input['status'] == 'token_limit' and 'scores' not in long_input


def test_changed_weights_are_rechecked_before_reusing_a_loaded_model(installed_models,tmp_path):
    root=tmp_path/MODEL_DIR
    root.mkdir()
    for name in FILES:
        shutil.copyfile(installed_models/MODEL_DIR/name,root/name)
    matcher=LocalSemanticMatcher(tmp_path)
    assert matcher.scores('整数运算',['先乘除后加减'])['status']=='ready'
    with (root/'model_optimized.onnx').open('r+b') as target:
        target.write(b'corrupt')
    invalid=matcher.scores('整数运算',['先乘除后加减'])
    assert invalid['status']=='invalid_assets' and 'scores' not in invalid
