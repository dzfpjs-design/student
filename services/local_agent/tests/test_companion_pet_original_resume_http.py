"""Real ASR/HTTP/SQLite with synthetic PCM and controlled model transport.

Task admission reuses the real Runtime fixture; no prompt or timer is fabricated.
Faults are injected after dispatcher admission, at the service write boundary.
"""
import json
import sqlite3
from contextlib import ExitStack
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from test_companion_goal_task_arrival_http import start_goal
from test_pending_question_activity_control import POLITE, capture_quote, decision
from test_pet_task_execution import CHILD, generated
from test_pet_task_resume_http import awaiting_case
from test_voice_runtime_binding import interpret


RESUME = '我想回到刚才暂停的学习。'


@pytest.fixture
def journey(tmp_path, monkeypatch, request):
    retained = getattr(request, 'param', 'direct') == 'retained'
    if retained:
        case, _, retained_prompt = awaiting_case(tmp_path, monkeypatch)
    else:
        case = generated(tmp_path, 'reading')
    db, sid, pid, _, _ = case
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = replace(Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False),
        model_provider='qwen', model_api_key='synthetic-03504f547a5a2fc55aec', model_name='isolated-model',
        voice_intent_model_name='isolated-model')
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if '只分类当前原话' in body['messages'][0]['content']:
            quote = json.loads(body['messages'][-1]['content'])['transcript']
            if quote == POLITE:
                content = decision(quote)
            elif quote == '继续原任务':
                content = json.dumps({'role': 'control', 'control': 'task.resume', 'quote': quote,
                    'confidence': .99, 'selector': {'subject': None, 'task_kind': None, 'relative_order': None}})
            elif quote == RESUME:
                content = json.dumps({'role': 'control', 'control': 'companion.activity_feedback', 'quote': quote,
                    'confidence': .99, 'activity_feedback': {'operation': 'request_resume'}})
            else:
                content = json.dumps({'role': 'activity_reply', 'control': None, 'quote': quote, 'confidence': .99})
        else:
            content = '轻轻放松肩膀，舒服就好。'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    def app():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(app(), raise_server_exceptions=False))
        if retained:
            quote = '继续原任务'
            vid = capture_quote(client, settings, monkeypatch, sid, CHILD, retained_prompt, quote, 'retained-start-audio')
            admitted = interpret(client, sid, quote, 'retained-start', vid)
            assert admitted['runtime_capability'] == 'task.resume'
            started = client.post(f"/v1/agent-turns/{admitted['agent_turn_id']}/execute")
            assert started.status_code == 200, started.text
            prompt = started.json()['execution']['result']['diagnostic_prompt']['prompt']
            assert prompt['prompt_id'] == retained_prompt['prompt_id']
            with db.connect() as c:
                assert c.execute("SELECT s.capability_name FROM plan_item_event e JOIN agent_step s ON s.idempotency_key=e.client_action_id WHERE e.plan_item_id=? AND e.event_type='started'", (pid,)).fetchone()[0] == 'task.resume'
        else:
            prompt = start_goal(client, case, CHILD)
        with db.connect() as c:
            before = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())
        vid = capture_quote(client, settings, monkeypatch, sid, CHILD, prompt, POLITE, 'pet-activity-audio')
        turn = interpret(client, sid, POLITE, 'pet-activity', vid)
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        for index, quote in enumerate(('我准备好了。', '我轻轻转了转肩膀。')):
            focus = client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus']
            assert focus
            vid = capture_quote(client, settings, monkeypatch, sid, CHILD, prompt, quote,
                f'activity-reply-{index}', focus=focus)
            reply = interpret(client, sid, quote, f'activity-turn-{index}', vid)
            assert reply['action']['source'] == 'cloud_companion', reply
        with db.connect() as c:
            assert c.execute('SELECT status FROM companion_activity').fetchone()[0] == 'completed'
        vid = capture_quote(client, settings, monkeypatch, sid, CHILD, prompt, RESUME, 'pet-resume-audio')
        turn = interpret(client, sid, RESUME, 'pet-resume', vid)
        assert turn['runtime_capability'] == 'companion.activity_feedback', turn
        yield db, sid, pid, prompt, before, client, app, requests, turn, stack.close


def assert_unresumed(db, pid):
    with db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'paused'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='resume_confirmed'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('journey', ['direct', 'retained'], indirect=True)
def test_pet_activity_resume_retains_exact_original_and_restarts(journey, monkeypatch):
    db, sid, pid, prompt, before, client, app, requests, turn, close = journey
    validate = StudyRepository._validate_pet_task_start
    checked = []
    def validate_source(c, item, source_turn_id=None):
        assert c.in_transaction and item['plan_item_id'] == pid
        assert source_turn_id and source_turn_id != turn['agent_turn_id']
        checked.append(source_turn_id)
        return validate(c, item, source_turn_id)
    monkeypatch.setattr(StudyRepository, '_validate_pet_task_start', staticmethod(validate_source))
    url = f"/v1/agent-turns/{turn['agent_turn_id']}/execute"
    result = client.post(url)
    assert result.status_code == 200, result.text
    assert result.json()['execution']['result']['activity']['resume']['status'] == 'confirmed'
    with db.connect() as c:
        after = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())
        assert after['status'] == 'active' and after['started_at'] == before['started_at']
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    original = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
    assert original['status'] == 'waiting_answer'
    assert original['prompt']['item_version'] == prompt['item_version']
    count = len(requests)
    close()
    with TestClient(app()) as restarted:
        assert restarted.post(url).json()['execution'] == result.json()['execution']
        assert len(requests) == count
    assert len(checked) == 1
    wire = json.dumps(requests, ensure_ascii=False)
    assert prompt['item']['prompt'] not in wire and prompt['prompt_id'] not in wire and pid not in wire


@pytest.mark.parametrize('fault', ['cancel_origin', 'cancel_origin_step', 'changed_version', 'wrong_owner',
    'changed_prepare_key', 'cancel_current', 'deleted_journal', 'cancelled_voice'])
def test_pet_resume_rechecks_original_inside_service_boundary(journey, monkeypatch, fault):
    db, sid, pid, prompt, _, client, _, _, turn, _ = journey
    real = CompanionActivityVoiceService.resume_original
    calls = []
    def changed(service, turn_id):
        calls.append(turn_id)
        with db.connect() as c:
            if fault == 'cancel_origin':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?", (prompt['prompt_id'],))
            elif fault == 'cancel_origin_step':
                c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?)", (prompt['prompt_id'],))
            elif fault == 'cancel_current':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn_id,))
            elif fault == 'deleted_journal':
                c.execute("DELETE FROM voice_control_intent_request WHERE request_key=(SELECT json_extract(context_manifest_json,'$.voice_control_binding.request_key') FROM agent_plan WHERE agent_turn_id=?)", (turn_id,))
            elif fault == 'cancelled_voice':
                c.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE voice_input_id=(SELECT json_extract(action_json,'$.voice_input_id') FROM agent_turn WHERE agent_turn_id=?)", (turn_id,))
            elif fault == 'changed_version':
                c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
            elif fault == 'wrong_owner':
                c.execute("UPDATE diagnostic_prompt SET child_id='child-junior-3' WHERE prompt_id=?", (prompt['prompt_id'],))
            else:
                c.execute("UPDATE diagnostic_prompt SET prepare_action_id='unrelated-prepare' WHERE prompt_id=?", (prompt['prompt_id'],))
            c.commit()
        return real(service, turn_id)
    monkeypatch.setattr(CompanionActivityVoiceService, 'resume_original', changed)
    result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert calls == [turn['agent_turn_id']]
    assert result.status_code == 409, result.text
    assert_unresumed(db, pid)


@pytest.mark.parametrize('journey', ['retained'], indirect=True)
@pytest.mark.parametrize('fault', ['cancel_prepare', 'change_prepare_binding'])
def test_retained_start_does_not_replace_original_prepare_authority(journey, monkeypatch, fault):
    db, _, pid, prompt, _, client, _, _, turn, _ = journey
    real = CompanionActivityVoiceService.resume_original
    calls = []
    def changed(service, turn_id):
        calls.append(turn_id)
        with db.connect() as c:
            origin = c.execute('''SELECT p.plan_id FROM agent_plan p JOIN agent_step s USING(plan_id)
                JOIN diagnostic_prompt q ON q.prepare_action_id='goal-task:'||s.idempotency_key
                WHERE q.prompt_id=?''', (prompt['prompt_id'],)).fetchone()[0]
            if fault == 'cancel_prepare':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE plan_id=?", (origin,))
            else:
                c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.goal_prompt_binding.item_version','changed') WHERE plan_id=?", (origin,))
            c.commit()
        return real(service, turn_id)
    monkeypatch.setattr(CompanionActivityVoiceService, 'resume_original', changed)
    result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert calls == [turn['agent_turn_id']]
    assert result.status_code == 409, result.text
    assert_unresumed(db, pid)


@pytest.mark.parametrize('retry_mode', ['same_process', 'restart', 'source_changed', 'untrusted_error'])
def test_pet_resume_confirmation_fault_rolls_back_timer_then_retry_same_event(journey, monkeypatch, retry_mode):
    db, _, pid, prompt, _, client, app, _, turn, close = journey
    original = CompanionActivityOutcomeService._append
    calls = []
    def fail(self, c, row, revision, kind, *args):
        if kind == 'resume_confirmed':
            calls.append(kind)
            assert c.in_transaction
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'active'
            raise sqlite3.OperationalError('isolated confirmation fault')
        return original(self, c, row, revision, kind, *args)
    monkeypatch.setattr(CompanionActivityOutcomeService, '_append', fail)
    url = f"/v1/agent-turns/{turn['agent_turn_id']}/execute"
    failed = client.post(url)
    assert failed.status_code == 500, failed.text
    assert calls == ['resume_confirmed']
    assert_unresumed(db, pid)
    monkeypatch.setattr(CompanionActivityOutcomeService, '_append', original)
    with db.connect() as c:
        if retry_mode == 'source_changed':
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
        elif retry_mode == 'untrusted_error':
            c.execute("UPDATE agent_turn SET tool_error_code='ValueError' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
        c.commit()
    if retry_mode == 'restart':
        close()
        with TestClient(app(), raise_server_exceptions=False) as restarted:
            result = restarted.post(url)
    else:
        result = client.post(url)
    if retry_mode in {'source_changed', 'untrusted_error'}:
        assert result.status_code == 409, result.text
        assert_unresumed(db, pid)
        return
    assert result.status_code == 200, result.text
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM companion_activity_outcome_event WHERE kind='resume_confirmed'").fetchone()[0] == 1
