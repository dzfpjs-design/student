"""Mistake verification must distinguish a new request from an answer."""
from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_voice_journey import settings, fixture, begin, say, focus, ok


def waiting(client):
    sid, db = fixture(client)
    begin(client, sid)
    for index, text in enumerate(('第四题', '对', '我选B', '是的', '我是看拼写选的，不知道比较读音的方法', '明白了')):
        say(client, sid, text, f'prepare-mistake-{index}')
    assert focus(client, sid)['phase'] == 'attempt'
    return sid, db


def hear(client, sid, text, key):
    return ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': text, 'client_action_id': key}))


def execute(client, turn):
    return ok(client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute"))


def test_unknown_request_and_question_do_not_write_mistake_attempts(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        sid, db = waiting(client)
        original = focus(client, sid)
        with db.connect() as c:
            before = [tuple(row) for row in c.execute('SELECT * FROM mastery_state ORDER BY rowid')]
        for index, text in enumerate(('昨天漏掉的那份今天能不能补上', '题目是在让我们找读音不同的那个吗', '那一份先放到后天再处理')):
            heard = hear(client, sid, text, f'not-a-mistake-answer-{index}')
            assert heard['action']['type'] == 'none'
            assert heard['action']['source'] == 'pending_answer_clarification'
            assert heard['action']['local_only'] and not heard['action']['cloud_transmission_allowed']
            execute(client, heard)
            assert focus(client, sid)['state'] == original['state']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM mistake_attempt').fetchone()[0] == 0
            assert [tuple(row) for row in c.execute('SELECT * FROM mastery_state ORDER BY rowid')] == before
        short = hear(client, sid, '我选A', 'real-short-mistake-answer')
        assert short['action']['type'] == 'continue_mistake_dialogue'
        execute(client, short)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM mistake_attempt').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['none', 'pause', 'source'])
def test_original_mistake_answer_confirmation_recovers_only_same_source(tmp_path, change):
    answer = 'bag /æ/ 不同，cake name /eɪ/'
    with TestClient(create_app(settings(tmp_path))) as client:
        sid, db = waiting(client)
        current = focus(client, sid)
        candidate = hear(client, sid, answer, 'mistake-answer-candidate')
        assert candidate['intent'] == 'pending_answer_clarification'
        execute(client, candidate)
        if change == 'pause':
            MistakeDialogueService(db).respond(sid, '跳过', 'external-pause')
        elif change == 'source':
            item = current['state']['next_activity']['item']['diagnostic_item_id']
            with db.connect() as c:
                c.execute("UPDATE diagnostic_item SET prompt=prompt || ' amended' WHERE diagnostic_item_id=?", (item,))
                c.commit()
    with TestClient(create_app(settings(tmp_path))) as client:
        confirmed = hear(client, sid, '刚才那句就是我的答案', 'mistake-answer-confirmation')
        if change == 'none':
            assert confirmed['action']['type'] == 'continue_mistake_dialogue'
            assert confirmed['action']['utterance'] == answer
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert AgentRuntimeStore.resolve_local_answer_submission(
                    c, confirmed['agent_turn_id'], 'mistake.dialogue')['answer'] == answer
                c.rollback()
            execute(client, confirmed)
            replay = hear(client, sid, '刚才那句就是我的答案', 'mistake-answer-confirmation')
            execute(client, replay)
        else:
            assert confirmed['action']['type'] != 'continue_mistake_dialogue'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM mistake_attempt').fetchone()[0] == (1 if change == 'none' else 0)


@pytest.mark.parametrize('cancel', [False, True])
def test_real_dispatcher_resumes_saved_answer_without_reauthorizing_cancelled_origin(tmp_path, monkeypatch, cancel):
    original_score = MistakeDialogueService._score_operation
    answer = 'bag /æ/ 不同，cake name /eɪ/'
    with TestClient(create_app(settings(tmp_path))) as client:
        sid, db = waiting(client)
        candidate = hear(client, sid, answer, 'recoverable-mistake-candidate')
        execute(client, candidate)
        confirmed = hear(client, sid, '刚才那句就是我的答案', 'recoverable-mistake-confirmation')
        def fail_score(*args, **kwargs):
            raise RuntimeError('synthetic unavailable score storage')
        monkeypatch.setattr(MistakeDialogueService, '_score_operation', fail_score)
        execute(client, confirmed)
        monkeypatch.setattr(MistakeDialogueService, '_score_operation', original_score)
        pending = focus(client, sid)['state']['pending_operation']
        assert pending['authorized_turn_id'] == confirmed['agent_turn_id']
        assert pending['utterance'] == answer
        if cancel:
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (confirmed['agent_turn_id'],))
                c.commit()
    with TestClient(create_app(settings(tmp_path)), raise_server_exceptions=False) as client:
        retry = hear(client, sid, '重试', 'new-mistake-retry-turn')
        assert retry['action']['type'] == 'continue_mistake_dialogue'
        result = client.post(f"/v1/agent-turns/{retry['agent_turn_id']}/execute")
        assert result.status_code == (409 if cancel else 200), result.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM mistake_attempt').fetchone()[0] == (0 if cancel else 1)
        if cancel:
            assert focus(client, sid)['state']['pending_operation']['authorized_turn_id'] == confirmed['agent_turn_id']
        else:
            assert 'pending_operation' not in focus(client, sid)['state']
