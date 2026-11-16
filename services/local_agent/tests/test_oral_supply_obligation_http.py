"""An explicit missing oral question survives restart and actual prompt delivery.

Availability changes below are test-only catalog fixtures, not evidence of model
generation, publication review, cross-day planning or real acoustic performance.
"""
import json
from synthetic_oral_app_lifecycle import fresh_oral_case

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_open_response_spoken_runtime_http import (
    test_natural_spoken_process_asr_bound_answer_formative_and_restart as run_journey,
)
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('subject', ['authored:数学', 'authored-p4:数学'])
@pytest.mark.parametrize('fault', [None, 'write_failure', 'source_changed', 'cancelled_goal', 'prepared_write_failure', 'request_fingerprint'])
def test_missing_question_request_replay_and_restart_keep_original_goal(tmp_path, monkeypatch, subject, fault,
        after_pending=None, after_available=None, http_client_address=None):
    def inspect(j):
        client, db, prompt = j['client'], j['db'], j['prompt']
        evaluated = client.post('/v1/agent-turns/' + j['follow']['feedback_turn_id'] + '/execute')
        assert evaluated.status_code == 200, evaluated.text
        with db.connect() as c:
            c.execute('BEGIN')
            packet = AgentRuntimeStore.resolve_oral_followup(c, child_id=j['child'], session_id=j['sid'],
                prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], kind='independent_check')
            assert packet['status'] == 'ready', packet
            next_id = packet['action']['diagnostic_item_id']
            goal = c.execute("SELECT objective_id FROM agent_objective WHERE child_id=? AND status='active'",
                (j['child'],)).fetchone()[0]
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id=? AND diagnostic_item_id!=?",
                (packet['action']['knowledge_node_id'], prompt['diagnostic_item_id']))
            if fault == 'write_failure':
                c.execute('''CREATE TRIGGER deny_oral_supply BEFORE INSERT ON agent_commitment
                    WHEN json_extract(NEW.payload_json,'$.origin')='oral_question_supply_v1'
                    BEGIN SELECT RAISE(ABORT,'synthetic commitment write failure'); END''')
            c.commit()
        quote = '换一道没做过的题，我自己试试'
        j['command_state'].update(control='oral.independent_check', quote=quote)
        voice = capture(client, j['settings'], monkeypatch, j['sid'], quote, prompt, 'gap-voice', child=j['child'])
        if fault == 'write_failure':
            # Keep the ordinary request code; surface an actual storage exception
            # without converting it to a fake successful response.
            response = TestClient(client.app, raise_server_exceptions=False)
            try:
                response.headers.update(client.headers)
                reply = response.post(f"/v1/sessions/{j['sid']}/agent-turn", json=dict(
                    transcript=quote, client_action_id='gap-request', voice_input_id=voice, cloud_text_allowed=True))
                assert reply.status_code >= 500, reply.text
            finally:
                response.close()
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM agent_commitment WHERE json_extract(payload_json,'$.origin')='oral_question_supply_v1'").fetchone()[0] == 0
                assert not c.execute("SELECT 1 FROM agent_turn WHERE client_action_id='gap-request'").fetchone()
                c.execute('DROP TRIGGER deny_oral_supply')
                c.commit()
        requested = interpret(client, j['sid'], quote, 'gap-request', voice)
        assert requested['action']['type'] == 'none'
        assert '记在刚才的学习目标下' in requested['response_text'], requested
        assert interpret(client, j['sid'], quote, 'gap-request', voice)['agent_turn_id'] == requested['agent_turn_id']
        assert client.post('/v1/agent-turns/' + requested['agent_turn_id'] + '/execute').status_code == 200
        with db.connect() as c:
            rows = c.execute("SELECT * FROM agent_commitment WHERE json_extract(payload_json,'$.origin')='oral_question_supply_v1'").fetchall()
            assert len(rows) == 1
            pending = dict(rows[0])
            payload = json.loads(pending['payload_json'])
            assert pending['objective_id'] == goal and pending['child_id'] == j['child']
            assert pending['status'] == 'pending' and pending['completed_at'] is None
            assert payload['source']['parent_prompt_id'] == prompt['prompt_id']
            assert payload['phase'] == 'awaiting_qualified_question' and payload['resolution'] is None
            assert payload['local_only'] and not payload['cloud_transmission_allowed']
            assert not payload['mastery_claim_allowed']
        if after_pending and after_pending(j, pending) is True:
            return
        if fault == 'request_fingerprint':
            from home_study_agent.agent_runtime import InvalidAgentPlanError
            from home_study_agent.oral_supply_obligation import record
            with db.connect() as c:
                c.execute('BEGIN')
                c.execute("UPDATE voice_control_intent_request SET fingerprint='different spoken request' WHERE request_key=?",
                    (payload['request_key'],))
                with pytest.raises(InvalidAgentPlanError, match='ANSWER_ORAL_SUPPLY_SOURCE_CHANGED'):
                    record(c, requested['agent_turn_id'], payload['request_key'])
                c.rollback()
                assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',
                    (pending['commitment_id'],)).fetchone()) == pending
            return
        if fault == 'cancelled_goal':
            with db.connect() as c:
                c.execute("UPDATE agent_objective SET status='cancelled' WHERE objective_id=?", (goal,))
                c.commit()
            AgentRuntimeStore(db).reconcile_learning_state(j['child'])
            AgentRuntimeStore(db).reconcile_learning_state(j['child'])
            with db.connect() as c:
                cancelled = c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',
                    (pending['commitment_id'],)).fetchone()
                assert cancelled['status'] == 'cancelled' and cancelled['completed_at'] is None
                assert cancelled['payload_json'] == pending['payload_json']
            return
        with fresh_oral_case({**j,'client_options':{}},TestClient,client=('testclient',50000)) as reopened_case:
            reopened=reopened_case['client']
            with db.connect() as c:
                assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',
                    (pending['commitment_id'],)).fetchone()) == pending
            second_voice = capture(reopened, j['settings'], monkeypatch, j['sid'], quote, prompt,
                'gap-repeat-voice', child=j['child'])
            second = interpret(reopened, j['sid'], quote, 'gap-repeat', second_voice)
            assert '记在刚才的学习目标下' in second['response_text']
            assert reopened.post('/v1/agent-turns/' + second['agent_turn_id'] + '/execute').status_code == 200
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM agent_commitment WHERE json_extract(payload_json,'$.origin')='oral_question_supply_v1'").fetchone()[0] == 1
                if fault == 'source_changed':
                    c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
                c.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id=?", (next_id,))
                c.commit()
            if after_available:
                quote = after_available(j, reopened, pending, next_id) or quote
                j['command_state']['quote'] = quote
            available_voice = capture(reopened, j['settings'], monkeypatch, j['sid'], quote, prompt,
                'gap-ready-voice', child=j['child'])
            available = interpret(reopened, j['sid'], quote, 'gap-ready', available_voice)
            if fault == 'source_changed':
                assert available.get('runtime_capability') != 'oral.independent_check', available
                with db.connect() as c:
                    assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',
                        (pending['commitment_id'],)).fetchone()[0] != 'completed'
                return
            assert available['runtime_capability'] == 'oral.independent_check', available
            path = '/v1/agent-turns/' + available['agent_turn_id'] + '/execute'
            if fault == 'prepared_write_failure':
                with db.connect() as c:
                    c.execute('''CREATE TRIGGER deny_supply_receipt BEFORE UPDATE ON agent_commitment
                        WHEN NEW.status='completed' AND json_extract(NEW.payload_json,'$.origin')='oral_question_supply_v1'
                        BEGIN SELECT RAISE(ABORT,'synthetic receipt write failure'); END''')
                    c.commit()
                portal = TestClient(reopened.app, raise_server_exceptions=False)
                try:
                    portal.headers.update(reopened.headers)
                    failed = portal.post(path)
                    assert failed.status_code >= 500, failed.text
                finally:
                    portal.close()
                with db.connect() as c:
                    assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE child_id=?', (j['child'],)).fetchone()[0] == 1
                    assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',
                        (pending['commitment_id'],)).fetchone()[0] == 'pending'
                    c.execute('DROP TRIGGER deny_supply_receipt')
                    c.commit()
            delivered = reopened.post(path)
            assert delivered.status_code == 200, delivered.text
            new_prompt = delivered.json()['execution']['result']['prompt']
            assert new_prompt['diagnostic_item_id'] == next_id
            assert reopened.post(path).json()['execution']['result']['prompt']['prompt_id'] == new_prompt['prompt_id']
            with db.connect() as c:
                done = dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',
                    (pending['commitment_id'],)).fetchone())
                resolution = json.loads(done['payload_json'])['resolution']
                assert done['status'] == 'completed' and done['completed_at']
                assert done['objective_id'] == goal
                assert resolution['prompt_id'] == new_prompt['prompt_id'] and not resolution['learning_completed']
                linked = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',
                    (available['agent_turn_id'],)).fetchone()[0]
                assert linked == goal
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?', (j['child'],)).fetchone()[0] == 1
                assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',
                    (j['attempt']['attempt_id'],)).fetchone()[0] == j['quote']
                assert c.execute('SELECT status FROM agent_objective WHERE objective_id=?', (goal,)).fetchone()[0] != 'completed'
            cloud = json.dumps(j['posts'], ensure_ascii=False)
            assert 'oral_question_supply_v1' not in cloud and pending['commitment_id'] not in cloud
    # The existing scheduled school fixture is junior-only. Primary enters its
    # own real learning goal by voice, never by borrowing the junior session.
    run_journey(tmp_path, monkeypatch, subject, None, not subject.startswith('authored-p4:'),
        None, after_saved=inspect,http_client_address=http_client_address)
