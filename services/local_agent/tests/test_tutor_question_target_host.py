"""Real Tutor HTTP consumes exact question identity but grants no cloud rights."""
from dataclasses import asdict
import json

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.tutor_request_reservation import client_request_hash
from test_teaching_context import flow, NODE, SECOND
from test_teaching_question_target import target_case, request
from test_tutor_child_model_controls import RecordingGateway


def test_actual_tutor_question_binding_parent_restart_and_private_audit(target_case, tmp_path):
    source, _, prompt = target_case
    db, _, _, _, sid = source
    gateway = RecordingGateway()
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    payload = {**request(target_case), 'client_action_id': 'bound-question-tutor'}
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        response = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()
        audit = saved['output_envelope']['local_teaching_control']
        assert audit['question_target']['status'] == 'resolved'
        assert audit['question_target']['knowledge_node_id'] == NODE
        assert audit['controls']['explanation_pacing'] == 'short_step_then_restate'
        transmitted = json.dumps(asdict(gateway.requests[-1]), ensure_ascii=False)
        for private in (NODE, prompt['prompt_id'], prompt['item_version'], 'question_target', 'local_teaching_control'):
            assert private not in transmitted
        calls = len(gateway.requests)
        retry = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert retry.status_code == 200 and retry.json()['turn_id'] == saved['turn_id']
        assert len(gateway.requests) == calls
        conflict = client.post(f'/v1/sessions/{sid}/tutor', json={**payload, 'item_version': 'different-version'})
        assert conflict.status_code == 409
        assert len(gateway.requests) == calls
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        parent_payload = {'subject': payload['subject'], 'question': payload['question'], 'hint_level': 2,
                          'parent_turn_id': saved['turn_id'], 'client_action_id': 'bound-parent-hint'}
        response = client.post(f'/v1/sessions/{sid}/tutor', json=parent_payload)
        assert response.status_code == 200, response.text
        target = response.json()['output_envelope']['local_teaching_control']['question_target']
        assert target['binding']['prompt_id'] == prompt['prompt_id']
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET source_ref='local-restricted-source' WHERE diagnostic_item_id=?", (SECOND,))
            c.commit()
        calls = len(gateway.requests)
        stale = client.post(f'/v1/sessions/{sid}/tutor', json={**parent_payload, 'client_action_id': 'withdrawn-parent-hint'})
        assert stale.status_code == 409, stale.text
        assert stale.json()['detail']['code'] == 'TUTOR_QUESTION_REFERENCE_CHANGED'
        assert len(gateway.requests) == calls


def test_unknown_prompt_is_not_a_cloud_authorization(target_case, tmp_path):
    db, _, _, _, sid = target_case[0]
    gateway = RecordingGateway()
    with TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False), model_gateway=gateway)) as client:
        response = client.post(f'/v1/sessions/{sid}/tutor', json={**request(target_case), 'prompt_id': 'another-child-or-missing', 'client_action_id': 'invalid-binding-tutor'})
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'TUTOR_QUESTION_REFERENCE_CHANGED'
        assert gateway.requests == []


def test_current_local_only_prompt_does_not_reach_gateway(flow, tmp_path):
    db, _, _, _, sid = flow
    gateway = RecordingGateway()
    with TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False), model_gateway=gateway)) as client:
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET source_ref='local_graph:v1' WHERE diagnostic_item_id=?", (SECOND,))
            c.commit()
        prompt = DiagnosticPromptService(db).prepare(sid, preferred_item_id=SECOND, client_action_id='local-only-prepare')['prompt']
        payload = {'subject': '数学', 'question': prompt['item']['prompt'], 'prompt_id': prompt['prompt_id'],
                   'item_version': prompt['item_version'], 'client_action_id': 'local-only-tutor-request'}
        response = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'TUTOR_QUESTION_LOCAL_ONLY'
        assert gateway.requests == []
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 0


def test_question_reference_hash_keeps_unbound_legacy_identity_but_separates_bindings():
    import hashlib
    original = {'session_id': 's', 'subject': '数学', 'question': '计算 1+1', 'hint_level': 1, 'parent_turn_id': None, 'ocr_confirmation_id': None}
    previous = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    args = ('s', '数学', '计算 1+1', 1, None, None)
    assert client_request_hash(*args) == previous
    assert client_request_hash(*args, prompt_id='a', item_version='v1') != previous
    assert client_request_hash(*args, prompt_id='a', item_version='v1') != client_request_hash(*args, prompt_id='a', item_version='v2')
