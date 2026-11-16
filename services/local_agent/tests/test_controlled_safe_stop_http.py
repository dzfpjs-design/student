"""Real isolated host commands; no native close, cloud call or learning claim."""
import base64
import pytest

from home_study_agent.database import Database
from test_app import client_for
from test_controlled_start_atomic_http import BODY, CHILD, setup, table
from test_controlled_end_identity_http import identity_key, stored
from test_companion_direct_start_atomic_http import task
from test_learning_runtime_http import NoCloud
from home_study_agent.local_media_ai import LocalMediaAI
from test_pet_media_evidence import wav_file


class AvailableButForbiddenGateway:
    configured = True

    def __init__(self):
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('Exact safe stop must not invoke any model')


def command(client, sid, text, key, cloud=False):
    return client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id': key, 'cloud_text_allowed': cloud})


@pytest.mark.parametrize('kind,text', [('content', '停止视频'), ('games', '停止游戏')])
@pytest.mark.parametrize('cloud', [False, True])
def test_direct_safe_stop_with_actual_outcome_never_needs_cloud_or_settles(tmp_path, kind, text, cloud):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        launched = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start', json=BODY)
        assert launched.status_code == 200, launched.text
        identity = launched.json()[identity_key(kind)]
        before, original = stored(db, kind, identity), task(db, pid)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome WHERE child_id=? AND session_id=?',
                             (CHILD, sid)).fetchone()[0] == 1
        result = command(client, sid, text, 'exact-safe-stop', cloud)
        assert result.status_code == 200, result.text
        heard = result.json()
        expected = 'stop_controlled_game' if kind == 'games' else 'stop_controlled_content'
        assert heard['action']['type'] == expected, heard
        assert heard['action'][identity_key(kind)] == identity
        assert heard['action']['controlled_stop_binding'] == {
            'child_id': CHILD, 'session_id': sid, 'kind': kind, 'activity_id': identity}
        assert heard['action']['cloud_transmission_allowed'] is False
        assert heard['action']['mastery_update_allowed'] is False
        assert heard['runtime_execution_location'] == 'client_device_bridge'
        assert command(client, sid, text, 'exact-safe-stop', cloud).json()['agent_turn_id'] == heard['agent_turn_id']
        assert stored(db, kind, identity) == before
        assert task(db, pid) == original
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action IN ('controlled_content_ended','controlled_game_ended')").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.mark.parametrize('text', ['不要停止视频', '我说的是“停止视频”这句话', '停止视频然后打开游戏',
                                '如果我说停止视频会怎样', '没有让你停止视频', '停止视频吗'])
def test_negated_quoted_conditional_or_mixed_is_not_local_stop(tmp_path, text):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, 'content')
        started = client.post(f'/v1/sessions/{sid}/controlled-content/start', json=BODY).json()
        before = stored(db, 'content', started['content_session_id'])
        result = command(client, sid, text, 'ambiguous-stop')
        assert result.status_code == 200, result.text
        assert result.json()['action']['type'] not in {'stop_controlled_content', 'stop_controlled_game'}
        assert stored(db, 'content', started['content_session_id']) == before
        assert task(db, pid)['status'] == 'paused'


def test_direct_stop_cannot_borrow_other_child_or_closed_session_activity(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, 'content')
        started = client.post(f'/v1/sessions/{sid}/controlled-content/start', json=BODY).json()
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': 'child-primary-4', 'client_action_id': 'other-child'})
        assert switched.status_code == 200, switched.text
        before = stored(db, 'content', started['content_session_id'])
        other = command(client, switched.json()['session_id'], '停止视频', 'other-stop')
        assert other.status_code == 200, other.text
        assert other.json()['action']['type'] != 'stop_controlled_content'
        old = command(client, sid, '停止视频', 'old-session-stop')
        assert old.status_code != 200 or old.json()['action']['type'] != 'stop_controlled_content'
        assert stored(db, 'content', started['content_session_id']) == before


@pytest.mark.parametrize('kind,text', [('content', '请关闭视频吧！'), ('games', '请帮我停止游戏。')])
def test_actual_parent_cloud_permission_on_does_not_override_exact_stop(tmp_path, kind, text):
    db = Database(tmp_path / 'test.db')
    gateway = AvailableButForbiddenGateway()
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, kind)
        started = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start', json=BODY)
        assert started.status_code == 200, started.text
        token = client.post('/v1/parent/unlock', json={'pin': '4826'}).json()['parent_token']
        granted = client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token},
                             json={'cloud_model_text_allowed': True})
        assert granted.status_code == 200, granted.text
        with db.connect() as c:
            assert c.execute('SELECT cloud_model_text_allowed FROM privacy_preference').fetchone()[0] == 1
        # Provider boundary only; the active session/activity and permission are real.
        client.app.state.agent_orchestrator.model_gateway = gateway
        client.app.state.agent_orchestrator.voice_control_intent.gateway = gateway
        result = command(client, sid, text, 'cloud-permitted-exact-stop', True)
        assert result.status_code == 200, result.text
        assert result.json()['action'][identity_key(kind)] == started.json()[identity_key(kind)]
        assert result.json()['action']['source'] == 'local_controlled_stop_v1'
        assert gateway.calls == 0


def test_same_original_stop_key_after_restart_preserves_binding_and_no_end(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, 'content')
        started = client.post(f'/v1/sessions/{sid}/controlled-content/start', json=BODY).json()
        first = command(client, sid, '停止视频', 'stop-before-restart').json()
        assert first['action']['content_session_id'] == started['content_session_id']
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        before = stored(db, 'content', started['content_session_id'])
        assert before['status'] == 'interrupted'
        retry = command(client, sid, '停止视频', 'stop-before-restart')
        assert retry.status_code == 200, retry.text
        assert retry.json()['agent_turn_id'] == first['agent_turn_id']
        assert retry.json()['action']['controlled_stop_binding'] == first['action']['controlled_stop_binding']
        current = command(client, sid, '停止视频', 'new-explicit-recovery')
        assert current.status_code == 200, current.text
        assert current.json()['action']['content_session_id'] == started['content_session_id']
        assert stored(db, 'content', started['content_session_id']) == before
        assert task(db, pid)['status'] == 'paused'


@pytest.mark.parametrize('text', ['我不看这个了', '先把这个关掉'])
def test_natural_external_stop_not_claimed_without_semantic_contract(tmp_path, text):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, 'content')
        started = client.post(f'/v1/sessions/{sid}/controlled-content/start', json=BODY).json()
        before = stored(db, 'content', started['content_session_id'])
        result = command(client, sid, text, 'unsupported-natural-stop', True)
        assert result.status_code == 200, result.text
        assert result.json()['action']['type'] == 'none'
        assert stored(db, 'content', started['content_session_id']) == before


def captured_stop(client, tmp_path, monkeypatch, sid, reference=None, key='safe-stop-asr'):
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: ('停止视频', 1000, 'synthetic', False))
    path = tmp_path / 'synthetic-stop.wav'
    wav_file(path)
    result = client.post(f'/v1/sessions/{sid}/asr', json={
        'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
        'client_action_id': key, 'capture_reference': {'child_id': CHILD,
            **({'activity_reference': reference} if reference else {})}})
    assert result.status_code == 200, result.text
    return result.json()


def test_captured_stop_cannot_borrow_activity_created_after_voice_arrival(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, 'content')
        first = client.post(f'/v1/sessions/{sid}/controlled-content/start', json=BODY).json()
        voice = captured_stop(client, tmp_path, monkeypatch, sid)
        ended = client.post(f'/v1/sessions/{sid}/controlled-content/end', json={
            'content_session_id': first['content_session_id'], 'reason': 'test_protocol_close', 'actual_seconds': 0})
        assert ended.status_code == 200, ended.text
        new = client.post(f'/v1/sessions/{sid}/controlled-content/start', json={**BODY, 'client_action_id': 'later-start'}).json()
        before = stored(db, 'content', new['content_session_id'])
        heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '停止视频', 'voice_input_id': voice['voice_input_id'],
            'client_action_id': 'late-stop-transcript', 'cloud_text_allowed': False})
        assert heard.status_code == 200, heard.text
        assert heard.json()['action']['type'] == 'none'
        assert stored(db, 'content', new['content_session_id']) == before


def test_stale_companion_audio_focus_never_becomes_external_media_stop(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, 'content')
        media = client.post(f'/v1/sessions/{sid}/controlled-content/start', json=BODY).json()
        companion = client.post(f'/v1/sessions/{sid}/companion-activities/start', json={
            'activity_type': 'movement', 'topic': 'Synthetic only', 'client_action_id': 'other-kind'}).json()
        focus = {'activity_id': companion['activity_id'], 'version': 'stale-version', 'last_turn_id': 'unverified'}
        voice = captured_stop(client, tmp_path, monkeypatch, sid, focus)
        assert voice['capture_binding']['activity_validation'] == 'stale'
        before = stored(db, 'content', media['content_session_id'])
        heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '停止视频', 'voice_input_id': voice['voice_input_id'],
            'client_action_id': 'focus-bound-stop', 'cloud_text_allowed': False})
        assert heard.status_code == 200, heard.text
        assert heard.json()['action']['type'] == 'none'
        assert heard.json()['action']['voice_input_binding']['activity_reference'] == focus
        assert stored(db, 'content', media['content_session_id']) == before
