"""Actual local publisher -> original prompt -> pixels; no scoring or cloud."""
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_pet_reviewed_source_actual_http import NoCloud
from test_visual_curriculum_supply_http import supply_case
from test_visual_material_host import setup_case, headers, REGION

LOCAL = {'X-HSC-Session': 'local-test'}
SUPPLY = '/api/parent/children/child/curriculum-supply'


def publish(client, db, prepared, args, region):
    parent = headers(client)
    client.app.state.visual_materials.stage_prepared('child', 'page', 'figure',
        revision_id=args['revision_id'], region_version=args['region_version'], prepared=prepared)
    r = client.post(REGION + '/storage', headers=parent, json={**args,
        'client_action_id': 'store', 'purpose': 'local_visual_material_review_v1'})
    assert r.status_code == 200, r.text
    image = client.get(REGION + '/image', headers=parent,
        params={**args, 'sha256': region['safe_png_sha256']})
    assert image.status_code == 200, image.text
    r = client.post(REGION + '/displayed', headers=parent, json={**args,
        'client_action_id': 'display', 'display_token': image.headers['X-HSC-Visual-Display-Token'],
        'sha256': region['safe_png_sha256'], 'width': prepared.width, 'height': prepared.height})
    assert r.status_code == 200, r.text
    r = client.post(REGION + '/confirm', headers=parent, json={**args,
        'client_action_id': 'confirm', 'display_receipt_id': r.json()['display_receipt_id']})
    assert r.status_code == 200, r.text
    binding = r.json()['detail']['binding']
    source, source_id, spec = supply_case(db, binding)
    r = client.post(SUPPLY + '/generate', headers=parent, json=dict(action_id='draft',
        expected_version=0, expected_scope_version=1, source=spec))
    assert r.status_code == 200, r.text
    candidate = r.json()['candidate_id']
    for version, op in enumerate(('validate', 'review', 'publish'), 1):
        body = dict(action_id=op, expected_version=version)
        if op == 'review':
            body.update(decision='approve', note='Reviewed synthetic original.')
        r = client.post(SUPPLY + '/' + candidate + '/' + op, headers=parent, json=body)
        assert r.status_code == 200, r.text
    item = r.json()['diagnostic_item_id']
    with db.connect() as c:
        c.execute("""INSERT INTO family_rule(child_id,max_learning_minutes,task_segment_minutes,
            break_minutes,pet_min_minutes,competition_minutes,version,updated_at)
            VALUES('child',60,20,5,0,0,1,'2026-09-10T00:00:00+00:00')""")
        c.commit()
    session = client.app.state.repository.start_session('child', 'diagnostic-visual-session')['session_id']
    r = client.post('/v1/sessions/' + session + '/diagnostic-prompt/prepare', headers=LOCAL,
        json=dict(track='school', preferred_item_id=item, client_action_id='original-prompt'))
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'waiting_answer', r.text
    prompt = r.json()['prompt']
    assert 'visual' not in prompt['item'] and 'image_descriptor' not in json.dumps(prompt)
    return dict(session=session, prompt=prompt, parent=parent, candidate=candidate,
        source_id=source_id, binding=binding)


def urls(state):
    return '/v1/sessions/' + state['session'] + '/diagnostic-visual', {
        'prompt_id': state['prompt']['prompt_id'], 'item_version': state['prompt']['item_version']}


def test_published_original_manifest_png_restart_withdraw_no_write(tmp_path):
    db, settings, prepared, args, region = setup_case(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42280)) as client:
        state = publish(client, db, prepared, args, region)
        base, query = urls(state)
        with db.connect() as c:
            before = list(c.iterdump())
        r = client.get(base, params=query, headers=LOCAL)
        assert r.status_code == 200, r.text
        value = r.json()
        assert set(value) == {'version', 'child_id', 'session_id', 'prompt_id', 'item_version',
            'materials', 'local_only', 'cloud_transmission_allowed', 'mastery_claim_allowed'}
        assert value['version'] == 'diagnostic_visual_v1'
        assert value['child_id'] == 'child' and value['session_id'] == state['session']
        assert value['local_only'] and not value['cloud_transmission_allowed'] and not value['mastery_claim_allowed']
        assert len(value['materials']) == 1
        material = value['materials'][0]
        assert set(material) == {'material_key', 'image_descriptor'}
        assert material['material_key'] == 'figure'
        assert material['image_descriptor']['sha256'] == hashlib.sha256(prepared.png).hexdigest()
        image_query = {**query, 'material_key': 'figure', 'sha256': region['safe_png_sha256']}
        image = client.get(base + '/image', params=image_query, headers=LOCAL)
        assert image.status_code == 200, image.text
        assert image.content == prepared.png
        assert image.headers['content-type'] == 'image/png'
        assert 'X-HSC-Visual-Display-Token' not in image.headers
        for response in (r, image):
            assert response.headers['cache-control'] == 'no-store'
        with db.connect() as c:
            assert list(c.iterdump()) == before
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42281)) as client:
        r = client.get(base, params=query, headers=LOCAL)
        assert r.status_code == 200 and r.json() == value, r.text
        assert client.get(base + '/image', params=image_query, headers=LOCAL).content == prepared.png
        r = client.post(REGION + '/retract', headers=headers(client), json={**args, 'client_action_id': 'retract'})
        assert r.status_code == 200, r.text
        for path, params in ((base, query), (base + '/image', image_query)):
            r = client.get(path, params=params, headers=LOCAL)
            assert r.status_code == 409, r.text
            assert r.headers['cache-control'] == 'no-store'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.fixture
def live(tmp_path):
    db, settings, prepared, args, region = setup_case(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42282)) as client:
        state = publish(client, db, prepared, args, region)
        yield client, db, settings, prepared, args, region, state


def test_auth_loopback_range_exact_identity_and_no_cache(live):
    client, db, settings, _, _, region, state = live
    base, query = urls(state)
    image_query = {**query, 'material_key': 'figure', 'sha256': region['safe_png_sha256']}
    for path, params in ((base, query), (base + '/image', image_query)):
        for h in ({}, {'X-HSC-Session': 'wrong'}, {'X-HSC-Parent': state['parent']['X-HSC-Parent']}):
            r = client.get(path, params=params, headers=h)
            assert r.status_code == 401 and r.headers['cache-control'] == 'no-store'
        r = client.get(path, params=params, headers={**LOCAL, 'Range': 'bytes=0-10'})
        assert r.status_code == 416 and r.headers['cache-control'] == 'no-store'
        for change in ({'prompt_id': 'missing'}, {'item_version': 'old'}):
            r = client.get(path, params={**params, **change}, headers=LOCAL)
            assert r.status_code == 409 and r.headers['cache-control'] == 'no-store', r.text
            assert 'local_path' not in r.text and 'artifact_id' not in r.text
        r = client.get(path, params={**params, 'prompt_id': ''}, headers=LOCAL)
        assert r.status_code == 422 and r.headers['cache-control'] == 'no-store'
    for change in ({'material_key': 'other'}, {'sha256': '0' * 64}):
        r = client.get(base + '/image', params={**image_query, **change}, headers=LOCAL)
        assert r.status_code == 409, r.text
    # Same application without entering a second lifespan.
    remote = TestClient(client.app, client=('192.0.2.8', 42283))
    try:
        for path, params in ((base, query), (base + '/image', image_query)):
            r = remote.get(path, params=params, headers=LOCAL)
            assert r.status_code == 403 and r.headers['cache-control'] == 'no-store'
    finally:
        remote.close()


def test_unconfigured_session_token_fails_closed(tmp_path):
    _, settings, _, _, _ = setup_case(tmp_path)
    with TestClient(create_app(replace(settings, session_token=None), model_gateway=NoCloud()),
                    client=('127.0.0.1', 42284)) as client:
        for suffix in ('', '/image'):
            r = client.get('/v1/sessions/study/diagnostic-visual' + suffix, headers=LOCAL,
                params=dict(prompt_id='unknown', item_version='unknown', material_key='figure', sha256='0' * 64))
            assert r.status_code == 401 and r.headers['cache-control'] == 'no-store', r.text


def test_current_prompt_cancellation_and_nonvisual_keep_no_images(live):
    client, db, _, _, _, _, state = live
    base, query = urls(state)
    r = client.post('/v1/sessions/' + state['session'] + '/diagnostic-prompt/cancel', headers=LOCAL, json=query)
    assert r.status_code == 200, r.text
    assert client.get(base, params=query, headers=LOCAL).status_code == 409
    r = client.post('/v1/sessions/' + state['session'] + '/diagnostic-prompt/prepare', headers=LOCAL,
        json=dict(track='school', preferred_item_id='diag-j3-physics-experiment-1', client_action_id='plain-original'))
    assert r.status_code == 200 and r.json()['status'] == 'waiting_answer', r.text
    plain = r.json()['prompt']
    r = client.get(base, headers=LOCAL, params={k: plain[k] for k in ('prompt_id', 'item_version')})
    assert r.status_code == 200 and r.json()['materials'] == [], r.text
    assert client.get(base, params=query, headers=LOCAL).status_code == 409  # never substitute the new prompt


def test_child_switch_rejects_original_and_cross_child_same_image(live):
    client, db, _, _, _, region, state = live
    base, query = urls(state)
    repo = client.app.state.repository
    other = next(c['child_id'] for c in repo.list_children() if c['child_id'] != 'child')
    switched = repo.switch_active_session_child(state['session'], other, 'switch-away')
    for sid in (state['session'], switched['session_id']):
        path = '/v1/sessions/' + sid + '/diagnostic-visual'
        r = client.get(path, params=query, headers=LOCAL)
        assert r.status_code == 409, r.text
        r = client.get(path + '/image', headers=LOCAL,
            params={**query, 'material_key': 'figure', 'sha256': region['safe_png_sha256']})
        assert r.status_code == 409, r.text
    returned = repo.switch_active_session_child(switched['session_id'], 'child', 'switch-back')
    r = client.get('/v1/sessions/' + returned['session_id'] + '/diagnostic-visual', params=query, headers=LOCAL)
    assert r.status_code == 409, r.text


@pytest.mark.parametrize('fault', ['asset_missing', 'asset_corrupt', 'item_changed', 'publication_withdrawn'])
def test_original_source_current_per_request_not_manifest_cache(live, fault):
    client, db, _, _, _, region, state = live
    base, query = urls(state)
    assert client.get(base, params=query, headers=LOCAL).status_code == 200
    if fault.startswith('asset_'):
        with db.connect() as c:
            path = Path(c.execute("SELECT local_path FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0])
        if fault == 'asset_missing':
            path.unlink()
        else:
            path.write_bytes(b'HSC1' + b'corrupt ciphertext' * 4)
    elif fault == 'item_changed':
        with db.connect() as c:
            c.execute('UPDATE diagnostic_item SET prompt=prompt || ? WHERE diagnostic_item_id=?',
                (' Changed version.', state['prompt']['diagnostic_item_id']))
            c.commit()
    else:
        r = client.post(SUPPLY + '/' + state['candidate'] + '/withdraw', headers=state['parent'],
            json=dict(action_id='withdraw-publication', expected_version=4, note='Source withdrawn.'))
        assert r.status_code == 200, r.text
    with db.connect() as c:
        before = list(c.iterdump())
    for path, params in ((base, query), (base + '/image', {**query,
            'material_key': 'figure', 'sha256': region['safe_png_sha256']})):
        r = client.get(path, params=params, headers=LOCAL)
        assert r.status_code == 409, r.text
        assert r.headers['cache-control'] == 'no-store'
    with db.connect() as c:
        assert list(c.iterdump()) == before  # no invalidation write/confirmation/evaluation


def test_borrowed_connection_read_only_and_rollback_no_hidden_connections(live, monkeypatch):
    from home_study_agent.diagnostic_visual import DiagnosticVisualService
    client, _, _, prepared, _, region, state = live
    database = client.app.state.repository.database
    service = DiagnosticVisualService(database, client.app.state.visual_materials)
    _, query = urls(state)
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE child_profile SET nickname='Temporary caller transaction' WHERE child_id='child'")
        changes = c.total_changes
        def no_connection():
            raise AssertionError('Read must borrow the caller connection')
        monkeypatch.setattr(database, 'connect', no_connection)
        c.set_authorizer(lambda action, *unused: sqlite3.SQLITE_DENY if action in {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_CREATE_TABLE} else sqlite3.SQLITE_OK)
        assert service.manifest(state['session'], **query, connection=c)['materials']
        assert service.image(state['session'], **query, material_key='figure',
            sha256=region['safe_png_sha256'], connection=c) == prepared.png
        assert c.in_transaction and c.total_changes == changes
        c.set_authorizer(lambda *unused: sqlite3.SQLITE_OK)
        c.rollback()
    monkeypatch.undo()
    with database.connect() as c:
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child'").fetchone()[0] != 'Temporary caller transaction'


def test_database_failure_not_reported_as_empty_or_unavailable(live, monkeypatch):
    from home_study_agent.diagnostic_visual import DiagnosticVisualService
    client, _, _, _, _, _, state = live
    database = client.app.state.repository.database
    service = DiagnosticVisualService(database, client.app.state.visual_materials)
    def broken():
        raise sqlite3.OperationalError('synthetic database failure')
    monkeypatch.setattr(database, 'connect', broken)
    _, query = urls(state)
    with pytest.raises(sqlite3.OperationalError, match='synthetic'):
        service.manifest(state['session'], **query)
    monkeypatch.undo()
