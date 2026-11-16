from uuid import uuid4

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_mistake_gate_reliability import CHILD, SECOND, flow, prepare


def test_differential_http_persists_orders_and_isolates(flow, tmp_path):
    db, learning, sid, clock = flow
    book, case = prepare(flow)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    path = f'/v1/mistakes/{case}/diagnosis'
    with TestClient(create_app(settings)) as client:
        assert client.get(path, params={'child_id': CHILD}).json()['version'] == 0
        assert client.get(path, params={'child_id': 'child-junior-3'}).status_code == 404
        payload = {'child_id': CHILD, 'expected_version': 0, 'client_action_id': str(uuid4()),
                   'hypotheses': [{'error_type': 'concept_gap', 'stance': 'candidate', 'explanation': 'Possible rule gap'}]}
        proposed = client.post(path + '/hypotheses', json=payload)
        assert proposed.status_code == 200, proposed.text
        assert client.post(path + '/hypotheses', json=payload).json() == proposed.json()
        stale = dict(payload, client_action_id=str(uuid4()))
        assert client.post(path + '/hypotheses', json=stale).status_code == 409
        hypothesis = next(h for h in proposed.json()['hypotheses'] if h['stance'] == 'candidate')
        question = client.post(path + '/probes', json={
            'child_id': CHILD, 'expected_version': 1, 'client_action_id': str(uuid4()),
            'diagnostic_item_id': SECOND, 'predictions': {hypothesis['hypothesis_id']: 'incorrect'},
        })
        assert question.status_code == 200, question.text
        answered = client.post(path + '/answers', json={
            'child_id': CHILD, 'expected_version': 2, 'client_action_id': str(uuid4()),
            'probe_event_id': question.json()['event_id'], 'answer': '42',
        })
        assert answered.status_code == 200, answered.text
        assert answered.json()['assessment']['support'] == []
        from datetime import timedelta
        clock[0] += timedelta(seconds=1)
        receipt = learning.submit_diagnostic(sid, SECOND, '42', str(uuid4()))
        attached = client.post(path + '/evidence', json={
            'child_id': CHILD, 'expected_version': 3, 'client_action_id': str(uuid4()),
            'answer_event_id': answered.json()['event_id'], 'evidence_id': receipt['evidence_id'],
        })
        assert attached.status_code == 200, attached.text
        assert attached.json()['assessment']['causal_status'] == 'unconfirmed'
    with TestClient(create_app(settings)) as client:
        current = client.get(path, params={'child_id': CHILD}).json()
        assert current['version'] == 4
        retracted = client.post(path + '/retractions', json={
            'child_id': CHILD, 'expected_version': 4, 'client_action_id': str(uuid4()),
            'target_event_id': answered.json()['event_id'], 'reason': 'Answer was misheard',
        })
        assert retracted.status_code == 200, retracted.text
        assert retracted.json()['assessment']['support'] == []
        assert retracted.json()['assessment']['counterevidence'] == []
        assert len(retracted.json()['history']) == 5
        foreign = dict(payload, child_id='child-junior-3', client_action_id=str(uuid4()))
        assert client.post(path + '/hypotheses', json=foreign).status_code == 404
        invalid = dict(payload, expected_version=True)
        assert client.post(path + '/hypotheses', json=invalid).status_code == 422
