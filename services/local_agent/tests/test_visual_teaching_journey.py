"""Actual local publisher, Tutor and restart journeys with synthetic pixels."""
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.visual_teaching import read_turn
from test_diagnostic_visual_http import live, LOCAL, urls, publish
from test_visual_material_host import setup_case, headers, REGION
from test_pet_reviewed_source_actual_http import NoCloud
from test_visual_teaching_http import request


def help_turn(client, state, **kwargs):
    r = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=request(state, **kwargs))
    assert r.status_code == 200, r.text
    return r.json()


def image_url(state):
    return '/v1/sessions/' + state['session'] + '/tutor-visual'


def test_exact_turn_restart_checkpoint_replay_withdraw(tmp_path):
    db, settings, prepared, args, region = setup_case(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42501)) as client:
        state = publish(client, db, prepared, args, region)
        turn = help_turn(client, state)
        following = help_turn(client, state, key='continue', level=2, parent=turn['turn_id'])
        assert following['parent_turn_id'] == turn['turn_id'] and following['answer'] != turn['answer']
        base = image_url(state)
        meta = client.get(base, params={'turn_id': turn['turn_id']}, headers=LOCAL)
        assert meta.status_code == 200, meta.text
        value = meta.json()
        assert value == dict(turn['output_envelope']['result']['local_visual'], turn_id=turn['turn_id'])
        query = dict(turn_id=turn['turn_id'], material_key='figure', sha256=region['safe_png_sha256'])
        r = client.get(base + '/image', params=query, headers=LOCAL)
        assert r.content == prepared.png and r.headers['cache-control'] == 'no-store'
        assert 'X-HSC-Visual-Display-Token' not in r.headers
        r = client.post('/v1/sessions/' + state['session'] + '/tutor-playback/begin', headers=LOCAL,
            json=dict(turn_id=turn['turn_id'], client_action_id='begin-original'))
        assert r.status_code == 200, r.text
        checkpoint = r.json()['playback']
        assert checkpoint['turn_id'] == turn['turn_id'] and checkpoint['status'] == 'paused'
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42502)) as client:
        assert help_turn(client, state)['turn_id'] == turn['turn_id']
        assert client.get(base, params={'turn_id': checkpoint['turn_id']}, headers=LOCAL).json() == value
        assert client.get(base + '/image', params=query, headers=LOCAL).content == prepared.png
        r = client.post(REGION + '/retract', headers=headers(client), json={**args, 'client_action_id': 'retract'})
        assert r.status_code == 200, r.text
        for path, params in ((base, {'turn_id': turn['turn_id']}), (base + '/image', query)):
            r = client.get(path, params=params, headers=LOCAL)
            assert r.status_code == 409 and r.headers['cache-control'] == 'no-store', r.text
        r = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=request(state))
        assert r.status_code == 409, r.text
        r = client.get('/v1/sessions/' + state['session'] + '/tutor-playback', headers=LOCAL)
        assert r.status_code == 200, r.text
        assert r.json()['playback']['status'] == 'invalidated' and r.json()['playback']['presentation'] is None
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 2
            assert c.execute('SELECT attempts FROM tutor_request_reservation WHERE action_id=?', ('visual-help',)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_manifest_read_borrowed_snapshot_zero_writes_and_auth(live, monkeypatch):
    client, db, _, _, _, region, state = live
    turn = help_turn(client, state)
    base = image_url(state)
    for h in ({}, {'X-HSC-Session': 'wrong'}, {'X-HSC-Parent': state['parent']['X-HSC-Parent']}):
        r = client.get(base, params={'turn_id': turn['turn_id']}, headers=h)
        assert r.status_code == 401 and r.headers['cache-control'] == 'no-store'
    assert client.get(base, params={'turn_id': turn['turn_id']}, headers={**LOCAL,'Range':'bytes=0-1'}).status_code == 416
    assert client.get(base, params={'turn_id':'missing'}, headers=LOCAL).status_code == 409
    service_db = client.app.state.visual_teaching.database
    with service_db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE visual_borrowed_probe(value TEXT)')
        c.execute("INSERT INTO visual_borrowed_probe VALUES('uncommitted')")
        before = c.total_changes
        monkeypatch.setattr(service_db, 'connect', lambda: pytest.fail('hidden connection'))
        manifest, _ = read_turn(c, state['session'], turn['turn_id'])
        assert manifest['turn_id'] == turn['turn_id'] and c.in_transaction and c.total_changes == before
        c.rollback()


def test_answered_original_can_explain_without_pending_visual_loophole(live):
    client, db, _, _, _, _, state = live
    query = {k:state['prompt'][k] for k in ('prompt_id','item_version')}
    r = client.post('/v1/sessions/' + state['session'] + '/diagnostic-prompt/answer', headers=LOCAL,
        json={**query, 'answer':'I see an outline; I cannot infer its measurements.', 'client_action_id':'original-answer'})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'answered', r.text
    assert client.get(urls(state)[0], params=query, headers=LOCAL).status_code == 409
    turn = help_turn(client, state)
    assert client.get(image_url(state), params={'turn_id':turn['turn_id']}, headers=LOCAL).status_code == 200
    with db.connect() as c:
        before = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
    for level in range(2,7):
        following = help_turn(client, state, key='visual-hint-'+str(level), level=level, parent=turn['turn_id'])
        assert not following['output_envelope']['result']['local_visual']['mastery_claim_allowed']
        assert following['output_envelope']['result']['observation_support']['observation_id'] == 'outline'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == before == 1
    r = client.post('/v1/sessions/' + state['session'] + '/diagnostic-prompt/prepare', headers=LOCAL,
        json=dict(track='school', preferred_item_id='diag-j3-physics-experiment-1', client_action_id='next-original'))
    assert r.status_code == 200 and r.json()['status'] == 'waiting_answer', r.text
    assert client.get(image_url(state), params={'turn_id':turn['turn_id']}, headers=LOCAL).status_code == 409


def test_source_change_between_projection_and_commit_rolls_back(live, monkeypatch):
    client, db, _, _, args, _, state = live
    service = client.app.state.visual_teaching
    original = service._project
    count = 0
    def changed(*a, **kw):
        nonlocal count
        value = original(*a, **kw)
        count += 1
        if count == 1:
            client.app.state.visual_materials.decide('child', 'page', 'figure',
                **args, client_action_id='late-retract', decision='retract')
        return value
    monkeypatch.setattr(service, '_project', changed)
    r = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=request(state))
    assert r.status_code == 409, r.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 0
        assert c.execute("SELECT status FROM tutor_request_reservation WHERE action_id='visual-help'").fetchone()[0] == 'failed'


def test_child_switch_and_wrong_original_version_never_returns_figure(live):
    client, db, _, _, _, _, state = live
    turn = help_turn(client,state)
    r = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL,
        json={**request(state,key='wrong-version'),'item_version':'old'})
    assert r.status_code == 409, r.text
    repo = client.app.state.repository
    other = next(c['child_id'] for c in repo.list_children() if c['child_id'] != 'child')
    switched = repo.switch_active_session_child(state['session'], other, 'switch-away')
    for sid in (state['session'], switched['session_id']):
        r = client.get('/v1/sessions/' + sid + '/tutor-visual', params={'turn_id':turn['turn_id']}, headers=LOCAL)
        assert r.status_code == 409, r.text


def test_failed_persist_same_key_retry_and_conflict(live):
    client, db, _, _, _, _, state = live
    with db.connect() as c:
        c.execute("""CREATE TRIGGER fail_local_visual_save BEFORE INSERT ON tutor_turn
            BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END""")
        c.commit()
    failed_client = TestClient(client.app, client=('127.0.0.1',42510), raise_server_exceptions=False)
    try:
        r = failed_client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=request(state))
        assert r.status_code == 500, r.text
    finally:
        failed_client.close()
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 0
        assert c.execute("SELECT status FROM tutor_request_reservation WHERE action_id='visual-help'").fetchone()[0] == 'failed'
        c.execute('DROP TRIGGER fail_local_visual_save'); c.commit()
    first = help_turn(client, state)
    assert help_turn(client, state)['turn_id'] == first['turn_id']
    r = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL,
        json=request(state, level=2))
    assert r.status_code == 409, r.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 1
        assert c.execute("SELECT attempts FROM tutor_request_reservation WHERE action_id='visual-help'").fetchone()[0] == 2
    # Losing the original typed publication cannot fall through to legacy replay.
    with db.connect() as c:
        c.execute('UPDATE specialized_supply_candidate SET diagnostic_item_id=NULL WHERE candidate_id=?', (state['candidate'],))
        c.commit()
    r = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=request(state))
    assert r.status_code == 409, r.text


def test_missing_png_and_corrupt_saved_board_never_restore(live):
    from pathlib import Path
    import json
    client, db, settings, _, _, _, state = live
    turn = help_turn(client,state)
    base = image_url(state)
    with db.connect() as c:
        saved = c.execute('SELECT output_envelope_json FROM tutor_turn WHERE turn_id=?',(turn['turn_id'],)).fetchone()[0]
        modified = json.loads(saved)
        modified['result']['blackboard']['segments'][0]['narration'] = 'Unapproved replacement.'
        c.execute('UPDATE tutor_turn SET output_envelope_json=? WHERE turn_id=?',(json.dumps(modified),turn['turn_id']))
        c.commit()
    assert client.get(base,params={'turn_id':turn['turn_id']},headers=LOCAL).status_code == 409
    with db.connect() as c:
        c.execute('UPDATE tutor_turn SET output_envelope_json=? WHERE turn_id=?',(saved,turn['turn_id']))
        path = Path(c.execute("SELECT local_path FROM capture_artifact WHERE child_id=? AND artifact_type='document_visual_region'",
            (state['prompt']['child_id'] if 'child_id' in state['prompt'] else 'child',)).fetchone()[0])
        c.commit()
    raw = path.read_bytes()
    try:
        path.unlink()
        assert client.get(base,params={'turn_id':turn['turn_id']},headers=LOCAL).status_code == 409
    finally:
        path.write_bytes(raw)
