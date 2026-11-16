"""Real disposable HTTP/SQLite; synthetic semantic models and page OCR."""
import json

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult, ModelResponseError
from test_cloud_first_agent import Planner
from test_document_voice_capture import page_context, begin, complete
from test_diagnostic_prompt_http import environment


PET = '我们先不看这张纸，你有PET学习资料可以给我介绍吗？'


def setup_models(j, monkeypatch, quote=PET, *, role='question', confidence=.97,
                 capability='conversation.respond', arguments=None, fail_plan=False):
    planner = Planner(capability, arguments or {'response':'我有一百万道PET题。'}, quote,
                      role=role if role in {'greeting','companionship','topic_mention'} else 'question',
                      objective_type='companion')
    planner.payload['spoken_transition'] = '我先回答你这次的问题。'
    j['gateway'].configured = True
    async def respond(request):
        j['gateway'].requests.append(request)
        if request.request_type == 'voice_interaction':
            value = {'role':role,'control':None,'quote':quote,'confidence':confidence}
        else:
            if fail_plan:
                raise ModelResponseError('MODEL_TIMEOUT', fallback_eligible=False)
            value = planner.payload
        return ModelResult(content=json.dumps(value, ensure_ascii=False), provider='test', model='synthetic',
                           latency_ms=1, prompt_tokens=20, completion_tokens=20)
    monkeypatch.setattr(j['gateway'], 'complete', respond)
    return planner


def ask(j, quote=PET, key='independent-paper-question'):
    assert begin(j).status_code == 200
    voice = complete(j, text=quote)
    body = {'transcript':quote, 'voice_input_id':voice['voice_input_id'],
            'client_action_id':key, 'cloud_text_allowed':True}
    response = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def test_pet_question_reads_real_resources_and_recovers_without_model_replay(page_context, monkeypatch):
    j = page_context
    setup_models(j, monkeypatch)
    value, body = ask(j)
    assert value['intent'] == 'system.inspect_learning', value
    assert value['action']['type'] == 'inspect_learning_system'
    assert value['action']['scope'] == 'resources' and value['action']['track'] == 'pet'
    assert '一百万' not in value['response_text']
    assert value['action']['voice_input_binding']['question_reference'] == j['reference']
    executed = j['client'].post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
    assert executed.status_code == 200, executed.text
    result = executed.json()['execution']
    assert result['result']['cloud_transmission_allowed'] is False
    assert any(item['track'] == 'pet' and item['item_count'] > 0
               for item in result['result']['diagnostic_resources'])
    assert '一百万' not in result['response_text']
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        same = restarted.post(f"/v1/sessions/{j['sid']}/agent-turn", json=body)
        assert same.status_code == 200 and same.json()['agent_turn_id'] == value['agent_turn_id']
        receipt = restarted.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
        assert receipt.status_code == 200 and receipt.json()['execution']['result'] == result['result']
    assert [r.request_type for r in j['gateway'].requests] == ['voice_interaction','agent_plan']
    for request in j['gateway'].requests:
        exported = json.dumps({'messages':request.messages, 'manifest':request.context_manifest,
                               'evidence':request.evidence_refs}, ensure_ascii=False)
        for secret in [j['original']['artifact_id'], j['original']['revision_id'],
                       j['original']['content_sha256'], j['original']['page_source']['sha256'],
                       'Calculate (24 + 6) x 3']:
            assert secret not in exported
    planning = j['gateway'].requests[1]
    payload = json.loads(planning.messages[1]['content'])
    assert set(payload['context']) == {'session','child','conversation_state'}
    assert {x['name'] for x in payload['available_capabilities']} == {
        'conversation.respond','system.inspect_learning','learning.explain'}
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 0


@pytest.mark.parametrize('role,confidence', [('mixed',.97),('unknown',.97),('question',.2)])
def test_ambiguous_or_low_confidence_does_not_gain_independent_authority(page_context, monkeypatch, role, confidence):
    j = page_context
    setup_models(j, monkeypatch, role=role, confidence=confidence)
    value, _ = ask(j)
    assert value['action']['type'] == 'none'
    assert value['action']['source'] == 'content_free_control_clarification'
    assert not any(r.request_type == 'agent_plan' for r in j['gateway'].requests)


def test_original_ocr_quotation_cannot_be_sent_as_an_independent_question(page_context, monkeypatch):
    j = page_context
    quote = 'Calculate (24 + 6) x 3 这是什么？'
    setup_models(j, monkeypatch, quote)
    value, _ = ask(j, quote)
    assert value['action']['type'] == 'none'
    assert not j['gateway'].requests


def test_planning_failure_preserves_speech_and_does_not_claim_execution(page_context, monkeypatch):
    j = page_context
    setup_models(j, monkeypatch, fail_plan=True)
    value, body = ask(j)
    assert value['action']['source'] == 'cloud_planner_unavailable'
    assert value['action']['type'] == 'none'
    same = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json=body)
    assert same.json()['agent_turn_id'] == value['agent_turn_id']
    assert len(j['gateway'].requests) == 2


def test_independent_question_cannot_escalate_to_device_mutation(page_context, monkeypatch):
    j = page_context
    setup_models(j, monkeypatch, capability='observation.pause', arguments={})
    value, _ = ask(j)
    assert value['action']['type'] == 'none'
    assert value['action']['source'] == 'cloud_planner_unavailable'


def test_academic_question_uses_current_speech_not_background_page(page_context, monkeypatch):
    j = page_context
    quote = '数学里两个负数相乘为什么得到正数？'
    planner = setup_models(j, monkeypatch, quote, capability='learning.explain',
                          arguments={'question':quote, 'subject':'数学'})
    planner.payload['objective']['type'] = 'learning'
    value, _ = ask(j, quote)
    assert value['action']['type'] == 'ask_tutor', value
    assert value['action']['question'] == quote
    assert 'local_document_context' not in value['action']
    assert value['action']['voice_input_binding']['question_reference'] == j['reference']


def test_model_cannot_swap_current_question_for_an_unrequested_lesson(page_context, monkeypatch):
    j = page_context
    quote = '数学里两个负数相乘为什么得到正数？'
    planner = setup_models(j, monkeypatch, quote, capability='learning.explain',
                          arguments={'question':'解释一元二次方程', 'subject':'数学'})
    planner.payload['objective']['type'] = 'learning'
    value, _ = ask(j, quote)
    assert value['action']['type'] == 'none'
    assert value['action']['source'] == 'cloud_planner_unavailable'


def test_cloud_consent_revocation_cannot_reopen_the_planner(page_context, monkeypatch):
    j = page_context
    setup_models(j, monkeypatch)
    original = j['gateway'].complete
    async def revoked(request):
        result = await original(request)
        with j['db'].connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0 WHERE singleton_id=1'); c.commit()
        return result
    monkeypatch.setattr(j['gateway'], 'complete', revoked)
    value, _ = ask(j)
    assert value['action']['type'] == 'none'
    assert [r.request_type for r in j['gateway'].requests] == ['voice_interaction']


@pytest.mark.parametrize('quote,role,response', [
    ('你好小知，今天见到你很开心。','greeting','很高兴见到你，我们聊聊今天吧。'),
    ('今天上课好累，想和你聊一会儿。','companionship','听起来今天挺累的。哪一段让你特别费劲？'),
    ('一元二次方程','topic_mention','你是想聊聊今天学的内容，还是有具体问题？'),
])
def test_background_page_does_not_swallow_natural_companionship(page_context, monkeypatch, quote, role, response):
    j = page_context
    planner = setup_models(j, monkeypatch, quote, role=role, arguments={'response':response})
    planner.payload['spoken_transition'] = response
    value, _ = ask(j, quote)
    assert value['action']['type'] == 'none' and value['action']['source'] == 'agent_runtime_planner', value
    assert value['response_text'] == response
    assert [r.request_type for r in j['gateway'].requests] == ['voice_interaction','agent_plan']
    assert 'local_document_context' not in value['action']


def test_chat_role_cannot_turn_a_bare_topic_into_a_lesson(page_context, monkeypatch):
    j = page_context
    quote = '一元二次方程'
    planner = setup_models(j, monkeypatch, quote, role='topic_mention', capability='learning.explain',
                          arguments={'question':quote, 'subject':'数学'})
    planner.payload['objective']['type'] = 'learning'
    planner.payload['utterance_binding']['role'] = 'question'
    value, _ = ask(j, quote)
    assert value['action']['type'] == 'none' and value['action']['source'] == 'cloud_planner_unavailable'


def test_new_chat_roles_do_not_expand_pending_answer_or_restricted_catalogues():
    from home_study_agent.voice_control_intent import VoiceControlIntent
    quote = '今天有点累'
    body = json.dumps({'role':'companionship','control':None,'quote':quote,'confidence':.97})
    for options in [{}, {'independent_dialogue':True,'restricted':True},
                    {'independent_dialogue':True,'tutor_question':True}]:
        with pytest.raises(ValueError, match='CONTROL_INTENT_INVALID'):
            VoiceControlIntent.parse(body, quote, **options)
    assert VoiceControlIntent.parse(body, quote, independent_dialogue=True)['role'] == 'companionship'


@pytest.mark.parametrize('quote,track', [('有没有PET题库？','pet'), ('PET学习资料有多少？','pet'),
                                      ('有没有carpet学习资料？','all')])
def test_mixed_chinese_latin_track_is_not_an_english_word_boundary(quote, track):
    from home_study_agent.learning_system_facts import detect_learning_system_query
    assert detect_learning_system_query(quote).track == track
