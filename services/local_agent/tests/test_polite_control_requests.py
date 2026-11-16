"""Polite semantic requests, exact local sources, no paid/model network."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_content_free_voice_control_http import ControlGateway
from test_tutor_playback import playback_flow, binding
from test_voice_runtime_binding import prepare, capture, interpret
from test_voice_device_capability_contract import receipt


@pytest.fixture
def flow(tmp_path):
    # The current prompt precedes its current teaching. open_context replays
    # this canonical preparation instead of opening a new question afterward.
    return playback_flow(tmp_path, pending_question=True)


def decision(text, selected_control, **changes):
    return json.dumps({'role': 'control', 'control': selected_control, 'quote': text,
                       'confidence': .99, **changes}, ensure_ascii=False)


@pytest.mark.parametrize('text,control', [
    ('可以暂停讲解吗？', 'tutor.pause'),
    ('帮我把摄像头停一下好吗？', 'observation.pause'),
    ('麻烦先让讲解停一会儿，可以吗?', 'tutor.pause'),
    ('这段能再播一回吗？', 'speech.repeat'),
    ('我想接着听原来的讲解，行吗？', 'tutor.resume'),
    ('能先停下高拍仪吗？', 'document.pause'),
])
def test_parser_accepts_model_understood_polite_request(text, control):
    assert VoiceControlIntent.parse(decision(text, control), text) == {'status': 'control', 'control': control}


@pytest.mark.parametrize('text', [
    '不要暂停讲解好吗？', '先别暂停讲解。', '我不是要暂停讲解。',
    '他说“暂停讲解”，是什么意思？', '假如暂停讲解会怎样？',
    '你有暂停讲解的功能吗？', '是否支持暂停讲解？',
    '先暂停讲解，然后帮我解答这道题。',
])
def test_parser_rejects_obvious_conflict_even_if_model_labels_control(text):
    assert VoiceControlIntent.parse(decision(text, 'tutor.pause'), text)['status'] != 'control'


@pytest.mark.parametrize('role', ['mixed', 'question', 'answer', 'unknown'])
def test_punctuation_never_upgrades_non_control_role(role):
    text = '可以暂停讲解吗？'
    assert VoiceControlIntent.parse(decision(text, None, role=role), text)['status'] != 'control'


@pytest.mark.parametrize('changes', [{'quote': '暂停讲解'}, {'confidence': .5},
                                     {'role': 'question'}, {'control': 'arbitrary.stop'}])
def test_complete_quote_role_enum_and_confidence_still_required(changes):
    text = '可以暂停讲解吗？'
    try:
        result = VoiceControlIntent.parse(decision(text, 'tutor.pause', **changes), text)
    except ValueError:
        return
    assert result['status'] != 'control'


def open_context(client, sid, turn):
    token = client.post('/v1/parent/setup', json={'pin': '7531'}).json()['parent_token']
    consent = client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token},
                         json={'cloud_model_text_allowed': True})
    assert consent.status_code == 200, consent.text
    prompt = prepare(client, sid, 'polite-original-question')
    path = f'/v1/sessions/{sid}/tutor-playback'
    response = client.post(path + '/begin', json={'turn_id': turn['turn_id'], 'client_action_id': 'polite-playback-begin'})
    assert response.status_code == 200, response.text
    state = response.json()['playback']
    response = client.post(path + '/control', json={**binding(state), 'command': 'resume', 'client_action_id': 'polite-playback-resume'})
    assert response.status_code == 200, response.text
    return prompt, response.json()['playback']


@pytest.mark.parametrize('text,control,action', [
    ('可以暂停讲解吗？', 'tutor.pause', 'control_tutor_playback'),
    ('这段先停一停，好吗？', 'tutor.pause', 'control_tutor_playback'),
    ('帮我把摄像头停一下好吗？', 'observation.pause', 'pause_observation'),
])
def test_actual_http_polite_control_preserves_waiting_question_and_exact_playback(flow, monkeypatch, text, control, action):
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    gateway = ControlGateway(text, control)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, playback = open_context(client, sid, turn)
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        heard = interpret(client, sid, text, 'polite-request', voice)
        assert heard['action']['type'] == action, heard
        assert heard['requires_control_admission']
        admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{heard['agent_turn_id']}/control-admission")
        assert admitted.status_code == 200 and admitted.json()['status'] == 'admitted', admitted.text
        if control == 'tutor.pause':
            assert heard['action']['tutor_playback_id'] == playback['playback_id']
            paused = client.post(f'/v1/sessions/{sid}/tutor-playback/control', json={
                **binding(playback), 'command': 'pause', 'client_action_id': heard['agent_turn_id'] + ':pause'})
            assert paused.status_code == 200, paused.text
            state = paused.json()['playback']
            assert state['status'] == 'paused' and state['playback_id'] == playback['playback_id']
            output = {key: state[key] for key in ('playback_id', 'status', 'segment_index', 'evidence_kind')}
            completed = client.put(f"/v1/agent-turns/{heard['agent_turn_id']}/outcome", json={
                'tool_status': 'completed', 'final_response': '讲解已暂停，进度已经保存。',
                'result': receipt('tutor.playback_control', output)})
            assert completed.status_code == 200, completed.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == 'waiting_answer'
        assert len(gateway.requests) == 1
        payload = json.loads(gateway.requests[0].messages[-1]['content'])
        assert set(payload) == {'transcript', 'state'}
        assert all(type(v) is bool for v in payload['state'].values())
        wire = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        assert all(v not in wire for v in (sid, voice, prompt['prompt_id'], prompt['item']['prompt'], turn['turn_id']))
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, text, 'polite-request', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert len(gateway.requests) == 1
        if control == 'tutor.pause':
            restored = client.get(f'/v1/sessions/{sid}/tutor-playback', params={'playback_id': playback['playback_id']})
            assert restored.status_code == 200 and restored.json()['playback']['status'] == 'paused'
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM tutor_playback_event WHERE operation='control:pause' AND playback_id=? AND client_action_id=?",
                                 (playback['playback_id'], heard['agent_turn_id'] + ':pause')).fetchone()[0] == 1


@pytest.mark.parametrize('text,role', [
    ('你有暂停镜头的功能吗？', 'control'),
    ('如果暂停镜头会怎样？', 'control'),
    ('先暂停镜头，然后帮我解答这道题。', 'mixed'),
])
def test_actual_http_nonrequest_does_not_pause_or_score(flow, monkeypatch, text, role):
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    gateway = ControlGateway(text, 'observation.pause' if role == 'control' else None, role=role)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, playback = open_context(client, sid, turn)
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        result = interpret(client, sid, text, 'nonrequest', voice)
        assert result['action']['type'] not in {'pause_observation', 'control_tutor_playback', 'submit_diagnostic_answer'}
        assert not result.get('requires_control_admission')
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM tutor_playback_checkpoint WHERE playback_id=?', (playback['playback_id'],)).fetchone()[0] == 'playing'
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == 'waiting_answer'


@pytest.mark.parametrize('role,confidence,allowed', [('question', .95, True), ('question', .4, False),
    ('mixed', .95, False), ('unknown', .95, False)])
def test_actual_tutor_question_requires_current_question_role_receipt(flow, monkeypatch, role, confidence, allowed):
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    text = '为什么这一步要这样算，请给我一个提示？'
    class QuestionGateway(ControlGateway):
        async def complete(self, request):
            result = await super().complete(request)
            from dataclasses import replace
            return replace(result, content=decision(text, None, role=role, confidence=confidence))
    gateway = QuestionGateway(text, None, role=role)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, playback = open_context(client, sid, turn)
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        result = interpret(client, sid, text, 'real-question', voice)
        assert len(gateway.requests) == 1
        assert (result['action']['type'] == 'ask_tutor') is allowed, result
        payload = json.loads(gateway.requests[0].messages[-1]['content'])
        assert set(payload) == {'transcript', 'state'}
        assert all(type(v) is bool for v in payload['state'].values())
        wire = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        assert all(v not in wire for v in (sid, voice, prompt['prompt_id'], prompt['item']['prompt'], turn['turn_id']))
        assert not result.get('requires_control_admission')
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM tutor_playback_checkpoint WHERE playback_id=?', (playback['playback_id'],)).fetchone()[0] == 'playing'


def test_target_cancelled_during_semantic_request_never_authorizes_tutor_control(flow, monkeypatch):
    from home_study_agent.tutor_playback import TutorPlaybackService
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    text = '可以暂停讲解吗？'
    gateway = ControlGateway(text, 'tutor.pause')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, playback = open_context(client, sid, turn)
        gateway.hook = lambda: TutorPlaybackService(db).control(sid, **binding(playback),
            command='cancel', client_action_id='cancel-during-interpret')
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'cancel-race', 'cloud_text_allowed': True, 'voice_input_id': voice})
        assert len(gateway.requests) == 1
        if result.status_code == 200:
            value = result.json()
            if value.get('requires_control_admission'):
                admission = client.post(f"/v1/sessions/{sid}/agent-turns/{value['agent_turn_id']}/control-admission")
                assert admission.status_code == 409, admission.text
            else:
                assert value['action']['type'] == 'none', value
        else:
            assert result.status_code == 409, result.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM tutor_playback_checkpoint WHERE playback_id=?', (playback['playback_id'],)).fetchone()[0] == 'cancelled'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('role', ['control', 'question'])
@pytest.mark.parametrize('change', ['same_id_source', 'replace_target'])
def test_late_semantic_result_cannot_borrow_changed_tutor_source(flow, monkeypatch, role, change):
    from home_study_agent.tutor_playback import TutorPlaybackService
    from home_study_agent.repository import StudyRepository
    db, _, sid, turn, board = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    text = '可以暂停讲解吗？' if role == 'control' else '为什么这一步要这样算，请给我一个提示？'
    gateway = ControlGateway(text, 'tutor.pause' if role == 'control' else None, role=role)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, playback = open_context(client, sid, turn)
        replacement = []
        def mutate():
            if change == 'same_id_source':
                # Exact persisted source revision changes while the semantic request is in flight.
                with db.connect() as c:
                    c.execute('UPDATE tutor_turn SET answer=? WHERE turn_id=?', ('Revised explanation', turn['turn_id']))
                    c.commit()
            else:
                service = TutorPlaybackService(db)
                service.control(sid, **binding(playback), command='cancel', client_action_id='switch-old-playback')
                new_turn = StudyRepository(db).save_tutor_turn(sid, None, 'math', 'Different question',
                    'Different explanation', 'test', 'test', 'replacement-tutor',
                    output_envelope={'result': {'blackboard': board}})
                new = service.begin(sid, turn_id=new_turn['turn_id'], client_action_id='new-playback')
                replacement.append(service.control(sid, **binding(new), command='resume', client_action_id='new-playback-resume'))
        gateway.hook = mutate
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'late-source', 'cloud_text_allowed': True, 'voice_input_id': voice})
        assert len(gateway.requests) == 1
        if response.status_code == 200:
            assert response.json()['action']['type'] == 'none', response.text
        else:
            assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM tutor_playback_event WHERE operation='control:pause'").fetchone()[0] == 0
            if replacement:
                assert c.execute('SELECT status FROM tutor_playback_checkpoint WHERE playback_id=?',
                                 (replacement[0]['playback_id'],)).fetchone()[0] == 'playing'


def finish_segments(client, sid, state, prefix):
    for segment in state['presentation']['segments']:
        for event in ('started', 'ended'):
            result = client.post(f'/v1/sessions/{sid}/tutor-playback/segment', json={
                **binding(state), 'segment_id': segment['segment_id'], 'event': event,
                'client_action_id': f"{prefix}:{segment['segment_id']}:{event}"})
            assert result.status_code == 200, result.text
            state = result.json()['playback']
    assert state['status'] == 'playback_finished'
    return state


def test_finished_playback_accepts_explicit_restart_and_completed_command_replays_readonly(flow, monkeypatch):
    db, _, sid, turn, _ = flow
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR', str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    text = '可以从头再讲一遍吗？'
    gateway = ControlGateway(text, 'tutor.restart')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt, state = open_context(client, sid, turn)
        finished = finish_segments(client, sid, state, 'first-delivery')
        voice = capture(client, settings, monkeypatch, sid, text, prompt)
        heard = interpret(client, sid, text, 'restart-finished', voice)
        assert heard['action']['type'] == 'control_tutor_playback', heard
        assert client.post(f"/v1/sessions/{sid}/agent-turns/{heard['agent_turn_id']}/control-admission").status_code == 200
        response = client.post(f'/v1/sessions/{sid}/tutor-playback/control', json={
            **binding(finished), 'command': 'restart', 'client_action_id': heard['agent_turn_id'] + ':restart'})
        assert response.status_code == 200, response.text
        restarted = response.json()['playback']
        output = {k: restarted[k] for k in ('playback_id', 'status', 'segment_index', 'evidence_kind')}
        outcome = client.put(f"/v1/agent-turns/{heard['agent_turn_id']}/outcome", json={
            'tool_status': 'completed', 'final_response': '已回到原讲解开头。', 'result': receipt('tutor.playback_control', output)})
        assert outcome.status_code == 200, outcome.text
        resumed = client.post(f'/v1/sessions/{sid}/tutor-playback/control', json={
            **binding(restarted), 'command': 'resume', 'client_action_id': 'render-restarted'})
        assert resumed.status_code == 200, resumed.text
        finish_segments(client, sid, resumed.json()['playback'], 'second-delivery')
        with db.connect() as c:
            event_count = c.execute('SELECT COUNT(*) FROM tutor_playback_event').fetchone()[0]
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, text, 'restart-finished', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert replay['tool_status'] == 'completed'
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_playback_event').fetchone()[0] == event_count
            assert c.execute('SELECT status FROM tutor_playback_checkpoint WHERE playback_id=?', (finished['playback_id'],)).fetchone()[0] == 'playback_finished'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
