"""Actual local HTTP source deletion and restore, with synthetic family data only."""
import json
from pathlib import Path
import sqlite3

import pytest
from privacy_restore_operation import restore_operation
from synthetic_native_restore_request import KEY, signed_restore_post
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.learning_obligations import project_child_mastery_evidence, withdrawn_evidence_ids
from test_companion_activity_host import NoCloud
from test_native_shared_source_erasure import family, owner_snapshot, CHILD, OTHER, ITEM, NODE


@pytest.fixture
def host_family(family):
    db = family[0]
    # Prove real startup performs schema setup; no withdrawal facts exist yet.
    with db.connect() as c:
        for table in ('privacy_source_evidence_withdrawal', 'privacy_source_erasure'):
            assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
            c.execute(f'DROP TABLE {table}')
        c.commit()
    return family


def settings_for(db, encrypted=False, *, native_launch_attestation_key=None):
    return Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False,
                    session_token='shared-source-http', data_encryption_key=b's' * 32 if encrypted else None,
                    native_launch_attestation_key=native_launch_attestation_key)


def parent_headers(client, settings, restart=False):
    local = {'X-HSC-Session':settings.session_token}
    response = client.post('/v1/parent/'+('unlock' if restart else 'setup'), headers=local, json={'pin':'2468'})
    assert response.status_code == 200, response.text
    return {**local,'X-HSC-Parent':response.json()['parent_token']}


def delete(client, headers, *, scope='knowledge', key='shared-http-delete'):
    return client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers,
        json={'scope':scope,'confirmation':'确认删除','client_action_id':key})


def completed(response):
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'completed', response.text
    return response.json()


def export(client, headers, db, child=CHILD):
    response = client.post(f'/v1/parent/children/{child}/export', headers=headers)
    assert response.status_code == 200, response.text
    path = Path(response.json()['path']).resolve()
    assert db.path.parent.resolve() in path.parents
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['scope'] == 'local_only' and not payload['cloud_transmission_allowed']
    return payload


def test_actual_http_shared_delete_get_original_restart_reimport_export(host_family):
    db, ids, theirs, ref, private, own, other, prompt, other_prompt = host_family
    settings = settings_for(db, encrypted=True)
    deletion_id = None
    for restart in (False, True):
        with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
            headers = parent_headers(client, settings, restart)
            if not restart:
                forbidden = delete(client, {'X-HSC-Session':settings.session_token})
                assert forbidden.status_code == 401
            before_read = client.get(f"/v1/sessions/{other_prompt['origin_session_id']}/diagnostic-prompt",
                headers=headers, params={'prompt_id':other_prompt['prompt_id']})
            assert before_read.status_code == 200 and before_read.json()['prompt'], before_read.text
            with db.connect() as c:
                before = owner_snapshot(c, OTHER)
                projected = project_child_mastery_evidence(c, OTHER)
                original = tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone())
            result = completed(delete(client, headers))
            if deletion_id:
                assert result['deletion_id'] == deletion_id
            deletion_id = result['deletion_id']
            after_read = client.get(f"/v1/sessions/{other_prompt['origin_session_id']}/diagnostic-prompt",
                headers=headers, params={'prompt_id':other_prompt['prompt_id']})
            assert after_read.status_code == 200 and after_read.json() == before_read.json(), after_read.text
            old = client.get(f"/v1/sessions/{prompt['origin_session_id']}/diagnostic-prompt", headers=headers,
                params={'prompt_id':prompt['prompt_id']})
            assert old.status_code == 200, old.text
            assert (old.json()['status'], old.json()['reason']) == ('invalidated','source_deleted')
            assert old.json()['prompt']['prompt_id'] == prompt['prompt_id']
            assert original[2] not in old.text
            assert not old.json()['prompt']['item'].get('prompt')
            with db.connect() as c:
                assert owner_snapshot(c, OTHER) == before
                assert project_child_mastery_evidence(c, OTHER) == projected
                assert tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone()) == original
                assert c.execute('SELECT status,item_json FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[:] == ('invalidated','{}')
                assert own['evidence_id'] in withdrawn_evidence_ids(c, CHILD)
                assert other['evidence_id'] not in withdrawn_evidence_ids(c, OTHER)
                assert c.execute('SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?', (private['diagnostic_item_id'],)).fetchone()[0] == 'retired'
                assert c.execute('PRAGMA foreign_key_check').fetchall() == []
            payload = export(client, headers, db)
            assert {r['source_id'] for r in payload['source_erasures']} == ids
            assert own['evidence_id'] in {r['evidence_id'] for r in payload['source_evidence_withdrawals']}
            assert {r['child_id'] for r in payload['source_evidence_withdrawals']} == {CHILD}
            if restart:
                imported = client.post(f'/v1/parent/children/{CHILD}/knowledge-sources', headers=headers, json={
                    'source_type':'school_material','title':'Explicit isolated reimport','subject':'数学',
                    'grade_band':'四年级','source_ref':ref,'license_status':'family_owned_index',
                    'parent_reviewed':True,'content_text':'Shared catalog body, child one copy'})
                assert imported.status_code == 200 and imported.json()['source_id'] not in ids, imported.text
                completed(delete(client, headers))  # Lost-response retry cannot delete the new import.
                with db.connect() as c:
                    assert c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?', (imported.json()['source_id'],)).fetchone()
                    assert own['evidence_id'] in withdrawn_evidence_ids(c, CHILD)


@pytest.mark.parametrize('callback', ['replay_source_mastery','reconcile_source_deletion'])
def test_actual_http_shared_source_rollback_restart_exact_retry(host_family, monkeypatch, callback):
    db, ids, _, _, private, own, *_ = host_family
    settings = settings_for(db, encrypted=True)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        headers = parent_headers(client, settings)
        with db.connect() as c:
            before = {table:[tuple(r) for r in c.execute(f'SELECT * FROM {table}')]
                for table in ('knowledge_source','knowledge_excerpt','diagnostic_item','diagnostic_prompt',
                              'diagnostic_attempt','learning_evidence','mastery_state',
                              'privacy_source_erasure','privacy_source_evidence_withdrawal')}
        def fail(*args, **kwargs):
            assert kwargs['connection'].in_transaction
            raise sqlite3.OperationalError('isolated final replay failure')
        with monkeypatch.context() as patch:
            patch.setattr(client.app.state.privacy_lifecycle, callback, fail)
            failed = delete(client, headers)
        assert failed.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE', failed.text
        assert failed.json()['retryable']
        with db.connect() as c:
            for table, rows in before.items():
                assert [tuple(r) for r in c.execute(f'SELECT * FROM {table}')] == rows
            job = c.execute("SELECT deletion_id,status FROM deletion_job WHERE client_action_id='shared-http-delete'").fetchone()
            assert job['status'] == 'failed'
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        result = completed(delete(client, parent_headers(client, settings, True)))
        assert result['deletion_id'] == job['deletion_id']
        with db.connect() as c:
            assert own['evidence_id'] in withdrawn_evidence_ids(c, CHILD)


@pytest.mark.parametrize('portable', [False, True])
def test_actual_http_tombstone_export_backup_restore_then_learning_delete(host_family, portable):
    db, ids, _, _, _, own, _, _, other_prompt = host_family
    settings = settings_for(db, encrypted=True, native_launch_attestation_key=None if portable else KEY)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        headers = parent_headers(client, settings)
        completed(delete(client, headers))
        sid = other_prompt['origin_session_id']
        transition = client.post(f'/v1/sessions/{sid}/transition', headers=headers, json={
            'expected_state':'study','target_state':'reflection','reason':'Finish isolated backup session',
            'client_action_id':'shared-backup-reflection'})
        assert transition.status_code == 200, transition.text
        finish = client.post(f'/v1/sessions/{sid}/reflection/complete', headers=headers,
            json={'skipped':True,'client_action_id':'shared-backup-finish'})
        assert finish.status_code == 200, finish.text
        saved = export(client, headers, db)
        assert own['evidence_id'] in {r['evidence_id'] for r in saved['source_evidence_withdrawals']}
        suffix = '/portable' if portable else ''
        backup = client.post('/v1/parent/backups'+suffix, headers=headers,
            json={'passphrase':'isolated shared source backup passphrase'} if portable else None)
        assert backup.status_code == 200, backup.text
        later = client.post(f'/v1/parent/children/{CHILD}/knowledge-sources', headers=headers, json={
            'source_type':'school_material','title':'Explicit post-backup import','subject':'数学',
            'grade_band':'四年级','source_ref':'isolated:after-backup','license_status':'family_owned_index',
            'parent_reviewed':True,'content_text':'Only exists after the selected backup'})
        assert later.status_code == 200, later.text
        values = ({'path':backup.json()['path'],'passphrase':'isolated shared source backup passphrase'}
                  if portable else {'backup_id':backup.json()['backup_id']})
        restore_body = {**values,'confirmation':'确认迁移恢复' if portable else '确认恢复',
                        'operation': restore_operation(client, headers=headers)}
        if portable:
            restored = client.post('/v1/parent/backups'+suffix+'/restore', headers=headers, json=restore_body)
        else:
            restored = signed_restore_post(client, headers=headers, json=restore_body)
        assert restored.status_code == 200 and restored.json()['status'] == 'restored', restored.text
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?', (later.json()['source_id'],)).fetchone()
        headers = parent_headers(client, settings, True)
        recovered = export(client, headers, db)
        for field in ('source_erasures','source_evidence_withdrawals'):
            assert recovered[field] == saved[field]
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        headers = parent_headers(client, settings, True)
        with db.connect() as c:
            assert own['evidence_id'] in withdrawn_evidence_ids(c, CHILD)
            other = owner_snapshot(c, OTHER)
        completed(delete(client, headers, scope='learning', key='shared-learning-final'))
        payload = export(client, headers, db)
        assert payload['source_erasures'] == payload['source_evidence_withdrawals'] == []
        with db.connect() as c:
            assert owner_snapshot(c, OTHER) == other
            assert c.execute('PRAGMA foreign_key_check').fetchall() == []
