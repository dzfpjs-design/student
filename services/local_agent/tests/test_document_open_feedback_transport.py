"""The normal voice request path cannot upload quoted private feedback."""
import json

import pytest

from test_document_open_check import opened
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('quoted', [False, True])
def test_quoted_local_rationale_does_not_reenter_intent_model(tmp_path, monkeypatch, quoted):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        prompt, _, _ = evaluated(j, monkeypatch, 'transport-first')
        say(j, monkeypatch, '我的思路怎么样', 'status', 'transport-status')
        private = 'Synthetic reference anchored to the saved response.'
        text = '你刚才说 '+('“'+private+'”' if quoted else private)+'，这是什么意思？'
        projected = '你刚才说 [本地引用]，这是什么意思？'
        voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], text, key='transport-voice')
        before = len(j['calls'])
        j['responses'].append(json.dumps(dict(role='question',control=None,quote=projected,confidence=.99)))
        turn = interpret(j['client'], j['sid'], text, 'transport-query', voice)
        assert turn['runtime_capability'] == 'learning.review_revisit', turn
        result = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert result.status_code == 200, result.text
        assert len(j['calls']) == before + 1
        sent = json.loads(j['calls'][-1]['messages'][1]['content'])
        assert sent == {'transcript':projected}
        assert private not in json.dumps(j['calls'][-1])
        assert j['answer'] not in json.dumps(j['calls'][-1],ensure_ascii=False)
        message = result.json()['execution']['response_text']
        assert '这条建议要你检查的是' in message and '要做到' in message
        assert private not in message  # Not merely repeating the same advice.
        count = len(j['calls'])
        repeat = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert repeat.status_code == 200 and repeat.json()['execution']['response_text']==message
        assert len(j['calls']) == count
