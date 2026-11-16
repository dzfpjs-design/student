"""Discover lost browser identities from local durable pending operations only."""
import json
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, start


def seed(db, sid, key, *, status='pending', action='submit_diagnostic_answer', child=None):
    with db.connect() as c:
        owner = child or c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
        c.execute('''INSERT INTO agent_turn (agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,tool_status,created_at,client_action_id)
            VALUES (?,?,?,'PRIVATE SYNTHETIC ANSWER','diagnostic.submit','PRIVATE REPLY',?,'test',?,?,?)''',
            ('turn-'+key, owner, sid, json.dumps({'type': action, 'answer': 'PRIVATE ANSWER'}),
             status, '2026-10-07T13:58:13.421Z', key))
        c.commit()


def test_owned_pending_identity_survives_restart_without_content_or_writes(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        seed(db, sid, 'original-saved-answer')
        seed(db, sid, 'internal-evaluation', action='none')
        seed(db, sid, 'earlier-completed', status='completed')
        seed(db, sid, 'failed-operation', status='failed')
        seed(db, sid, 'wrong-owner', child='child-junior-3')
        route = f'/v1/sessions/{sid}/agent-execution-recovery-index?child_id=child-primary-4'
        with db.connect() as c:
            before = c.execute('SELECT * FROM agent_turn ORDER BY agent_turn_id').fetchall()
        response = client.get(route)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        result = response.json()
        assert result['local_only'] is True and result['cloud_transmission_allowed'] is False
        assert result['replay_allowed'] is False and result['has_more'] is False
        assert result['entries'] == [{'agent_turn_id': 'turn-original-saved-answer',
            'client_action_id': 'original-saved-answer', 'created_at': '2026-10-07T13:58:13.421Z'}]
        assert 'PRIVATE' not in response.text and 'action_json' not in response.text
        assert client.get(route.replace('child-primary-4', 'child-junior-3')).status_code == 404
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM agent_turn ORDER BY agent_turn_id')] == [tuple(r) for r in before]
    with TestClient(create_app(settings)) as client:
        assert client.get(route).json() == result


def test_index_bounds_backlog_truthfully(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        for i in range(129):
            seed(db, sid, f'pending-{i:03}')
        response = client.get(f'/v1/sessions/{sid}/agent-execution-recovery-index?child_id=child-primary-4')
        assert response.status_code == 200
        assert len(response.json()['entries']) == 128 and response.json()['has_more'] is True
