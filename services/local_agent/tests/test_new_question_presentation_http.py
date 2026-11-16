"""Source-fixed regression candidate; the prior teaching remains history."""
from test_independent_question_voice_http import context
from test_tutor_first_check_http import OverhelpGateway


def test_new_source_question_hides_old_tutor_and_invalidates_old_playback(tmp_path, monkeypatch):
    client, db, settings, gateway, sid, original, second, voice = context(tmp_path, monkeypatch)
    original_complete = gateway.complete
    tutor = OverhelpGateway()
    async def complete(request):
        if request.request_type != 'voice_interaction':
            return await tutor.complete(request)
        return await original_complete(request)
    gateway.complete = complete
    try:
        response = client.post(f'/v1/sessions/{sid}/tutor', json={
            'subject': original['item']['subject'], 'question': original['item']['prompt'],
            'prompt_id': original['prompt_id'], 'item_version': original['item_version'],
            'client_action_id': 'old-source-presentation'})
        assert response.status_code == 200, response.text
        taught = response.json()
        assert client.get(f'/v1/sessions/{sid}').json()['latest_tutor_turn']['turn_id'] == taught['turn_id']
        begin = client.post(f'/v1/sessions/{sid}/tutor-playback/begin', json={
            'turn_id': taught['turn_id'], 'client_action_id': 'old-source-playback'})
        assert begin.status_code == 200, begin.text
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'school', 'subject': original['item']['subject'],
            'knowledge_node_id': original['item']['knowledge_node_id'], 'require_unexposed': True,
            'preferred_item_id': second['diagnostic_item_id'], 'client_action_id': 'actual-new-source'})
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()['prompt']['prompt_id'] != original['prompt_id']
        assert client.get(f'/v1/sessions/{sid}').json()['latest_tutor_turn'] is None
        assert client.get(f'/v1/sessions/{sid}/tutor-playback').json()['playback']['status'] == 'invalidated'
        from fastapi.testclient import TestClient
        from home_study_agent.app import create_app
        client.__exit__(None, None, None)
        with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as restarted:
            assert restarted.get(f'/v1/sessions/{sid}').json()['latest_tutor_turn'] is None
            assert restarted.get(f'/v1/sessions/{sid}/tutor-playback').json()['playback']['status'] == 'invalidated'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    finally:
        client.__exit__(None, None, None)
