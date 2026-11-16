"""Writing-only review of uncertain OCR; raw page quality never becomes authority."""
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_writing_voice_runtime_http import (
    ControlGateway, capture, environment, interpret, page, readback_receipt,
    start, writing_prompt, TEXT,
)


def warning_read(client, db, settings, monkeypatch, gateway, text=TEXT, quality_details=None):
    sid = start(client)
    prompt = writing_prompt(client, db, sid)
    revision = page(db, sid, text)
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET status='quality_warning',confidence=.49 WHERE artifact_id='page'")
        c.execute("UPDATE document_page_revision SET review_required=1,ocr_confidence=.49 WHERE artifact_id='page'")
        details=quality_details if quality_details is not None else {
            'quality':{'passed':False,'issues':['low_ocr_confidence']}}
        c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='page'",(json.dumps(details),))
        c.commit()
    gateway.quote, gateway.control = '作文写完了，你帮我看看吧', 'writing.read_original'
    voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt)
    interpret(client, sid, gateway.quote, 'warning-look', voice)
    quote = '这页只有我写的答案'
    voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'warning-scope-voice')
    turn = interpret(client, sid, quote, 'warning-scope', voice)
    assert turn['action']['type'] == 'read_writing_original', turn['response_text']
    return sid, prompt, turn, revision


def finish_read(client, sid, turn, key):
    receipt = readback_receipt(client, sid, turn, playback_id=key)
    result = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
        'final_response': '原稿已读回', 'tool_status': 'completed', 'result': receipt})
    assert result.status_code == 200, result.text


def test_warning_scope_only_prepares_readback_not_original(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        packet = client.get(f'/v1/sessions/{sid}/writing-ocr', params=turn['action']['writing_ocr_confirmation']).json()
        assert TEXT in packet['readback_text']
        assert '需要核对' in packet['readback_text']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0] == 0
            assert c.execute("SELECT status FROM capture_artifact WHERE artifact_id='page'").fetchone()[0] == 'quality_warning'
            assert c.execute('SELECT review_required FROM document_page_revision').fetchone()[0] == 1


def test_warning_correction_requires_new_full_readback_and_confirmation(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        finish_read(client, sid, turn, 'first-read')
        old, new = 'Saturday', 'Sunday'
        assert TEXT.count(old) == 1
        quote = f'识别更正，把“{old}”改成“{new}”'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'correct-voice')
        calls = len(gateway.requests)
        corrected = interpret(client, sid, quote, 'correct-ocr', voice)
        assert len(gateway.requests) == calls
        assert corrected['action']['type'] == 'read_writing_original', corrected
        binding = corrected['action']['writing_ocr_confirmation']
        assert binding != turn['action']['writing_ocr_confirmation']
        packet = client.get(f'/v1/sessions/{sid}/writing-ocr', params=binding).json()
        assert packet['response_text'] == TEXT.replace(old, new)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        finish_read(client, sid, corrected, 'corrected-read')
        gateway.quote, gateway.control = '这是我自己写的答案识别正确', 'writing.confirm_original'
        voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt, 'accurate-voice')
        confirmed = interpret(client, sid, gateway.quote, 'accurate-ocr', voice)
        assert confirmed['action']['type'] == 'confirm_writing_original'
        response = client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        with db.connect() as c:
            assert [r[0] for r in c.execute('SELECT submitted_answer FROM diagnostic_attempt')] == [TEXT.replace(old, new)]
            assert c.execute("SELECT extracted_text FROM capture_artifact WHERE artifact_id='page'").fetchone()[0] == TEXT
            assert c.execute("SELECT status FROM capture_artifact WHERE artifact_id='page'").fetchone()[0] == 'quality_warning'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        packet = client.get(f'/v1/sessions/{sid}/writing-ocr', params=binding)
        assert packet.status_code == 200, packet.text
        assert packet.json()['response_text'] == TEXT.replace(old, new)
    assert all(TEXT not in json.dumps(request.messages) for request in gateway.requests)


@pytest.mark.parametrize('fault', ['no_readback', 'missing_anchor', 'nonunique_anchor', 'same_text', 'no_voice'])
def test_invalid_correction_preserves_frozen_original_review(environment, monkeypatch, fault):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        if fault != 'no_readback':
            finish_read(client, sid, turn, 'before-bad-correction')
        old = {'missing_anchor': 'not in this page', 'nonunique_anchor': 'the'}.get(fault, 'Saturday')
        new = old if fault == 'same_text' else 'Sunday'
        quote = f'识别更正，把“{old}”改成“{new}”'
        voice = None if fault == 'no_voice' else capture(client, settings, monkeypatch, sid, quote, prompt, 'bad-correction-voice')
        result = interpret(client, sid, quote, 'bad-correction', voice)
        assert result['action']['type'] == 'none'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM writing_ocr_confirmation').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['page_changed', 'raw_changed', 'retracted', 'unfinished_corrected_read', 'scope_only'])
def test_review_cannot_authorize_other_or_unread_text(environment, monkeypatch, fault):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        finish_read(client, sid, turn, 'review-read')
        binding = turn['action']['writing_ocr_confirmation']
        if fault == 'page_changed':
            page(db, sid, TEXT.replace('Saturday', 'Monday'), 'next-page')
        elif fault == 'raw_changed':
            with db.connect() as c:
                c.execute("UPDATE capture_artifact SET extracted_text=extracted_text||' changed' WHERE artifact_id='page'")
                c.commit()
        elif fault == 'retracted':
            response = client.post(f'/v1/sessions/{sid}/writing-ocr/retract', json={**binding, 'client_action_id': 'withdraw-review'})
            assert response.status_code == 200, response.text
        elif fault == 'unfinished_corrected_read':
            quote = '识别更正，把“Saturday”改成“Sunday”'
            voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'unread-correction-voice')
            new = interpret(client, sid, quote, 'unread-correction', voice)
            assert new['action']['type'] == 'read_writing_original'
            # An old full-read event cannot complete the new full-text read.
            old_read = client.post(f'/v1/sessions/{sid}/writing-ocr/readback', json={**binding,
                'client_action_id':'old-read-again','playback_id':'old-again','event':'started',
                'readback_sha256':'0'*64})
            assert old_read.status_code == 409
        quote = '这页只有我写的答案' if fault == 'scope_only' else '这是我自己写的答案识别正确'
        gateway.quote, gateway.control = quote, 'writing.confirm_original'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'bad-confirm-voice')
        turn = interpret(client, sid, quote, 'bad-confirm', voice)
        if turn['action']['type'] == 'confirm_writing_original':
            result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert result.status_code == 409 or result.json().get('execution', {}).get('status') == 'blocked', result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0] == 0


def test_restart_can_voice_reread_same_corrected_version(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        finish_read(client, sid, turn, 'restart-first-read')
        quote = '识别更正，把“Saturday”改成“Sunday”'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'restart-correct-voice')
        corrected = interpret(client, sid, quote, 'restart-correct', voice)
        binding = corrected['action']['writing_ocr_confirmation']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.quote, gateway.control = '请再读一次本页原稿', 'writing.read_original'
        voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt, 'reread-voice')
        reread = interpret(client, sid, gateway.quote, 'reread-after-restart', voice)
        assert reread['action']['writing_ocr_confirmation'] == binding
        finish_read(client, sid, reread, 'fresh-epoch-read')
        packet = client.get(f'/v1/sessions/{sid}/writing-ocr', params=binding)
        assert packet.json()['response_text'] == TEXT.replace('Saturday', 'Sunday')
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM writing_ocr_confirmation').fetchone()[0] == 2
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_other_session_cannot_read_or_correct_warning(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        finish_read(client, sid, turn, 'private-review-read')
        result = client.get('/v1/sessions/unrelated-session/writing-ocr', params=turn['action']['writing_ocr_confirmation'])
        assert result.status_code in (404,409)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_review_export_and_erasure_keep_child_scope(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    app = create_app(settings, model_gateway=gateway)
    with TestClient(app) as client:
        sid, _, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        finish_read(client, sid, turn, 'export-review-read')
        from test_writing_voice_runtime_http import CHILD
        exported = app.state.privacy_lifecycle.export_child(CHILD)
        content = json.loads(Path(exported['path']).read_text())
        assert len(content['writing_ocr_reviews']) == 1
        token = client.post('/v1/parent/setup', json={'pin':'2468'}).json()['parent_token']
        result = client.post(f'/v1/parent/children/{CHILD}/delete', headers={'X-HSC-Parent':token}, json={
            'scope':'media','confirmation':'确认删除','client_action_id':'delete-review'})
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM writing_ocr_review').fetchone()[0] == 0
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
