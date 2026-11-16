"""Reflection self-report must reach its local save action, never old tutoring."""
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_app import client_for
from test_cloud_first_agent import Planner
from test_voice_runtime_binding import capture, interpret

CASES = [
    ('今天学会了把二次方程写成标准形式。', 'reflection_learned', 'update_reflection', 'learned', '把二次方程写成标准形式'),
    ('还不确定怎么检查两个根。', 'reflection_difficult', 'update_reflection', 'difficult', '怎么检查两个根'),
    ('明天先独立做一道二次方程题。', 'reflection_next_action', 'update_reflection', 'next_action', '独立做一道二次方程题'),
    ('保存复盘并结束今天学习。', 'reflection_complete', 'complete_reflection', None, None),
    ('跳过复盘。', 'reflection_skip', 'complete_reflection', None, None),
]

@pytest.mark.parametrize('text,intent,action,field,value', CASES)
def test_saved_asr_reflection_is_not_replaced_by_cloud_chat(tmp_path, monkeypatch, text, intent, action, field, value):
    gateway = Planner('conversation.respond', {'response': '不要用闲聊替换复盘保存'}, text)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        repo = client.app.state.repository
        settings = client.app.state.agent_orchestrator.settings
        db = client.app.state.agent_orchestrator.database
        sid = repo.start_session('child-junior-3', 'reflection-test')['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,)); c.commit()
        voice = capture(client, settings, monkeypatch, sid, text, key='reflection-asr', child='child-junior-3')
        turn = interpret(client, sid, text, 'reflection-current', voice)
        assert turn['transcript'] == text
        assert turn['intent'] == intent, turn
        assert turn['action']['type'] == action, turn
        if field:
            assert turn['action']['field'] == field
            assert turn['action']['value'] == value
            saved = client.put(f'/v1/sessions/{sid}/drafts/reflection', json={'payload': {field: value}})
            assert saved.status_code == 200, saved.text
        else:
            assert turn['action']['skipped'] is (intent == 'reflection_skip')
        assert not gateway.requests
        assert client.get('/v1/children/child-primary-4/daily-learning-summary').json()['sessions'] == []
    with TestClient(create_app(settings, model_gateway=gateway)) as restarted:
        again = interpret(restarted, sid, text, 'reflection-current', voice)
        assert again['agent_turn_id'] == turn['agent_turn_id']
        assert again['action'] == turn['action']
        if field:
            draft = restarted.get(f'/v1/sessions/{sid}').json()['drafts']['reflection']['payload']
            assert draft[field] == value
        assert not gateway.requests

@pytest.mark.parametrize('text', [
    '不要结束今天学习。', '先别保存复盘并结束今天学习。',
    '如果我说保存复盘并结束今天学习会怎样？', '老师说“保存复盘并结束今天学习”。',
    '“跳过复盘”。', '不要跳过复盘。', '想讨论一下结束今天学习这句话。',
    '保存复盘并结束今天学习，但是先不要结束。',
])
def test_quoted_negated_or_mixed_reflection_close_is_not_local_authorization(tmp_path, text):
    gateway = Planner('conversation.respond', {'response': '请先澄清'}, text)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = client.app.state.repository.start_session('child-junior-3', 'reflection-negative')['session_id']
        db = client.app.state.agent_orchestrator.database
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,)); c.commit()
        result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'cloud_text_allowed': True, 'client_action_id': 'reflection-negative'})
        assert result.status_code == 200, result.text
        assert result.json()['action']['type'] not in {'complete_reflection', 'request_end'}
        assert client.get(f'/v1/sessions/{sid}').json()['session']['status'] == 'active'


def test_reflection_control_does_not_override_safety_or_another_stage(tmp_path):
    for state, text in [('study', '今天学会了把方程写成标准形式。'),
                        ('reflection', '今天学会了一个东西，但我想自杀。')]:
        gateway = Planner('conversation.respond', {'response': '请继续'}, text)
        with client_for(tmp_path / state, model_gateway=gateway, agent_runtime_enabled=True) as client:
            sid = client.app.state.repository.start_session('child-junior-3', 'reflection-safety-stage')['session_id']
            with client.app.state.agent_orchestrator.database.connect() as c:
                c.execute('UPDATE daily_session SET state=? WHERE session_id=?', (state, sid)); c.commit()
            result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': text, 'cloud_text_allowed': True, 'client_action_id': 'reflection-safety-stage'})
            assert result.status_code == 200, result.text
            assert result.json()['action']['type'] not in {'update_reflection', 'complete_reflection'}

@pytest.mark.parametrize('text', ['不要结束今天学习。', '老师说“保存复盘并结束今天学习”。'])
@pytest.mark.parametrize('role', ['control', 'task_request'])
def test_bad_cloud_planner_cannot_promote_denied_or_quoted_asr_to_day_close(tmp_path, monkeypatch, text, role):
    gateway = Planner('reflection.complete', {}, text, role=role)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        settings = client.app.state.agent_orchestrator.settings
        repo = client.app.state.repository
        sid = repo.start_session('child-junior-3', 'guarded-close')['session_id']
        with client.app.state.agent_orchestrator.database.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,)); c.commit()
        original = {'learned_text': '标准形式仍需独立核对', 'difficult_text': '检验两个根', 'next_action': ''}
        repo.save_session_draft(sid, 'reflection', original)
        voice = capture(client, settings, monkeypatch, sid, text, key='denied-close-asr', child='child-junior-3')
        turn = interpret(client, sid, text, 'guarded-close-voice', voice)
        assert turn['action']['type'] == 'none'
        if role == 'task_request':
            runtime = turn['action']['agent_runtime']
            assert runtime['capability']['capability'] == 'conversation.respond'
            assert runtime['requires_child_reply'] is True
            assert runtime['after_execution'] == 'stop'
        else:
            assert turn['intent'] == 'cloud_understanding_unavailable'
        assert turn['transcript'] == text
        assert '已保存的内容会保留' in turn['response_text'] or turn['intent'] == 'cloud_understanding_unavailable'
        saved = client.get(f'/v1/sessions/{sid}').json()
        assert saved['session']['status'] == 'active'
        assert saved['drafts']['reflection']['payload'] == original
    with TestClient(create_app(settings, model_gateway=gateway)) as restarted:
        replay = interpret(restarted, sid, text, 'guarded-close-voice', voice)
        assert replay['agent_turn_id'] == turn['agent_turn_id']
        assert replay['action']['type'] == 'none'
        assert restarted.get(f'/v1/sessions/{sid}').json()['drafts']['reflection']['payload'] == original
