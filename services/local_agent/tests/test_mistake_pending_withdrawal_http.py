"""Withdraw a saved answer through the real dispatcher, never silently replace it."""
import base64
import copy
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import InvalidAgentPlanError, AgentRuntimeStore
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.mistake_pending_answer import permits_control
from home_study_agent.local_media_ai import LocalMediaAI
from test_cloud_first_agent import Planner
from test_mistake_pending_recovery_control_http import checkpoint
from test_mistake_pending_answer_http import hear, execute, waiting
from test_mistake_voice_journey import focus, ok, settings
from test_pet_media_evidence import wav_file


def withdraw(client, sid, text='撤回刚才的回答', cloud=False, key='withdraw-pending-answer'):
    return ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id': key, 'cloud_text_allowed': cloud}))


@pytest.mark.parametrize('text', ['呃…不要撤回刚才的回答', '那个……先别，撤销',
    '哥哥说不要撤销那份', '哥哥说撤销那份', '不，等一下……撤销', '甭撤销了',
    "don't withdraw the saved answer", 'Do not cancel that saved reply'])
def test_independently_reviewed_synthetic_denials(text):
    assert not permits_control(text, 'withdraw')


@pytest.mark.parametrize('natural', [False, True])
def test_withdraw_restart_late_retry_and_fresh_answer(tmp_path, monkeypatch, natural):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    text = '我不打算用刚才保存的那份回答了，把它撤销掉吧' if natural else '撤回刚才的回答'
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'withdraw'}, text)
    with TestClient(create_app(configured, model_gateway=planner if natural else None)) as client:
        stale = copy.deepcopy(focus(client, sid))
        with db.connect() as c:
            old_target = AgentRuntimeStore.local_mistake_answer_target(c, sid, stale['child_id'],
                authorized_turn_id=pending['authorized_turn_id'])
        request = withdraw(client, sid, text, natural)
        assert request['action']['type'] == 'continue_mistake_dialogue'
        result = execute(client, request)
        assert 'pending_operation' not in focus(client, sid)['state']
        assert execute(client, request) == result
    with TestClient(create_app(configured)) as client:
        assert 'pending_operation' not in focus(client, sid)['state']
        with db.connect() as c:
            target = AgentRuntimeStore.local_mistake_answer_target(c, sid, stale['child_id'])
            assert target['binding']['source_version'] != old_target['binding']['source_version']
            assert target['binding']['item_id'] == old_target['binding']['item_id']
        service = MistakeDialogueService(db)
        with pytest.raises(InvalidAgentPlanError, match='WITHDRAWN'):
            service._score_operation(stale, pending)
        cloned = {**pending, 'action_id': 'different-key-same-withdrawn-origin'}
        with pytest.raises(InvalidAgentPlanError, match='WITHDRAWN'):
            service._checkpoint(stale, cloned)
        old = service.respond(sid, pending['utterance'], pending['action_id'])
        assert old['withdrawn_pending_answer']['operation'] == pending
        execute(client, hear(client, sid, '我选A', 'fresh-answer-after-withdrawal'))
    with db.connect() as c:
        saved = c.execute('SELECT submitted_answer FROM mistake_attempt').fetchall()
        assert len(saved) == 1 and saved[0][0] == 'A'
        event = c.execute('SELECT result_json FROM mistake_dialogue_event WHERE client_action_id=?',
                          (pending['action_id'],)).fetchone()
        assert json.loads(event[0])['withdrawn_pending_answer']['operation'] == pending
    if natural:
        assert len(planner.requests) == 1
        payload = json.dumps(planner.requests[0].messages, ensure_ascii=False)
        assert pending['utterance'] not in payload and pending['action_id'] not in payload


@pytest.mark.parametrize('text', ['不要撤回刚才的回答', '先别，撤销那份回答',
                                  '如果我说撤回刚才的回答呢', '老师说撤回刚才的回答'])
def test_model_cannot_authorize_negated_or_reported_withdrawal(tmp_path, monkeypatch, text):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'withdraw'}, text)
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = withdraw(client, sid, text, True)
        if request['action']['type'] == 'continue_mistake_dialogue':
            execute(client, request)
        assert focus(client, sid)['state']['pending_operation'] == pending
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


def test_changed_operation_does_not_withdraw_newer_answer(tmp_path, monkeypatch):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    with TestClient(create_app(configured)) as client:
        request = withdraw(client, sid)
        with db.connect() as c:
            c.execute("UPDATE mistake_dialogue SET state_json=json_set(state_json,'$.pending_operation.answer','A') WHERE session_id=?", (sid,))
            c.commit()
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert response.status_code in {200, 409, 422}
        assert focus(client, sid)['state']['pending_operation']['answer'] == 'A'
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM mistake_dialogue_event WHERE client_action_id=?', (pending['action_id'],)).fetchone()


def test_withdrawal_audit_write_failure_rolls_back_and_retry_recovers(tmp_path, monkeypatch):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    with db.connect() as c:
        c.execute("""CREATE TRIGGER fail_withdrawal_audit BEFORE INSERT ON mistake_dialogue_event
            WHEN json_extract(NEW.result_json,'$.withdrawn_pending_answer.version')=1
            BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END""")
        c.commit()
    with TestClient(create_app(configured), raise_server_exceptions=False) as client:
        request = withdraw(client, sid)
        client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert focus(client, sid)['state']['pending_operation'] == pending
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM mistake_dialogue_event WHERE client_action_id=?', (pending['action_id'],)).fetchone()
        c.execute('DROP TRIGGER fail_withdrawal_audit')
        c.commit()
    with TestClient(create_app(configured)) as client:
        request = withdraw(client, sid, key='retry-explicit-withdrawal')
        execute(client, request)
        assert 'pending_operation' not in focus(client, sid)['state']


@pytest.mark.parametrize('change', ['model_receipt', 'model_status', 'binding', 'model_owner'])
def test_natural_withdrawal_requires_current_owned_model_receipt(tmp_path, monkeypatch, change):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    text = '把刚才没处理的那份回答作废吧'
    planner = Planner('mistake.dialogue', {'utterance': text, 'pending_answer_control': 'withdraw'}, text)
    with TestClient(create_app(configured, model_gateway=planner)) as client:
        request = withdraw(client, sid, text, True)
        assert request['action']['type'] == 'continue_mistake_dialogue'
        with db.connect() as c:
            if change == 'model_receipt':
                c.execute("UPDATE model_call SET context_manifest_json=json_remove(context_manifest_json,'$.mistake_pending_withdraw') WHERE purpose='agent_plan'")
            elif change == 'model_status':
                c.execute("UPDATE model_call SET verification_status='failed' WHERE purpose='agent_plan'")
            elif change == 'model_owner':
                c.execute("UPDATE model_call SET child_id='child-junior-3' WHERE purpose='agent_plan'")
            else:
                c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.pending_answer_operation.operation_id','other-child-operation') WHERE agent_turn_id=?", (request['agent_turn_id'],))
            c.commit()
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert response.status_code in {200, 409, 422}
        assert focus(client, sid)['state']['pending_operation'] == pending
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
        assert not c.execute('SELECT 1 FROM mistake_dialogue_event WHERE client_action_id=?',
                             (pending['action_id'],)).fetchone()


def test_withdraw_paused_answer_keeps_pause_until_fresh_resume(tmp_path, monkeypatch):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    with TestClient(create_app(configured)) as client:
        execute(client, hear(client, sid, '暂停', 'pause-before-withdrawal'))
        assert focus(client, sid)['phase'] == 'paused'
        execute(client, withdraw(client, sid))
        current = focus(client, sid)
        assert current['phase'] == 'paused' and 'pending_operation' not in current['state']
    with TestClient(create_app(configured)) as client:
        execute(client, hear(client, sid, '继续错题', 'resume-after-withdrawal'))
        execute(client, hear(client, sid, '我选A', 'answer-after-paused-withdrawal'))
    with db.connect() as c:
        answers = c.execute('SELECT submitted_answer FROM mistake_attempt').fetchall()
        assert len(answers) == 1 and answers[0][0] == 'A'


def test_withdrawal_rejects_prewithdrawal_audio_but_accepts_new_capture(tmp_path, monkeypatch):
    configured = settings(tmp_path)
    transcript = ['我选B']
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav',
        lambda *args, **kwargs: (transcript[0], 1000, 'synthetic-recognition-port', False))
    audio = tmp_path / 'synthetic-answer.wav'
    wav_file(audio)
    payload = base64.b64encode(audio.read_bytes()).decode()
    with TestClient(create_app(configured)) as client:
        sid, db = waiting(client)
        child = focus(client, sid)['child_id']
        with db.connect() as c:
            original_target = AgentRuntimeStore.local_mistake_answer_target(c, sid, child)
        captured = ok(client.post(f'/v1/sessions/{sid}/asr', json={
            'wav_base64': payload, 'purpose': 'agent_command', 'client_action_id': 'before-withdraw-asr',
            'capture_reference': {'child_id': child, 'question_reference': {
                'kind': 'mistake', 'binding': original_target['binding']}}}))
        request = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': transcript[0], 'client_action_id': 'captured-original-answer',
            'voice_input_id': captured['voice_input_id']}))
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic scoring unavailable')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_score_operation', fail)
            execute(client, request)
        assert focus(client, sid)['state']['pending_operation']['utterance'] == transcript[0]
        execute(client, withdraw(client, sid))
    with TestClient(create_app(configured)) as client:
        late = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': transcript[0], 'client_action_id': 'late-voice-new-request-id',
            'voice_input_id': captured['voice_input_id']})
        assert late.status_code in {200, 409, 422}, late.text
        if late.status_code == 200:
            client.post(f"/v1/agent-turns/{late.json()['agent_turn_id']}/execute")
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
            new_target = AgentRuntimeStore.local_mistake_answer_target(c, sid, child)
        transcript[0] = '我选A'
        fresh = ok(client.post(f'/v1/sessions/{sid}/asr', json={
            'wav_base64': payload, 'purpose': 'agent_command', 'client_action_id': 'after-withdraw-asr',
            'capture_reference': {'child_id': child, 'question_reference': {
                'kind': 'mistake', 'binding': new_target['binding']}}}))
        execute(client, ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': transcript[0], 'client_action_id': 'captured-fresh-answer',
            'voice_input_id': fresh['voice_input_id']})))
    with db.connect() as c:
        answers = [row[0] for row in c.execute('SELECT submitted_answer FROM mistake_attempt')]
        assert answers == ['A']


def test_scored_answer_cannot_be_cancelled_as_unprocessed_work(tmp_path, monkeypatch):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    with TestClient(create_app(configured)) as client:
        # Reproduce a committed score followed by loss of the dialogue receipt.
        service = MistakeDialogueService(db)
        service._score_operation(focus(client, sid), pending)
        with db.connect() as c:
            before = [tuple(row) for row in c.execute('SELECT * FROM mistake_attempt')]
        request = withdraw(client, sid)
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        refusal = response.json()
        assert refusal['execution']['result']['status'] == 'blocked'
        assert refusal['execution']['result']['reason'] == 'answer_already_recorded'
        assert refusal['execution']['evidence'] == []
        assert '还没有撤回' in refusal['turn']['response_text']
        assert refusal['turn']['action']['execution_blocked'] is True
        assert execute(client, request) == refusal
        assert focus(client, sid)['state']['pending_operation'] == pending
        with db.connect() as c:
            assert [tuple(row) for row in c.execute('SELECT * FROM mistake_attempt')] == before
            assert len(before) == 1
    with TestClient(create_app(configured)) as client:
        assert execute(client, request) == refusal
        execute(client, hear(client, sid, '继续错题', 'recover-recorded-score'))
        assert 'pending_operation' not in focus(client, sid)['state']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 1
        row = c.execute('SELECT result_json FROM mistake_dialogue_event WHERE client_action_id=?',
                         (pending['action_id'],)).fetchone()
        assert 'withdrawn_pending_answer' not in json.loads(row[0])
