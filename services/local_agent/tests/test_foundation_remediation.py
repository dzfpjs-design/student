"""The actual saved foundation error, not an old case, owns the next activity."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.database import Database
from test_goal_roadmap import flow, CHILD, ROOT
from test_goal_foundation_execution import foundation, planned
from test_learning_runtime_http import NoCloud
from test_voice_runtime_binding import capture, interpret


def start(client, sid, settings, monkeypatch):
    workspace, item = planned(client, sid)
    for other in workspace['plan_items']:
        if other['plan_item_id'] != item['plan_item_id']:
            result = client.post(f"/v1/plan-items/{other['plan_item_id']}/reschedule", json={
                'reason': 'Synthetic isolated task', 'client_action_id': 'defer-' + other['plan_item_id']})
            assert result.status_code == 200
    result = client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'plan_review',
        'target_state': 'study', 'reason': '确认', 'client_action_id': 'auto-foundation-study'})
    assert result.status_code == 200, result.text
    voice = capture(client, settings, monkeypatch, sid, '开始任务', key='start-audio')
    turn = interpret(client, sid, '开始任务', 'auto-foundation-start', voice)
    result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    return item, result.json()['execution']['result']['diagnostic_prompt']['prompt']


def wrong(client, sid, settings, monkeypatch, prompt):
    voice = capture(client, settings, monkeypatch, sid, '答案是-999', prompt, key='wrong-audio')
    turn = interpret(client, sid, '答案是-999', 'auto-foundation-wrong', voice)
    result = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
        'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
        'answer': turn['action']['answer'], 'authorized_turn_id': turn['agent_turn_id'], 'client_action_id': 'wrong-answer'})
    assert result.status_code == 200, result.text
    return result.json()


def test_wrong_foundation_opens_exact_persisted_followup_and_recovers(foundation, monkeypatch):
    db, sid, settings = foundation
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        task, prompt = start(client, sid, settings, monkeypatch)
        result = wrong(client, sid, settings, monkeypatch, prompt)
        followup = result.get('foundation_remediation')
        assert followup, result.get('local_learning_evaluation')
        assert followup['origin']['prompt_id'] == prompt['prompt_id']
        assert followup['origin']['attempt_id'] == result['attempt']['attempt_id']
        assert followup['mistake_id'] == result['attempt']['mistake']['mistake_id']
        assert followup['cloud_transmission_allowed'] is False
        from home_study_agent.mistake_dialogue import MistakeDialogueService
        dialogue_service = MistakeDialogueService(db)
        expected_prompt = dialogue_service.prompt(dialogue_service.state(sid))
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == 'paused'
        voice = capture(client, settings, monkeypatch, sid, '继续任务', key='resume-foundation-audio')
        resumed_turn = interpret(client, sid, '继续任务', 'resume-foundation-task', voice)
        resumed_task = client.post(f"/v1/agent-turns/{resumed_turn['agent_turn_id']}/execute")
        assert resumed_task.status_code == 200, resumed_task.text
        assert resumed_task.json()['execution']['result']['status'] == 'active', resumed_task.json()['execution']
        assert resumed_task.json()['execution']['requires_child_reply'] is True
        assert expected_prompt in resumed_task.json()['execution']['response_text']
        recovered = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/resume', json={
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']})
        assert recovered.status_code == 200, recovered.text
        assert recovered.json().get('foundation_remediation') == followup
        dialogue = client.get(f'/v1/sessions/{sid}/mistake-dialogue').json()['dialogue']
        assert dialogue['mistake_id'] == followup['mistake_id']
        assert dialogue['state']['foundation_origin'] == followup['origin']
        explanation = '我从列式这一步开始不确定'
        voice = capture(client, settings, monkeypatch, sid, explanation, key='foundation-diagnosis-audio')
        turn = interpret(client, sid, explanation, 'foundation-diagnosis', voice)
        assert turn['action']['type'] == 'continue_mistake_dialogue', turn['action']
        diagnosed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert diagnosed.status_code == 200, diagnosed.text
        assert diagnosed.json()['execution']['result']['mistake_id'] == followup['mistake_id']
        assert diagnosed.json()['execution']['result']['phase'] == 'diagnosis_probe'
        assert diagnosed.json()['execution']['result']['state']['diagnosis']['hypotheses'][0]['explanation'] == explanation
        replay = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/resume', json={
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']})
        assert replay.status_code == 200
        assert not replay.json().get('foundation_remediation')
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == 'active'
    with Database(db.path).connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_dialogue_event WHERE client_action_id=?',
            ('foundation-remediation:' + prompt['prompt_id'],)).fetchone()[0] == 1
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == 'paused'


@pytest.mark.parametrize('change', ['other_task', 'other_child', 'other_attempt', 'withdrawn', 'late_change'])
def test_remediation_cannot_resume_changed_or_unrelated_focus(foundation, monkeypatch, change):
    db, sid, settings = foundation
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        task, prompt = start(client, sid, settings, monkeypatch)
        wrong(client, sid, settings, monkeypatch, prompt)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        def mutate():
            with db.connect() as c:
                if change == 'withdrawn':
                    c.execute("UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (ROOT,))
                elif change == 'other_child':
                    c.execute("UPDATE mistake_occurrence SET child_id='child-junior-3'")
                else:
                    key = 'attempt_id' if change == 'other_attempt' else 'plan_item_id'
                    c.execute('UPDATE mistake_dialogue SET state_json=json_set(state_json,?,?) WHERE session_id=?',
                              ('$.foundation_origin.' + key, 'not-the-original', sid))
                c.commit()
        if change != 'late_change':
            mutate()
        voice = capture(client, settings, monkeypatch, sid, '继续任务', key='changed-resume-audio')
        turn = interpret(client, sid, '继续任务', 'changed-resume-task', voice)
        if change == 'late_change':
            assert turn['action']['type'] == 'resume_paused_task'
            mutate()
        client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone()[0] == 'paused'


def test_foundation_handoff_does_not_extend_task_budget(foundation, monkeypatch):
    db, sid, settings = foundation
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        task, prompt = start(client, sid, settings, monkeypatch)
        with db.connect() as c:
            c.execute('UPDATE plan_item SET active_seconds=expected_minutes*60-60 WHERE plan_item_id=?', (task['plan_item_id'],))
            before = c.execute('SELECT count(*),sum(expected_minutes) FROM plan_item').fetchone()
            c.commit()
        result = wrong(client, sid, settings, monkeypatch, prompt)
        assert not result.get('foundation_remediation')
        assert result['attempt']['mistake']['mistake_id']
        with db.connect() as c:
            assert tuple(c.execute('SELECT count(*),sum(expected_minutes) FROM plan_item').fetchone()) == tuple(before)
            assert c.execute('SELECT count(*) FROM mistake_dialogue').fetchone()[0] == 0


def test_foundation_handoff_save_failure_rolls_back_and_retries(foundation, monkeypatch):
    from home_study_agent import foundation_remediation as remediation
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    db, sid, settings = foundation
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        task, prompt = start(client, sid, settings, monkeypatch)
        with monkeypatch.context() as patch:
            patch.setattr(remediation, 'continue_after_answer', lambda *args: None)
            result = wrong(client, sid, settings, monkeypatch, prompt)
        save = MistakeDialogueService._save
        def fail_after_save(self, *args, **kwargs):
            save(self, *args, **kwargs)
            raise RuntimeError('synthetic save failure')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_save', fail_after_save)
            with pytest.raises(RuntimeError, match='synthetic save failure'):
                remediation.continue_after_answer(db, sid, prompt['prompt_id'])
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_dialogue').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM mistake_dialogue_event').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE attempt_id=?',
                             (result['attempt']['attempt_id'],)).fetchone()[0] == 1
        recovered = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/resume', json={
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']})
        assert recovered.status_code == 200, recovered.text
        followup = recovered.json()['foundation_remediation']
        assert followup['origin']['plan_item_id'] == task['plan_item_id']
        assert followup['origin']['attempt_id'] == result['attempt']['attempt_id']
