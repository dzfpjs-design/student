"""Exact original identity survives restore, not source-use authorization."""

from local_restore_operation_fixture import restore_local
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.diagnostic_history_identity import historical_identity
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from test_diagnostic_history_identity import saved
from test_diagnostic_prompt import flow, prepare, CHILD, OTHER
from test_diagnostic_visual_http import publish, LOCAL, urls
from test_visual_material_host import setup_case, REGION, headers
from test_pet_reviewed_source_actual_http import NoCloud


@pytest.fixture
def lifecycle_flow(flow):
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    db = flow[0]
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False,
        data_encryption_key=b'h' * 32)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 43212)) as client:
        yield flow, client.app.state.privacy_lifecycle


@pytest.fixture
def admitted_lifecycle_flow(flow, portable):
    # Only the local restore branch needs the existing synthetic native issuer.
    # Keep the already-passing portable legacy branch's original settings.
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from synthetic_native_restore_request import KEY
    db = flow[0]
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False,
        data_encryption_key=b'h' * 32, native_launch_attestation_key=None if portable else KEY)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 43212)) as client:
        yield flow, client.app.state.privacy_lifecycle, client


@pytest.mark.parametrize('portable', [False, True])
def test_real_backup_restore_export_and_learning_delete_preserve_other_child(admitted_lifecycle_flow, portable):
    flow, privacy, client = admitted_lifecycle_flow
    db, service, sid = flow
    original = prepare(service, sid)['prompt']
    original_row = saved(db, original)
    service.cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
    repo = StudyRepository(db)
    other_sid = repo.switch_active_session_child(sid, OTHER, 'switch-history-owner')['session_id']
    other = service.prepare(other_sid, track='school', preferred_item_id='diag-j3-chinese-reading-1',
        client_action_id='other-history')['prompt']
    other_row = saved(db, other)
    service.cancel(other_sid, prompt_id=other['prompt_id'], item_version=other['item_version'])
    for current, target in zip(('check_in','classroom_recall','homework_capture','plan_review','study'),
                               ('classroom_recall','homework_capture','plan_review','study','reflection')):
        if current in {'classroom_recall', 'homework_capture'}:
            repo.declare_empty_stage(other_sid, 'no_recall' if current == 'classroom_recall' else 'no_homework',
                'history-empty-'+current)
        repo.transition(other_sid, current, target, 'isolated-history-finish', 'history-finish-'+target)
    repo.complete_with_reflection(other_sid, '', '', '', '', True, 'history-skipped-reflection')
    exported = privacy.export_child(CHILD)
    contents = json.loads(Path(exported['path']).read_text())
    prompts = contents['diagnostic_prompts']
    assert len(prompts) == 1 and prompts[0]['identity_snapshot_json'] == original_row['identity_snapshot_json']
    if portable:
        backup = privacy.create_portable_backup('isolated-history-passphrase')
    else:
        backup = privacy.create_backup()
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET explanation='post-backup explanation' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
        c.commit()
    if portable:
        restored = privacy.restore_portable_backup(backup['path'], 'isolated-history-passphrase', '确认迁移恢复')
    else:
        from privacy_restore_operation import restore_operation
        from synthetic_native_restore_request import signed_restore_post
        parent = client.post('/v1/parent/setup', json={'pin': '7391'})
        assert parent.status_code == 200, parent.text
        headers = {'X-HSC-Parent': parent.json()['parent_token']}
        response = signed_restore_post(client, headers=headers, json={
            'backup_id': backup['backup_id'], 'confirmation': '确认恢复',
            'operation': restore_operation(client, headers=headers)})
        assert response.status_code == 200, response.text
        restored = response.json()
    assert restored['status'] == 'restored'
    DiagnosticPromptService(db)
    row = saved(db, original)
    assert row['identity_snapshot_json'] == original_row['identity_snapshot_json']
    with db.connect() as c:
        assert historical_identity(c, CHILD, row, 'different')
    deleted = privacy.delete_child_scope(CHILD, 'learning', '确认删除', 'delete-history-child')
    assert deleted['status'] == 'completed', deleted
    DiagnosticPromptService(db)
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM diagnostic_prompt WHERE child_id=?', (CHILD,)).fetchone()
    assert saved(db, other)['identity_snapshot_json'] == other_row['identity_snapshot_json']


def test_visual_history_never_rereads_current_materials_or_grants_image_use(tmp_path):
    from home_study_agent.app import create_app
    from home_study_agent.learning_obligations import question_identity
    from home_study_agent.diagnostic_history_identity import _raw_item
    db, settings, prepared, args, region = setup_case(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 43210)) as client:
        state = publish(client, db, prepared, args, region)
        row = saved(db, state['prompt'])
        with db.connect() as c:
            expected = question_identity(_raw_item(c, row['diagnostic_item_id']), connection=c)
            frozen = json.loads(row['identity_snapshot_json'])['proof']
            assert frozen['visual']['candidate_id'] == state['candidate']
            assert frozen['visual']['materials'][0]['image_descriptor']['sha256'] == region['safe_png_sha256']
        retracted = client.post(REGION + '/retract', headers=headers(client), json={**args, 'client_action_id': 'history-retract'})
        assert retracted.status_code == 200, retracted.text
        base, query = urls(state)
        assert client.get(base, params=query, headers=LOCAL).status_code == 409
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 43211)) as client:
        assert client.get(base, params=query, headers=LOCAL).status_code == 409
        row = saved(db, state['prompt'])
        with db.connect() as c:
            # Material reads fail even if somebody accidentally adds a hidden
            # current-source lookup. Identity requires only the saved proof.
            def only_original_tables(op, table, column, database, trigger):
                return sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_READ and table not in {'diagnostic_item', 'knowledge_node'} else sqlite3.SQLITE_OK
            c.set_authorizer(only_original_tables)
            assert historical_identity(c, 'child', row, expected) == expected
            c.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        erased = client.post('/v1/parent/children/child/delete', headers=headers(client),
            json=dict(scope='knowledge', confirmation='确认删除', client_action_id='erase-visual-history'))
        assert erased.status_code == 200 and erased.json()['status'] == 'completed', erased.text
        with db.connect() as c:
            remaining = c.execute('SELECT identity_snapshot_json,item_json FROM diagnostic_prompt WHERE prompt_id=?',
                (state['prompt']['prompt_id'],)).fetchone()
            assert remaining is None or (remaining['identity_snapshot_json'] is None and remaining['item_json'] == '{}')


def test_legacy_visual_requires_original_full_region_seal(tmp_path):
    from home_study_agent.app import create_app
    from home_study_agent.diagnostic_history_identity import HistoryIdentityUnavailable
    from test_school_history_identity_http import as_legacy
    db, settings, prepared, args, region = setup_case(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 43213)) as client:
        state = publish(client, db, prepared, args, region)
        as_legacy(db, state['prompt'])
        row = saved(db, state['prompt'])
        with db.connect() as c:
            before = historical_identity(c, 'child', row, 'different')
            package = c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',
                (state['candidate'],)).fetchone()[0]
            proof = json.loads(package)
            material = proof['open_response_materials']['materials'][0]['visual']
            material['region']['safe_png_sha256'] = '0' * 64
            material['image_descriptor']['sha256'] = '0' * 64
            from home_study_agent.local_supply import digest
            proof['open_response_materials']['binding_version'] = digest(dict(
                version='open_response_visual_materials_v3', materials=proof['open_response_materials']['materials']))
            c.execute('UPDATE curriculum_supply_package SET source_snapshot_json=? WHERE candidate_id=?',
                (json.dumps(proof), state['candidate']))
            with pytest.raises(HistoryIdentityUnavailable):
                historical_identity(c, 'child', row, 'different')
            c.rollback()
            assert historical_identity(c, 'child', row, 'different') == before


def test_pre_column_backup_restores_before_live_same_service_prepare(lifecycle_flow):
    flow, privacy = lifecycle_flow
    db, service, sid = flow
    prompt = prepare(service, sid)['prompt']
    service.cancel(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
    repo = StudyRepository(db)
    repo.transition(sid, 'study', 'reflection', 'isolated-stop', 'old-db-reflection')
    repo.complete_with_reflection(sid, '', '', '', '', True, 'old-db-skipped')
    with db.connect() as c:
        # Reconstitute the pre-column schema, preserving every original field,
        # FK/index and the real original full item_version. No evidence backfill.
        c.execute('DROP TRIGGER diagnostic_identity_immutable')
        c.execute('DROP TRIGGER diagnostic_identity_source_erased')
        c.execute('ALTER TABLE diagnostic_prompt DROP COLUMN identity_snapshot_json')
        c.commit()
    backup = privacy.create_backup()
    DiagnosticPromptService(db)  # Running version is upgraded before old restore.
    restore_local(privacy,backup['backup_id'], '确认恢复')
    # Crucially do NOT instantiate a new service after restore.
    new_sid = repo.start_session(CHILD, 'after-legacy-restore')['session_id']
    result = service.prepare(new_sid, track='school', preferred_item_id='diag-p4-arithmetic-closed-2',
        client_action_id='first-post-legacy-restore')
    assert result['status'] == 'waiting_answer'
    assert saved(db, result['prompt'])['identity_snapshot_json']
    assert saved(db, prompt)['identity_snapshot_json'] is None
