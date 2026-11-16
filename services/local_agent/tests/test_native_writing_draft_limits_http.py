"""Existing study draft storage, real loopback HTTP and lifespan restart."""
from dataclasses import replace
from test_native_writing_shell_http import scheduled, create_app, _http_client, KEY, headers, draft


def test_long_legal_draft_survives_restart_and_oversized_save_does_not_replace_it(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        settings = replace(case['settings'], native_launch_attestation_key=KEY)
        scope = {k: v for k, v in draft(case).items() if k not in ('text', 'client_action_id')}
        payload = {'native_writing_draft': {'input_kind': 'typed', 'scope': scope, 'text': 'x' * 4001}}
        path = f'/v1/sessions/{case["sid"]}'
        with _http_client(create_app(settings, model_gateway=case['gateway_factory']())) as client:
            saved = client.put(path + '/drafts/study', json={'payload': payload})
            assert saved.status_code == 200, saved.text
            assert saved.json()['payload'] == payload
            oversized = {'native_writing_draft': {**payload['native_writing_draft'], 'text': '稿' * 4001}}
            rejected = client.put(path + '/drafts/study', json={'payload': oversized})
            assert rejected.status_code == 422, rejected.text
            assert '草稿内容过长' in rejected.text
            body = {**draft(case), 'text': 'x' * 4001}
            assert client.post(path + '/native-writing-original', json=body, headers=headers(body)).status_code == 409
            assert client.get(path).json()['drafts']['study']['payload'] == payload
            calls = len(case['transport'].calls)
        with _http_client(create_app(settings, model_gateway=case['gateway_factory']())) as client:
            assert client.get(path).json()['drafts']['study']['payload'] == payload
            assert len(case['transport'].calls) == calls
            with case['db'].connect() as c:
                assert not c.execute("SELECT 1 FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()
                assert not c.execute('PRAGMA foreign_key_check').fetchall()
