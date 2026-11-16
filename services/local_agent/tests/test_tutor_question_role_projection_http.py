"""Exact local Tutor questions: actual ASR/HTTP, controlled classifier only."""
import json
from dataclasses import replace
import hashlib
import asyncio

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_content_free_voice_control_http import ControlGateway
from test_polite_control_requests import open_context, flow
from test_voice_runtime_binding import capture, interpret


class ProjectedGateway(ControlGateway):
    async def complete(self, request):
        hook, self.hook = self.hook, None
        try:
            result = await super().complete(request)
        finally:
            self.hook = hook
        if hook:
            # A concurrent real HTTP caller cannot use TestClient's sync portal
            # from the gateway's own event-loop thread.
            await asyncio.to_thread(hook)
        value = json.loads(result.content)
        value['quote'] = json.loads(request.messages[-1]['content'])['transcript']
        value.update(getattr(self, 'override', {}))
        return replace(result, content=json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize('quoted_kind', ['term', 'whole_original_question'])
def test_quoted_tutor_question_returns_original_question_path_without_sending_quoted_source(flow, monkeypatch, quoted_kind):
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    gateway = ProjectedGateway('', None, role='question')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, playback = open_context(client, sid, turn)
        quoted = '移项' if quoted_kind == 'term' else prompt['item']['prompt']
        text = f'能解释“{quoted}”是什么意思吗？'
        gateway.quote = text
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        heard = interpret(client, sid, text, 'quoted-question', voice)
        assert heard['action']['type'] == 'ask_tutor', heard
        assert len(gateway.requests) == 1
        wire = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        assert quoted not in wire
        assert all(value not in wire for value in (sid, voice, prompt['prompt_id'], turn['turn_id'], prompt['item']['prompt']))
        assert not heard.get('requires_control_admission')
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM tutor_playback_checkpoint WHERE playback_id=?',
                             (playback['playback_id'],)).fetchone()[0] == 'playing'
            journal = c.execute('SELECT * FROM voice_control_intent_request').fetchone()
            frozen = json.loads(journal['binding_json'])['tutor_question_projection']
            projected = json.loads(gateway.requests[0].messages[-1]['content'])['transcript']
            assert frozen['version'] == 'tutor_question_role_projection_v1'
            assert frozen['original_sha256'] == hashlib.sha256(text.encode()).hexdigest()
            assert frozen['projected_sha256'] == hashlib.sha256(projected.encode()).hexdigest()
            assert frozen['question_reference']['binding'] == {k: prompt[k] for k in ('prompt_id', 'item_version')}
            assert quoted not in json.dumps(frozen, ensure_ascii=False)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, text, 'quoted-question', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert replay['action']['type'] == 'ask_tutor'
        assert len(gateway.requests) == 1


@pytest.mark.parametrize('override', [
    {'role': 'control', 'control': 'tutor.pause'}, {'role': 'answer'}, {'role': 'mixed'},
    {'role': 'unknown'}, {'confidence': .3}, {'quote': '能解释“移项”是什么意思吗？'},
])
def test_projected_role_can_never_authorize_control_answer_or_uncertain_question(flow, monkeypatch, override):
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    text = '能解释“移项”是什么意思吗？'
    gateway = ProjectedGateway(text, None, role='question')
    gateway.override = override
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, playback = open_context(client, sid, turn)
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        heard = interpret(client, sid, text, 'not-a-qualified-question', voice)
        assert heard['action']['type'] == 'none', heard
        assert not heard.get('requires_control_admission')
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM tutor_playback_checkpoint WHERE playback_id=?',
                             (playback['playback_id'],)).fetchone()[0] == 'playing'


@pytest.mark.parametrize('change', ['consent_regrant', 'tutor_source', 'question_cancel', 'voice_cancel', 'journal_retract'])
def test_projected_question_receipt_never_survives_source_consent_or_original_input_change(flow, monkeypatch, change):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    text = '能解释“移项”是什么意思吗？'
    gateway = ProjectedGateway(text, None, role='question')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, _ = open_context(client, sid, turn)
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        mutated = []
        def mutate():
            if change == 'consent_regrant':
                token = client.post('/v1/parent/unlock', json={'pin': '7531'}).json()['parent_token']
                for allowed in (False, True):
                    response = client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token}, json={'cloud_model_text_allowed': allowed})
                    assert response.status_code == 200, response.text
            elif change == 'question_cancel':
                DiagnosticPromptService(db).cancel(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
            elif change == 'voice_cancel':
                assert client.post(f'/v1/sessions/{sid}/asr/actions/capture-voice/cancel').status_code == 200
            else:
                with db.connect() as c:
                    if change == 'tutor_source':
                        c.execute('UPDATE tutor_turn SET answer=? WHERE turn_id=?', ('Source revised', turn['turn_id']))
                    else:
                        c.execute('DELETE FROM voice_control_intent_request')
                    c.commit()
            mutated.append(change)
        gateway.hook = mutate
        request = {'transcript': text, 'client_action_id': 'projected-source-race',
                   'cloud_text_allowed': True, 'voice_input_id': voice}
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        assert mutated == [change], 'The actual invalidation must run; a swallowed fixture error is not evidence.'
        assert len(gateway.requests) == 1
        if response.status_code == 200:
            assert response.json()['action']['type'] == 'none', response.text
        else:
            assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert not c.execute("SELECT 1 FROM agent_turn WHERE client_action_id='projected-source-race' AND json_extract(action_json,'$.type')='ask_tutor'").fetchone()


def test_projection_requires_real_parent_consent_record_before_any_dispatch(flow, monkeypatch):
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    text = '能解释“移项”是什么意思吗？'
    gateway = ProjectedGateway(text, None, role='question')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, _ = open_context(client, sid, turn)
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        with db.connect() as c:
            c.execute("DELETE FROM consent_record WHERE scope='cloud_model_text_allowed'")
            c.commit()
        result = interpret(client, sid, text, 'no-consent-record', voice)
        assert result['action']['type'] == 'none'
        assert not gateway.requests
