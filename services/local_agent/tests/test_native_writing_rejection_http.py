from dataclasses import replace
import hashlib
import json

from test_native_writing_receipt_http import _process_client
from test_native_writing_shell_http import scheduled, create_app, _http_client, headers, draft, KEY


def query(body, answer_action_id=''):
    return {**{k: body[k] for k in ('child_id', 'prompt_id', 'item_version', 'diagnostic_item_id')},
            'text_sha256': hashlib.sha256(body['text'].encode()).hexdigest(),
            'capture_action_id': body['client_action_id'], 'answer_action_id': answer_action_id}


def test_signed_capture_definitive_rejection_survives_restart_and_fences_old_key(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body = draft(case)
        bad = {**body, 'item_version': 'obsolete-version'}
        base = f'/v1/sessions/{case["sid"]}'
        with _process_client(case) as (client, first_pid):
            denied = client.post(base+'/native-writing-original', json=bad, headers=headers(bad))
            assert denied.status_code == 409
            receipt = client.get(base+'/native-writing-receipt', params=query(bad))
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()['state'] == 'capture_rejected'
        with _process_client(case) as (client, second_pid):
            assert second_pid != first_pid
            with case['db'].connect() as c:
                before = list(c.iterdump())
            assert client.get(base+'/native-writing-receipt', params=query(bad)).json()['state'] == 'capture_rejected'
            with case['db'].connect() as c:
                assert list(c.iterdump()) == before
            # A late callback cannot revive the old identity, even with corrected input.
            assert client.post(base+'/native-writing-original', json=body, headers=headers(body)).status_code == 409
            fixed = {**body, 'text': body['text']+' Thank you.', 'client_action_id': 'explicit-corrected-capture'}
            captured = client.post(base+'/native-writing-original', json=fixed, headers=headers(fixed))
            assert captured.status_code == 200, captured.text
            with case['db'].connect() as c:
                assert c.execute("SELECT count(*) FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()[0] == 1
                assert c.execute("SELECT count(*) FROM audit_event WHERE action='native_writing_rejected'").fetchone()[0] == 1
        (tmp_path/'capture-rejection-process.json').write_text(json.dumps(dict(pids=[first_pid,second_pid],
            first_http=409,restored_state='capture_rejected',old_key_fenced=True,
            rejected_key=bad['client_action_id'],explicit_new_key=fixed['client_action_id'],
            original_artifact_id=captured.json()['artifact_id'],readonly_database_unchanged=True,
            owned_processes_remaining=0,provider_posts=0),indent=2))


def test_answer_definitive_rejection_then_explicit_corrected_new_key(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body = draft(case)
        base = f'/v1/sessions/{case["sid"]}'
        with _process_client(case) as (client, first_pid):
            original = client.post(base+'/native-writing-original', json=body, headers=headers(body)).json()
            answer = dict(prompt_id=body['prompt_id'], item_version=body['item_version'], answer=body['text'],
                          client_action_id='native-answer-rejected', writing_original_artifact_id=original['artifact_id'],
                          writing_original_revision='0'*64)
            denied = client.post(base+'/diagnostic-prompt/answer', json=answer)
            assert denied.status_code == 409, denied.text
            receipt = client.get(base+'/native-writing-receipt', params=query(body, answer['client_action_id']))
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()['state'] == 'answer_rejected'
        with _process_client(case) as (client, second_pid):
            assert client.get(base+'/native-writing-receipt', params=query(body, answer['client_action_id'])).json()['state'] == 'answer_rejected'
            fixed = {**answer, 'writing_original_revision': original['original_revision']}
            assert client.post(base+'/diagnostic-prompt/answer', json=fixed).status_code == 409
            fixed['client_action_id'] = 'explicit-corrected-answer'
            result = client.post(base+'/diagnostic-prompt/answer', json=fixed)
            assert result.status_code == 200, result.text
            assert result.json()['attempt']['submitted_answer'] == body['text']
        (tmp_path/'answer-rejection-process.json').write_text(json.dumps(dict(pids=[first_pid,second_pid],
            first_http=409,restored_state='answer_rejected',old_key_fenced=True,
            rejected_key=answer['client_action_id'],explicit_new_key=fixed['client_action_id'],
            original_artifact_id=original['artifact_id'],attempt_id=result.json()['attempt']['attempt_id'],
            owned_processes_remaining=0,provider_posts=0),indent=2))


def test_postcommit_capture_409_is_not_a_definitive_rejection(tmp_path, monkeypatch):
    import home_study_agent.native_writing as native
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body = draft(case)
        base = f'/v1/sessions/{case["sid"]}'
        original_receipt = native.original_receipt
        def lost_after_commit(*args):
            raise ValueError('synthetic postcommit receipt failure')
        with _http_client(create_app(replace(case['settings'], native_launch_attestation_key=KEY), model_gateway=case['gateway_factory']())) as client:
            monkeypatch.setattr(native, 'original_receipt', lost_after_commit)
            assert client.post(base+'/native-writing-original', json=body, headers=headers(body)).status_code == 409
            monkeypatch.setattr(native, 'original_receipt', original_receipt)
            assert client.get(base+'/native-writing-receipt', params=query(body)).json()['state'] == 'captured'
            with case['db'].connect() as c:
                assert c.execute("SELECT count(*) FROM audit_event WHERE action='native_writing_rejected'").fetchone()[0] == 0


def test_answer_saved_then_409_never_becomes_rejected_or_resumes_on_read(tmp_path, monkeypatch):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService, DiagnosticPromptError
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body=draft(case)
        base=f'/v1/sessions/{case["sid"]}'
        with _http_client(create_app(replace(case['settings'],native_launch_attestation_key=KEY),model_gateway=case['gateway_factory']())) as client:
            original=client.post(base+'/native-writing-original',json=body,headers=headers(body)).json()
            def fail_resume(*args,**kwargs):
                raise DiagnosticPromptError('synthetic_after_answer_saved')
            monkeypatch.setattr(DiagnosticPromptService,'resume',fail_resume)
            answer=dict(prompt_id=body['prompt_id'],item_version=body['item_version'],answer=body['text'],
                client_action_id='saved-then-conflict',writing_original_artifact_id=original['artifact_id'],
                writing_original_revision=original['original_revision'])
            assert client.post(base+'/diagnostic-prompt/answer',json=answer).status_code==409
            with case['db'].connect() as c:before=list(c.iterdump())
            assert client.get(base+'/native-writing-receipt',params=query(body,answer['client_action_id'])).json()['state']=='answer_saved'
            with case['db'].connect() as c:
                assert list(c.iterdump())==before
                assert c.execute("SELECT count(*) FROM audit_event WHERE action='native_writing_rejected'").fetchone()[0]==0


def test_unattested_rejection_cannot_install_a_native_operation_fence(tmp_path):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body=draft(case)
        base=f'/v1/sessions/{case["sid"]}'
        with _http_client(create_app(replace(case['settings'],native_launch_attestation_key=KEY),model_gateway=case['gateway_factory']())) as client:
            assert client.post(base+'/native-writing-original',json=body).status_code==409
            assert client.get(base+'/native-writing-receipt',params=query(body)).json()['state']=='capture_unknown'
            with case['db'].connect() as c:
                assert c.execute("SELECT count(*) FROM audit_event WHERE action='native_writing_rejected'").fetchone()[0]==0
            assert client.post(base+'/native-writing-original',json=body,headers=headers(body)).status_code==200
