"""Actual reviewed material publication to separately scoped parent permission."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.writing_question_authorization import (
    MATERIAL_VERSION, WritingQuestionAuthorizationService, authorized_question_projection,
)
from test_material_question_licenses import LICENSE
from test_open_response_materials import material_case, publish_open, CHILD


def published_material(tmp_path, material_license='public'):
    db, supply, spec, source, binding, text = material_case(tmp_path,
        question_license='public', material_license=material_license)
    publication = publish_open(supply, spec)
    privacy = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b'M' * 32)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    service = WritingQuestionAuthorizationService(db)
    service.initialize()
    return db, service, publication['diagnostic_item_id'], binding, text, privacy


def grant_arguments(service, item):
    view = service.read(CHILD, diagnostic_item_id=item)
    args = {key: view[key] for key in ('diagnostic_item_id', 'item_version', 'source_version', 'curriculum_version')}
    args.update(license_basis=LICENSE, expected_revision=view['revision'], client_action_id='material-grant', actor_id='parent_ui')
    if view['preview'] and view['preview'].get('materials'):
        args['material_license_bases'] = [
            {**{key: material[key] for key in ('material_key', 'source_version', 'text_hash')}, 'license_basis': LICENSE}
            for material in view['preview']['materials']]
    return view, args


def test_material_grant_exact_preview_restart_revoke_and_old_request(tmp_path):
    db, service, item, binding, text, _ = published_material(tmp_path)
    view, args = grant_arguments(service, item)
    assert view['status'] == 'authorization_required' and view['purpose_version'] == MATERIAL_VERSION
    assert text in view['preview']['question']
    question_only = {key: value for key, value in args.items() if key != 'material_license_bases'}
    with pytest.raises(ValueError, match='MATERIAL_LICENSES_REQUIRED'):
        service.grant(CHILD, **question_only)
    receipt = service.grant(CHILD, **args)
    assert receipt['current']['status'] == 'authorized'
    reopened = WritingQuestionAuthorizationService(Database(db.path))
    assert reopened.read(CHILD, diagnostic_item_id=item)['status'] == 'authorized'
    with db.connect() as c:
        projection = authorized_question_projection(c, child_id=CHILD, diagnostic_item_id=item)
        assert text in projection['question'] and projection['purpose_version'] == MATERIAL_VERSION
        assert not projection['cloud_transmission_allowed']
        stored = c.execute('SELECT binding_json,license_json FROM writing_question_authorization_event').fetchone()
        assert json.loads(stored['binding_json'])['material_bindings'] == [binding]
        assert len(json.loads(stored['license_json'])['materials']) == 1
    reopened.revoke(CHILD, diagnostic_item_id=item, expected_revision=1, client_action_id='material-revoke', actor_id='parent_ui')
    replay = reopened.grant(CHILD, **args)
    assert replay['receipt'] == receipt['receipt'] and replay['current']['status'] == 'revoked'


def test_public_question_cannot_authorize_private_material(tmp_path):
    db, service, item, _, _, _ = published_material(tmp_path, 'family_owned_index')
    view = service.read(CHILD, diagnostic_item_id=item)
    assert view['status'] == 'blocked' and view['preview'] is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_question_authorization_event').fetchone()[0] == 0


def test_unknown_material_rights_block_before_publication(tmp_path):
    _, supply, spec, _, _, _ = material_case(tmp_path, question_license='public', material_license='unknown')
    draft = supply.generate_draft(CHILD, spec, author='author', action_id='unknown-material')
    checked = supply.validate(CHILD, draft['candidate_id'], actor='validator',
        action_id='unknown-material-check', expected_version=1)
    assert checked['state'] == 'blocked' and 'OPEN_RESPONSE_MATERIAL_SOURCE_NOT_REVIEWED' in checked['blockers']


@pytest.mark.parametrize('fault', ['source', 'consent', 'restore'])
def test_material_permission_cannot_reuse_stale_source_or_permission(tmp_path, fault):
    db, service, item, binding, _, privacy = published_material(tmp_path)
    _, args = grant_arguments(service, item)
    service.grant(CHILD, **args)
    if fault == 'source':
        with db.connect() as c:
            c.execute("UPDATE knowledge_source SET title='changed material title' WHERE source_id=?", (binding['source_id'],))
            c.commit()
    elif fault == 'consent':
        privacy.update_preferences({'cloud_model_text_allowed': False})
        privacy.update_preferences({'cloud_model_text_allowed': True})
    else:
        backup = privacy.create_backup(prune=False)
        _restore_local_through_original_http(tmp_path, db, backup['backup_id'])
        with db.connect() as c:
            assert {row[0] for row in c.execute('SELECT purpose_version FROM writing_question_authorization_event')} == {MATERIAL_VERSION}
    with Database(db.path).connect() as c:
        with pytest.raises(ValueError, match='WRITING_QUESTION_'):
            authorized_question_projection(c, child_id=CHILD, diagnostic_item_id=item)


def _restore_local_through_original_http(tmp_path, db, backup_id):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_knowledge_source_revision_http import parent
    from test_pet_reviewed_source_actual_http import NoCloud
    from privacy_restore_operation import restore_operation
    from synthetic_native_restore_request import KEY, signed_restore_post

    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        data_encryption_key=b'M' * 32, native_launch_attestation_key=KEY)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 53003)) as client:
        headers = parent(client, True)
        restored = signed_restore_post(client, headers=headers, json={
            'backup_id': backup_id, 'confirmation': '确认恢复',
            'operation': restore_operation(client, headers=headers)})
        assert restored.status_code == 200, restored.text
