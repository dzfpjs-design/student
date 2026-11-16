import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_learning_rubric_review import candidate, gate, flow, CHILD


def payload(snapshot, key='review-http'):
    return {'expected_revision': 0, 'client_action_id': key, 'operation': 'approve',
            'reason': 'Checked the original answer and conditions', 'independent': True, 'hint_level': 0,
            'dimensions': [{'dimension_id': d['dimension_id'], 'score': 3,
                            'response_quotes': [snapshot['response']], 'rationale': 'Checked actual reasoning'}
                           for d in snapshot['dimensions']],
            'criterion_evidence': [{'criterion': c, 'response_quote': snapshot['response']}
                                   for c in snapshot['criteria']]}


def test_real_application_review_replay_revision_and_restart(gate, tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=gate.db.path, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        snapshot, _, _ = candidate(gate)
        root = f'/v1/parent/children/{CHILD}/learning-rubrics'
        path = root + '/' + snapshot['assessment_id']
        assert client.get(root).status_code in {401, 403}
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        listed = client.get(root)
        assert listed.status_code == 200, listed.text
        assert listed.headers['cache-control'] == 'no-store'
        assert listed.json()['total'] == 1
        body = payload(snapshot)
        first = client.post(path + '/review', json=body)
        assert first.status_code == 200, first.text
        replay = client.post(path + '/review', json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['event'] == first.json()['event']
        assert first.json()['cloud_transmission_allowed'] is False
        assert client.post(path + '/review', json={**body, 'reviewer_role': 'teacher'}).status_code == 422
        assert client.post(path + '/review', json={**body, 'reason': 'changed'}).status_code == 409
        assert client.get(path.replace(CHILD, 'child-junior-3')).status_code == 409
    with TestClient(create_app(settings)) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        restored = client.get(path).json()
        assert len(restored['history']) == 1
        assert restored['assessment']['review_revision'] == 1
        retract = client.post(path + '/review', json={'expected_revision': 1,
            'client_action_id': 'retract-http', 'operation': 'retract', 'reason': 'Child had assistance'})
        assert retract.status_code == 200, retract.text
        assert retract.json()['assessment']['score_status'] == 'withdrawn'
        assert retract.json()['qualification']['eligible_for_mastery_gate'] is False


@pytest.mark.parametrize('scope', ['learning', 'tutor', 'all_history'])
def test_rubric_export_and_scope_deletion(gate, tmp_path, scope):
    snapshot, _, _ = candidate(gate)
    from test_learning_rubric_review import review
    from home_study_agent.learning_rubric import RubricEvidenceService
    review(RubricEvidenceService(gate.db), snapshot)
    service = PrivacyLifecycleService(gate.db, tmp_path)
    service.initialize()
    assert service.deletion_preview(CHILD)['rubrics'] == 2
    exported = service.export_child(CHILD)
    with open(exported['path'], encoding='utf-8') as stream:
        data = json.load(stream)
    assert len(data['rubric_assessments']) == len(data['rubric_review_events']) == 1
    result = service.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))
    assert result['status'] == 'completed'
    assert service.deletion_preview(CHILD)['rubrics'] == 0
    with gate.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
