"""Real published synthetic figure -> local Tutor, never model/scoring evidence."""
import json

from test_diagnostic_visual_http import live, LOCAL, urls


def request(state, *, key='visual-help', level=1, parent=None):
    return dict(subject=state['prompt']['item']['subject'], question=state['prompt']['item']['prompt'],
        hint_level=level, parent_turn_id=parent, client_action_id=key,
        prompt_id=state['prompt']['prompt_id'], item_version=state['prompt']['item_version'])


def test_published_visual_tutor_local_branch_with_cloud_disabled(live):
    client, db, _, _, _, _, state = live
    consent = client.put('/v1/parent/privacy', headers=state['parent'], json={'cloud_model_text_allowed':False})
    assert consent.status_code == 200 and consent.json()['cloud_model_text_allowed'] is False
    response = client.post('/v1/sessions/' + state['session'] + '/tutor', headers=LOCAL, json=request(state))
    assert response.status_code == 200, response.text
    turn = response.json()
    assert turn['provider'] == 'local' and turn['model'] == 'visual_teaching_v1'
    assert turn['local_only'] is True and turn['cloud_transmission_allowed'] is False
    envelope = turn['output_envelope']
    assert envelope['local_only'] and not envelope['cloud_transmission_allowed']
    value = envelope['result']['local_visual']
    assert value['prompt_id'] == state['prompt']['prompt_id'] and value['item_version'] == state['prompt']['item_version']
    assert value['teaching_scope'] == 'observation_scaffold' and not value['mastery_claim_allowed']
    assert 'A closed outline.' in turn['answer']
    assert 'Identify the visible marks.' in turn['answer']
    assert envelope['result']['blackboard']['segments'][-1]['blocks'][-1]['kind'] == 'question'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        stored = c.execute('SELECT status,tutor_turn_id FROM tutor_request_reservation WHERE action_id=?', ('visual-help',)).fetchone()
        assert tuple(stored) == ('completed', turn['turn_id'])
