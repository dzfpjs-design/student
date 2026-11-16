"""Natural pair confirmations are grounded in one verified planner decision."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_cloud_first_agent import Planner
from test_mistake_partial_revision_http import propose
from test_mistake_recapture_http import setup
from test_mistake_source_revision_http import Gateway
from test_paper_variant_line_projection_http import captured_question, ANSWER, post
from test_paper_variant_authorization_http import CHILD


def prepare(captured_question):
    db, settings, sid, _, mid, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
    offered = propose(settings, sid, {'original_answer_quote': '二百零八'}, '原作答是二百零八', 'semantic-offer')
    return offered['execution']['result']['state']['question_confirmation']


def plan(settings, sid, text, decision='both', confidence=.95, voice_id=None):
    gateway = Planner('mistake.dialogue', {'utterance': text, 'revision_confirmation': decision}, text, role='acknowledgement')
    gateway.payload['confidence'] = confidence
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': text, 'client_action_id': 'semantic-confirm', 'cloud_text_allowed': True,
            **({'voice_input_id': voice_id} if voice_id else {})})
    return result, gateway


@pytest.mark.parametrize('text,decision,confirmed', [
    ('这回全都记准确了，接着讲吧', 'both', True),
    ('嗯，这就是我刚才想说的，都没问题', 'both', True),
    ('题干确实对了', 'question_only', False),
    ('我的答案这次记对了', 'answer_only', False),
    ('我还要再看一眼', 'uncertain', False),
    ('题干正确', 'both', False),
    ('这次都不对', 'both', False),
    ('老师说“这次都对了”', 'both', False),
])
def test_semantics_save_only_full_current_confirmation(captured_question, text, decision, confirmed):
    db, settings, sid, _, mid, _ = captured_question
    pending = prepare(captured_question)
    requested, planner = plan(settings, sid, text, decision)
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.configured = True
        result = post(client, f"/v1/agent-turns/{requested['agent_turn_id']}/execute", {})
        if confirmed:
            assert result['execution'].get('tutor_turn')
            assert post(client, f"/v1/agent-turns/{requested['agent_turn_id']}/execute", {}) == result
        else:
            assert not result['execution'].get('tutor_turn')
            assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == pending
    with db.connect() as c:
        assert c.execute('SELECT submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == ('二百零八' if confirmed else ANSWER)
        assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == int(confirmed)
    assert len(planner.requests) == 1
    context = json.loads(planner.requests[0].messages[-1]['content'])['context']
    assert context['conversation_state']['mistake_revision_readback'] == {'status': 'awaiting_confirmation', 'scope': 'question_and_original_answer'}
    transmitted = json.dumps(planner.requests[0].messages, ensure_ascii=False)
    assert pending['review_id'] not in transmitted and pending['question'] not in transmitted
    assert pending['replacement_answer'] not in transmitted


@pytest.mark.parametrize('change', ['new_offer', 'revoked_model_receipt'])
def test_delayed_or_unverified_semantic_confirmation_cannot_write(captured_question, change):
    db, settings, sid, _, mid, _ = captured_question
    prepare(captured_question)
    requested, _ = plan(settings, sid, '这次两个部分都记准啦')
    assert requested['action']['source'] == 'mistake_question_voice_confirmation'
    if change == 'new_offer':
        propose(settings, sid, {'original_answer_quote': '二百零九'}, '原作答改成二百零九', 'semantic-newer')
    else:
        with db.connect() as c:
            c.execute("UPDATE model_call SET verification_status='rejected' WHERE call_id=?",
                      (requested['action']['revision_semantics']['call_id'],))
            c.commit()
    pending = MistakeDialogueService(db).state(sid)['state']['question_confirmation']
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        result = client.post(f"/v1/agent-turns/{requested['agent_turn_id']}/execute", json={})
        if change == 'new_offer':
            assert result.status_code in {409, 422}
        else:
            assert result.status_code == 200, result.text
            assert not result.json()['execution'].get('tutor_turn')
    assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == pending
    with db.connect() as c:
        assert c.execute('SELECT submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == ANSWER


@pytest.mark.parametrize('before', [False, True])
def test_semantic_voice_confirmation_must_follow_current_readback(captured_question, monkeypatch, before):
    from test_voice_runtime_binding import capture
    db, settings, sid, _, mid, _ = captured_question
    text = '这回全都记准确了'
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
        if before:
            voice_id = capture(client, settings, monkeypatch, sid, text, child=CHILD)
    pending = prepare(captured_question)
    if not before:
        with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
            voice_id = capture(client, settings, monkeypatch, sid, text, child=CHILD)
    request, _ = plan(settings, sid, text, voice_id=voice_id)
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.configured = True
        result = post(client, f"/v1/agent-turns/{request['agent_turn_id']}/execute", {})
        assert bool(result['execution'].get('tutor_turn')) is not before
    with db.connect() as c:
        assert c.execute('SELECT submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == (ANSWER if before else '二百零八')
    if before:
        assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == pending


def test_low_confidence_semantics_cannot_authorize_revision(captured_question):
    db, settings, sid, _, _, _ = captured_question
    pending = prepare(captured_question)
    request, _ = plan(settings, sid, '这次都记准啦', confidence=.79)
    assert request['action'].get('source') != 'mistake_question_voice_confirmation'
    assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == pending
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM model_call WHERE json_extract(context_manifest_json,'$.mistake_revision_confirmation') IS NOT NULL").fetchone()[0] == 0


@pytest.mark.skipif(os.getenv('HSC_RUN_SEMANTIC_REVISION_LIVE') != '1', reason='explicit single paid synthetic probe')
def test_official_model_understands_natural_joint_confirmation(captured_question):
    from dataclasses import replace
    from pathlib import Path
    import subprocess
    import time
    import httpx
    from home_study_agent.model_gateway import ModelGateway
    db, settings, sid, _, mid, _ = captured_question
    pending = prepare(captured_question)
    credential = subprocess.run(['security', 'find-generic-password', '-s', 'hsc-deepseek-api-key', '-w'],
                                capture_output=True, text=True, timeout=15)
    assert credential.returncode == 0, 'authorized model credential unavailable'
    configured = replace(settings, model_provider='deepseek', model_api_key=credential.stdout.strip(),
        model_base_url='https://api.deepseek.com/v1', model_name='deepseek-v4-pro',
        voice_intent_model_name='deepseek-v4-pro', model_timeout_seconds=180,
        fallback_model_provider=None, fallback_model_api_key=None)
    metrics = []
    async def transport(request):
        assert not metrics and request.url.host == 'api.deepseek.com'
        payload = json.loads(request.content)
        sent = json.dumps(payload['messages'], ensure_ascii=False)
        assert mid not in sent and pending['question'] not in sent and pending['replacement_answer'] not in sent
        entry = {'requested_model': payload['model']}
        metrics.append(entry)
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            response = await client.send(request)
        body = response.json()
        entry.update(status=response.status_code, actual_model=body.get('model'), usage=body.get('usage'),
                     elapsed_seconds=round(time.monotonic() - started, 3))
        return response
    text = '我对照过啦，你这回两个地方都记准了，可以接着给我讲'
    try:
        with TestClient(create_app(configured, model_gateway=ModelGateway(configured, transport=httpx.MockTransport(transport)))) as client:
            planned = post(client, f'/v1/sessions/{sid}/agent-turn', {
                'transcript': text, 'client_action_id': 'live-natural-pair', 'cloud_text_allowed': True})
            assert planned['action'].get('source') == 'mistake_question_voice_confirmation'
            assert planned['action']['revision_semantics']['decision'] == 'both'
        gateway = Gateway(settings)
        with TestClient(create_app(settings, model_gateway=gateway)) as client:
            gateway.configured = True
            saved = post(client, f"/v1/agent-turns/{planned['agent_turn_id']}/execute", {})
            assert saved['execution'].get('tutor_turn')
            assert post(client, f"/v1/agent-turns/{planned['agent_turn_id']}/execute", {}) == saved
        with db.connect() as c:
            assert c.execute('SELECT submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == '二百零八'
        assert len(metrics) == 1
    finally:
        Path('/tmp/hsc-semantic-confirmation-live-metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
