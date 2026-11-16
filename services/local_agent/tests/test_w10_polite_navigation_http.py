"""Real host/ASR with controlled semantic transport, never paid inference."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from test_diagnostic_prompt_http import environment
from test_natural_task_navigation_http import setup_case, hear, execute, Offline
from test_natural_task_navigation_cloud_http import application
from test_pet_task_resume_http import awaiting_case


def test_polite_next_uses_model_and_exact_arrival(environment, monkeypatch):
    settings, db = environment
    db.initialize()
    requests = []
    with TestClient(application(settings, db, requests)) as client:
        sid, repo = setup_case(client, db, 'completed')
        heard, _ = hear(client, settings, monkeypatch, sid, '能开始下一项吗？', cloud=True)
        assert len(requests) == 1
        assert heard['runtime_capability'] == 'task.start', heard
        result = execute(client, heard)
        assert result['execution']['result']['plan_item_id'] == 'control-task-2'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'completed'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'


def semantic_app(settings, requests, role='control', hook=None):
    def transport(request):
        body = json.loads(request.content)
        data = json.loads(body['messages'][-1]['content'])
        assert set(data) == {'transcript', 'state'}
        assert all(type(v) is bool for v in data['state'].values())
        requests.append(body)
        if hook:
            hook()
        output = {'role': role, 'control': 'task.resume' if role == 'control' else None,
                  'quote': data['transcript'], 'confidence': .99}
        if role == 'control':
            output['selector'] = {'subject': None, 'task_kind': None, 'relative_order': None}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
    return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))


@pytest.mark.parametrize('role,quote,allowed', [
    ('control', '可以继续刚才任务吗？', True),
    ('control', '不要继续刚才任务，可以吗？', False),
    ('question', '你有继续任务这个功能吗？', False),
    ('control', '你有继续任务这个功能吗？', False),
    ('control', '老师说“继续刚才任务”，是什么意思？', False),
    ('mixed', '可以继续刚才任务吗，还是先讲个故事？', False),
])
def test_polite_original_and_semantic_noncommands(tmp_path, monkeypatch, role, quote, allowed):
    case, settings, prompt = awaiting_case(tmp_path, monkeypatch)
    db, sid, pid, _, original = case
    requests = []
    with TestClient(semantic_app(settings, requests, role)) as client:
        heard, _ = hear(client, settings, monkeypatch, sid, quote, cloud=True, prompt=prompt)
        # Quoted commands can be rejected by the existing local guard before dispatch.
        assert len(requests) == (0 if '老师说' in quote else 1)
        if allowed:
            assert heard['runtime_capability'] == 'task.resume', heard
            result = execute(client, heard)
            assert result['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        else:
            assert heard['action']['type'] in {'none', 'ask_tutor'}, heard
            if heard['action']['type'] == 'none':
                execute(client, heard)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == int(allowed)
        wire = json.dumps(requests, ensure_ascii=False)
        for secret in (prompt['prompt_id'], prompt['item_version'], original['prompt'], sid, pid):
            assert secret not in wire


@pytest.mark.parametrize('quote', ['能开始下一项吗？', '可以继续刚才任务吗？'])
def test_polite_sentence_does_not_extend_offline_grammar(environment, monkeypatch, quote):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Offline())) as client:
        sid, repo = setup_case(client, db, 'completed')
        heard, _ = hear(client, settings, monkeypatch, sid, quote)
        assert heard['action']['type'] == 'none', heard
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'


@pytest.mark.parametrize('change', ['source', 'plan_version'])
def test_polite_start_still_checks_frozen_arrival(environment, monkeypatch, change):
    settings, db = environment
    db.initialize()
    requests = []
    with TestClient(application(settings, db, requests)) as client:
        sid, repo = setup_case(client, db, 'completed')
        heard, _ = hear(client, settings, monkeypatch, sid, '能开始下一项吗？', cloud=True)
        assert heard['runtime_capability'] == 'task.start', heard
        with db.connect() as c:
            if change == 'source':
                c.execute("UPDATE plan_item SET source_ref='changed' WHERE plan_item_id='control-task-2'")
            else:
                c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='control-plan'")
            c.commit()
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id='control-task-2'").fetchone()[0] == 0


@pytest.mark.parametrize('control', ['task.start', 'task.resume'])
@pytest.mark.parametrize('quote', ['老师说“开始下一项”', '如果我继续任务呢？', '你有开始任务的功能吗？'])
def test_semantic_control_cannot_turn_quotation_or_feature_query_into_permission(control, quote):
    from home_study_agent.voice_control_intent import VoiceControlIntent
    value = {'role': 'control', 'control': control, 'quote': quote, 'confidence': .99}
    if control == 'task.resume':
        value['selector'] = {'subject': None, 'task_kind': None, 'relative_order': None}
    assert VoiceControlIntent.parse(json.dumps(value), quote)['status'] == 'clarify'
