"""Accepted original-source facts survive lifecycle, not source withdrawal."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import utc_now
from test_diagnostic_prompt_http import environment, start, endpoint, binding, CHILD
from test_specialized_closed_http import generated_http
from test_spoken_numeric import numeric_generated
import test_specialized_closed_contract as closed


def accepted(client, db, monkeypatch, *, confirmed=False, transcript=None, settings=None):
    monkeypatch.setattr(closed, 'generated', numeric_generated)
    headers, rows = generated_http(client, db)
    candidate = next(r for r in rows if r['stage'] == 'parallel')
    approval = client.post(f"/v1/parent/specialized-supply/candidates/{candidate['candidate_id']}/review", headers=headers,
        json={'decision': 'approve', 'note': 'Synthetic scalar reference checked', 'client_action_id': 'source-review31'})
    assert approval.status_code == 200, approval.text
    item = approval.json()['diagnostic_item_id']
    sid = start(client)
    prompt = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math', 'preferred_item_id': item,
        'client_action_id': 'source-prepare31'}).json()['prompt']
    transcript = transcript or ('答案为六' if confirmed else '答案是六')
    def voice_metadata(text, key):
        if settings is None:
            return {}
        from test_voice_runtime_binding import capture
        return {'voice_input_id': capture(client, settings, monkeypatch, sid, text, prompt, key)}
    turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': transcript,
        'client_action_id': 'source-original31', 'cloud_text_allowed': True,
        **voice_metadata(transcript, 'source-asr31')}).json()
    source_id = turn['agent_turn_id']
    if confirmed:
        assert turn['intent'] == 'pending_answer_clarification', turn
        assert client.post(f'/v1/agent-turns/{source_id}/execute').status_code == 200
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '刚才那句就是我的答案',
            'client_action_id': 'source-confirm31', 'cloud_text_allowed': False,
            **voice_metadata('刚才那句就是我的答案', 'confirmation-asr31')}).json()
        assert turn['action']['answer_source_turn_id'] == source_id
    assert turn['action']['type'] == 'submit_diagnostic_answer', turn
    response = client.post(endpoint(sid, 'answer'), json={**binding(prompt), 'answer': turn['action']['answer'],
        'client_action_id': 'source-answer31', 'authorized_turn_id': turn['agent_turn_id']})
    assert response.status_code == 200, response.text
    receipt = response.json()['attempt']
    assert receipt['correctness'] == 1 and receipt['mastery_update_allowed'], receipt
    return sid, prompt, turn, source_id, receipt


def current(db, receipt, *, transaction=False):
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        if transaction:
            c.execute('BEGIN')
        changes = c.total_changes
        value = LearningStateService._attempt(c, receipt['attempt_id'])
        assert c.total_changes == changes
        assert value['correctness'] == receipt['correctness']
        return value


def end(db, sid):
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id=?", (utc_now(), sid))
        c.commit()


@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('phase', ['read_only', 'transaction', 'completed_turn', 'new_turn', 'new_prompt', 'ended', 'restart'])
def test_accepted_direct_and_confirmed_history_survives_lifecycle(environment, monkeypatch, confirmed, phase):
    settings, db = environment
    gateway = closed.ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, source, receipt = accepted(client, db, monkeypatch, confirmed=confirmed)
        if phase == 'completed_turn':
            completed = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
                'final_response': '作答已经保存', 'tool_status': 'completed',
                'result': {'execution_receipt': {'capability': 'diagnostic.submit',
                    'execution_location': 'client_device_bridge', 'action_type': 'submit_diagnostic_answer',
                    'status': 'completed', 'completed_at': utc_now(), 'output': {'attempt': receipt}}}})
            assert completed.status_code == 200, completed.text
            assert completed.json()['tool_status'] == 'completed'
        elif phase == 'new_turn':
            newer = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '今天计划做到哪了',
                'client_action_id': 'later-unrelated31', 'cloud_text_allowed': False})
            assert newer.status_code == 200, newer.text
        elif phase == 'ended':
            end(db, sid)
        elif phase == 'new_prompt':
            newer = client.post(endpoint(sid, 'prepare'), json={'track': 'school',
                'preferred_item_id': 'diag-p4-arithmetic-closed-1', 'client_action_id': 'new-question31'})
            assert newer.status_code == 200, newer.text
            assert newer.json()['prompt']['prompt_id'] != prompt['prompt_id']
        requests_before_read = len(gateway.requests)
        assert current(db, receipt, transaction=phase == 'transaction')['mastery_update_allowed']
    if phase == 'restart':
        with TestClient(create_app(settings, model_gateway=gateway)):
            assert current(db, receipt)['mastery_update_allowed']
    assert current(db, receipt)['score_status'] == 'verified_local'
    assert len(gateway.requests) == requests_before_read


@pytest.mark.parametrize('lossy', [False, True])
def test_legacy_lossy_is_excluded_in_active_transaction_and_ended_but_control_is_retained(environment, monkeypatch, lossy):
    settings, db = environment
    gateway = closed.ClosedGateway()
    transcript = '答案是6!' if lossy else '答案是6'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        with monkeypatch.context() as old:
            original = AgentRuntimeStore.local_answer_forms
            old.setattr(AgentRuntimeStore, 'local_answer_forms', staticmethod(
                lambda text, capability: (text, '6') if text == '答案是6!' else original(text, capability)))
            sid, prompt, turn, source, receipt = accepted(client, db, monkeypatch, transcript=transcript)
        assert current(db, receipt, transaction=True)['mastery_update_allowed'] is not lossy
        assert current(db, receipt)['mastery_update_allowed'] is not lossy
        end(db, sid)
        assert current(db, receipt, transaction=True)['mastery_update_allowed'] is not lossy
    with TestClient(create_app(settings, model_gateway=gateway)):
        assert current(db, receipt)['mastery_update_allowed'] is not lossy
        with db.connect() as c:
            assert c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?', (source,)).fetchone()[0] == transcript
            assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?', (receipt['attempt_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('fault', ['source_text', 'source_deleted', 'prompt_withdrawn', 'prompt_deleted', 'binding', 'child', 'item_version', 'saved_answer'])
def test_historical_sources_still_fail_closed_and_rollback_restores(environment, monkeypatch, confirmed, fault):
    settings, db = environment
    gateway = closed.ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, source, receipt = accepted(client, db, monkeypatch, confirmed=confirmed)
        with db.connect() as c:
            if fault == 'source_text':
                c.execute("UPDATE agent_turn SET transcript='不是我的答案' WHERE agent_turn_id=?", (source,))
            elif fault == 'source_deleted':
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (source,))
            elif fault == 'prompt_withdrawn':
                c.execute("UPDATE diagnostic_prompt SET status='invalidated' WHERE prompt_id=?", (prompt['prompt_id'],))
            elif fault == 'prompt_deleted':
                c.execute('DELETE FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],))
            elif fault == 'binding':
                c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.diagnostic_prompt_id','other') WHERE agent_turn_id=?", (turn['agent_turn_id'],))
            elif fault == 'child':
                c.execute("UPDATE agent_turn SET child_id='child-junior-3' WHERE agent_turn_id=?", (source,))
            elif fault == 'item_version':
                c.execute("UPDATE diagnostic_prompt SET item_version='changed' WHERE prompt_id=?", (prompt['prompt_id'],))
            else:
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='6!' WHERE attempt_id=?", (receipt['attempt_id'],))
            value = LearningStateService._attempt(c, receipt['attempt_id'])
            assert not value['mastery_update_allowed'], (confirmed, fault, value)
            c.rollback()
        assert current(db, receipt)['mastery_update_allowed']


@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('fault', [None, 'capture_cancelled', 'capture_deleted', 'transcript', 'child', 'question'])
def test_actual_asr_history_survives_restart_but_not_changed_capture(environment, monkeypatch, confirmed, fault):
    settings, db = environment
    gateway = closed.ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn, source, receipt = accepted(client, db, monkeypatch, confirmed=confirmed, settings=settings)
        assert current(db, receipt, transaction=True)['mastery_update_allowed']
        end(db, sid)
    requests = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway)):
        assert current(db, receipt)['mastery_update_allowed']
        with db.connect() as c:
            voice_id = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (source,)).fetchone()[0])['voice_input_id']
            if fault == 'capture_cancelled':
                c.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE voice_input_id=?", (voice_id,))
            elif fault == 'capture_deleted':
                c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?', (voice_id,))
            elif fault == 'transcript':
                c.execute("UPDATE voice_input SET transcript='不是我的答案' WHERE voice_input_id=?", (voice_id,))
            elif fault == 'child':
                c.execute("UPDATE voice_input SET child_id='child-junior-3' WHERE voice_input_id=?", (voice_id,))
            elif fault == 'question':
                c.execute("UPDATE voice_capture_binding SET question_reference_json=json_set(question_reference_json,'$.binding.item_version','other') WHERE voice_input_id=?", (voice_id,))
            assert LearningStateService._attempt(c, receipt['attempt_id'])['mastery_update_allowed'] is (fault is None)
            c.rollback()
        assert current(db, receipt)['mastery_update_allowed']
    assert len(gateway.requests) == requests


@pytest.mark.parametrize('fault', [None, 'retract', 'text_changed', 'deleted', 'wrong_child'])
def test_writing_confirmation_preserves_historical_source_not_confirmation_speech(environment, monkeypatch, fault):
    from home_study_agent.historical_answer_source import validate_historical_diagnostic_answer
    from test_content_free_voice_control_http import ControlGateway
    from test_voice_runtime_binding import capture, interpret
    from test_writing_voice_runtime_http import read_turn, readback_receipt
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, read = read_turn(client, db, settings, monkeypatch, gateway)
        result = readback_receipt(client, sid, read)
        assert client.put(f"/v1/agent-turns/{read['agent_turn_id']}/outcome", json={
            'final_response': '原稿已读回', 'tool_status': 'completed', 'result': result}).status_code == 200
        gateway.quote, gateway.control = '我自己写的，识别得没有错，可以用这份', 'writing.confirm_original'
        voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt, 'history-writing-voice31')
        author = interpret(client, sid, gateway.quote, 'history-writing31', voice)
        submitted = client.post(f"/v1/agent-turns/{author['agent_turn_id']}/execute")
        assert submitted.status_code == 200, submitted.text
        with db.connect() as c:
            attempt = dict(c.execute('SELECT a.* FROM diagnostic_attempt a JOIN diagnostic_prompt p ON p.attempt_id=a.attempt_id WHERE p.prompt_id=?', (prompt['prompt_id'],)).fetchone())
            assert attempt['submitted_answer'] != gateway.quote
        if fault == 'retract':
            revoked = client.post(f'/v1/sessions/{sid}/writing-ocr/retract', json={
                **read['action']['writing_ocr_confirmation'], 'client_action_id': 'history-writing-retract31'})
            assert revoked.status_code == 200, revoked.text
        end(db, sid)
    with TestClient(create_app(settings, model_gateway=gateway)):
        with db.connect() as c:
            if fault == 'text_changed':
                c.execute("UPDATE capture_artifact SET extracted_text='changed original' WHERE artifact_type='document_ocr'")
            elif fault == 'deleted':
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (author['agent_turn_id'],))
            c.execute('PRAGMA query_only=ON')
            args = dict(child_id='child-junior-3' if fault == 'wrong_child' else CHILD, session_id=sid,
                prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], item_id=attempt['diagnostic_item_id'], answer=attempt['submitted_answer'])
            if fault:
                with pytest.raises(ValueError):
                    validate_historical_diagnostic_answer(c, author['agent_turn_id'], **args)
            else:
                validate_historical_diagnostic_answer(c, author['agent_turn_id'], **args)
            c.rollback()
