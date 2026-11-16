"""Actual loopback request/PNG bytes and same database restart, no model traffic."""
from test_diagnostic_visual_loopback import host
from test_diagnostic_visual_http import LOCAL, publish
from test_visual_material_host import setup_case, headers, REGION
from test_visual_teaching_http import request
from test_visual_teaching_journey import help_turn, image_url


def test_actual_tcp_local_help_exact_image_restart_then_withdraw(tmp_path):
    db, settings, prepared, args, region = setup_case(tmp_path)
    with host(settings) as client:
        state = publish(client, db, prepared, args, region)
        disabled = client.put('/v1/parent/privacy', headers=state['parent'], json={'cloud_model_text_allowed':False})
        assert disabled.status_code == 200, disabled.text
        turn = help_turn(client, state)
        base = image_url(state)
        query = dict(turn_id=turn['turn_id'], material_key='figure', sha256=region['safe_png_sha256'])
        first = client.get(base, params={'turn_id': turn['turn_id']}, headers=LOCAL)
        assert first.status_code == 200, first.text
        assert client.get(base + '/image', params=query, headers=LOCAL).content == prepared.png
        assert client.get(base, params={'turn_id':turn['turn_id']}).status_code == 401
    with host(settings) as client:
        assert help_turn(client,state)['turn_id'] == turn['turn_id']
        assert client.get(base, params={'turn_id':turn['turn_id']}, headers=LOCAL).json() == first.json()
        assert client.get(base + '/image', params=query, headers=LOCAL).content == prepared.png
        r = client.post(REGION + '/retract', headers=headers(client), json={**args,'client_action_id':'tcp-local-retract'})
        assert r.status_code == 200, r.text
        assert client.get(base + '/image', params=query, headers=LOCAL).status_code == 409
        r = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=request(state))
        assert r.status_code == 409, r.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
