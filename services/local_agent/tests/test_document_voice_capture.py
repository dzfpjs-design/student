"""Owned ordinary page references through real HTTP/SQLite, synthetic OCR only."""
import base64
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.document_voice_source import validate_reference
from home_study_agent.local_ocr.service import RapidOcrEngine
from home_study_agent.voice_capture_binding import normalize_question_reference, VoiceCaptureBindingError
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_agent_continuation import Gateway


@pytest.fixture
def page_context(environment, monkeypatch):
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'D' * 32)
    gateway = Gateway({}); gateway.configured = False
    lines = [{'text': '1. Calculate (24 + 6) x 3.', 'confidence': .99,
              'box': [[35, 90], [730, 90], [730, 155], [35, 155]]}]
    monkeypatch.setattr(RapidOcrEngine, 'recognize', lambda *a, **k: lines)
    monkeypatch.setattr(RapidOcrEngine, 'recognize_page_orientation', lambda *a, **k: lines)
    with ExitStack() as parent_lifetime:
        client = parent_lifetime.enter_context(TestClient(create_app(settings, model_gateway=gateway)))
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,)); c.commit()
        assert client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id':'start-owned-page'}).status_code == 200
        image = np.full((260, 800, 3), 230, dtype=np.uint8)
        cv2.putText(image, 'Calculate (24 + 6) x 3', (35, 145), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
        ok, encoded = cv2.imencode('.png', image); assert ok
        def save(key):
            captured = client.post(f'/v1/sessions/{sid}/document-monitor/ocr', json={
                'image_base64': base64.b64encode(encoded.tobytes()).decode(), 'client_action_id':'ocr-' + key})
            assert captured.status_code == 200, captured.text
            revision = client.post(f'/v1/sessions/{sid}/document-monitor/revisions', json={
                'artifact_id':captured.json()['artifact_id'], 'content_signature':key, 'client_action_id':'revision-' + key})
            assert revision.status_code == 200, revision.text
            return revision.json()
        original = save('ordinary-original')
        reference = {'kind':'document', 'binding':{
            'revision_id':original['revision_id'], 'artifact_id':original['artifact_id'],
            'content_sha256':original['content_sha256'], 'page_sha256':original['page_source']['sha256']}}
        parent_close_attempted = False

        def close_parent_app():
            # One handoff of the original App, not HTTP client.close(). The
            # TestClient exit consumes lifespan and joins its original portal.
            nonlocal parent_close_attempted
            assert parent_close_attempted is False, 'PARENT_APP_CLOSE_ALREADY_ATTEMPTED'
            parent_close_attempted = True
            portal, lifespan = client.portal, client.task
            owner = client.app.state.money_owner
            app_db = client.app.state.agent_orchestrator.database
            maintain_code = client.app.state.visual_capture.maintain.__func__.__code__
            def original_visual_tasks():
                return tuple(task for task in asyncio.all_tasks()
                             if getattr(task.get_coro(), 'cr_code', None) is maintain_code)
            visual_tasks = portal.call(original_visual_tasks)
            assert len(visual_tasks) == 1, 'PARENT_ORIGINAL_VISUAL_TASK_REQUIRED'
            parent_lifetime.close()
            assert client.portal is None and portal._event_loop_thread_id is None, 'PARENT_PORTAL_CLOSE_UNCONFIRMED'
            assert lifespan.done() and not lifespan.cancelled(), 'PARENT_LIFESPAN_CLOSE_UNCONFIRMED'
            assert lifespan.result(timeout=0) is None, 'PARENT_LIFESPAN_CLOSE_UNCONFIRMED'
            assert client.app.state.money_owner is owner and owner.closed is True, 'PARENT_MONEY_OWNER_CLOSE_UNCONFIRMED'
            assert all(task.done() and (task.cancelled() or task.exception() is None)
                       for task in visual_tasks), 'PARENT_VISUAL_TASK_CLOSE_UNCONFIRMED'
            assert all(task.done() for task in client.app.state.background_tasks), 'PARENT_BACKGROUND_TASK_CLOSE_UNCONFIRMED'
            assert not app_db._original_connections and not db._original_connections, 'PARENT_DATABASE_CONNECTION_CLOSE_UNCONFIRMED'
            return {'lifespan_consumed': True, 'portal_joined': True,
                    'money_owner_closed': True, 'visual_task_closed': True,
                    'background_tasks_closed': True, 'database_connections_closed': True}

        yield dict(client=client, db=db, sid=sid, settings=settings, gateway=gateway, original=original,
                   reference=reference, save=save, close_parent_app=close_parent_app)


def begin(j, key='ordinary-voice-onset', reference=None, sid=None):
    return j['client'].post(f"/v1/sessions/{sid or j['sid']}/voice/realtime/begin", json={
        'client_action_id':key, 'model_label':'synthetic-asr',
        'captured_at_ms':int(datetime.now(timezone.utc).timestamp() * 1000),
        'capture_reference':{'child_id': CHILD if sid is None else OTHER,
                             'question_reference':reference or j['reference']}})


def complete(j, key='ordinary-voice-onset', text='小知，关闭摄像头'):
    result = j['client'].post(f"/v1/sessions/{j['sid']}/voice/realtime/complete", json={
        'client_action_id':key, 'transcript':text})
    assert result.status_code == 200, result.text
    return result.json()


def test_original_page_survives_turning_page_and_runtime_restart_without_answer_authority(page_context):
    j = page_context
    with j['db'].connect() as c:
        validate_reference(c, j['sid'], CHILD, j['reference']['binding'],
                           page_source_store=j['client'].app.state.media_ai)
    accepted = begin(j)
    assert accepted.status_code == 200, accepted.text
    current = j['save']('later-ordinary-page')
    assert current['revision_id'] != j['original']['revision_id']
    original = complete(j)
    assert original['question_reference'] == j['reference']
    assert original['question_validation'] == 'valid'
    binding = j['reference']['binding']
    for action in ['submit_diagnostic_answer', 'submit_exam_answer', 'submit_pet_vocabulary_answer', 'continue_mistake_dialogue']:
        assert not AgentRuntimeStore.capture_matches_answer(original, action, binding)
    body = {'transcript':original['original_transcript'], 'voice_input_id':original['voice_input_id'],
            'client_action_id':'runtime-ordinary-page-voice', 'cloud_text_allowed':True}
    result = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json=body)
    assert result.status_code == 200, result.text
    turn = result.json()
    assert turn['action']['voice_input_binding']['question_reference'] == j['reference']
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        recovered = restarted.post(f"/v1/sessions/{j['sid']}/agent-turn", json=body)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()['agent_turn_id'] == turn['agent_turn_id']
        assert recovered.json()['action']['voice_input_binding']['question_reference'] == j['reference']
    with j['db'].connect() as c:
        assert json.loads(c.execute('SELECT question_reference_json FROM voice_capture_binding WHERE voice_input_id=?',
                                   (original['voice_input_id'],)).fetchone()[0]) == j['reference']
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 0
    assert not j['gateway'].requests


@pytest.mark.parametrize('change', ['text', 'page_hash', 'deleted', 'artifact_owner', 'revision_owner', 'corrupt_original'])
def test_changed_original_is_preserved_as_stale_not_replaced_by_latest(page_context, change):
    j = page_context
    accepted = begin(j)
    assert accepted.status_code == 200, accepted.text
    with j['db'].connect() as c:
        if change == 'text':
            c.execute("UPDATE capture_artifact SET extracted_text='different problem' WHERE artifact_id=?", (j['original']['artifact_id'],))
        elif change == 'page_hash':
            row = c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (j['original']['artifact_id'],)).fetchone()
            value = json.loads(row[0]); value['page_source']['sha256'] = '0' * 64
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(value), j['original']['artifact_id']))
        elif change == 'deleted':
            c.execute('UPDATE capture_artifact SET local_path=NULL WHERE artifact_id=?', (j['original']['artifact_id'],))
        elif change == 'artifact_owner':
            c.execute('UPDATE capture_artifact SET child_id=? WHERE artifact_id=?', (OTHER, j['original']['artifact_id']))
        elif change == 'revision_owner':
            c.execute('UPDATE document_page_revision SET child_id=? WHERE revision_id=?', (OTHER, j['original']['revision_id']))
        else:
            path = c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?', (j['original']['artifact_id'],)).fetchone()[0]
            Path(path).write_bytes(b'HSC1' + b'broken-ciphertext' * 4)
        c.commit()
    saved = complete(j)
    assert saved['question_reference'] == j['reference']
    assert saved['question_validation'] == 'stale'
    assert saved['original_transcript'] == '小知，关闭摄像头'


def test_runtime_rechecks_original_bytes_after_completed_speech(page_context):
    j = page_context
    assert begin(j).status_code == 200
    saved = complete(j)
    assert saved['question_validation'] == 'valid'
    with j['db'].connect() as c:
        path = c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?', (j['original']['artifact_id'],)).fetchone()[0]
    Path(path).write_bytes(b'HSC1' + b'broken-ciphertext' * 4)
    result = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json={
        'transcript':saved['original_transcript'], 'voice_input_id':saved['voice_input_id'],
        'client_action_id':'runtime-corrupt-original-after-speech', 'cloud_text_allowed':True})
    assert result.status_code == 200, result.text
    assert result.json()['action']['voice_input_binding']['question_validation'] == 'stale'
    assert not j['gateway'].requests


def test_natural_help_reads_the_frozen_page_not_latest_and_does_not_export_ocr(page_context):
    j = page_context
    quote = '刚才这道题我卡住了，能陪我理一理吗'
    j['gateway'].configured = True
    j['gateway'].output = {'role':'control', 'control':'document.review', 'quote':quote, 'confidence':.95}
    assert begin(j).status_code == 200
    voice = complete(j, text=quote)
    latest = j['save']('next-page-while-thinking')
    turn_response = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json={
        'transcript':quote,'voice_input_id':voice['voice_input_id'],
        'client_action_id':'original-document-natural-help','cloud_text_allowed':True})
    assert turn_response.status_code == 200, turn_response.text
    turn = turn_response.json()
    assert turn['action']['type'] == 'control_document_monitor' and turn['action']['command'] == 'review'
    source = j['client'].get(f"/v1/sessions/{j['sid']}/agent-turns/{turn['agent_turn_id']}/document-source")
    assert source.status_code == 200, source.text
    assert source.json()['revision_id'] == j['original']['revision_id'] != latest['revision_id']
    assert source.headers['cache-control'] == 'no-store'
    assert turn['requires_control_admission'] is True
    admission = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turns/{turn['agent_turn_id']}/control-admission")
    assert admission.status_code == 200 and admission.json()['status'] == 'admitted', admission.text
    output = {'artifact_id': j['original']['artifact_id'], 'revision_id': j['original']['revision_id'],
              'page_status': source.json()['page_status'], 'response_text':'原页已读取，尚未讲解或评分。'}
    receipt = {'capability':'document.monitor_control', 'execution_location':'client_device_bridge',
        'action_type':'control_document_monitor', 'status':'completed',
        'completed_at':datetime.now(timezone.utc).isoformat(), 'output':output}
    body = {'final_response':output['response_text'], 'tool_status':'completed',
            'result':{'execution_receipt':receipt, 'client_bridge_receipt':deepcopy(receipt)}}
    wrong = deepcopy(body)
    for value in wrong['result'].values():
        value['output']['revision_id'] = latest['revision_id']
        value['output']['artifact_id'] = latest['artifact_id']
    assert j['client'].put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=wrong).status_code == 409
    unsaved = j['client'].put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=body)
    assert unsaved.status_code == 409
    assert unsaved.json()['code'] == 'DEVICE_RECEIPT_DOCUMENT_REVIEW_NOT_SAVED'
    review = {'artifactId': j['original']['artifact_id'], 'text': j['original']['extracted_text'],
              'status': 'uncertain' if j['original']['review_required'] else 'needs_confirmation',
              'createdAt': j['original']['created_at']}
    stored = j['client'].put(f"/v1/sessions/{j['sid']}/drafts/study", json={
        'payload':{'question':'', 'tutor_ocr_review':review, 'tutor_ocr_review_text':review['text']}})
    assert stored.status_code == 200, stored.text
    for key, value in [('artifactId', latest['artifact_id']), ('text', 'not the original text'), ('status', 'confirmed')]:
        changed = {**review, key:value}
        assert j['client'].put(f"/v1/sessions/{j['sid']}/drafts/study", json={
            'payload':{'tutor_ocr_review':changed,'tutor_ocr_review_text':review['text']}}).status_code == 200
        refused = j['client'].put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=body)
        assert refused.status_code == 409 and refused.json()['code'] == 'DEVICE_RECEIPT_DOCUMENT_REVIEW_NOT_SAVED'
    assert j['client'].put(f"/v1/sessions/{j['sid']}/drafts/study", json={
        'payload':{'question':'','tutor_ocr_review':review,'tutor_ocr_review_text':review['text']}}).status_code == 200
    finished = j['client'].put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json=body)
    assert finished.status_code == 200, finished.text
    assert finished.json()['tool_status'] == 'completed'
    assert len(j['gateway'].requests) == 1
    request = j['gateway'].requests[0]
    payload = json.loads(request.messages[-1]['content'])
    assert set(payload) == {'transcript','state'}
    assert payload['state']['has_frozen_document_page'] is True
    assert all(type(v) is bool for v in payload['state'].values())
    serialized = json.dumps([request.messages, request.context_manifest, request.evidence_refs], ensure_ascii=False)
    for private in [j['original']['extracted_text'], *j['reference']['binding'].values(), j['sid'], CHILD]:
        assert private not in serialized
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        j['client'] = restarted
        recovered = restarted.get(f"/v1/sessions/{j['sid']}/agent-turns/{turn['agent_turn_id']}/document-source")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()['revision_id'] == j['original']['revision_id']
        workspace = restarted.get(f"/v1/sessions/{j['sid']}")
        assert workspace.status_code == 200, workspace.text
        study = workspace.json()['drafts']['study']['payload']
        assert study['tutor_ocr_review'] == review
        with j['db'].connect() as c:
            path = c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?', (j['original']['artifact_id'],)).fetchone()[0]
        Path(path).write_bytes(b'HSC1' + b'broken-ciphertext' * 4)
        assert j['client'].get(f"/v1/sessions/{j['sid']}/agent-turns/{turn['agent_turn_id']}/document-source").status_code == 409
        assert len(j['gateway'].requests) == 1


def test_quote_from_frozen_ocr_is_not_exported_for_control_classification(page_context):
    j = page_context
    quote = '这页上的 ' + j['original']['extracted_text'] + ' 该怎么理解？'
    j['gateway'].configured = True
    j['gateway'].output = {'role':'control','control':'document.review','quote':quote,'confidence':.95}
    assert begin(j).status_code == 200
    voice = complete(j, text=quote)
    reply = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json={
        'transcript':quote,'voice_input_id':voice['voice_input_id'],
        'client_action_id':'ocr-quote-stays-local','cloud_text_allowed':True})
    assert reply.status_code == 200, reply.text
    assert not j['gateway'].requests
    assert reply.json()['action']['voice_input_binding']['question_reference'] == j['reference']
    with j['db'].connect() as c:
        assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (voice['voice_input_id'],)).fetchone()[0] == quote
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_foreign_child_cannot_bind_the_other_original_at_onset(page_context):
    j = page_context
    with j['db'].connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (j['sid'],)); c.commit()
    sid = start(j['client'], child=OTHER, action='start-other-page-owner')
    response = begin(j, sid=sid)
    assert response.status_code == 409, response.text
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM voice_capture_binding').fetchone()[0] == 0


@pytest.mark.parametrize('bad', [None, '', 'x' * 64, 'A' * 64, '0' * 63, 1])
def test_document_content_hash_is_exact_not_an_arbitrary_version(bad):
    source = {'kind':'document', 'binding':{'revision_id':'r', 'artifact_id':'a',
        'content_sha256':bad, 'page_sha256':'0' * 64}}
    with pytest.raises(VoiceCaptureBindingError, match='VOICE_CAPTURE_QUESTION_INVALID'):
        normalize_question_reference(source)
