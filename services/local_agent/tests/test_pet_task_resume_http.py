"""Natural resume of a retained, unstarted PET question through the real host."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from test_pet_task_execution import CHILD, generated, command, state
from test_voice_runtime_binding import capture, interpret, submit


def awaiting_case(tmp_path, monkeypatch):
    case = generated(tmp_path, 'reading', with_homework=True)
    db, sid, pid, _, _ = case
    tid = command(case, child_id=CHILD)
    repo = StudyRepository(db)
    def fail_timer(*args, **kwargs):
        raise OSError('isolated timer admission failure')
    monkeypatch.setattr(repo, 'start_plan_item', fail_timer)
    with pytest.raises(OSError):
        AgentCapabilityExecutor(db, repo).execute(tid)
    prompt = DiagnosticPromptService(db).read(sid)
    assert prompt['status'] == 'awaiting_task_start'
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        model_api_key='synthetic-c7645253514c0a467fb7', model_provider='qwen', model_name='isolated-control',
                        voice_intent_model_name='isolated-control')
    return case, settings, prompt['prompt']


def application(settings, requests, *, hook=None):
    def transport(request):
        body = json.loads(request.content)
        data = json.loads(body['messages'][-1]['content'])
        assert set(data) == {'transcript', 'state'}
        assert all(type(value) is bool for value in data['state'].values())
        requests.append(body)
        if hook:
            hook()
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'role': 'control', 'control': 'task.resume', 'quote': data['transcript'], 'confidence': .99,
            'selector': {'subject': None, 'task_kind': None, 'relative_order': None}})}}]})
    return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))


@pytest.mark.parametrize('lost_receipt', [False, True])
def test_natural_continue_original_starts_not_resumes_and_completes_after_restart(tmp_path, monkeypatch, lost_receipt):
    case, settings, prompt = awaiting_case(tmp_path, monkeypatch)
    db, sid, pid, _, original = case
    requests = []
    with TestClient(application(settings, requests), raise_server_exceptions=False) as client:
        assert client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()['status'] == 'awaiting_task_start'
        quote = '继续原任务'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'resume-original-voice')
        heard = interpret(client, sid, quote, 'resume-original-command', voice)
        assert heard['runtime_capability'] == 'task.resume', heard
        with db.connect() as c:
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                          (heard['agent_turn_id'],)).fetchone()[0])
        target = manifest['voice_control_binding']['targets']['task_resume']['item']
        assert target['operation'] == 'start' and target['plan_item_id'] == pid
        assert target['goal_prompt_binding']['prompt_id'] == prompt['prompt_id']
        url = f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        start = StudyRepository.start_plan_item
        if lost_receipt:
            def lost(self, *args, **kwargs):
                start(self, *args, **kwargs)
                raise OSError('isolated lost start receipt')
            with monkeypatch.context() as local:
                local.setattr(StudyRepository, 'start_plan_item', lost)
                assert client.post(url).status_code == 500
        else:
            result = client.post(url)
            assert result.status_code == 200, result.text
            current = result.json()['execution']['result']['diagnostic_prompt']
            assert current['status'] == 'waiting_answer' and current['prompt']['prompt_id'] == prompt['prompt_id']
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type IN ('paused','resumed')", (pid,)).fetchone()[0] == 0
    with TestClient(application(settings, requests)) as client:
        replay = client.post(url)
        assert replay.status_code == 200, replay.text
        assert replay.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        # Host restart honestly pauses active timing; old receipts cannot undo it.
        assert state(db, pid)['status'] == 'paused'
        again = '原任务接着做'
        voice = capture(client, settings, monkeypatch, sid, again, prompt, 'resume-real-pause-voice')
        resumed = interpret(client, sid, again, 'resume-real-pause', voice)
        result = client.post(f"/v1/agent-turns/{resumed['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert state(db, pid)['status'] == 'active'
        voice = capture(client, settings, monkeypatch, sid, '90', prompt, 'actual-answer-voice')
        heard = interpret(client, sid, '90', 'actual-answer-command', voice)
        answer = submit(client, sid, prompt, heard)
        assert answer.status_code == 200 and answer.json()['status'] == 'answered', answer.text
        complete = client.post(f'/v1/plan-items/{pid}/complete', json={'actual_minutes': 1, 'client_action_id': 'actual-practice-complete'})
        assert complete.status_code == 200 and complete.json()['status'] == 'completed', complete.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='resumed'", (pid,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    assert len(requests) == 2
    data = json.loads(requests[0]['messages'][-1]['content'])
    assert data['state']['has_pending_task_start'] is True and data['state']['has_paused_task'] is False
    wire = json.dumps(requests, ensure_ascii=False)
    assert all(secret not in wire for secret in (sid, pid, prompt['prompt_id'], original['prompt']))


@pytest.mark.parametrize('fault', ['source_changed', 'another_active', 'control_withdrawn'])
def test_natural_resume_rechecks_arrival_and_never_retargets_or_pauses_another(tmp_path, monkeypatch, fault):
    case, settings, prompt = awaiting_case(tmp_path, monkeypatch)
    db, sid, pid, node, _ = case
    requests = []
    with TestClient(application(settings, requests)) as client:
        quote = '继续原任务'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'before-change-voice')
        heard = interpret(client, sid, quote, 'before-change-command', voice)
        assert heard['runtime_capability'] == 'task.resume', heard
        if fault == 'another_active':
            with db.connect() as c:
                other = c.execute("SELECT plan_item_id FROM plan_item WHERE source_type='school_homework' LIMIT 1").fetchone()[0]
                c.execute("UPDATE plan_item SET status='pending' WHERE plan_item_id=?", (other,))
                c.commit()
            StudyRepository(db).start_plan_item(other, 'another-real-task-start')
        else:
            with db.connect() as c:
                if fault == 'source_changed':
                    c.execute("UPDATE knowledge_node SET knowledge_version='after-arrival-revision' WHERE knowledge_node_id=?", (node,))
                else:
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE session_id=?", (sid,))
                c.commit()
        with db.connect() as c:
            before = [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')]
        response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        with db.connect() as c:
            assert [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')] == before
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=?', (pid,)).fetchone()[0] == 0
        assert state(db, pid)['status'] == 'pending'
    assert len(requests) == 1
