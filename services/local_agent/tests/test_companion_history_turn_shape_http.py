"""Only the optional original-turn field is damaged, not the valid return."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_companion_verified_return import returned, execution, goal_db, exact_goal_item, CHILD, OTHER


@pytest.mark.parametrize('turn_ref', [['invalid-ref'], {'invalid':'ref'}], ids=['array','object'])
def test_parent_projection_isolates_nonstring_event_turn_reference(returned, monkeypatch, turn_ref):
    db, sid, _, prompt, answer, good_id, _ = returned
    async def forbidden(*args, **kwargs):
        pytest.fail('Parent reads must not call a model')
    monkeypatch.setattr(ModelGateway, 'complete', forbidden)
    settings = Settings(data_dir=db.path.parent, database_path=db.path,
        data_encryption_key=b'c'*32, maintenance_enabled=False)
    route = f'/v1/parent/children/{CHILD}/longitudinal-model'
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin':'7391'}).json()['parent_token']
        headers = {'X-HSC-Parent':token}
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id':prompt['prompt_id'], 'item_version':prompt['item_version'],
            'answer':answer, 'client_action_id':'original-answer'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'answered'
        assert client.get(route, headers=headers).json()['payload']['companion_balance']['verified_child_return_sessions'] == 1
        runtime = AgentRuntimeStore(db)
        runtime.start_companion_activity(sid, 'movement', 'Synthetic unrelated stretch', 'second-activity', None)
        runtime.end_companion_activity(sid, 'turn_limit')
        bad_id = next(row['outcome_id'] for row in CompanionActivityOutcomeService(db).list(CHILD)['items']
                      if row['outcome_id'] != good_id)
        with db.connect() as c:
            row = c.execute('SELECT event_id,payload_json FROM companion_activity_outcome_event WHERE outcome_id=?', (bad_id,)).fetchone()
            original = json.loads(row['payload_json'])
            damaged = json.dumps({**original, 'agent_turn_id':turn_ref})
            trigger = c.execute("SELECT sql FROM sqlite_master WHERE name='companion_activity_outcome_event_no_update'").fetchone()[0]
            c.execute('DROP TRIGGER companion_activity_outcome_event_no_update')
            c.execute('UPDATE companion_activity_outcome_event SET payload_json=? WHERE event_id=?', (damaged,row['event_id']))
            c.execute(trigger)
            c.commit()
        response = client.get(route, headers=headers)
        assert response.status_code == 200, response.text
        balance = response.json()['payload']['companion_balance']
        assert balance['verified_child_return_sessions'] == 1
        assert balance['task_resume_receipt_sessions'] == 1
        assert balance['habit_success_sessions'] == 0
        damaged_view = next(e for e in balance['outcome_evidence'] if e['outcome_id'] == bad_id)
        assert damaged_view['return_verification']['reason'] == 'activity_history_unavailable'
        assert damaged_view['return_verification']['status'] == 'unknown'
        assert damaged_view['source_valid'] is False
        with db.connect() as c:
            assert c.execute('SELECT payload_json FROM companion_activity_outcome_event WHERE event_id=?', (row['event_id'],)).fetchone()[0] == damaged
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        other = client.get(route.replace(CHILD,OTHER), headers=headers)
        assert other.status_code == 200, other.text
        assert other.json()['payload']['companion_balance']['verified_child_return_sessions'] == 0
