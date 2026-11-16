"""A missing selector is incomplete interpretation, not a generic resume request."""
import json

import pytest

from home_study_agent.voice_control_intent import VoiceControlIntent
from home_study_agent.model_gateway import ModelResult
from home_study_agent.app import create_app
from fastapi.testclient import TestClient
from test_diagnostic_prompt_http import environment
from test_task_resume_voice_http import ControlGateway, paused_case, hear


@pytest.mark.parametrize('quote', ['数学那项我想接着做', '继续暂停的任务'])
def test_live_resume_requires_explicit_selector_slots(quote):
    result = VoiceControlIntent.parse(json.dumps({
        'role': 'control', 'control': 'task.resume', 'quote': quote, 'confidence': .99}), quote)
    assert result == {'status': 'clarify', 'reason': 'CONTROL_RESUME_SELECTOR_REQUIRED'}


def test_explicit_empty_slots_are_distinct_from_missing_interpretation():
    quote = '继续暂停的任务'
    selector = dict(subject=None, task_kind=None, relative_order=None)
    result = VoiceControlIntent.parse(json.dumps({
        'role': 'control', 'control': 'task.resume', 'quote': quote, 'confidence': .99,
        'selector': selector}), quote)
    assert result == {'status': 'control', 'control': 'task.resume', 'selector': selector}


def test_missing_selector_never_resumes_the_only_unrelated_task(environment, monkeypatch):
    class MissingSelector(ControlGateway):
        async def complete(self, request):
            result = await super().complete(request)
            value = json.loads(result.content)
            value.pop('selector', None)
            return ModelResult(content=json.dumps(value), provider='test', model='omitted-selector', latency_ms=1)
    settings, db = environment
    gateway = MissingSelector('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt,
                    '先继续英语', 'missing-subject-selector', 'task.resume')
        assert turn['action']['type'] == 'none', turn
        response = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        with db.connect() as c:
            decision = json.loads(c.execute('SELECT decision_json FROM voice_control_intent_request ORDER BY rowid DESC LIMIT 1').fetchone()[0])
            assert decision['reason'] == 'CONTROL_RESUME_SELECTOR_REQUIRED'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0


def test_official_negative_misclassification_cannot_pause_another_active_task(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        repo.start_plan_item('control-task-2', 'another-active-task')
        # Replay the actual official model's erroneous label, not an ideal label.
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt,
                    '数学那项先别继续', 'negative-resume-not-pause', 'task.pause')
        assert turn['action']['type'] == 'none', turn
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'active'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id='control-task-2' AND event_type='paused'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('quote,control,subject,resumes', [
    ('别停，继续英语', 'task.resume', 'english', True),
    ('先不要继续，等我一下', 'task.pause', None, False),
    ('别继续数学，先英语', 'task.resume', 'english', True),
    ('不要继续数学，先英语', 'task.resume', 'english', True),
    ('别继续数学，先英语', 'task.resume', 'math', False),
])
def test_negation_scope_preserves_explicit_other_subject(environment, monkeypatch, quote, control, subject, resumes):
    from test_task_resume_semantic_http import SelectorGateway, two_paused
    settings, db = environment
    gateway = SelectorGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = two_paused(client, settings, db, monkeypatch, gateway)
        gateway.selector = {'subject': subject, 'task_kind': None, 'relative_order': None}
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt, quote, 'scoped-negative-task', control)
        assert turn['action']['type'] == ('resume_paused_task' if resumes else 'none'), turn
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        assert repo._get_plan_item_by_id('control-task-2')['status'] == ('active' if resumes else 'paused')
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_pause_label_without_an_active_target_is_not_an_executable_decision(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt,
                    '手头的任务暂停一下', 'already-paused-scope', 'task.pause')
        assert turn['action']['type'] == 'none', turn
        with db.connect() as c:
            value = json.loads(c.execute('SELECT decision_json FROM voice_control_intent_request ORDER BY rowid DESC LIMIT 1').fetchone()[0])
            assert value == {'status': 'clarify', 'reason': 'CONTROL_PAUSE_TARGET_REQUIRED'}
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'


def test_quote_check_still_precedes_missing_selector_clarification():
    with pytest.raises(ValueError, match='CONTROL_INTENT_INVALID'):
        VoiceControlIntent.parse(json.dumps({'role': 'control', 'control': 'task.resume',
            'quote': '先继续数学', 'confidence': .99}), '先别继续数学')


@pytest.mark.parametrize('recorded', [False, True])
def test_legacy_missing_selector_requires_new_authority_except_real_receipt(environment, monkeypatch, recorded):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, repo, prompt = paused_case(client, settings, db, monkeypatch, gateway)
        turn = hear(client, settings, monkeypatch, gateway, sid, prompt,
                    '继续暂停的任务', 'legacy-resume-shape', 'task.resume')
        assert turn['action']['type'] == 'resume_paused_task'
        url = f"/v1/agent-turns/{turn['agent_turn_id']}/execute"
        if recorded:
            assert client.post(url).status_code == 200
            repo.pause_plan_item('control-task-1', 'later-real-pause')
        # Exact v1 durable shape: before mandatory selectors, both journal and
        # plan kept the same candidate but omitted these interpretation fields.
        with db.connect() as c:
            row = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                            (turn['agent_turn_id'],)).fetchone()
            manifest = json.loads(row[0])
            frozen = manifest['voice_control_binding']
            frozen['targets']['task_resume'].pop('selector')
            c.execute("UPDATE voice_control_intent_request SET decision_json=json_remove(decision_json,'$.selector'),binding_json=? WHERE request_key=?",
                      (json.dumps(frozen['targets']), frozen['request_key']))
            c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',
                      (json.dumps(manifest), turn['agent_turn_id']))
            c.commit()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = client.post(url)
        assert result.status_code == (200 if recorded else 409), result.text
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == int(recorded)
