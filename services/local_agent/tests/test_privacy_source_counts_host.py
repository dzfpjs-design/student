"""Actual source cleanup receipts retain counts from every cleanup phase."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import LocalMediaRuntime
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.repository import utc_now
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageService, TextbookOcrPageInput
from test_companion_activity_host import NoCloud
from test_learning_outcome_mastery_gate import gate
from test_mistake_gate_reliability import flow, ITEM, CHILD
from test_parent_textbook_capture import Adapter


@pytest.mark.parametrize('scope', ['knowledge', 'all_history'])
def test_native_rubric_counts_survive_later_cleanup_and_restart(gate, scope):
    gate.db.local_media_runtime = LocalMediaRuntime(gate.db.path.parent, b'C' * 32)
    result, _, _ = gate.run(local=True)
    snapshot = result['evaluation']['rubric_assessment']
    RubricEvidenceService(gate.db).review(CHILD, snapshot['assessment_id'],
        expected_revision=0, client_action_id='count-review', reviewer_id='parent',
        reviewer_role='parent', operation='approve', reason='synthetic original review',
        independent=True, hint_level=0, dimensions=[],
        criterion_evidence=[{'criterion': value, 'response_quote': '90'} for value in snapshot['criteria']])
    with gate.db.connect() as c:
        c.execute("INSERT INTO device_binding(device_type,system_id_hash,label,capabilities_json,verified_at,updated_at) VALUES('document_camera','count-camera','synthetic','{}',?,?)", (utc_now(), utc_now()))
        c.commit()
    capture = ParentTextbookCaptureService(gate.db, Adapter())
    lease = capture.acquire(CHILD, 'count-camera', {})
    page = capture.recognize(CHILD, lease['lease_id'], 'aGVsbG8=', {'title': 'synthetic count page'}, 'count-page')
    ref = capture.page_source_ref(page['capture_ref'])
    TextbookOcrPageService(gate.db).import_page(TextbookOcrPageInput(
        title='synthetic count page', ocr_text='Synthetic question origin', source_ref=ref))
    with gate.db.connect() as c:
        c.execute('UPDATE diagnostic_item SET source_ref=? WHERE diagnostic_item_id=?', (ref, ITEM))
        before = {table: c.execute(f'SELECT COUNT(*) FROM {table} WHERE child_id=?', (CHILD,)).fetchone()[0]
                  for table in ('rubric_review_event', 'rubric_assessment')}
        assert before == {'rubric_review_event': 1, 'rubric_assessment': 1}
        c.commit()
    settings = Settings(data_dir=gate.db.path.parent, database_path=gate.db.path,
        maintenance_enabled=False, session_token='counts-isolated', data_encryption_key=b'C' * 32)
    local = {'X-HSC-Session': settings.session_token}
    saved = None
    for restart in (False, True):
        with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
            token = client.post('/v1/parent/' + ('unlock' if restart else 'setup'), headers=local,
                json={'pin': '2468'}).json()['parent_token']
            response = client.post(f'/v1/parent/children/{CHILD}/delete',
                headers={**local, 'X-HSC-Parent': token}, json={'scope': scope,
                    'confirmation': '确认删除', 'client_action_id': 'source-count-delete'})
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert receipt['status'] == 'completed', receipt
            assert {table: receipt['deleted_counts'][table] for table in before} == before
            if saved:
                assert receipt['deletion_id'] == saved['deletion_id']
                assert receipt['deleted_counts'] == saved['deleted_counts']
            saved = receipt
    with gate.db.connect() as c:
        for table in before:
            assert c.execute(f'SELECT COUNT(*) FROM {table} WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
