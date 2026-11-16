from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.parent_reports import ParentReportService
from test_period_report_reliable_evidence import flow, answer, report, CHILD, OTHER
from test_daily_learning_closure_audit import learning_clock


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_real_period_action_http_feedback_restart_and_retraction(flow, tmp_path, kind):
    settings = Settings(data_dir=tmp_path, database_path=flow.db.path, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        answer(flow, '12')
        draft = ParentReportService(flow.db).create_from_period(report(flow, kind))
        report_path = f"/v1/parent/report-snapshots/{draft['snapshot_id']}"
        response = client.post(report_path + '/approve', headers=headers, json={
            'coaching_decision': 'custom', 'coaching_action': '一起核对原题的一步',
            'coaching_script': '你来选择从哪一步开始。', 'coaching_duration_minutes': 3,
        })
        assert response.status_code == 200, response.text
        action = response.json()['parent_action']
        assert action['report_id'] is None
        assert action['source_kind'] == 'period_report'
        root = f"/v1/parent/children/{CHILD}/parent-actions/{action['snapshot_id']}"
        wrong_child = root.replace(CHILD, OTHER)
        assert client.get(root).status_code == 401
        assert client.get(wrong_child, headers=headers).status_code == 404
        assert client.get(root, headers=headers).json()['snapshot_id'] == action['snapshot_id']
        payload = {'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'actual-period-action-feedback',
            'expected_revision': 0, 'executed': True, 'executed_at': datetime.now(timezone.utc).isoformat(),
            'effectiveness': 3, 'stress_rating': 2, 'notes_summary': '合成记录，不证明因果'}
        assert client.put(root + '/feedback', json=payload).status_code == 401
        assert client.put(wrong_child + '/feedback', headers=headers, json=payload).status_code == 404
        mismatch = client.put(root + '/feedback', headers=headers, json={**payload, 'action_snapshot_id': 'wrong-action'})
        assert mismatch.status_code == 409
        assert client.put(root + '/feedback', headers=headers, json={'executed': False}).status_code == 422
        saved = client.put(root + '/feedback', headers=headers, json=payload)
        assert saved.status_code == 200, saved.text
        receipt = saved.json()
        assert receipt['action_snapshot_id'] == action['snapshot_id']
        assert receipt['period_report_id'] == action['period_report_id']
        assert receipt['report_id'] is None
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        assert client.get(root, headers=headers).json()['feedback'] == receipt
        assert client.put(root + '/feedback', headers=headers, json=payload).json() == receipt
        history = client.get(root + '/history', headers=headers)
        assert history.status_code == 200
        assert history.json()['items'] == [receipt]
        assert client.post(report_path + '/revoke', headers=headers, json={'reason': '重新核对建议'}).status_code == 200
        denied = client.put(root + '/feedback', headers=headers, json={**payload,
            'client_action_id': 'after-revocation-feedback', 'expected_revision': 1})
        assert denied.status_code == 409, denied.text
        retracted = client.put(root + '/feedback', headers=headers, json={
            'action_snapshot_id': action['snapshot_id'], 'client_action_id': 'withdraw-period-feedback',
            'expected_revision': 1, 'operation': 'retract', 'reason': '原反馈不适用',
        })
        assert retracted.status_code == 200, retracted.text
        assert retracted.json()['operation'] == 'retract'
        assert [event['revision'] for event in client.get(root + '/history', headers=headers).json()['items']] == [1, 2]
