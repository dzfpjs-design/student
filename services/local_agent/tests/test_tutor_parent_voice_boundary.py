from datetime import datetime, timedelta, timezone

from test_app import FakeModelGateway, client_for
from test_tutor_ocr_evidence import _new_artifact, _confirm


def test_global_followup_keeps_ocr_authorization_and_rejects_wrong_parent(tmp_path):
    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'parent-voice-session'}).json()['session_id']
        artifact = _new_artifact(client, sid, 'parent-voice-artifact')
        confirmation = _confirm(client, sid, artifact['artifact_id'], '12加8等于多少？', 'manual_correction', 'parent-voice-confirm')
        root = client.post(f'/v1/sessions/{sid}/tutor', json={'subject': '数学', 'question': '12加8等于多少？', 'ocr_confirmation_id': confirmation['confirmation_id'], 'client_action_id': 'parent-voice-root'})
        assert root.status_code == 200, root.text
        payload = {'subject': '数学', 'question': root.json()['question'], 'hint_level': 2, 'parent_turn_id': root.json()['turn_id'], 'client_action_id': 'parent-voice-followup'}
        follow = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert follow.status_code == 200, follow.text
        assert follow.json()['ocr_confirmation_id'] == confirmation['confirmation_id']
        calls = len(gateway.messages)
        for change in ({'parent_turn_id': 'forged-turn'}, {'question': '另一个孩子的题目'}, {'subject': '物理'}):
            rejected = client.post(f'/v1/sessions/{sid}/tutor', json={**payload, **change, 'client_action_id': 'parent-voice-rejected-' + str(len(change))})
            assert rejected.status_code == 409
        with client.app.state.agent_orchestrator.database.connect() as connection:
            connection.execute('UPDATE tutor_turn SET child_id=? WHERE turn_id=?', ('child-junior-3', root.json()['turn_id']))
            connection.commit()
        rejected = client.post(f'/v1/sessions/{sid}/tutor', json={**payload, 'client_action_id': 'parent-voice-other-child'})
        assert rejected.status_code == 409
        assert len(gateway.messages) == calls
