import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.parent_reports import ParentReportService
from test_parent_coaching_feedback_semantics import CHILD, repository, report


@pytest.mark.parametrize('decision', ['custom', 'skip'])
def test_actual_parent_review_feedback_and_restart_use_reviewed_action(repository, tmp_path, decision):
    saved = report(repository)
    review = ParentReportService(repository.database).create_from_daily(saved)
    settings = Settings(data_dir=tmp_path, database_path=repository.database.path, maintenance_enabled=False)
    review_path = f"/v1/parent/report-snapshots/{review['snapshot_id']}"
    feedback_path = f"/v1/parent/reports/{saved['report_id']}/feedback"
    with TestClient(create_app(settings)) as client:
        patch = {'coaching_decision': decision, 'coaching_action': '家长陪孩子画一小步',
                 'coaching_script': '先听你说想画哪一步。', 'coaching_duration_minutes': 2}
        assert client.post(review_path + '/approve', json=patch).status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        # App startup revalidates the legacy fixture report. Use the actual
        # current draft offered by the parent screen, never approve the old one.
        current_reviews = client.get(f'/v1/parent/children/{CHILD}/report-snapshots', headers=headers).json()['items']
        review = next(item for item in current_reviews if item['report_id'] == saved['report_id'] and item['status'] == 'draft')
        review_path = f"/v1/parent/report-snapshots/{review['snapshot_id']}"
        approved = client.post(review_path + '/approve', json=patch, headers=headers)
        assert approved.status_code == 200, approved.text
        current = client.get(f'/v1/children/{CHILD}/reports', headers=headers).json()['items'][0]
        action = current['parent_action']
        assert action['review_binding']['decision'] == decision
        assert action['snapshot_id'] != saved['parent_action']['snapshot_id']
        payload = {'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'actual-reviewed-action',
                   'expected_revision': 0, 'executed': True, 'executed_at': action['created_at'], 'effectiveness': 3}
        response = client.put(feedback_path, headers=headers, json=payload)
        if decision == 'skip':
            assert response.status_code == 422
            assert 'PARENT_ACTION_SKIPPED_NOT_EXECUTABLE' in response.text
        else:
            assert response.status_code == 200, response.text
            assert response.json()['action_snapshot_id'] == action['snapshot_id']
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        current = client.get(f'/v1/children/{CHILD}/reports', headers=headers).json()['items'][0]
        assert current['parent_action']['snapshot_id'] == action['snapshot_id']
        if decision == 'custom':
            assert current['parent_action']['feedback']['client_action_id'] == 'actual-reviewed-action'
            assert client.put(feedback_path, headers=headers, json=payload).json() == response.json()
        revoked = client.post(review_path + '/revoke', headers=headers, json={'reason': '重新核对孩子当天状态'})
        assert revoked.status_code == 200
        current = client.get(f'/v1/children/{CHILD}/reports', headers=headers).json()['items'][0]
        assert current['parent_action']['binding_status'] == 'unknown'
        denied = client.put(feedback_path, headers=headers,
            json={**payload, 'client_action_id': 'late-after-review-revocation', 'expected_revision': 1 if decision == 'custom' else 0})
        assert denied.status_code == 409
