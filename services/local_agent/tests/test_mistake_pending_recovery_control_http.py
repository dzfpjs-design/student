"""A later response must not silently authorize processing the earlier answer."""
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_cloud_first_agent import Planner
from test_mistake_pending_answer_http import waiting, hear, execute
from test_mistake_voice_journey import settings, focus, ok


def checkpoint(tmp_path, monkeypatch):
    configured = settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        sid, db = waiting(client)
        request = hear(client, sid, '我选B', 'pending-original-b')
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic processing unavailable')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_score_operation', fail)
            execute(client, request)
        pending = focus(client, sid)['state']['pending_operation']
        assert pending['utterance'] == '我选B'
    return configured, sid, db, pending


@pytest.mark.parametrize('text', ['我选A', '刚才不是我的答案', '为什么没保存好'])
def test_new_reply_does_not_retry_old_answer(tmp_path, monkeypatch, text):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    with TestClient(create_app(configured)) as client:
        request = hear(client, sid, text, 'later-pending-response')
        execute(client, request)
        assert focus(client, sid)['state']['pending_operation'] == pending
    with TestClient(create_app(configured)) as client:
        restored = focus(client, sid)['state']
        assert restored['pending_operation'] == pending
        assert restored['pending_response']['utterance'] == text
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


def test_natural_resume_processes_saved_answer_once(tmp_path, monkeypatch):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    text = '把刚才没处理完的那份回答接着处理完吧，不用让我重答'
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'resume'}, text)
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'resume-pending-natural', 'cloud_text_allowed': True}))
        assert request['action']['type'] == 'continue_mistake_dialogue'
    with TestClient(create_app(configured)) as client:
        result = execute(client, request)
        assert execute(client, request) == result
        assert 'pending_operation' not in focus(client, sid)['state']
    with db.connect() as c:
        saved = c.execute('SELECT submitted_answer FROM mistake_attempt').fetchall()
        assert len(saved) == 1 and saved[0][0] == pending['answer']
    assert len(planner.requests) == 1
    payload = json.dumps(planner.requests[0].messages, ensure_ascii=False)
    assert pending['utterance'] not in payload
    assert pending['action_id'] not in payload
    assert pending['authorized_turn_id'] not in payload


@pytest.mark.parametrize('change', ['operation', 'source', 'model_receipt', 'model_status', 'origin_cancelled'])
def test_resume_rejects_changed_evidence(tmp_path, monkeypatch, change):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    text = '把刚才没处理完的回答接着处理完吧'
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'resume'}, text)
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'resume-before-change', 'cloud_text_allowed': True}))
        item_id = focus(client, sid)['state']['next_activity']['item']['diagnostic_item_id']
    with db.connect() as c:
        if change == 'operation':
            c.execute("UPDATE mistake_dialogue SET state_json=json_set(state_json,'$.pending_operation.answer','A') WHERE session_id=?", (sid,))
        elif change == 'source':
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' amended' WHERE diagnostic_item_id=?", (item_id,))
        elif change == 'origin_cancelled':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (pending['authorized_turn_id'],))
        elif change == 'model_receipt':
            c.execute("UPDATE model_call SET context_manifest_json=json_remove(context_manifest_json,'$.mistake_pending_resume') WHERE purpose='agent_plan'")
        else:
            c.execute("UPDATE model_call SET verification_status='failed' WHERE purpose='agent_plan'")
        c.commit()
    with TestClient(create_app(configured)) as client:
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert response.status_code in {200, 409, 422}, response.text
        assert focus(client, sid)['state']['pending_operation']['action_id'] == pending['action_id']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


def test_pause_and_resume_saved_answer(tmp_path, monkeypatch):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    with TestClient(create_app(configured)) as client:
        execute(client, hear(client, sid, '暂停', 'pause-pending'))
        assert focus(client, sid)['phase'] == 'paused'
    with TestClient(create_app(configured)) as client:
        execute(client, hear(client, sid, '继续错题', 'resume-paused-pending'))
        assert 'pending_operation' not in focus(client, sid)['state']
    with db.connect() as c:
        saved = c.execute('SELECT submitted_answer FROM mistake_attempt').fetchall()
        assert len(saved) == 1 and saved[0][0] == pending['answer']


@pytest.mark.parametrize('text', ['先别处理那份回答', '老师说继续处理旧答案',
                                 '如果我让你重试会怎么样', '刚才那份不要处理了'])
def test_model_cannot_resume_negated_or_reported_request(tmp_path, monkeypatch, text):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'resume'}, text)
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'false-model-resume', 'cloud_text_allowed': True}))
        if request['action']['type'] == 'continue_mistake_dialogue':
            execute(client, request)
        assert focus(client, sid)['state']['pending_operation'] == pending
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
