"""Signed native voice transport, synthetic speech text and model; no devices or network."""
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelResult
from test_realtime_native_curriculum import CHILD, KEY, TOKEN, native_voice, propose, signed_post


class Gateway:
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResult(content='先看具体的关系。', provider='test', model='synthetic', latency_ms=1)


def test_native_followup_requires_claim_and_frozen_source(tmp_path, monkeypatch):
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path / 'native-followup.db'))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'native-followup.db', session_token=TOKEN,
                        native_launch_attestation_key=KEY, maintenance_enabled=False, agent_runtime_enabled=True)
    gateway = Gateway()
    app = create_app(settings, model_gateway=gateway)
    with TestClient(app, client=('127.0.0.1', 50000), headers={'X-HSC-Session': TOKEN}) as client:
        parent = client.post('/v1/parent/setup', json={'pin': '8264'}).json()
        assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': parent['parent_token']},
                          json={'cloud_model_text_allowed': True, 'cloud_audio_allowed': True}).status_code == 200
        session = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'native-followup-session'}).json()
        root = '/v1/sessions/' + session['session_id']
        lesson = client.post(root + '/tutor', json={'subject': '化学', 'question': '什么是物质的性质？',
            'hint_level': 6, 'client_action_id': 'native-followup-root'})
        assert lesson.status_code == 200, lesson.text
        words = '刚才那个例子为什么是这样？'
        identity = native_voice(client, root, 'followup-native', words)
        admitted = propose(client, root, identity, 'learning.explain', {
            'subject': '化学', 'question_source': 'spoken_request', 'question_relation': 'followup', 'support_level': 6})
        dispatch = admitted['client_dispatch']
        action = dispatch['turn']['action']
        assert action['tutor_followup_binding']['turn_id'] == lesson.json()['turn_id']
        payload = {'source_turn_id': lesson.json()['turn_id'], 'question': words,
                   'client_action_id': action['tutor_request_action_id']}
        before = len(gateway.requests)
        assert client.post(root + '/tutor-followup', json=payload).status_code == 409
        assert len(gateway.requests) == before
        claimed = signed_post(client, root, 'client', {**identity, 'phase': 'claim', 'dispatch_id': dispatch['dispatch_id']})
        assert claimed['status'] == 'claimed'
        rejected = client.post(root + '/tutor-followup', json={**payload, 'question': '换一道题？'})
        assert rejected.status_code == 409 and len(gateway.requests) == before
        agent_id = dispatch['turn']['agent_turn_id']
        with app.state.repository.database.connect() as connection:
            row = connection.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (agent_id,)).fetchone()
            saved_manifest = row['context_manifest_json']
            tampered = json.loads(saved_manifest)
            tampered['qwen_realtime_capability_v1']['native_sources_sha256'] = '0' * 64
            connection.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',
                               (json.dumps(tampered, ensure_ascii=False), agent_id))
            connection.commit()
        assert client.post(root + '/tutor-followup', json=payload).status_code == 409
        assert len(gateway.requests) == before
        with app.state.repository.database.connect() as connection:
            connection.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?', (saved_manifest, agent_id))
            connection.commit()
        taught = client.post(root + '/tutor-followup', json=payload)
        assert taught.status_code == 200, taught.text
        assert lesson.json()['question'] in gateway.requests[-1].messages[-1]['content']
        turn = taught.json()
        receipt = {'capability': 'learning.explain', 'execution_location': 'local_model', 'action_type': 'ask_tutor',
                   'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(),
                   'output': {'tutor_turn_id': turn['turn_id'], 'blackboard_present': True}}
        completed = signed_post(client, root, 'client', {**identity, 'phase': 'complete', 'dispatch_id': dispatch['dispatch_id'],
            'final_response': turn['answer'], 'tool_status': 'completed', 'result': {'execution_receipt': receipt}})
        assert completed['execution']['status'] == 'completed'
        replay = client.post(root + '/tutor-followup', json=payload)
        assert replay.status_code == 200 and replay.json()['turn_id'] == turn['turn_id'], replay.text
        new_words = '另一个问题，光为什么会反射？'
        new_identity = native_voice(client, root, 'new-topic-native', new_words)
        new_admitted = propose(client, root, new_identity, 'learning.explain', {
            'subject': '物理', 'question_source': 'spoken_request', 'question_relation': 'new', 'support_level': 6})
        new_dispatch = new_admitted['client_dispatch']
        new_action = new_dispatch['turn']['action']
        assert new_action['tutor_request_action_id'].startswith('native-tutor-')
        assert not new_action.get('tutor_followup_binding')
        assert client.post(root + '/tutor-followup', json=payload).status_code == 409
        signed_post(client, root, 'client', {**new_identity, 'phase': 'claim', 'dispatch_id': new_dispatch['dispatch_id']})
        independent = client.post(root + '/tutor', json={'subject': new_action['subject'],
            'question': new_action['question'], 'hint_level': 6, 'client_action_id': new_action['tutor_request_action_id']})
        assert independent.status_code == 200, independent.text
        assert lesson.json()['question'] not in gateway.requests[-1].messages[-1]['content']
        assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': parent['parent_token']},
                          json={'cloud_model_text_allowed': False}).status_code == 200
        assert client.post(root + '/tutor-followup', json=payload).status_code == 409
