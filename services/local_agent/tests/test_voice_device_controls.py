import json

from home_study_agent.voice_playback import VoicePlaybackGuard
from test_app import client_for


def test_playback_provenance_is_bounded_session_scoped_and_preserves_math():
    now = [0.0]
    guard = VoicePlaybackGuard(clock=lambda: now[0])
    spoken = "这道题的结果是a²+b²=5，需要先计算每一部分，再核对条件。"
    guard.record("a", "playback-one", spoken, "started")
    assert guard.matches("a", "playback-one", spoken)
    assert not guard.matches("b", "playback-one", spoken)
    assert not guard.matches("a", "playback-one", spoken.replace("+", "-"))
    assert not guard.matches("a", "playback-one", spoken + "但是我不同意")
    assert not guard.matches("a", "playback-one", "我不懂")
    guard.record("a", "playback-one", spoken, "ended")
    assert guard.matches("a", "playback-one", spoken)
    now[0] = 181
    assert not guard.matches("a", "playback-one", spoken)
    for index in range(40):
        guard.record("a", str(index), spoken, "started")
    assert len(guard.entries) == 32


def test_text_overlap_is_retained_and_clarified_without_learning_evidence(tmp_path):
    with client_for(tmp_path) as client:
        sid = client.post('/v1/sessions', json={
            'child_id': 'child-primary-4', 'client_action_id': 'device-voice-session',
        }).json()['session_id']
        spoken = '当前会话还没有可核对的页面记录，我不能判断你刚才写了什么。'
        registered = client.post(f'/v1/sessions/{sid}/voice-playback', json={
            'playback_id': 'playback-local-001', 'text': spoken, 'event': 'started',
        })
        assert registered.status_code == 200
        payload = {'transcript': spoken, 'client_action_id': 'overlap-turn-001',
                   'playback_overlap_id': 'playback-local-001'}
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json=payload).json()
        # Playback timing and matching text do not identify the speaker.
        assert turn['intent'] == 'playback_overlap_clarification'
        assert turn['action'].get('suppress_speech') is not True
        assert turn['action']['echo_source_verified'] is False
        assert turn['response_text']
        assert turn['action']['cloud_transmission_allowed'] is False
        assert turn['action']['independent_evidence'] is False
        assert turn['transcript'] == spoken
        replay = client.post(f'/v1/sessions/{sid}/agent-turn', json=payload).json()
        assert replay['agent_turn_id'] == turn['agent_turn_id']
        fresh = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': spoken, 'client_action_id': 'independent-repeat-001',
        }).json()
        assert fresh['intent'] != 'speech_echo_ignored'
        assert fresh['action'].get('suppress_speech') is not True
        with client.app.state.agent_orchestrator.database.connect() as connection:
            assert connection.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?',
                                      (turn['agent_turn_id'],)).fetchone()[0] == spoken
    with client_for(tmp_path) as restarted:
        with restarted.app.state.agent_orchestrator.database.connect() as connection:
            saved = connection.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
                                       (turn['agent_turn_id'],)).fetchone()[0]
            assert json.loads(saved).get('suppress_speech') is not True
            assert json.loads(saved)['original_utterance_retained'] is True
        assert not restarted.app.state.agent_orchestrator.voice_playback.entries


def test_pause_camera_does_not_stop_microphone_or_document_camera(tmp_path):
    with client_for(tmp_path) as client:
        sid = client.post('/v1/sessions', json={
            'child_id': 'child-primary-4', 'client_action_id': 'camera-pause-session',
        }).json()['session_id']
        leases = {}
        for role in ('camera', 'document_camera', 'microphone'):
            result = client.post(f'/v1/sessions/{sid}/devices/register', json={
                'device_type': role, 'system_id_hash': role * 4, 'capabilities': {},
            })
            assert result.status_code == 200, result.text
            leases[role] = result.json()['lease_id']
        assert client.post(f'/v1/sessions/{sid}/observation/pause', json={
            'client_action_id': 'camera-only-pause',
        }).status_code == 200
        with client.app.state.agent_orchestrator.database.connect() as connection:
            statuses = {role: connection.execute('SELECT status FROM sensor_lease WHERE lease_id=?',
                                                (lease,)).fetchone()[0] for role, lease in leases.items()}
        assert statuses == {'camera': 'released', 'document_camera': 'active', 'microphone': 'active'}
