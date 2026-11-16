"""Controlled semantic/OCR boundaries with real planning, capture and Tutor storage."""
import base64
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_ocr.service import RapidOcrEngine
from home_study_agent.model_gateway import ModelGateway
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, start
from test_voice_runtime_binding import capture, interpret

QUOTE = '看看这题，讲我不会的，再提醒我复习'
BODY = 'A box has twelve red pencils and eight blue pencils. How many pencils are there in all?'


@pytest.fixture
def journey(environment, monkeypatch, request=None, *, raw_media=False):
    settings, db = environment
    mode, child = getattr(request, 'param', ('capture', 'child-primary-4'))
    capability = 'learning.capture_question' if mode == 'capture' else 'document.monitor_control'
    settings = replace(settings, model_api_key='synthetic-only', model_base_url='https://document-goal.invalid/v1',
        model_name='controlled-document-goal', agent_runtime_enabled=True, data_encryption_key=b'G' * 32)
    payload = Planner(capability, {} if mode == 'capture' else {'command': mode}, QUOTE, objective_type='learning').payload
    payload.update(after_execution='replan', continuation_scope=[
        {'capability': 'learning.explain', 'arguments': {'subject': '数学'}, 'request_quote': '讲我不会的'},
        {'capability': 'conversation.respond', 'arguments': {}, 'request_quote': '再提醒我复习'}])
    payload['objective'].update(summary=QUOTE, success_criteria=['核对并讲解当前题目', '确认后续复习安排'])
    calls = []
    responses = []

    async def transport(request):
        assert request.url.host == 'document-goal.invalid'
        content = json.loads(request.content)
        calls.append(content)
        value = responses.pop(0) if responses else (json.dumps(payload, ensure_ascii=False) if len(calls) == 1 else '先把两种颜色的数量合起来。你能说说为什么要相加吗？')
        return httpx.Response(200, json={'choices': [{'message': {'content': value}}]})

    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    lines = [{'text': BODY, 'confidence': .99, 'box': [[55, 100], [935, 100], [935, 170], [55, 170]]}]
    monkeypatch.setattr(RapidOcrEngine, 'recognize', lambda *a, **k: lines)
    monkeypatch.setattr(RapidOcrEngine, 'recognize_page_orientation', lambda *a, **k: lines)
    with ExitStack() as original_client_lifecycle:
        client = original_client_lifecycle.enter_context(
            TestClient(create_app(settings, model_gateway=gateway), client=('127.0.0.1', 41225)))
        token = client.post('/v1/parent/setup', json={'pin': '8264'}).json()['parent_token']
        assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token},
            json={'cloud_model_text_allowed': True, 'raw_media_storage_allowed': raw_media}).status_code == 200
        if mode == 'capture':
            sid = start(client, child=child)
        else:
            from test_app import advance_to_study
            sid, _ = advance_to_study(client, child_id=child)
            assert client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id': 'start-goal-monitor'}).status_code == 200
            calls.clear()
        voice = capture(client, settings, monkeypatch, sid, QUOTE, None, 'document-goal-voice', child=child)
        root = interpret(client, sid, QUOTE, 'document-goal-root', voice)
        assert root['runtime_capability'] == capability, root
        picture = np.full((300, 1000, 3), 230, dtype=np.uint8)
        cv2.putText(picture, '12 red pencils, 8 blue pencils', (55, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
        ok, encoded = cv2.imencode('.png', picture)
        assert ok
        response = client.post(f'/v1/sessions/{sid}/' + ('ocr' if mode == 'capture' else 'document-monitor/ocr'), json={
            'image_base64': base64.b64encode(encoded.tobytes()).decode(), 'client_action_id': 'document-goal-page'})
        assert response.status_code == 200, response.text
        source = response.json()
        receipt = {'capability': 'learning.capture_question', 'execution_location': 'client_device_bridge',
            'action_type': 'request_capture', 'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(),
            'output': {'command_id': root['agent_turn_id'], 'device_status': 'recognized',
                'capture_artifact_id': source['artifact_id'], 'ocr': {'status': 'completed', 'artifact_id': source['artifact_id']}}}
        if mode != 'capture':
            page = client.post(f'/v1/sessions/{sid}/document-monitor/revisions', json={
                'artifact_id': source['artifact_id'], 'content_signature': 'synthetic-goal-page', 'client_action_id': 'save-goal-page'})
            assert page.status_code == 200, page.text
            fallback = source.get('details', {}).get('cloud_vision_fallback') or {}
            receipt.update(capability=capability, action_type='control_document_monitor',
                completed_at=datetime.now(timezone.utc).isoformat(), output={
                    'artifact_id': source['artifact_id'], 'revision_id': page.json()['revision_id'],
                    'page_status': 'blocked' if (fallback.get('status') or fallback.get('state')) == 'blocked' else 'text_available',
                    'response_text': '原页面已保存，仍需核对。'})
        response = client.put(f'/v1/agent-turns/{root["agent_turn_id"]}/outcome', json={
            'final_response': '题目已拍到，请核对正文。', 'tool_status': 'completed',
            'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}})
        assert response.status_code == 200, response.text
        response = client.post(f'/v1/sessions/{sid}/tutor-ocr-confirmations', json={
            'artifact_id': source['artifact_id'], 'confirmed_text': BODY, 'confirmation_kind': 'ocr_verified',
            'client_action_id': 'document-goal-confirm'})
        assert response.status_code == 200, response.text
        yield dict(client=client, settings=settings, db=db, sid=sid, root=root,
            source=source, confirmation=response.json(), gateway=gateway, calls=calls, responses=responses,
            finish_client=original_client_lifecycle.close)


@pytest.fixture
def continuous_journey(environment, monkeypatch, request):
    yield from journey.__wrapped__(environment, monkeypatch, request=request)


@pytest.mark.parametrize('continuous_journey', [(mode, child) for mode in ('inspect', 'review')
    for child in ('child-primary-4', 'child-junior-3')], indirect=True)
def test_continuous_page_preserves_original_teaching_goal_through_restart(continuous_journey):
    j = continuous_journey
    sid, root = j['sid'], j['root']['agent_turn_id']
    pending = j['client'].post(f'/v1/sessions/{sid}/agent-turns/{root}/continue').json()
    assert pending.get('document_goal', {}).get('phase') == 'awaiting_confirmation', pending
    assert pending['document_goal']['artifact_id'] == j['source']['artifact_id']
    assert j['root']['action']['defer_document_review'] is True
    assert len(j['calls']) == 1, 'Unconfirmed source must not invoke cloud teaching'
    j['finish_client']()  # End the original App/owner before creating a fresh App.
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        restored = restarted.post(f'/v1/sessions/{sid}/agent-turns/{root}/continue').json()
        assert restored['document_goal'] == pending['document_goal']
        j['client'] = restarted
        _, tutor = teach(j)
        after = restarted.post(f'/v1/sessions/{sid}/agent-turns/{root}/continue').json()
        assert after['document_goal']['phase'] == 'awaiting_time'
        assert after['document_goal']['tutor_turn_id'] == tutor['turn_id']
        assert after['document_goal']['artifact_id'] == j['source']['artifact_id']
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM document_page_revision').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0


def test_confirmed_question_derives_tutor_plan_from_original_scope_locally(journey):
    j = journey
    root, sid, client = j['root']['agent_turn_id'], j['sid'], j['client']
    path = f'/v1/sessions/{sid}/document-goals/{root}/confirm'
    response = client.post(path, json={'confirmation_id': j['confirmation']['confirmation_id'], 'revision': 1})
    assert response.status_code == 200, response.text
    ready = response.json()
    assert ready['status'] == 'ready'
    following = client.post(f'/v1/sessions/{sid}/agent-turns/{root}/continue').json()
    assert following['status'] == 'ready', following
    assert following['turn']['action']['question'] == BODY
    assert following['turn']['action']['ocr_confirmation_id'] == j['confirmation']['confirmation_id']
    assert following['turn']['runtime_capability'] == 'learning.explain'
    assert following['turn']['transcript'] == ''
    assert len(j['calls']) == 1, 'Source binding must not invoke cloud replan'
    with j['db'].connect() as c:
        parents = c.execute('SELECT objective_id,plan_json FROM agent_plan ORDER BY rowid').fetchall()
        assert len(parents) == 2 and parents[0]['objective_id'] == parents[1]['objective_id']
        assert json.loads(parents[0]['plan_json'])['continuation_scope'][0]['arguments'] == {'subject': '数学'}
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM agent_commitment').fetchone()[0] == 0


def teach(j, *, before_outcome=None, after_prepare=None, expected_question=None):
    root, sid, client = j['root']['agent_turn_id'], j['sid'], j['client']
    prepared = client.post(f'/v1/sessions/{sid}/document-goals/{root}/confirm', json={
        'confirmation_id': j['confirmation']['confirmation_id'], 'revision': 1})
    assert prepared.status_code == 200, prepared.text
    if after_prepare:
        after_prepare()
    turn = client.post(f'/v1/sessions/{sid}/agent-turns/{root}/continue').json()['turn']
    assert turn['action']['question'] == (expected_question or j['confirmation']['confirmed_text'])
    admission = client.post(f'/v1/sessions/{sid}/agent-turns/{turn["agent_turn_id"]}/continuation-admission')
    assert admission.json()['status'] == 'admitted', admission.text
    tutor = client.post(f'/v1/sessions/{sid}/tutor', json={
        'subject': '数学', 'question': '已确认的本地题干', 'hint_level': 1,
        'client_action_id': 'document-goal:'+root+':tutor',
        'ocr_confirmation_id': j['confirmation']['confirmation_id']})
    assert tutor.status_code == 200, tutor.text
    tutor = tutor.json()
    receipt = {'capability': 'learning.explain', 'execution_location': 'local_model',
        'action_type': 'ask_tutor', 'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(),
        'output': {'tutor_turn_id': tutor['turn_id'], 'provider': tutor['provider'], 'model': tutor['model'],
            'response_kind': tutor['response_kind'], 'verification_status': tutor['verification_status'],
            'safety_status': tutor['safety_status'], 'blackboard_present': bool((tutor.get('output_envelope') or {}).get('result', {}).get('blackboard'))}}
    if before_outcome:
        before_outcome(turn, tutor, receipt)
    saved = client.put('/v1/agent-turns/'+turn['agent_turn_id']+'/outcome', json={
        'final_response': tutor['answer'], 'tool_status': 'completed',
        'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}})
    if before_outcome:
        return turn, tutor, saved
    assert saved.status_code == 200, saved.text
    return turn, tutor


def test_saved_tutor_returns_to_root_and_keeps_missing_review_time_after_restart(journey):
    j = journey
    turn, tutor = teach(j)
    root, sid = j['root']['agent_turn_id'], j['sid']
    for client in [j['client']]:
        pending = client.post(f'/v1/sessions/{sid}/agent-turns/{root}/continue').json()
        assert pending['document_goal']['phase'] == 'awaiting_time', pending
        assert pending['document_goal']['tutor_turn_id'] == tutor['turn_id']
        assert '什么时候' in pending['document_goal']['message']
    j['finish_client']()  # End the original App/owner before creating a fresh App.
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        restored = restarted.post(f'/v1/sessions/{sid}/agent-turns/{root}/continue').json()
        assert restored['document_goal'] == pending['document_goal']
    with j['db'].connect() as c:
        rows = c.execute('SELECT objective_id FROM agent_plan ORDER BY rowid').fetchall()
        assert len(rows) == 2 and rows[0][0] == rows[1][0]
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM agent_commitment').fetchone()[0] == 0


def test_admitted_teaching_is_not_offered_for_duplicate_execution(journey):
    j = journey
    sid, root = j['sid'], j['root']['agent_turn_id']
    response = j['client'].post(f'/v1/sessions/{sid}/document-goals/{root}/confirm', json={
        'confirmation_id': j['confirmation']['confirmation_id'], 'revision': 1})
    turn = response.json()['next_turn_id']
    assert j['client'].post(f'/v1/sessions/{sid}/agent-turns/{turn}/continuation-admission').json()['status'] == 'admitted'
    for path in [f'/v1/sessions/{sid}/agent-turns/{root}/continue', f'/v1/sessions/{sid}/agent-continuation']:
        response = j['client'].post(path) if path.endswith('continue') else j['client'].get(path)
        assert response.json()['status'] == 'awaiting_receipt', response.text
        assert 'turn' not in response.json()


@pytest.mark.parametrize('change', ['answer', 'confirmation', 'action_marker'])
def test_changed_teaching_evidence_cannot_resume_review(journey, change):
    j = journey
    turn, tutor = teach(j)
    with j['db'].connect() as c:
        if change == 'answer':
            c.execute('UPDATE tutor_turn SET answer=? WHERE turn_id=?', ('altered', tutor['turn_id']))
        elif change == 'confirmation':
            c.execute('UPDATE tutor_ocr_confirmation SET revoked_at=? WHERE confirmation_id=?',
                (datetime.now(timezone.utc).isoformat(), j['confirmation']['confirmation_id']))
        else:
            c.execute("UPDATE agent_turn SET action_json=json_remove(action_json,'$.document_goal_root_turn_id') WHERE agent_turn_id=?", (turn['agent_turn_id'],))
        c.commit()
    response = j['client'].post(f'/v1/sessions/{j["sid"]}/agent-turns/{j["root"]["agent_turn_id"]}/continue')
    assert response.json()['document_goal']['phase'] == 'blocked', response.text
    assert 'tutor_turn_id' not in response.json()['document_goal']


@pytest.mark.parametrize('change', ['marker', 'action_array', 'missing_admission', 'tutor_action', 'receipt_id'])
def test_invalid_teaching_completion_does_not_advance_or_overwrite_answer(journey, change):
    j = journey
    def alter(turn, tutor, receipt):
        with j['db'].connect() as c:
            if change == 'marker':
                c.execute("UPDATE agent_turn SET action_json=json_remove(action_json,'$.document_goal_root_turn_id') WHERE agent_turn_id=?", (turn['agent_turn_id'],))
            elif change == 'action_array':
                c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', ('[]', turn['agent_turn_id']))
            elif change == 'missing_admission':
                c.execute('UPDATE agent_continuation SET execution_admitted_at=NULL WHERE next_turn_id=?', (turn['agent_turn_id'],))
            elif change == 'tutor_action':
                c.execute('UPDATE tutor_turn SET client_action_id=? WHERE turn_id=?', ('unrelated-request', tutor['turn_id']))
            else:
                receipt['output']['tutor_turn_id'] = 'unrelated-tutor'
            c.commit()
    turn, tutor, response = teach(j, before_outcome=alter)
    assert response.status_code == 409, response.text
    with j['db'].connect() as c:
        row = c.execute('SELECT response_text,tool_status FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
        assert row['response_text'] != tutor['answer'] and row['tool_status'] == 'pending'
        state = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
            (j['root']['agent_turn_id'],)).fetchone()[0])['document_goal_handoff_v1']
        assert state['phase'] == 'ready' and not state.get('tutor_turn_id')
        assert c.execute('SELECT count(*) FROM agent_commitment').fetchone()[0] == 0


def test_unrelated_legacy_nonobject_action_is_not_parsed_by_document_hook(journey):
    from home_study_agent.document_goal_handoff import complete_teaching
    j = journey
    with j['db'].connect() as c:
        c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', ('[]', j['root']['agent_turn_id']))
        complete_teaching(c, j['root']['agent_turn_id'], None)
        c.rollback()


def confirm_by_voice(j, monkeypatch, suffix='', corrected_text=None, confirmed_text=BODY, manual=False):
    client, sid, root = j['client'], j['sid'], j['root']['agent_turn_id']
    quote = '题干是对的' if corrected_text is None else '改成：' + corrected_text
    confirmation_plan = Planner('document.confirm_ocr' if corrected_text is None else 'document.correct_ocr',
        {} if corrected_text is None else {'corrected_text': corrected_text}, quote, objective_type='learning').payload
    confirmation_plan['objective']['mode'] = 'continue'
    j['responses'].append(json.dumps(confirmation_plan, ensure_ascii=False))
    voice = capture(client, j['settings'], monkeypatch, sid, quote, key='confirm-document-voice'+suffix)
    heard = interpret(client, sid, quote, 'confirm-document-turn'+suffix, voice)
    assert heard['action']['type'] == ('confirm_ocr_text' if corrected_text is None else 'correct_ocr_text'), heard
    assert heard['action']['document_goal_confirmation_root'] == root
    confirmed = client.post(f'/v1/sessions/{sid}/tutor-ocr-confirmations', json={
        'artifact_id': j['source']['artifact_id'], 'confirmed_text': corrected_text or confirmed_text,
        'confirmation_kind': 'manual_correction' if manual or corrected_text or confirmed_text != BODY else 'ocr_verified',
        'client_action_id': 'after-voice-document-confirm'+suffix})
    assert confirmed.status_code == 200, confirmed.text
    j['confirmation'] = confirmed.json()
    return heard


def test_pending_review_is_read_only_and_restores_the_original_material_across_restart(journey):
    j = journey
    path = f'/v1/sessions/{j["sid"]}/document-goals/{j["root"]["agent_turn_id"]}/review'
    def snapshot():
        with j['db'].connect() as c:
            return {table: [dict(row) for row in c.execute('SELECT * FROM '+table)] for table in
                ('capture_artifact', 'tutor_ocr_confirmation', 'tutor_turn', 'agent_turn', 'agent_plan', 'agent_continuation')}
    before = snapshot()
    result = j['client'].get(path)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['review']['text'] == BODY
    assert data['review']['artifactId'] == j['source']['artifact_id']
    assert data['review']['status'] == 'uncertain', 'The narrow synthetic page has a quality warning, not a clear-page pass'
    assert data['cloud_transmission_allowed'] is data['mastery_update_allowed'] is False
    assert 'local_path' not in json.dumps(data) and 'image_base64' not in json.dumps(data)
    j['finish_client']()  # End the original App/owner before creating a fresh App.
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        assert restarted.get(path).json() == data
    code = '''import json,sys
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.document_goal_handoff import review
print(json.dumps(review(Database(Path(sys.argv[1])),sys.argv[2],sys.argv[3])))
'''
    process = subprocess.run([sys.executable, '-c', code, str(j['db'].path), j['root']['agent_turn_id'], j['sid']],
        capture_output=True, text=True, timeout=20, env={'PATH':'/usr/bin:/bin', 'HOME':str(Path(j['db'].path).parent),
            'PYTHONPATH':str(Path(__file__).resolve().parents[1] / 'src')})
    assert process.returncode == 0, process.stderr[-1200:]
    assert json.loads(process.stdout) == data
    assert snapshot() == before
    assert len(j['calls']) == 1


@pytest.mark.parametrize('change', ['session', 'source', 'phase', 'newer_voice', 'manifest'])
def test_review_cannot_restore_another_or_changed_goal(journey, monkeypatch, change):
    j = journey
    sid = j['sid']
    if change == 'phase':
        teach(j)
    elif change == 'session':
        sid = 'another-session'
    elif change == 'source':
        with j['db'].connect() as c:
            c.execute('UPDATE capture_artifact SET extracted_text=? WHERE artifact_id=?', ('different', j['source']['artifact_id']))
            c.commit()
    elif change == 'manifest':
        with j['db'].connect() as c:
            c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?', ('[]', j['root']['agent_turn_id']))
            c.commit()
    else:
        # New requests, not read-only inspection, take precedence over old work.
        with j['db'].connect() as c:
            arrival = dict(c.execute('SELECT * FROM learning_input_arrival ORDER BY rowid DESC LIMIT 1').fetchone())
            c.execute('UPDATE learning_input_arrival SET client_action_id=? WHERE arrival_id=?',
                ('newer-review-input', arrival['arrival_id']))
            c.commit()
    response = j['client'].get(f'/v1/sessions/{sid}/document-goals/{j["root"]["agent_turn_id"]}/review')
    assert response.status_code in (404, 409), response.text
    assert BODY not in response.text


def test_spoken_correction_survives_restart_and_a_later_plain_confirmation(journey, monkeypatch):
    j = journey
    corrected = BODY.replace('eight blue', 'nine blue')
    confirm_by_voice(j, monkeypatch, '-correction', corrected_text=corrected)
    path = f'/v1/sessions/{j["sid"]}/document-goals/{j["root"]["agent_turn_id"]}'
    j['finish_client']()  # End the original App/owner before creating a fresh App.
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        restored = restarted.get(path+'/review')
        assert restored.status_code == 200, restored.text
        assert restored.json()['review']['text'] == corrected
        assert restored.json()['review']['status'] == 'uncertain'
        j['client'] = restarted
        confirm_by_voice(j, monkeypatch, '-confirm-correction', confirmed_text=corrected)
        prepared = j['client'].post(path+'/confirm', json={'confirmation_id':j['confirmation']['confirmation_id'], 'revision':2})
        assert prepared.status_code == 200 and prepared.json()['status'] == 'ready', prepared.text
    with j['db'].connect() as c:
        following = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (prepared.json()['next_turn_id'],)).fetchone()
        assert json.loads(following[0])['question'] == corrected
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0


@pytest.mark.parametrize('changed', [False, True])
def test_plain_voice_confirmation_of_uncertain_source_only_accepts_its_exact_text(journey, monkeypatch, changed):
    j = journey
    confirm_by_voice(j, monkeypatch, manual=True, confirmed_text=BODY+' invented' if changed else BODY)
    path = f'/v1/sessions/{j["sid"]}/document-goals/{j["root"]["agent_turn_id"]}/confirm'
    response = j['client'].post(path, json={'confirmation_id':j['confirmation']['confirmation_id'], 'revision':1})
    assert response.status_code == (409 if changed else 200), response.text


def test_recovery_rejects_a_changed_saved_correction_voice(journey, monkeypatch):
    j = journey
    heard = confirm_by_voice(j, monkeypatch, '-correction', corrected_text=BODY.replace('eight', 'nine'))
    confirm_by_voice(j, monkeypatch, '-later', confirmed_text=BODY.replace('eight', 'nine'))
    with j['db'].connect() as c:
        c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.corrected_text','invented') WHERE agent_turn_id=?", (heard['agent_turn_id'],))
        c.commit()
    response = j['client'].get(f'/v1/sessions/{j["sid"]}/document-goals/{j["root"]["agent_turn_id"]}/review')
    assert response.status_code == 409
    assert BODY not in response.text


def test_new_voice_confirmation_after_unprepared_attempt_rebinds_without_losing_the_goal(journey, monkeypatch):
    j = journey
    confirm_by_voice(j, monkeypatch)
    heard = confirm_by_voice(j, monkeypatch, '-retry')
    assert heard['action']['document_goal_confirmation_revision'] == 2
    response = j['client'].post(f'/v1/sessions/{j["sid"]}/document-goals/{j["root"]["agent_turn_id"]}/confirm', json={
        'confirmation_id': j['confirmation']['confirmation_id'], 'revision': 2})
    assert response.status_code == 200 and response.json()['status'] == 'ready', response.text


def test_late_confirmation_does_not_fall_back_to_an_unowned_new_tutor(journey, monkeypatch):
    j = journey
    teach(j)
    payload = Planner('document.confirm_ocr', {}, '题干是对的', objective_type='learning').payload
    payload['objective']['mode'] = 'continue'
    j['responses'].append(json.dumps(payload, ensure_ascii=False))
    voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], '题干是对的', key='late-confirm-voice')
    heard = interpret(j['client'], j['sid'], '题干是对的', 'late-confirm-turn', voice)
    assert heard['action']['document_goal_confirmation_blocked'] is True


def test_voice_confirmation_keeps_exact_original_capture_goal(journey, monkeypatch):
    j = journey
    heard = confirm_by_voice(j, monkeypatch)
    assert heard['action']['document_goal_confirmation_revision'] == 1
    def save_confirmation_receipt():
        receipt = {'capability': 'document.confirm_ocr', 'execution_location': 'client_device_bridge',
            'action_type': 'confirm_ocr_text', 'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(), 'output': {}}
        result = j['client'].put('/v1/agent-turns/'+heard['agent_turn_id']+'/outcome', json={
            'final_response': '原题已经核对。', 'tool_status': 'completed', 'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}})
        assert result.status_code == 200, result.text
    turn, tutor = teach(j, after_prepare=save_confirmation_receipt)
    pending = j['client'].post(f'/v1/sessions/{j["sid"]}/agent-turns/{j["root"]["agent_turn_id"]}/continue').json()
    assert pending['document_goal']['phase'] == 'awaiting_time', pending
    assert pending['document_goal']['tutor_turn_id'] == tutor['turn_id']
    assert BODY not in json.dumps(j['calls'][:2]), 'Capture evidence must not leak into semantic confirmation planning'


@pytest.mark.parametrize('change', ['voice', 'root_binding', 'confirmation_time'])
def test_changed_voice_confirmation_cannot_prepare_original_lesson(journey, monkeypatch, change):
    j = journey
    heard = confirm_by_voice(j, monkeypatch)
    with j['db'].connect() as c:
        if change == 'voice':
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_id','unrelated-voice') WHERE agent_turn_id=?", (heard['agent_turn_id'],))
        elif change == 'root_binding':
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.document_goal_confirmation_root','unrelated-root') WHERE agent_turn_id=?", (heard['agent_turn_id'],))
        else:
            c.execute('UPDATE tutor_ocr_confirmation SET confirmed_at=? WHERE confirmation_id=?',
                ('2000-01-01T00:00:00+00:00', j['confirmation']['confirmation_id']))
        c.commit()
    response = j['client'].post(f'/v1/sessions/{j["sid"]}/document-goals/{j["root"]["agent_turn_id"]}/confirm', json={
        'confirmation_id': j['confirmation']['confirmation_id'], 'revision': 1})
    assert response.status_code == 409, response.text
    with j['db'].connect() as c:
        assert c.execute("SELECT count(*) FROM agent_turn WHERE intent='document_goal_handoff'").fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 0
