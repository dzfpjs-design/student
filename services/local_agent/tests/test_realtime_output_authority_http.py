from dataclasses import replace
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_realtime_output_authority import case, proof, KEY, DOMAIN


def settings_for(db):
    return Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False,
                    session_token='synthetic-output-session-token', native_launch_attestation_key=KEY)


def post(client, scope, operation='reserve', response_id='', headers=True, **patch):
    return client.post(f"/v1/sessions/{scope['session_id']}/voice-output-authority",
                       headers={'X-HSC-Session': 'synthetic-output-session-token'} if headers else {},
                       json=proof(scope, operation, response_id) | patch)


def test_actual_application_route_requires_native_proof_and_durable_source(case):
    db, scope, _ = case
    settings = settings_for(db)
    with TestClient(create_app(settings)) as client:
        assert post(client, scope, headers=False).status_code == 401
        rejected = post(client, scope, signature='forged')
        assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'REALTIME_OUTPUT_PROOF_INVALID'
        rejected = post(client, scope, response_text='任意调用者添加的文字')
        assert rejected.status_code == 409
        rejected = client.post(f"/v1/sessions/{scope['session_id']}/voice-output-authority",
            headers={'X-HSC-Session': settings.session_token}, content=iter([b'x' * 3000, b'x' * 3000]))
        assert rejected.status_code == 409
        reserved = post(client, scope)
        assert reserved.status_code == 200, reserved.text
        assert reserved.headers['cache-control'] == 'no-store'
        assert reserved.json()['response_text'] == '我们接着看下一步。'
        bound = post(client, scope, 'bind', 'resp_synthetic_output')
        assert bound.status_code == 200 and bound.json()['dispatch_allowed'] is True
    with TestClient(create_app(settings)) as client:
        assert post(client, scope).status_code == 409
        result = post(client, scope, 'inspect')
        assert result.status_code == 200 and result.json()['dispatch_allowed'] is False
        assert 'response_text' not in result.json()
        with db.connect() as c:
            assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 2


def test_native_authority_rotation_closes_old_output_after_application_restart(case):
    db, scope, _ = case
    settings = settings_for(db)
    with TestClient(create_app(settings)) as client:
        assert post(client, scope).status_code == 200
    rotated = replace(settings, native_launch_attestation_key='cd' * 32)
    with TestClient(create_app(rotated)) as client:
        assert post(client, scope, 'inspect').status_code == 409
        from test_realtime_output_authority import proof
        result = client.post(f"/v1/sessions/{scope['session_id']}/voice-output-authority",
            headers={'X-HSC-Session': settings.session_token}, json=proof(scope, 'inspect', key=rotated.native_launch_attestation_key))
        assert result.status_code == 200 and result.json()['state'] == 'revoked'
        assert 'response_text' not in result.json()
