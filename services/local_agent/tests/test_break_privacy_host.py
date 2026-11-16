"""Actual break receipts are exported and erased, not revived as new commands."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_companion_activity_host import NoCloud
from test_diagnostic_prompt_http import environment, start, CHILD


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_break_receipt_export_delete_restart(environment, scope):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        result = client.post(f'/v1/sessions/{sid}/break', json={'client_action_id': 'break-for-privacy'})
        assert result.status_code == 200, result.text
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        exported = client.app.state.privacy_lifecycle.export_child(CHILD)
        data = json.loads(Path(exported['path']).read_text())
        assert len(data['break_control_receipts']) == 1
        assert data['break_control_receipts'][0]['operation'] == 'begin'
        assert data['break_control_receipts'][0]['child_id'] == CHILD
        response = client.post(f'/v1/parent/children/{CHILD}/delete', json={
            'scope': scope, 'confirmation': '确认删除', 'client_action_id': 'delete-break-receipt'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed'
        assert response.json()['deleted_counts']['break_control_receipt'] == 1
    with TestClient(create_app(settings, model_gateway=NoCloud())):
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM break_control_receipt WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
