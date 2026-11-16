"""Optional historical damage cannot disable an unrelated exact school review."""
import json
import sqlite3

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.school_open_reviews import SchoolReviewError
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD
from test_voice_runtime_binding import capture, interpret


def old_control(case, monkeypatch):
    db, client, _, sid = case[:4]
    text = '暂停高拍仪'
    before = len(client.school_gateway.requests)
    response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text,
        'client_action_id': 'history-control-command',
        'cloud_text_allowed': True,
    })
    assert response.status_code == 200, response.text
    turn = response.json()
    assert turn['action']['type'] == 'control_document_monitor'
    assert len(client.school_gateway.requests) == before
    with db.connect() as c:
        row = c.execute('SELECT plan_id FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
    assert row
    return row[0]


@pytest.mark.parametrize('bad', ['{broken', '[]', 'null', '"text"', '1'])
def test_unrelated_control_manifest_keeps_review_voice_and_restart(school_case, monkeypatch, bad):
    db, client, _, _ = school_case[:4]
    answer_and_review(school_case)
    plan_id = old_control(school_case, monkeypatch)
    case, task, _, turn, _ = next_day(school_case, monkeypatch, key='history-near', execute=False)
    client = case[1]
    with db.connect() as c:
        c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?', (bad, plan_id))
        c.commit()
    first = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert first.status_code == 200, first.text
    prompt = first.json()['execution']['result']['diagnostic_prompt']['prompt']
    before = len(client.school_gateway.requests)
    replay = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert replay.status_code == 200, replay.text
    assert len(client.school_gateway.requests) == before
    reopened = client.school_reopen()
    got = reopened.get(f"/v1/sessions/{case[3]}/diagnostic-prompt", params={'prompt_id':prompt['prompt_id']})
    assert got.status_code == 200, got.text
    assert got.json()['prompt']['prompt_id'] == prompt['prompt_id']
    assert got.json()['status'] == 'waiting_answer'
    with db.connect() as c:
        assert c.execute('SELECT context_manifest_json FROM agent_plan WHERE plan_id=?', (plan_id,)).fetchone()[0] == bad
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (task['plan_item_id'],)).fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (task['source_ref'],)).fetchone()[0] == 0


@pytest.mark.parametrize('bad', ['{broken', '[]', 'null', '"text"', '1'])
def test_required_plan_snapshot_damage_is_controlled_not_repaired(school_case, monkeypatch, bad):
    db = school_case[0]
    answer_and_review(school_case)
    case, task, prompt, _, _ = next_day(school_case, monkeypatch, key='required-near')
    with db.connect() as c:
        row = c.execute("SELECT decision_id FROM plan_constraint_decision WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?) AND source_ref=? AND decision='scheduled'",
            (task['plan_item_id'], task['source_ref'])).fetchone()
        c.execute('UPDATE plan_constraint_decision SET metadata_json=? WHERE decision_id=?', (bad, row[0]))
        c.commit()
    got = case[1].get(f'/v1/sessions/{case[3]}/diagnostic-prompt', params={'prompt_id':prompt['prompt_id']})
    assert got.status_code == 200, got.text
    assert got.json()['status'] == 'invalidated', got.text
    assert got.json()['attempt'] is None
    reopened = case[1].school_reopen()
    again = reopened.get(f'/v1/sessions/{case[3]}/diagnostic-prompt', params={'prompt_id':prompt['prompt_id']})
    assert again.status_code == 200 and again.json()['status'] == 'invalidated', again.text
    with db.connect() as c:
        assert c.execute('SELECT metadata_json FROM plan_constraint_decision WHERE decision_id=?', (row[0],)).fetchone()[0] == bad
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (task['source_ref'],)).fetchone()[0] == 0


def test_school_history_guards_do_not_swallow_database_failure():
    from home_study_agent.school_review_execution import _bindings, resolve_execution
    from home_study_agent.school_open_reviews import reconcile_school_reviews
    class Broken:
        in_transaction = True
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError('isolated IO read failure')
    for run in (lambda: _bindings(Broken(), 'child', 'session', 'task'),
                lambda: resolve_execution(Broken(), 'session', 'task'),
                lambda: reconcile_school_reviews(Broken(), 'child')):
        with pytest.raises(sqlite3.OperationalError, match='isolated IO read failure'):
            run()
