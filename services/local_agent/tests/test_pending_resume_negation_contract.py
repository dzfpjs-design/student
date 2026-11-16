"""Denial vetoes protect saved work; passing a veto is never write permission."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_pending_answer import permits_resume, authorized
from test_cloud_first_agent import Planner
from test_mistake_pending_answer_http import execute
from test_mistake_pending_recovery_control_http import checkpoint
from test_mistake_voice_journey import focus, ok


@pytest.mark.parametrize('text', [
    '先不处理刚才的回答', '先别，处理刚才的回答', '不要，继续处理旧答案',
    '先不，继续刚才的回答', '不继续处理旧答案', '不，继续处理旧答案',
    '暂时不处理刚才的答案', '先不要处理我刚才说的、还没来得及检查的那份旧答案',
    '不用让我重答，但不要继续处理旧答案',
    '不要继续处理旧答案，不用让我重答',
    '不用让我重答，先不处理', '老师说不用让我重答，把原来的回答接着处理完',
    '如果不用让我重答，把原来的回答接着处理完会怎样',
    '“不用让我重答，把原来的回答接着处理完”是什么意思',
    '取消刚才的回答', '撤回刚才的答案',
])
def test_denial_is_not_bypassed_by_hesitation_or_non_answer_clause(text):
    assert not permits_resume(text)


@pytest.mark.parametrize('text', [
    '不用让我重答，把原来的回答接着处理完',
    '不需要我重新作答，继续处理已经保存的答案',
    '不要让我再答一遍，继续处理原来的回答',
    '把原来的回答接着处理完，不用让我重答',
    '把刚才那份回答继续处理完吧',
])
def test_declining_a_new_answer_does_not_deny_saved_answer_processing(text):
    assert permits_resume(text)
    assert not authorized(None, {}, text, None)


@pytest.mark.parametrize('text', [
    '不用让我重答，把原来的回答接着处理完',
    '不需要我重新作答，继续处理已经保存的答案',
])
def test_resume_after_restart_needs_receipt_and_scores_original_once(tmp_path, monkeypatch, text):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'resume'}, text)
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'scope-aware-resume', 'cloud_text_allowed': True}))
        assert request['action']['type'] == 'continue_mistake_dialogue'
    with TestClient(create_app(configured)) as client:
        result = execute(client, request)
        assert execute(client, request) == result
        assert 'pending_operation' not in focus(client, sid)['state']
    with db.connect() as c:
        answers = c.execute('SELECT submitted_answer FROM mistake_attempt').fetchall()
        assert len(answers) == 1 and answers[0][0] == pending['answer']


@pytest.mark.parametrize('text', ['先不处理刚才的回答', '不要，继续处理旧答案',
                                  '不用让我重答，先不处理'])
def test_false_model_resume_cannot_score_denied_answer(tmp_path, monkeypatch, text):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'resume'}, text)
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'denied-scope-resume', 'cloud_text_allowed': True}))
        if request['action']['type'] == 'continue_mistake_dialogue':
            execute(client, request)
    with TestClient(create_app(configured)) as client:
        assert focus(client, sid)['state']['pending_operation'] == pending
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
