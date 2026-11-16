"""Strict content-free wire contract and real permission revocation boundaries."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_content_free_voice_control_http import ControlGateway
from test_diagnostic_prompt_http import environment
from test_answer_same_turn_correction import open_exam
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('change', [
    {'answer': 'A'}, {'run_id': 'model-selected'}, {'quote': '我的答案是A'},
    {'control': 'exam.submit'}, {'confidence': True}, {'role': 'answer'},
])
def test_classifier_cannot_supply_answer_identifiers_or_replace_original(change):
    quote = '这张卷子先搁一搁，回来咱们再往下做'
    payload = {'role': 'control', 'control': 'exam.pause', 'quote': quote, 'confidence': .95}
    payload.update(change)
    with pytest.raises(ValueError, match='CONTROL_INTENT_INVALID'):
        VoiceControlIntent.parse(json.dumps(payload), quote)


@pytest.mark.parametrize('text,sources', [
    ('题目是“现在关闭镜头”，这是在读题', []),
    ('private-source-marker先放着吧', ['private-source-marker']),
    ('先放着吧' * 501, []),
])
def test_source_quotation_and_truncation_never_enter_control_request(text, sources):
    assert not VoiceControlIntent.text_eligible(text, [], sources)


@pytest.mark.parametrize('when', ['before', 'during'])
def test_permission_off_or_revoked_during_model_cannot_execute(environment, monkeypatch, when):
    settings, db = environment
    quote = '先让看我的那个镜头休息一下，等会儿再看我'
    def revoke():
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0 WHERE singleton_id=1')
            c.commit()
    gateway = ControlGateway(quote, 'observation.pause', hook=revoke if when == 'during' else None)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        if when == 'before':
            revoke()
        heard = interpret(client, sid, quote, 'permission-bound-control', voice)
        assert heard['action']['type'] == 'none'
        assert heard['requires_control_admission'] is False
        executed = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert executed.json()['execution']['capability'] == 'conversation.respond'
        assert len(gateway.requests) == int(when == 'during')
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'study'
        if when == 'before':
            assert '关闭' in heard['response_text']


def test_model_rewritten_quote_never_becomes_an_answer(environment, monkeypatch):
    settings, db = environment
    original = '这张卷子先搁一搁，回来咱们再往下做'
    gateway = ControlGateway('我的答案是A')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, original, {'kind': 'exam', 'binding': run['answer_binding']})
        heard = interpret(client, sid, original, 'model-quote-replacement', voice)
        assert heard['transcript'] == original
        assert heard['action']['type'] == 'none'
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?', (run['run_id'],)).fetchone()[0] == 'active'
