"""Verify the opt-in official probe through its real service, without a network."""
import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from test_diagnostic_prompt_http import environment


@pytest.mark.parametrize('provider_status,case,role', [(200, 'math', 'control'), (500, 'math', 'control'),
                                                    (200, 'mixed', 'answer'), (200, 'negative', 'control')])
def test_probe_real_service_and_one_post_budget(environment, monkeypatch, provider_status, case, role):
    settings, _ = environment
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(settings.database_path))
    path = Path(__file__).resolve().parents[3] / 'scripts' / 'probe-task-resume-selector.py'
    spec = importlib.util.spec_from_file_location('resume_probe', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    seen = []
    def transport(request):
        body = json.loads(request.content)
        seen.append(body)
        assert 'selector' in body['messages'][0]['content']
        data = json.loads(body['messages'][1]['content'])
        assert set(data) == {'transcript', 'state'}
        assert data['transcript'] == probe.CASES[case][0]
        if provider_status == 500:
            return httpx.Response(500, json={'error': {'message': 'synthetic failure'}})
        control = ('task.pause' if case == 'negative' else 'task.resume') if role == 'control' else None
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'role': role, 'control': control,
            'quote': data['transcript'], 'confidence': .99,
            **({'selector': {'subject': 'math', 'task_kind': None, 'relative_order': None}} if control == 'task.resume' else {})})}}]})
    settings = replace(settings, model_api_key='synthetic-not-a-key', model_provider='qwen',
                       model_name='synthetic-model', voice_intent_model_name='synthetic-model')
    result = asyncio.run(probe.probe(settings, case, transport=httpx.MockTransport(transport)))
    assert result['passed'] == (provider_status == 200 and role == 'control' and case == 'math'), result
    if case == 'negative':
        assert result['semantic_passed'] is False
        assert result['safety_passed'] is True
    assert len(seen) == result['posts'] == 1
    assert result['official_model'] is False
    assert result['attempts'] == 0
    assert 'synthetic-not-a-key' not in json.dumps(result)
