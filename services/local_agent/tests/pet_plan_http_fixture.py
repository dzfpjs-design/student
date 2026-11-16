"""Transcript/API integration, not acoustic or child-outcome evidence."""
import pytest


@pytest.fixture
def app_listening_resources(monkeypatch):
    from home_study_agent import goal_task_prompt
    from pet_listening_fixture import reviewed_audio
    prepare = goal_task_prompt.prepare_goal_task_prompt

    def with_audio(database, *args, **kwargs):
        result = prepare(database, *args, **kwargs)
        prompt = result.get('prompt') or {}
        if (prompt.get('item') or {}).get('pet_audio_modality') == 'listening':
            root = database.path.parent / 'media'
            root.mkdir(parents=True, exist_ok=True)
            reviewed_audio(database, root, prompt)
        return result

    monkeypatch.setattr(goal_task_prompt, 'prepare_goal_task_prompt', with_audio)


def start_pet_task(client, session_id, item, action_key):
    turn = client.post(f'/v1/sessions/{session_id}/agent-turn', json={
        'transcript': '开始任务', 'client_action_id': action_key + ':turn'})
    assert turn.status_code == 200, turn.text
    value = turn.json()
    assert value['runtime_capability'] == 'task.start', value
    assert value['runtime_execution_location'] == 'server', value
    executed = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
    assert executed.status_code == 200, executed.text
    result = executed.json()['execution']['result']
    assert result['status'] == 'active', result
    prompt = result['diagnostic_prompt']['prompt']
    assert prompt['diagnostic_item_id'] == item['source_ref'], result
    workspace = client.get(f'/v1/sessions/{session_id}').json()
    active = [row for row in workspace['plan_items'] if row['status'] == 'active']
    assert [row['plan_item_id'] for row in active] == [item['plan_item_id']]
    return prompt


def answer_pet_task(client, session_id, prompt, answer, action_key):
    response = client.post(f'/v1/sessions/{session_id}/diagnostic-prompt/answer', json={
        'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
        'answer': answer, 'client_action_id': action_key})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'answered', response.text
    assert response.json()['prompt']['prompt_id'] == prompt['prompt_id']
    return response
