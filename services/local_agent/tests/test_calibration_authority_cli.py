import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.rubric_calibration_authority_cli import main
from test_calibration_evaluator_host import signer, CHILD
from test_calibration_authority_lifecycle_http import release, local_app, authenticate


def test_operator_check_import_and_lost_receipt_retry_use_real_parent_http(tmp_path, monkeypatch, capsys):
    sign, now = signer(tmp_path, monkeypatch)
    signed = sign('scorer_revision', release(CHILD, now))
    path = tmp_path / 'issued-proof.json'
    path.write_text(json.dumps({k: signed[k] for k in ('envelope', 'signature')}))
    assert main(['check', '--file', str(path)]) == 0
    with TestClient(local_app(tmp_path), client=('127.0.0.1', 41000)) as client:
        authenticate(client, setup=True)
        for _ in range(2):
            assert main(['import', '--file', str(path)], client=client, prompt_pin=lambda _: '8371') == 0
        from home_study_agent.database import Database
        with Database(tmp_path / 'isolated.db').connect() as c:
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='rubric_authority_import_v1'").fetchone()[0] == 1
    output = capsys.readouterr().out
    assert '8371' not in output and 'parent_token' not in output and 'public_key_pem' not in output


@pytest.mark.parametrize('base', ['https://example.invalid', 'http://127.0.0.1@remote.invalid', 'http://127.0.0.1?token=x'])
def test_operator_never_sends_receipt_or_pin_to_nonlocal_endpoint(base):
    def no_pin(_):
        pytest.fail('must reject the endpoint before requesting credentials')
    assert main(['import', '--file', '/nonexistent', '--base-url', base], prompt_pin=no_pin) == 1
