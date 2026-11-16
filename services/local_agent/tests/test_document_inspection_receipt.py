"""Inspection completion must reference its actual, durable local page."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.document_monitoring import DocumentMonitoringService
from test_device_receipt_validation import runtime, complete, stored


@pytest.fixture(params=['inspect', 'review'])
def inspection(runtime, request):
    db, _, sid, _ = runtime
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id='capture-turn'",
                  (json.dumps({'type': 'control_document_monitor', 'command': request.param}),))
        c.execute("UPDATE agent_step SET capability_name='document.monitor_control'")
        c.commit()
    monitor = DocumentMonitoringService(db)
    monitor.start(sid, 'start-inspection-monitor')
    saved = monitor.record_revision(sid, 'artifact', 'synthetic-page-signature', 'save-inspection-page')
    if request.param == 'review':
        with db.connect() as c:
            c.execute("UPDATE agent_turn SET created_at=? WHERE agent_turn_id='capture-turn'",
                      (datetime.now(timezone.utc).isoformat(),))
            c.commit()
    value = {'capability': 'document.monitor_control', 'execution_location': 'client_device_bridge',
             'action_type': 'control_document_monitor', 'status': 'completed',
             'completed_at': datetime.now(timezone.utc).isoformat(),
             'output': {'artifact_id': 'artifact', 'revision_id': saved['revision_id'],
                        'page_status': 'text_available', 'response_text': '这次文字还需要核对。'}}
    return runtime, {'execution_receipt': value, 'client_bridge_receipt': deepcopy(value)}, monitor


@pytest.mark.parametrize('fault', ['missing_output', 'missing_artifact', 'wrong_revision', 'empty_claim',
                                  'stale', 'changed_text', 'failed_artifact', 'foreign_revision',
                                  'foreign_artifact', 'wrong_type', 'blocked_as_text', 'blocked_null_status'])
def test_invalid_page_cannot_complete_inspection(inspection, fault):
    runtime, result, _ = inspection
    db = runtime[0]
    with db.connect() as c:
        if fault == 'missing_output':
            for receipt in result.values():
                receipt.pop('output')
        elif fault in {'missing_artifact', 'wrong_revision', 'empty_claim'}:
            key, value = {'missing_artifact': ('artifact_id', 'missing'),
                          'wrong_revision': ('revision_id', 'missing'),
                          'empty_claim': ('page_status', 'empty')}[fault]
            for receipt in result.values():
                receipt['output'][key] = value
        elif fault == 'stale':
            command = json.loads(c.execute("SELECT action_json FROM agent_turn WHERE agent_turn_id='capture-turn'").fetchone()[0])['command']
            table = 'document_page_revision' if command == 'review' else 'capture_artifact'
            c.execute(f"UPDATE {table} SET created_at='2000-01-01T00:00:00+00:00'")
        elif fault == 'changed_text':
            c.execute("UPDATE capture_artifact SET extracted_text='different question'")
        elif fault == 'failed_artifact':
            c.execute("UPDATE capture_artifact SET status='failed'")
        elif fault == 'wrong_type':
            c.execute("UPDATE capture_artifact SET artifact_type='voice'")
        elif fault in {'blocked_as_text', 'blocked_null_status'}:
            c.execute("UPDATE capture_artifact SET details_json=?",
                      (json.dumps({'cloud_vision_fallback': {'state': 'blocked',
                          **({'status': None} if fault == 'blocked_null_status' else {})}}),))
        else:
            c.execute("""INSERT INTO daily_session
                (session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                SELECT 'other-session',child_id,'completed',state,started_at,last_checkpoint_at,'other-session'
                FROM daily_session LIMIT 1""")
            table = 'document_page_revision' if fault == 'foreign_revision' else 'capture_artifact'
            c.execute(f"UPDATE {table} SET session_id='other-session'")
        c.commit()
    before = stored(runtime)
    with pytest.raises(ValueError, match='DEVICE_RECEIPT_DOCUMENT_INSPECTION_'):
        complete(runtime, result)
    assert stored(runtime) == before


@pytest.mark.parametrize('page_status', ['text_available', 'empty', 'blocked'])
def test_saved_inspection_survives_restart_without_becoming_mastery(inspection, page_status):
    runtime, result, _ = inspection
    if page_status != 'text_available':
        with runtime[0].connect() as c:
            if page_status == 'empty':
                import hashlib
                c.execute("UPDATE capture_artifact SET extracted_text=''")
                c.execute('UPDATE document_page_revision SET content_sha256=?', (hashlib.sha256(b'').hexdigest(),))
            else:
                c.execute("UPDATE capture_artifact SET details_json=?",
                          (json.dumps({'cloud_vision_fallback': {'status': 'blocked'}}),))
            c.commit()
    for receipt in result.values():
        receipt['output']['page_status'] = page_status
    complete(runtime, result)
    saved = stored(runtime)
    assert saved['status'] == 'completed'
    restarted = AgentRuntimeStore(runtime[0])
    with runtime[0].connect() as c:
        restarted.complete_turn_execution(c, 'capture-turn', True, None, result)
        c.commit()
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM document_page_revision').fetchone()[0] == 1
    assert stored(runtime) == saved


@pytest.mark.parametrize('child_id', ['child-primary-4', 'child-junior-3'])
@pytest.mark.parametrize('command', ['inspect', 'review'])
def test_http_inspect_ocr_revision_receipt_and_restart(tmp_path, monkeypatch, child_id, command):
    import base64
    import cv2
    import numpy as np
    from home_study_agent.local_ocr.service import RapidOcrEngine
    from test_app import client_for, advance_to_study

    body = 'There are twelve red pencils and eight blue pencils.'
    lines = [{'text': body, 'confidence': .99, 'box': [[55, 100], [935, 100], [935, 170], [55, 170]]}]
    monkeypatch.setattr(RapidOcrEngine, 'recognize', lambda *a, **k: lines)
    monkeypatch.setattr(RapidOcrEngine, 'recognize_page_orientation', lambda *a, **k: lines)
    picture = np.full((300, 1000, 3), 230, dtype=np.uint8)
    cv2.putText(picture, '12 red pencils, 8 blue pencils', (55, 140), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
    ok, encoded = cv2.imencode('.png', picture)
    assert ok
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid, _ = advance_to_study(client, child_id=child_id)
        started = client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id': 'inspection-start'})
        assert started.status_code == 200, started.text
        if command == 'inspect':
            turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': '检查当前页', 'client_action_id': 'inspection-turn'}).json()
        ocr_receipt_path = f'/v1/sessions/{sid}/document-monitor/receipts/ocr/inspection-ocr'
        assert client.post(ocr_receipt_path + '/prepare').status_code == 200
        ocr = client.post(f'/v1/sessions/{sid}/document-monitor/ocr', json={
            'image_base64': base64.b64encode(encoded.tobytes()).decode(), 'client_action_id': 'inspection-ocr'})
        assert ocr.status_code == 200, ocr.text
        artifact = ocr.json()
        assert client.get(ocr_receipt_path).json()['status'] == 'saved'
        revision_receipt_path = f'/v1/sessions/{sid}/document-monitor/receipts/revision/inspection-revision'
        assert client.post(revision_receipt_path + '/prepare').status_code == 200
        revision = client.post(f'/v1/sessions/{sid}/document-monitor/revisions', json={
            'artifact_id': artifact['artifact_id'], 'content_signature': 'synthetic-http-page', 'client_action_id': 'inspection-revision'})
        assert revision.status_code == 200, revision.text
        revision = revision.json()
        assert client.get(revision_receipt_path).json()['revision_id'] == revision['revision_id']
        assert revision['artifact_id'] == artifact['artifact_id'] and body in artifact['extracted_text']
        if command == 'review':
            turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': '核对这一页', 'client_action_id': 'inspection-turn'}).json()
        assert turn['action']['type'] == 'control_document_monitor' and turn['action']['command'] == command, turn
        fallback = artifact['details'].get('cloud_vision_fallback') or {}
        blocked = (fallback.get('status') or fallback.get('state')) == 'blocked'
        receipt = {'capability': 'document.monitor_control', 'execution_location': 'client_device_bridge',
                   'action_type': 'control_document_monitor', 'status': 'completed',
                   'completed_at': datetime.now(timezone.utc).isoformat(),
                   'output': {'artifact_id': artifact['artifact_id'], 'revision_id': revision['revision_id'],
                              'page_status': 'blocked' if blocked else 'text_available', 'response_text': '页面已记录，文字需要核对。'}}
        payload = {'final_response': receipt['output']['response_text'], 'tool_status': 'completed',
                   'result': {'execution_receipt': receipt, 'client_bridge_receipt': deepcopy(receipt)}}
        path = '/v1/agent-turns/' + turn['agent_turn_id'] + '/outcome'
        invalid = deepcopy(payload)
        for envelope in invalid['result'].values():
            envelope['output']['revision_id'] = 'unrelated-page'
        refused = client.put(path, json=invalid)
        assert refused.status_code == 409, refused.text
        accepted = client.put(path, json=payload)
        assert accepted.status_code == 200, accepted.text
        db = client.app.state.agent_orchestrator.database
        with db.connect() as c:
            saved = c.execute('SELECT result_json FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',
                              (turn['agent_turn_id'],)).fetchone()[0]
            assert json.loads(saved)['execution_receipt']['output'] == receipt['output']
    with client_for(tmp_path, agent_runtime_enabled=True) as restarted:
        pending_path = f'/v1/sessions/{sid}/document-monitor/receipts'
        pending = restarted.get(pending_path).json()
        assert len(pending) == 2 and all(row['status'] == 'saved' for row in pending)
        assert restarted.post(ocr_receipt_path + '/acknowledge').status_code == 200
        assert restarted.post(revision_receipt_path + '/acknowledge').status_code == 200
        assert restarted.get(pending_path).json() == []
        replay = restarted.put(path, json=payload)
        assert replay.status_code == 200, replay.text
        status = restarted.get(f'/v1/sessions/{sid}/document-monitor').json()
        assert status['latest_revision']['revision_id'] == revision['revision_id']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM document_page_revision WHERE session_id=?', (sid,)).fetchone()[0] == 1
            assert c.execute('SELECT result_json FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',
                             (turn['agent_turn_id'],)).fetchone()[0] == saved
        deleted = restarted.app.state.privacy_lifecycle.delete_child_scope(child_id, 'media', '确认删除', 'delete-document-recovery')
        assert deleted['deleted_counts']['document_operation_receipt'] == 2
        assert deleted['deleted_counts']['document_operation_delivery'] == 2
        assert restarted.get(ocr_receipt_path).json()['status'] == 'unconfirmed'
        assert restarted.get(revision_receipt_path).json()['status'] == 'unconfirmed'
        assert restarted.get(pending_path).json() == []
