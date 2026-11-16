"""Actual ASR/semantic fixture and executor, then a persisted source change.

The transport/recognizer are declared synthetic; no device or model claim.
"""
import json

from test_school_supply_resume_http import school


def test_scheduled_original_rejects_changed_plan_source_without_changing_prompt(school):
    case = school
    turn = case['say']('visual-check-school-original')
    assert turn['runtime_capability'] == 'task.resume', turn
    executed = case['api'].post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
    assert executed.status_code == 200, executed.text
    prompt = executed.json()['execution']['result']['diagnostic_prompt']['prompt']
    path = '/v1/sessions/' + case['sid'] + '/diagnostic-visual'
    params = {k: prompt[k] for k in ('prompt_id', 'item_version')}
    result = case['api'].get(path, params=params)
    assert result.status_code == 200 and result.json()['materials'] == [], result.text
    with case['db'].connect() as c:
        before = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone())
        command = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
        target = json.loads(command[0])['task_start_binding']['plan_item_id']
        # Fault injection after a real start: the item is still current, but
        # its original scheduled source is no longer the persisted binding.
        c.execute('UPDATE plan_item SET source_ref=? WHERE plan_item_id=?', ('isolated:replaced-source', target))
        c.commit()
    result = case['api'].get(path, params=params)
    assert result.status_code == 409, result.text
    assert result.headers['cache-control'] == 'no-store'
    with case['db'].connect() as c:
        assert dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()) == before
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
