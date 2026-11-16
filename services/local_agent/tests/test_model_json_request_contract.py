from types import SimpleNamespace

import pytest

from home_study_agent.model_gateway import ModelGateway


@pytest.mark.parametrize('provider', ['deepseek','qwen'])
def test_json_mode_has_protocol_instruction_without_mutating_frozen_source(provider):
    endpoint=SimpleNamespace(provider=provider,base_url='https://api.deepseek.com',model='synthetic-model')
    messages=({'role':'system','content':'只分类意图。'}, {'role':'user','content':'合成输入'})
    payload=ModelGateway.request_payload(endpoint,messages,'voice_interaction')
    assert payload['response_format']=={'type':'json_object'}
    assert payload['messages'][0]=={'role':'system','content':'Return only a valid JSON object.'}
    assert payload['messages'][1:]==list(messages)
    assert messages[0]['content']=='只分类意图。'
    explicit=({'role':'system','content':'Output a JSON object.'},messages[1])
    assert ModelGateway.request_payload(endpoint,explicit,'voice_interaction')['messages']==explicit
    plain=ModelGateway.request_payload(endpoint,messages,'companion_dialogue')
    if 'response_format' not in plain:
        assert plain['messages']==messages
