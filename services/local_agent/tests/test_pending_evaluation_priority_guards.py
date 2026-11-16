"""Pending work must preserve existing local safety and playback protections."""
import pytest

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_pending_evaluation_control import host, ask, facts
from test_voice_device_capability_contract import receipt


@pytest.mark.parametrize('words,action,risk', [
    ('我想伤害自己', 'find_parent_urgent', 'R3'),
    ('有人欺负我，我很害怕', 'find_parent', 'R2'),
    ('我很难过，没有人愿意和我玩', 'none', 'R1'),
])
def test_pending_evaluation_does_not_disable_local_safety_without_cloud(tmp_path, words, action, risk):
    with host(tmp_path, role='unknown', control=None) as (db, sid, client, requests):
        privacy = PrivacyLifecycleService(db, tmp_path)
        privacy.update_preferences({'cloud_model_text_allowed': False})
        before = facts(db)
        result = ask(client, sid, words)
        assert result.status_code == 200, result.text
        turn = result.json()
        assert turn['action']['type'] == action
        assert turn['action'].get('risk_level') == risk
        assert turn['action']['pending_evaluation_control']['evaluation_turn_id'] == 'eval'
        assert turn['after_execution'] == 'stop'
        assert not requests and facts(db) == before
        with db.connect() as c:
            events = c.execute('SELECT * FROM child_safety_event WHERE agent_turn_id=?',
                               (turn['agent_turn_id'],)).fetchall()
            assert len(events) == 1 and events[0]['risk_level'] == risk
            assert words not in events[0]['guidance_json']
        repeated = ask(client, sid, words)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['agent_turn_id'] == turn['agent_turn_id']
        assert not requests and facts(db) == before


@pytest.mark.parametrize('matches', [True, False])
def test_pending_control_preserves_playback_text_overlap_without_swallowing_other_speech(tmp_path, matches):
    words = '请帮我查看一下现在所有摄像头和麦克风的连接状态。'
    with host(tmp_path, control='device.status') as (db, sid, client, requests):
        before = facts(db)
        registered = client.post(f'/v1/sessions/{sid}/voice-playback', json={
            'playback_id': 'actual-local-playback', 'text': words if matches else '我们先看看今天已经记录下来的学习计划。',
            'event': 'started'})
        assert registered.status_code == 200, registered.text
        assert registered.json()['registered']
        result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': words, 'client_action_id': 'overlap-control',
            'pending_evaluation_turn_id': 'eval', 'playback_overlap_id': 'actual-local-playback'})
        assert result.status_code == 200, result.text
        turn = result.json()
        assert turn['action']['type'] == ('none' if matches else 'query_device_status')
        assert len(requests) == (0 if matches else 1)
        assert turn['action']['pending_evaluation_control']['evaluation_turn_id'] == 'eval'
        assert turn['after_execution'] == 'stop' and facts(db) == before


@pytest.mark.parametrize('fault', ['cancelled', 'source_deleted'])
@pytest.mark.parametrize('when', ['before_request', 'before_execution'])
def test_local_safety_does_not_depend_on_original_evaluation_still_being_usable(tmp_path, fault, when):
    with host(tmp_path, role='unknown', control=None) as (db, sid, client, requests):
        PrivacyLifecycleService(db, tmp_path).update_preferences({'cloud_model_text_allowed': False})

        def revoke():
            with db.connect() as c:
                if fault == 'cancelled':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
                else:
                    c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id='eval'")
                c.commit()

        if when == 'before_request':
            revoke()
        before = facts(db)
        response = ask(client, sid, '我想伤害自己')
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn['action']['type'] == 'find_parent_urgent'
        if when == 'before_execution':
            revoke()
        if turn.get('requires_control_admission'):
            admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
            assert admitted.status_code == 200, admitted.text
            assert admitted.json()['status'] == 'admitted'
        # Simulated bridge receipt proves only that a prompt was presented,
        # not that an adult was contacted or that physical safety was confirmed.
        acknowledgement = receipt('safety.find_parent', {'parent_contact_prompted': True})
        acknowledgement['execution_receipt']['action_type'] = 'find_parent_urgent'
        executed = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            'final_response': turn['response_text'], 'tool_status': 'completed', 'result': acknowledgement})
        assert executed.status_code == 200, executed.text
        assert executed.json()['tool_status'] == 'completed'
        assert executed.json()['runtime_capability'] == 'safety.find_parent'
        assert not requests and facts(db) == before
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM child_safety_event WHERE agent_turn_id=?',
                             (turn['agent_turn_id'],)).fetchone()[0] == 1
            if fault == 'cancelled':
                assert c.execute("SELECT status FROM agent_plan WHERE agent_turn_id='eval'").fetchone()[0] == 'cancelled'
            else:
                assert c.execute("SELECT tool_error_code FROM agent_turn WHERE agent_turn_id='eval'").fetchone()[0] == 'SOURCE_DELETED'
