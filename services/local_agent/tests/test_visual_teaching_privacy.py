"""Local figures never become a model/TTS payload through Tutor continuation."""
import json
import pytest
from test_diagnostic_visual_http import live, LOCAL
from test_visual_teaching_http import request
from test_visual_teaching_journey import help_turn


def test_local_turn_narration_and_chunks_never_reach_cloud_tts(live, monkeypatch):
    client, db, _, _, _, _, state = live
    turn = help_turn(client, state)
    def forbidden(*args, **kwargs):
        raise RuntimeError('local narration reached cloud eligibility/transport')
    monkeypatch.setattr(client.app.state.voice_synthesis, 'assert_eligible', forbidden)
    for index, text in enumerate((turn['answer'], 'A closed outline.', 'Identify the visible marks.')):
        r = client.post('/v1/sessions/' + state['session'] + '/voice/synthesize/stream', headers=LOCAL,
            json=dict(text=text, client_action_id='legacy-tts-'+str(index), local_only=False))
        assert r.status_code == 409 and r.json()['detail']['code'] == 'LOCAL_ONLY_SPEECH_REQUIRED', r.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_parent_continuation_without_prompt_never_reaches_model_context(live):
    client, db, _, _, _, _, state = live
    turn = help_turn(client, state)
    body = request(state, key='parent-without-explicit-prompt', level=3, parent=turn['turn_id'])
    body.pop('prompt_id'); body.pop('item_version')
    response = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=body)
    assert response.status_code == 200, response.text
    continuation = response.json()
    assert continuation['provider'] == 'local' and continuation['output_envelope']['local_only']
    assert continuation['output_envelope']['result']['local_visual']['prompt_id'] == state['prompt']['prompt_id']
    body.update(client_action_id='invalid-parent-continuation', question='Now solve another private drawing.')
    assert client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=body).status_code == 409
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 2
    # Existing cloud context builder has no prior Tutor answer/board argument;
    # exact parent requests above must stay on the local branch before it is used.
    assert 'image' not in json.dumps(continuation['output_envelope']['evidence_refs'])
