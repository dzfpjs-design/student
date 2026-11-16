"""Disposable HTTP/database recovery; synthetic speech, no paid model or device."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_system_facts import LearningSystemFacts
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, http, start, CHILD, OTHER


def saved(db, sid, key, body='private synthetic original', completed=True):
    service = VoiceCaptureBindingService(db)
    service.begin_realtime(sid, key, 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
    return service.complete_realtime(sid, key, body) if completed else None


def facts(db, sid):
    return LearningSystemFacts(db).inspect(CHILD, sid, 'requests', 'all')['facts']


def test_saved_prepared_cancelled_and_corrupt_admissions_do_not_grant_replay(http):
    client, db, sid = http
    saved(db, sid, 'stored-original')
    saved(db, sid, 'prepared-original', completed=False)
    cancelled = saved(db, sid, 'cancelled-original')
    client.post(f'/v1/sessions/{sid}/asr/actions/cancelled-original/cancel')
    corrupt = saved(db, sid, 'corrupt-original')
    with db.connect() as c:
        c.execute("UPDATE voice_transcription_action SET child_id=? WHERE client_action_id='corrupt-original'", (OTHER,))
        c.commit()
    result = facts(db, sid)
    states = {row['client_action_id']: row['status'] for row in result['requests']}
    assert states == {'stored-original': 'saved_unconfirmed', 'prepared-original': 'prepared',
                      'cancelled-original': 'cancelled', 'corrupt-original': 'unconfirmed'}
    assert result['replay_allowed'] is False and result['mastery_update_allowed'] is False
    assert result['cloud_transmission_allowed'] is False
    assert 'private synthetic original' not in json.dumps(result)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (corrupt['voice_input_id'],)).fetchone()[0]
        assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (cancelled['voice_input_id'],)).fetchone()[0]


def test_request_read_is_scoped_bounded_and_does_not_change_database(http):
    client, db, sid = http
    for index in range(12):
        saved(db, sid, f'original-input-{index:02d}')
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        c.commit()
    other_sid = start(client, OTHER, 'other-session')
    service = VoiceCaptureBindingService(db)
    service.begin_realtime(other_sid, 'other-private-input', 'agent_command', {'child_id': OTHER}, model_version='synthetic-asr')
    service.complete_realtime(other_sid, 'other-private-input', 'other child private')
    before = db.path.read_bytes()
    result = facts(db, sid)
    assert len(result['requests']) == 8 and result['has_more'] is True
    assert result == facts(db, sid)
    assert db.path.read_bytes() == before
    assert 'other-private-input' not in json.dumps(result)
    with pytest.raises(ValueError, match='OWNER_MISMATCH'):
        LearningSystemFacts(db).inspect(OTHER, sid, 'requests', 'all')


@pytest.mark.parametrize('utterance', ['刚才我让你做的事情到底处理到哪了呀', '换了语音以后，你收到我前面说的话了吗'])
def test_semantic_registered_query_reads_real_receipts_after_restart(environment, utterance):
    settings, db = environment
    planner = Planner('system.inspect_learning', {'scope': 'requests', 'track': 'all'}, utterance,
                      role='question', objective_type='execution')
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        original = saved(db, sid, 'stored-before-restart', 'private original about a paper answer')
    with TestClient(create_app(settings, model_gateway=planner)) as restarted:
        response = restarted.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': utterance, 'client_action_id': 'semantic-status-check', 'cloud_text_allowed': True})
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn['action']['type'] == 'inspect_learning_system'
        execution = restarted.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert execution.status_code == 200, execution.text
        value = execution.json()['execution']
        assert value['result']['requests'][0]['client_action_id'] == 'stored-before-restart'
        assert value['result']['requests'][0]['status'] == 'saved_unconfirmed'
        assert len(value['result']['requests']) == 1
        assert '不能' in value['response_text'] and '未确认' in value['response_text']
        assert value['result']['replay_allowed'] is False
    with TestClient(create_app(settings, model_gateway=planner)) as again:
        repeat = again.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()['execution']['result'] == value['result']
    assert len(planner.requests) == 1
    assert 'private original about a paper answer' not in str(planner.requests)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (original['voice_input_id'],)).fetchone()[0] == 'private original about a paper answer'


def test_known_arrival_without_a_turn_is_received_not_claimed_running(http):
    _client, db, sid = http
    original = saved(db, sid, 'unknown-runtime-original')
    with db.connect() as c:
        c.execute('''INSERT INTO learning_input_arrival(arrival_id,child_id,session_id,client_action_id,
            utterance,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?)''',
            ('synthetic-arrival', CHILD, sid, 'realtime-' + original['voice_input_id'],
             'private synthetic original', '{}', '2026-10-03T00:00:00+00:00'))
        c.commit()
    result = facts(db, sid)
    assert len(result['requests']) == 1
    assert result['requests'][0]['status'] == 'received'
    assert '不代表它仍在运行' in result['requests'][0]['response_text']


def test_completed_conversation_has_one_receipt_and_status_queries_do_not_become_work(environment):
    settings, db = environment
    original_text = 'private synthetic original'
    planner = Planner('conversation.respond', {'response': '我们继续聊。'}, original_text,
                      role='question', objective_type='companion')
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        original = saved(db, sid, 'completed-original', original_text)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': original_text,
            'voice_input_id': original['voice_input_id'], 'client_action_id': 'non-default-agent-key', 'cloud_text_allowed': True})
        assert response.status_code == 200, response.text
        turn = response.json()
        assert client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute").status_code == 200
        result = facts(db, sid)
        assert len(result['requests']) == 1 and result['requests'][0]['status'] == 'recorded_completed'
        assert '没有执行学习或设备操作' in result['requests'][0]['response_text']
        query = '那刚才说的事情处理了吗'
        planner.payload = Planner('system.inspect_learning', {'scope': 'requests', 'track': 'all'}, query,
                                  role='question', objective_type='execution').payload
        checked = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': query,
            'client_action_id': 'canonical-status-request', 'cloud_text_allowed': True}).json()
        assert checked['action']['type'] == 'inspect_learning_system'
        assert client.post(f"/v1/agent-turns/{checked['agent_turn_id']}/execute").status_code == 200
        assert facts(db, sid) == result


def test_modified_runtime_transcript_cannot_prove_the_original_input_completed(environment):
    settings, db = environment
    text = 'private synthetic original'
    planner = Planner('conversation.respond', {'response': '我们继续聊。'}, text,
                      role='question', objective_type='companion')
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        original = saved(db, sid, 'altered-runtime-original', text)
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': text,
            'voice_input_id': original['voice_input_id'], 'client_action_id': 'realtime-' + original['voice_input_id'],
            'cloud_text_allowed': True}).json()
        assert client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            c.execute('UPDATE agent_turn SET transcript=? WHERE agent_turn_id=?', ('different original', turn['agent_turn_id']))
            c.commit()
        result = facts(db, sid)
        assert len(result['requests']) == 1
        assert result['requests'][0]['status'] == 'unconfirmed'
        assert '不能确认完成' in result['requests'][0]['response_text']


def test_status_query_during_a_bound_exam_uses_content_free_control_not_answer_scoring(environment):
    from test_content_free_voice_control_http import ControlGateway
    from test_answer_same_turn_correction import open_exam
    settings, db = environment
    quote = '换了语音以后，你收到我前面说的话了吗'
    gateway = ControlGateway(quote, 'system.inspect_learning')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        saved(db, sid, 'unsent-exam-input')
        service = VoiceCaptureBindingService(db)
        binding = {'kind': 'exam', 'binding': run['answer_binding']}
        service.begin_realtime(sid, 'protected-status-input', 'agent_command',
            {'child_id': CHILD, 'question_reference': binding}, model_version='synthetic-asr')
        current = service.complete_realtime(sid, 'protected-status-input', quote)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': quote,
            'voice_input_id': current['voice_input_id'], 'client_action_id': 'protected-status-turn',
            'cloud_text_allowed': True})
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn['action']['type'] == 'inspect_learning_system'
        assert turn['action']['scope'] == 'requests'
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        rows = executed.json()['execution']['result']['requests']
        assert len(rows) == 1 and rows[0]['client_action_id'] == 'unsent-exam-input'
        assert rows[0]['status'] == 'saved_unconfirmed'
        assert len(gateway.requests) == 1 and gateway.requests[0].request_type == 'voice_interaction'
        payload = json.loads(gateway.requests[0].messages[-1]['content'])
        assert set(payload) == {'transcript', 'state'}
        assert payload['transcript'] == quote and all(type(x) is bool for x in payload['state'].values())
        serialized = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        for forbidden in (sid, run['run_id'], run['current_item']['prompt'], 'private synthetic original', current['voice_input_id']):
            assert forbidden not in serialized
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?', (run['run_id'],)).fetchone()[0] == 'active'


def test_status_query_during_pending_evaluation_does_not_resubmit_the_answer(tmp_path):
    from test_pending_evaluation_control import host, ask, facts as evaluation_facts
    with host(tmp_path, control='system.inspect_learning') as (db, sid, client, requests):
        before = evaluation_facts(db)
        response = ask(client, sid, '刚才的话处理到哪了，我不用再答一遍吧')
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn['action']['type'] == 'inspect_learning_system'
        assert turn['action']['scope'] == 'requests' and turn['after_execution'] == 'stop'
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert executed.json()['execution']['result']['replay_allowed'] is False
        assert evaluation_facts(db) == before and len(requests) == 1
