"""Synthetic qualified tasks/PCM; real HTTP dispatch, arrival and SQLite writes.

The guard cases deliberately use Runtime admission with a controlled decision.
The separate natural-language case keeps the real orchestrator, including any
missing control-lane capability, and must not be counted as passing with those.
"""
import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_runtime import (
    AgentRuntimeStore, AgentPlanDecision, ObjectiveDraft, PlannedStep, CapabilityRegistry,
)
from home_study_agent.app import create_app
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.config import Settings
from home_study_agent.learning_input_revision import LearningInputRevisionService
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item
from test_goal_task_prompt import command
from test_pet_task_execution import generated
from test_companion_request_provenance_http import QUOTE, planner
from test_voice_runtime_binding import interpret, wav_file


@pytest.fixture(params=['pet', 'junior'])
def task_case(request, tmp_path):
    case = generated(tmp_path, 'reading') if request.param == 'pet' else request.getfixturevalue('execution')
    db, sid, pid, _, _ = case
    with db.connect() as c:
        child = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    return case, child, settings


def start_goal(client, case, child):
    # Real Runtime scheduling and HTTP dispatcher; no fabricated running state.
    tid = command(case, child_id=child)
    result = client.post(f'/v1/agent-turns/{tid}/execute')
    assert result.status_code == 200, result.text
    result = result.json()['execution']['result']
    assert result['status'] == 'active', result
    return result['diagnostic_prompt']['prompt']


def voice(client, settings, monkeypatch, sid, child, prompt):
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (QUOTE, 1000, 'fixture', False))
    path = settings.data_dir / 'synthetic-goal-request.wav'
    wav_file(path)
    result = client.post(f'/v1/sessions/{sid}/asr', json={
        'wav_base64': base64.b64encode(path.read_bytes()).decode(),
        'purpose': 'agent_command', 'client_action_id': 'goal-activity-voice',
        'capture_reference': {'child_id': child, 'question_reference': {
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']}}})
    assert result.status_code == 200, result.text
    return result.json()['voice_input_id']


def admit(db, sid, child, voice_id):
    """Controlled intent only: production arrival, voice resolver and admission."""
    runtime = AgentRuntimeStore(db)
    service = LearningInputRevisionService(db)
    tid = str(uuid4())
    cap = CapabilityRegistry().require('companion.start_activity', 'study')
    args = {'activity_type': 'movement', 'topic': '伸展', 'opening': '我们轻轻活动一下。'}
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        arrival = service.capture_arrival(sid, tid, QUOTE, connection=c)
        captured = runtime.resolve_captured_voice(c, session_id=sid, child_id=child,
            voice_input_id=voice_id, transcript=QUOTE)
        action = {'type': 'start_companion_activity', **args, 'voice_input_id': voice_id,
            'voice_input_binding': runtime.voice_capture_snapshot(captured)}
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,?,?,'',?,'pending','controlled-admission',?,?)''',
            (tid, child, sid, QUOTE, 'start_companion_activity', json.dumps(action),
             datetime.now(timezone.utc).isoformat(), tid))
        runtime.persist_plan(c, child_id=child, session_id=sid, agent_turn_id=tid, client_action_id=tid,
            context_manifest={'learning_input_arrival_id': arrival['arrival_id']},
            decision=AgentPlanDecision(ObjectiveDraft('none', None, None, (), 'execution'),
                PlannedStep(cap, args, 'Controlled request', cap.expected_evidence), '', False, 1))
        c.commit()
    return tid


def activity_gateway():
    gateway = planner()
    original = gateway.complete
    async def classify(request):
        if request.request_type != 'voice_interaction':
            return await original(request)
        gateway.requests.append(request)
        sent = json.loads(request.messages[-1]['content'])
        assert set(sent) == {'transcript', 'state'}
        assert sent['transcript'] == QUOTE
        assert all(type(value) is bool for value in sent['state'].values())
        return ModelResult(content=json.dumps({'role': 'control', 'control': 'companion.start_activity',
            'quote': QUOTE, 'confidence': .99, 'activity_type': 'movement'}),
            provider='test', model='controlled-intent', latency_ms=1)
    gateway.complete = classify
    return gateway


@pytest.mark.parametrize('change', ['plan_version', 'task_type', 'segment', 'started_at', 'plan_id', 'legacy_task_type'])
def test_goal_activity_rejects_changed_execution_identity_before_any_pause(task_case, monkeypatch, change):
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        prompt = start_goal(client, case, child)
        vid = voice(client, settings, monkeypatch, sid, child, prompt)
        tid = admit(db, sid, child, vid)
        with db.connect() as c:
            if change == 'plan_version':
                c.execute('UPDATE daily_plan SET version=version+1 WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)', (pid,))
            elif change == 'plan_id':
                row = dict(c.execute('SELECT * FROM daily_plan WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)', (pid,)).fetchone())
                old_id = row['plan_id']
                row.update(plan_id=str(uuid4()), client_action_id='replacement-plan')
                c.execute("UPDATE daily_plan SET status='superseded' WHERE plan_id=?", (old_id,))
                c.execute('INSERT INTO daily_plan (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
                c.execute('UPDATE plan_item SET plan_id=? WHERE plan_item_id=?', (row['plan_id'], pid))
            elif change == 'legacy_task_type':
                row = c.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?', (tid,)).fetchone()
                items = json.loads(row['snapshot_json'])
                for entry in items:
                    for item in entry.get('plans', []):
                        item.pop('task_type', None)
                c.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?', (json.dumps(items), row['arrival_id']))
            elif change == 'task_type':
                c.execute("UPDATE plan_item SET task_type='different-test-type' WHERE plan_item_id=?", (pid,))
            else:
                c.execute(f"UPDATE plan_item SET {change if change == 'started_at' else 'active_segment_started_at'}='2001-01-01T00:00:00+00:00' WHERE plan_item_id=?", (pid,))
            c.commit()
            before = tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())
        result = client.post(f'/v1/agent-turns/{tid}/execute')
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()) == before
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 0


def test_goal_activity_controlled_admission_preserves_original_and_history(task_case, monkeypatch):
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        prompt = start_goal(client, case, child)
        vid = voice(client, settings, monkeypatch, sid, child, prompt)
        tid = admit(db, sid, child, vid)
        result = client.post(f'/v1/agent-turns/{tid}/execute')
        assert result.status_code == 200, result.text
        receipt = result.json()['execution']
        outcome = CompanionActivityOutcomeService(db).list(child)['items'][0]
        assert outcome['request_provenance']['status'] == 'verified'
        assert outcome['request_provenance']['binding']['task_binding']['plan_item_id'] == pid
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        replay = client.post(f'/v1/agent-turns/{tid}/execute')
        assert replay.status_code == 200, replay.text
        assert replay.json()['execution'] == receipt
        assert CompanionActivityOutcomeService(db).list(child)['items'][0]['request_provenance']['status'] == 'verified'
        with db.connect() as c:
            c.execute('UPDATE daily_plan SET version=version+1 WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)', (pid,))
            c.commit()
        item = CompanionActivityOutcomeService(db).list(child)['items'][0]
        assert item['request_provenance']['status'] == 'unknown'
        assert item['request_provenance']['binding'] == outcome['request_provenance']['binding']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_real_goal_pending_question_allows_natural_activity_without_source_upload(task_case, monkeypatch):
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    gateway = activity_gateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        prompt = start_goal(client, case, child)
        vid = voice(client, settings, monkeypatch, sid, child, prompt)
        turn = interpret(client, sid, QUOTE, 'natural-goal-activity', vid)
        with db.connect() as c:
            arrival = c.execute("SELECT * FROM learning_input_arrival WHERE client_action_id='natural-goal-activity'").fetchone()
            tasks = [p for entry in json.loads(arrival['snapshot_json']) if entry['kind'] == 'task_execution_arrival' for p in entry['plans']]
        assert len(tasks) == 1 and tasks[0]['plan_item_id'] == pid
        for request in gateway.requests:
            wire = json.dumps(request.messages, ensure_ascii=False)
            assert all(value not in wire for value in (pid, vid, prompt['prompt_id'], prompt['diagnostic_item_id'], 'task_execution_arrival'))
            assert prompt['item']['prompt'] not in wire
        assert turn['action']['type'] == 'start_companion_activity', turn['action']
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        receipt = response.json()['execution']
        proof = CompanionActivityOutcomeService(db).list(child)['items'][0]['request_provenance']
        assert proof['status'] == 'verified'
        assert proof['classification'] == 'session_voice_request'
        assert proof['speaker_identity'] == 'unverified'
        assert proof['initiative'] == proof['autonomy'] == 'unknown'
        assert proof['binding']['voice_input_id'] == vid
        assert proof['binding']['task_binding']['plan_item_id'] == pid
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'paused'
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        calls = len(gateway.requests)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert response.json()['execution'] == receipt
        assert len(gateway.requests) == calls
        assert CompanionActivityOutcomeService(db).list(child)['items'][0]['request_provenance'] == proof
        cancelled = client.post(f'/v1/sessions/{sid}/asr/actions/goal-activity-voice/cancel')
        assert cancelled.status_code == 200, cancelled.text
        assert CompanionActivityOutcomeService(db).list(child)['items'][0]['request_provenance']['status'] == 'unknown'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 1


def test_goal_storage_failure_recovers_only_original_system_pause(task_case, monkeypatch):
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    with TestClient(create_app(settings, model_gateway=planner()), raise_server_exceptions=False) as client:
        prompt = start_goal(client, case, child)
        vid = voice(client, settings, monkeypatch, sid, child, prompt)
        tid = admit(db, sid, child, vid)
        with db.connect() as c:
            c.execute("CREATE TRIGGER fail_goal_outcome BEFORE INSERT ON companion_activity_outcome_event BEGIN SELECT RAISE(ABORT,'isolated failure'); END")
            c.commit()
        assert client.post(f'/v1/agent-turns/{tid}/execute').status_code == 500
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'active'
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0] == 0
            c.execute('DROP TRIGGER fail_goal_outcome')
            c.commit()
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        with db.connect() as c:
            before = tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())
            event = c.execute("SELECT * FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()
            assert json.loads(event['payload_json'])['reason'] == 'runtime_shutdown'
        result = client.post(f'/v1/agent-turns/{tid}/execute')
        assert result.status_code == 200, result.text
        replay = client.post(f'/v1/agent-turns/{tid}/execute')
        assert replay.status_code == 200, replay.text
        assert replay.json()['execution'] == result.json()['execution']
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()) == before
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE json_extract(payload_json,'$.reason')='companion_activity'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 1


def test_semantic_withdrawal_after_executor_inspection_cannot_pause_goal(task_case, monkeypatch):
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    with TestClient(create_app(settings, model_gateway=activity_gateway())) as client:
        prompt = start_goal(client, case, child)
        vid = voice(client, settings, monkeypatch, sid, child, prompt)
        turn = interpret(client, sid, QUOTE, 'racing-activity', vid)
        assert turn['action']['type'] == 'start_companion_activity', turn['action']
        with db.connect() as c:
            before = tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())
            binding = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                (turn['agent_turn_id'],)).fetchone()[0])['voice_control_binding']
        original = AgentRuntimeStore.start_companion_activity
        calls = []
        def revoked(self, *args, **kwargs):
            # Called by the real dispatcher after its successful preflight, not
            # before /execute. The Runtime writer must independently reject this.
            calls.append(True)
            with db.connect() as c:
                c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE request_key=?", (binding['request_key'],))
                c.commit()
            return original(self, *args, **kwargs)
        monkeypatch.setattr(AgentRuntimeStore, 'start_companion_activity', revoked)
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert calls == [True]
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()) == before
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM companion_activity').fetchone()[0] == 0
