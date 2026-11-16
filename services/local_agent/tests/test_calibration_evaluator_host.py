"""Actual HTTP evaluator transport with synthetic offline authority.

These fixtures demonstrate binding, never representative model calibration.
Question publication and original answer use production services; the objective
setup is structured, not evidence of a child's voice or independent behaviour.
"""
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_evidence import digest, encoded
from test_writing_question_authorization import published_question, grant, CHILD
from test_writing_question_authorization_http import host, parent
from test_writing_question_evaluator import projection
from test_writing_question_dispatch_revocation_http import model_response

ROOT = f'/v1/parent/children/{CHILD}/rubric-calibration'


def signer(tmp_path, monkeypatch, *, children=None):
    private = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    trust = {'version': 'rubric_authority_trust_v1', 'issuers': [{
        'key_id': 'isolated-signer', 'public_key_pem': private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        'kinds': ['scorer_revision', 'holdout', 'blind_label'], 'child_ids': children or [CHILD],
        'expires_at': (now + timedelta(days=2)).isoformat()}]}
    path = tmp_path / 'isolated-trust.json'
    path.write_text(json.dumps(trust))
    monkeypatch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE', str(path))

    def sign(kind, payload):
        envelope = {'version': 'rubric_authority_envelope_v1', 'key_id': 'isolated-signer',
            'kind': kind, 'child_id': payload['child_id'], 'receipt_id': payload['receipt_id'],
            'issued_at': now.isoformat(), 'expires_at': (now + timedelta(days=1)).isoformat(),
            'payload': payload}
        return {'envelope': envelope,
            'signature': base64.b64encode(private.sign(encoded(envelope).encode())).decode(),
            'client_action_id': 'import:' + payload['receipt_id']}
    return sign, now


def freeze_one_observed_case(client, headers, spec, sign, now):
    scorer = {**spec['scorer'], 'model_revision': 'synthetic-immutable-v1',
              'revision_receipt_id': 'release'}
    revision = {'child_id': CHILD, 'receipt_id': 'release', 'status': 'current',
        'provider': scorer['provider'], 'model': scorer['model'],
        'model_revision': scorer['model_revision'], 'verification_method': 'signed_release_manifest',
        'evidence_hash': 'a' * 64, 'endpoint_scope_sha256': spec['endpoint_scope_sha256'],
        'verified_at': now.isoformat(), 'expires_at': (now + timedelta(days=1)).isoformat()}
    source = spec['source']
    scope = {'subject': source['subject'], 'grade': source['grade'],
             'task_family': scorer['profile_id'], 'strata': ['ordinary']}
    holdout = {'child_id': CHILD, 'receipt_id': 'holdout', 'status': 'current',
        'corpus_kind': 'observed_original', 'blind_protocol_version': 'blind_labels_v1',
        'provenance_receipt_hash': 'b' * 64, 'scope_hash': digest(scope), 'frozen_at': now.isoformat(),
        'cases': [{'evidence_id': source['evidence_id'], 'group_hash': 'c' * 64,
                   'stratum': 'ordinary', 'source_ids': []}],
        'application_scope': [{'profile_id': scorer['profile_id'],
                               'answer_types': ['writing'], 'stratum': 'ordinary'}]}
    for kind, payload in [('scorer_revision', revision), ('holdout', holdout)]:
        response = client.post(ROOT + '/authority/import', headers=headers, json=sign(kind, payload))
        assert response.status_code == 200, response.text
    response = client.post(ROOT + '/protocols/freeze', headers=headers, json={
        'scorer': scorer, 'scope': scope, 'holdout_receipt_id': 'holdout', 'client_action_id': 'freeze'})
    assert response.status_code == 200, response.text
    return response.json()['current']['protocol_id']


@pytest.mark.parametrize('contract',['current','stale_parser'])
def test_actual_original_execute_saves_exact_scorer_binding_without_fake_qualification(
        published_question, projection, tmp_path, monkeypatch,contract):
    case, _ = projection
    db, sid, prompts, prompt, _ = case
    grant(published_question)
    sign, now = signer(tmp_path, monkeypatch)
    settings = replace(host(published_question, monkeypatch), model_provider='qwen',
        model_name='isolated-model', model_api_key='synthetic-69c92b8a1f26c7ac5e4763bd',
        model_base_url='https://isolated.invalid/v1', fallback_model_api_key=None)
    posts = []

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        return model_response(body)

    app = create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TestClient(app, raise_server_exceptions=False, client=('127.0.0.1', 41000)) as client:
        headers = parent(client, setup=True)
        attempt = prompts.read(sid, prompt_id=prompt['prompt_id'])['attempt']
        follow = app.state.agent_runtime.finalize_diagnostic_evaluation(sid, attempt['evidence_id'])
        tid = follow['feedback_turn_id']
        spec = client.get(ROOT + '/evaluations/' + tid + '/request-spec', headers=headers)
        assert spec.status_code == 200, spec.text
        descriptor=spec.json()
        if contract=='stale_parser':
            from home_study_agent.learning_outcome_evaluator import PROMPT_VERSION
            descriptor={**descriptor,'scorer':{**descriptor['scorer'],'parser_version':PROMPT_VERSION}}
        pid = freeze_one_observed_case(client, headers, descriptor, sign, now)
        response = client.post('/v1/agent-turns/' + tid + '/execute')
        assert response.status_code == 200, response.text
        assert len(posts) == 1
        assert set(json.loads(posts[0]['messages'][1]['content'])) == {'question', 'response', 'rubric'}
        assert all(value not in json.dumps(posts) for value in (CHILD, pid, 'signed_authority', 'calibration_execution'))
        with db.connect() as c:
            assessment = c.execute('SELECT * FROM rubric_assessment').fetchone()
            assert assessment
            payload = json.loads(assessment['payload_json'])
            if contract=='current':
                assert payload['calibration_execution']['mode'] == 'holdout'
                assert payload['calibration_execution']['protocol_id'] == pid
            else:
                assert payload['calibration_execution']['status']=='unknown'
                assert payload['calibration_binding'] is None
                assert payload['automatic_quality_status']=='not_calibrated'
            service = RubricCalibrationService(db)
            row = service._owned(c, CHILD, pid)
            if contract=='current':
                service._scored(c, row, assessment['assessment_id'], service._now())
            assert not service._read(c, row, service._now())['score_qualified']
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        replay = client.post('/v1/agent-turns/' + tid + '/execute')
        assert replay.status_code == 200, replay.text
        assert len(posts) == 1


@pytest.mark.parametrize('change', ['revision_revoke', 'normal_retry', 'fallback_endpoint'])
def test_bound_scorer_revalidates_every_physical_post(
        published_question, projection, tmp_path, monkeypatch, change):
    case, _ = projection
    db, sid, prompts, prompt, _ = case
    grant(published_question)
    sign, now = signer(tmp_path, monkeypatch)
    settings = replace(host(published_question, monkeypatch), model_provider='qwen',
        model_name='isolated-model', model_api_key='synthetic-69c92b8a1f26c7ac5e4763bd', model_base_url='https://isolated.invalid/v1',
        fallback_model_provider='deepseek', fallback_model_name='other-model',
        fallback_model_api_key='synthetic-67cbf16601936ffd6a5b', fallback_model_base_url='https://other.invalid/v1')
    posts = []
    app, headers = None, None

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        if len(posts) == 1:
            if change == 'revision_revoke':
                from home_study_agent.rubric_calibration_authority import resolve
                with db.connect() as c:
                    proof = resolve(c, child_id=CHILD, kind='scorer_revision', reference_id='release')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 41001)),
                                             base_url='http://testserver') as admin:
                    revoked = await admin.post(ROOT + '/authority/revoke', headers=headers, json={
                        'kind': 'scorer_revision', 'receipt_id': 'release',
                        'expected_hash': proof['signed_authority']['envelope_hash'], 'client_action_id': 'revoke-inflight'})
                assert revoked.status_code == 200, revoked.text
            return httpx.Response(403 if change == 'fallback_endpoint' else 500)
        return model_response(body)

    app = create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TestClient(app, raise_server_exceptions=False, client=('127.0.0.1', 41000)) as client:
        headers = parent(client, setup=True)
        attempt = prompts.read(sid, prompt_id=prompt['prompt_id'])['attempt']
        follow = app.state.agent_runtime.finalize_diagnostic_evaluation(sid, attempt['evidence_id'])
        tid = follow['feedback_turn_id']
        spec = client.get(ROOT + '/evaluations/' + tid + '/request-spec', headers=headers)
        assert spec.status_code == 200, spec.text
        freeze_one_observed_case(client, headers, spec.json(), sign, now)
        result = client.post('/v1/agent-turns/' + tid + '/execute')
        assert result.status_code == (200 if change == 'normal_retry' else 409), result.text
        assert len(posts) == (2 if change == 'normal_retry' else 1)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == int(change == 'normal_retry')


@pytest.mark.parametrize('revoke', [False, True], ids=['current', 'revoked'])
def test_response_ready_actual_restart_recovers_original_binding_without_reposting(
        published_question, projection, tmp_path, monkeypatch, revoke):
    case, _ = projection
    db, sid, prompts, prompt, _ = case
    grant(published_question)
    sign, now = signer(tmp_path, monkeypatch)
    settings = replace(host(published_question, monkeypatch), model_provider='qwen',
        model_name='isolated-model', model_api_key='synthetic-69c92b8a1f26c7ac5e4763bd',
        model_base_url='https://isolated.invalid/v1', fallback_model_api_key=None)
    posts = []

    async def transport(request):
        body = json.loads(request.content)
        posts.append(body)
        return model_response(body)

    def application():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))

    app = application()
    with TestClient(app, raise_server_exceptions=False, client=('127.0.0.1', 41000)) as client:
        headers = parent(client, setup=True)
        attempt = prompts.read(sid, prompt_id=prompt['prompt_id'])['attempt']
        follow = app.state.agent_runtime.finalize_diagnostic_evaluation(sid, attempt['evidence_id'])
        tid = follow['feedback_turn_id']
        spec = client.get(ROOT + '/evaluations/' + tid + '/request-spec', headers=headers)
        assert spec.status_code == 200, spec.text
        pid = freeze_one_observed_case(client, headers, spec.json(), sign, now)

        def storage_failure(*args, **kwargs):
            raise RuntimeError('isolated response-ready storage failure')
        monkeypatch.setattr(app.state.learning_outcome_evaluator, '_persist', storage_failure)
        result = client.post('/v1/agent-turns/' + tid + '/execute')
        assert result.status_code == 409, result.text
        with db.connect() as c:
            saved = c.execute("SELECT record_json FROM learning_evaluation_execution WHERE state='response_ready'").fetchone()
            assert saved
            frozen = json.loads(saved[0])['calibration']
            assert frozen['protocol_id'] == pid
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0
        if revoke:
            revoked = client.post(ROOT + '/' + pid + '/revoke', headers=headers,
                                  json={'expected_revision': 1, 'client_action_id': 'revoke-ready'})
            assert revoked.status_code == 200, revoked.text
    with TestClient(application(), raise_server_exceptions=False, client=('127.0.0.1', 41000)) as client:
        retry = client.post('/v1/agent-turns/' + tid + '/execute')
        assert retry.status_code == (409 if revoke else 200), retry.text
        assert len(posts) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == (0 if revoke else 1)
            row = c.execute('SELECT record_json FROM learning_evaluation_execution').fetchone()
            assert json.loads(row[0])['calibration'] == frozen
