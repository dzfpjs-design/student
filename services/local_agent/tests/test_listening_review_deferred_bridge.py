"""Exact frontend early-answer bridge outcome, through the actual app router.

The frozen journey remains unchanged. The client wrapper inserts the browser's
real PUT after the first 409; all production methods and model fixtures still run.
"""
import json

from fastapi.testclient import TestClient

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.companion_memory import CompanionMemory
from home_study_agent.repository import utc_now
from test_pet_media_evidence import media
from test_listening_review_journey import test_day_summary_to_real_voice_review_waits_for_new_original_audio as journey


def facts(database, prompt):
    tables = ('diagnostic_attempt', 'learning_evidence', 'learning_evaluation_execution',
        'rubric_assessment', 'learning_feedback', 'companion_memory_source',
        'companion_memory', 'companion_memory_evidence')
    with database.connect() as c:
        counts = {table: c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            for table in tables if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()}
        counts['prompt'] = tuple(c.execute('''SELECT status,attempt_id,answer_action_id,answer_json
            FROM diagnostic_prompt WHERE prompt_id=?''', (prompt['prompt_id'],)).fetchone())
        counts['review'] = tuple(c.execute('''SELECT status,result_evidence_id FROM review_task
            WHERE review_task_id=?''', (prompt['review_task_id'],)).fetchone())
        counts['tasks'] = [tuple(row) for row in c.execute('''SELECT plan_item_id,status,started_at,completed_at
            FROM plan_item WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?)
            ORDER BY plan_item_id''', (prompt['origin_session_id'],)).fetchall()]
        counts['mastery'] = [tuple(row) for row in c.execute('''SELECT * FROM mastery_state
            WHERE child_id=? ORDER BY knowledge_node_id''', (prompt['child_id'],)).fetchall()]
    return counts


def test_frontend_completed_bridge_is_not_an_answer_and_new_confirmation_survives_restart(media, tmp_path, monkeypatch):
    post = TestClient.post
    observed = {}
    calls = {'finalizer': [], 'memory': []}
    finalize = AgentRuntimeStore.finalize_diagnostic_turn
    observe = CompanionMemory.observe_turn

    def traced_finalize(self, turn_id):
        result = finalize(self, turn_id)
        calls['finalizer'].append((turn_id, result))
        return result

    def traced_observe(self, turn_id):
        result = observe(self, turn_id)
        calls['memory'].append(turn_id)
        return result

    monkeypatch.setattr(AgentRuntimeStore, 'finalize_diagnostic_turn', traced_finalize)
    monkeypatch.setattr(CompanionMemory, 'observe_turn', traced_observe)

    def current_browser_post(client, url, **kwargs):
        response = post(client, url, **kwargs)
        if observed or not str(url).endswith('/diagnostic-prompt/answer') or response.status_code != 409:
            return response
        body = kwargs['json']
        if not body['client_action_id'].startswith('diagnostic-answer:'):
            return response
        observed['turn_id'] = body['authorized_turn_id']
        sid = str(url).split('/')[3]
        output_response = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': body['prompt_id']})
        assert output_response.status_code == 200, output_response.text
        output = output_response.json()
        assert output['status'] == 'waiting_answer' and output['attempt'] is None, output
        prompt = output['prompt']
        db = client.app.state.repository.database
        before = facts(db, prompt)
        assert before['prompt'] == ('waiting_answer', None, None, None)
        assert before['review'] == ('pending', None)
        receipt = {'capability': 'diagnostic.submit', 'execution_location': 'client_device_bridge',
            'action_type': 'submit_diagnostic_answer', 'status': 'completed',
            'completed_at': utc_now(), 'output': output}
        payload = {'final_response': output['deferred_answer']['message'], 'tool_status': 'completed',
            'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}}
        url = f"/v1/agent-turns/{body['authorized_turn_id']}/outcome"
        completed = client.put(url, json=payload)
        observed.update(status=completed.status_code, body=completed.json(), before=before,
            after=facts(db, prompt), calls=calls)
        (tmp_path / 'deferred-bridge-result.json').write_text(json.dumps(observed, ensure_ascii=False, default=str))
        assert completed.status_code == 200, completed.text
        assert observed['after'] == before, observed
        assert completed.json().get('local_evaluation') is None, completed.text
        with db.connect() as c:
            row = c.execute('''SELECT t.tool_status,s.status AS step_status,s.result_json,p.status AS plan_status
                FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0''',
                (body['authorized_turn_id'],)).fetchone()
            assert row['tool_status'] == row['step_status'] == 'completed'
            assert row['plan_status'] == 'waiting_child'
            assert json.loads(row['result_json'])['client_bridge_receipt']['output'] == output
        replay = client.put(url, json=payload)
        assert replay.status_code == 200 and facts(db, prompt) == before, replay.text
        assert all(result is None for turn_id, result in calls['finalizer'] if turn_id == observed['turn_id'])
        return response

    monkeypatch.setattr(TestClient, 'post', current_browser_post)
    journey(media, tmp_path, monkeypatch, False, False, False)
    assert observed['status'] == 200
