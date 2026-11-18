import base64
import json
import sqlite3
import pytest

from test_app import client_for
from test_visual_material import frame as synthetic_image
from home_study_agent.repository import PlanInputError


def test_disabled_candidate_and_existing_unconfirmed_artifacts_cannot_enter_homework_or_plan(tmp_path):
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        session = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'guard-session-1'}).json()
        sid = session['session_id']
        client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'check_in', 'target_state': 'classroom_recall', 'reason': 'ready', 'client_action_id': 'guard-transition-1'})
        client.post(f'/v1/sessions/{sid}/recalls', json={'subject': '数学', 'summary': '复习计算。', 'client_action_id': 'guard-recall-1'})
        client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'classroom_recall', 'target_state': 'homework_capture', 'reason': 'done', 'client_action_id': 'guard-transition-2'})

        blocked = client.post(f'/v1/sessions/{sid}/document-monitor/ocr', json={
            'image_base64': base64.b64encode(synthetic_image()).decode(),
            'client_action_id': 'guard-disabled-candidate', 'capture_kind': 'multiframe_candidate'})
        assert blocked.status_code == 409
        assert blocked.json()['detail'] == 'DOCUMENT_COMPOSITE_DISABLED'
        with sqlite3.connect(tmp_path / 'test.db') as c:
            for artifact_id, child_id, artifact_type in [
                ('guard-candidate', 'child-primary-4', 'document_ocr_candidate'),
                ('guard-foreign', 'child-junior-3', 'document_ocr'),
                ('guard-valid', 'child-primary-4', 'document_ocr'),
            ]:
                c.execute('''INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,
                    extracted_text,confidence,details_json,status,created_at,client_action_id)
                    VALUES(?,?,?,?,?,0.9,?,'completed',datetime('now'),?)''',
                    (artifact_id, child_id, sid, artifact_type, '合成题目', json.dumps({}), f'action-{artifact_id}'))
        with client.app.state.document_monitoring.database.connect() as c:
            with pytest.raises(PlanInputError):
                client.app.state.repository._require_homework_ocr_source(c, 'guard-valid', 'child-primary-4', 'other-session')
        homework = {'subject': '数学', 'title': '合成习题', 'expected_minutes': 15, 'task_type': 'written'}
        url = f'/v1/sessions/{sid}/homework'
        for source in ('guard-candidate', 'guard-foreign', 'missing-source'):
            response = client.post(url, json={**homework, 'source_artifact_id': source, 'client_action_id': f'guard-new-{source}'})
            assert response.status_code == 422, response.text
        created = client.post(url, json={**homework, 'source_artifact_id': 'guard-valid', 'client_action_id': 'guard-valid-create'})
        assert created.status_code == 201, created.text
        hid = created.json()['homework_id']
        for source in ('guard-candidate', 'guard-foreign'):
            response = client.put(f'{url}/{hid}', json={**homework, 'title': '不应保存的修改', 'source_artifact_id': source,
                'client_action_id': f'guard-edit-{source}'})
            assert response.status_code == 422, response.text
        with sqlite3.connect(tmp_path / 'test.db') as c:
            assert c.execute('SELECT title,source_artifact_id FROM homework_item WHERE homework_id=?', (hid,)).fetchone() == ('合成习题', 'guard-valid')
            # Model a record written by an older build. Replaying its original
            # request cannot silently certify the already-invalid reference.
            c.execute('UPDATE homework_item SET source_artifact_id=? WHERE homework_id=?', ('guard-candidate', hid))
        replay = client.post(url, json={**homework, 'source_artifact_id': 'guard-valid', 'client_action_id': 'guard-valid-create'})
        assert replay.status_code == 422, replay.text
        with sqlite3.connect(tmp_path / 'test.db') as c:
            assert c.execute('SELECT title,source_artifact_id FROM homework_item WHERE homework_id=?', (hid,)).fetchone() == ('合成习题', 'guard-candidate')
        repair = client.put(f'{url}/{hid}', json={**homework, 'source_artifact_id': 'guard-valid', 'client_action_id': 'guard-repair-homework'})
        assert repair.status_code == 200, repair.text
        with sqlite3.connect(tmp_path / 'test.db') as c:
            old, new = c.execute('SELECT before_json,after_json FROM homework_revision WHERE client_action_id=?', ('guard-repair-homework',)).fetchone()
            assert json.loads(old)['source_artifact_id'] == 'guard-candidate'
            assert json.loads(new)['source_artifact_id'] == 'guard-valid'
        assert client.post(f"/v1/capture-artifacts/guard-candidate/mistake-candidates/prepare", json={
            'child_id': 'child-primary-4', 'client_action_id': 'guard-no-candidate'}).status_code in {404, 409}
        workspace = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'guard-generate'}).json()
        item = next(x for x in workspace['plan_items'] if x['source_type'] == 'school_homework')
        client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'plan_review', 'target_state': 'study', 'reason': 'confirmed', 'client_action_id': 'guard-study'})
        client.post(f"/v1/plan-items/{item['plan_item_id']}/start", json={'client_action_id': 'guard-start'})
        bad = client.post(f"/v1/plan-items/{item['plan_item_id']}/complete", json={'actual_minutes': 5,
            'completion_evidence_type': 'child_confirmed', 'completion_artifact_id': 'guard-candidate', 'client_action_id': 'guard-bad-complete'})
        assert bad.status_code == 422, bad.text
        assert '不匹配' in bad.json()['message']
        with sqlite3.connect(tmp_path / 'test.db') as c:
            row = c.execute('SELECT status,completion_artifact_id FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()
            assert row[0] != 'completed' and row[1] is None
        good = client.post(f"/v1/plan-items/{item['plan_item_id']}/complete", json={'actual_minutes': 5,
            'completion_evidence_type': 'child_confirmed', 'completion_artifact_id': 'guard-valid', 'client_action_id': 'guard-good-complete'})
        assert good.status_code == 200, good.text
        client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'study', 'target_state': 'reflection',
            'reason': 'tasks_completed', 'client_action_id': 'guard-reflection'})
        client.post(f'/v1/sessions/{sid}/reflection/complete', json={'skipped': True, 'client_action_id': 'guard-finish'})
        parent = client.post('/v1/parent/setup', json={'pin': '2468'}).json()
        report = client.get(f'/v1/sessions/{sid}/report', headers={'X-HSC-Parent': parent['parent_token']})
        assert report.status_code == 200, report.text
        assert 'guard-candidate' not in json.dumps(report.json(), ensure_ascii=False)
        # A previously accepted bad reference is retained for review, but
        # replaying the original completion cannot silently certify it.
        with sqlite3.connect(tmp_path / 'test.db') as c:
            c.execute('UPDATE plan_item SET completion_artifact_id=? WHERE plan_item_id=?', ('guard-candidate', item['plan_item_id']))
        replay_completion = client.post(f"/v1/plan-items/{item['plan_item_id']}/complete", json={'actual_minutes': 5,
            'completion_evidence_type': 'child_confirmed', 'completion_artifact_id': 'guard-valid', 'client_action_id': 'guard-good-complete'})
        assert replay_completion.status_code == 422, replay_completion.text
        with sqlite3.connect(tmp_path / 'test.db') as c:
            assert c.execute('SELECT completion_artifact_id FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()[0] == 'guard-candidate'
