"""Actual HTTP and isolated SQLite; synthetic page/OCR, no household data."""

from local_restore_operation_fixture import restore_local
import base64
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService, CaptureConflictError
from test_parent_textbook_capture import Auth, setup
from test_parent_textbook_derivation import page, RecordingAdapter


def test_file_original_review_import_survives_two_application_restarts(tmp_path):
    db = Database(tmp_path/'parent.db'); setup(db)
    adapter = RecordingAdapter()
    def app():
        application = FastAPI()
        application.state.ocr_workflow = SimpleNamespace(adapter=adapter)
        install_knowledge_graph_routes(application, db, Auth())
        return application
    root = '/v1/parent/knowledge-graph/textbook-capture'
    headers = {'X-HSC-Parent':'ok'}
    raw = page()
    payload = dict(image_base64=base64.b64encode(raw).decode(), image_transform={'rotation':90},
                   page_context={'title':'合成四上英语'}, client_action_id='parent-file-original-001')
    with TestClient(app()) as client:
        assert client.post(root+'/child-4/file-ocr',json=payload).status_code == 401
        assert client.post(root+'/child-4/file-ocr',headers=headers,json={**payload,'lease_id':'fake-camera'}).status_code == 422
        response = client.post(root+'/child-4/file-ocr',headers=headers,json=payload)
        assert response.status_code == 200, response.text
        artifact = response.json()['artifact']; ref = artifact['capture_ref']
        assert artifact['source_kind'] == 'file' and 'file_source_id' not in artifact
        assert client.post(root+'/child-4/file-ocr',headers=headers,json=payload).json()['artifact']['capture_ref'] == ref
        assert adapter.enqueued == 1
        assert client.post(root+'/child-4/file-ocr',headers=headers,json={**payload,'image_transform':{'rotation':180}}).status_code == 409
        assert client.post(root+'/child-4/ocr',headers=headers,json={**payload,'lease_id':'fake-camera'}).status_code != 200
        assert client.get(root+f'/child-4/artifacts/{ref}/source',headers=headers).content == raw
    with TestClient(app()) as client:
        assert client.post(root+f'/child-4/artifacts/{ref}/retry',headers=headers,
            json={'client_action_id':'parent-file-retry-001'}).status_code == 200
        assert adapter.images[1] == adapter.images[0]
        assert client.put(root+f'/child-4/artifacts/{ref}/review',headers=headers,
            json={'extracted_text':'Words: apple','client_action_id':'parent-file-review-001'}).status_code == 200
        imported = client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json={
            'title':'合成四上英语','ocr_text':'Words: apple','child_id':'child-4','capture_ref':ref,
            'client_action_id':'parent-file-import-001'})
        assert imported.status_code == 200, imported.text
    with TestClient(app()) as client:
        assert client.get(root+f'/child-4/artifacts/{ref}/source',headers=headers).content == raw
        assert client.get(root+'/child-4/artifacts',headers=headers).json()['items'] == []
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_capture_lease').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_file_source').fetchone()[0] == 1
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_file_request_cannot_rebind_another_child_or_camera(tmp_path):
    db = Database(tmp_path/'parent.db'); setup(db)
    adapter = RecordingAdapter(); service = ParentTextbookCaptureService(db,adapter)
    encoded = base64.b64encode(page()).decode()
    artifact = service.recognize_file('child-4',encoded,{'title':'Page'},'fixed-file-identity')
    with db.connect() as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) SELECT 'child-9',nickname,grade,curriculum_region,status,created_at,updated_at FROM child_profile WHERE child_id='child-4'"); c.commit()
    with pytest.raises(CaptureConflictError):
        service.recognize_file('child-9',encoded,{'title':'Page'},'fixed-file-identity')
    lease = service.acquire('child-4','bound-document-hash',{})['lease_id']
    with pytest.raises(CaptureConflictError):
        service.recognize('child-4',lease,encoded,{'title':'Page'},'fixed-file-identity')
    with pytest.raises(ValueError):
        service.retry_requested('child-9',artifact['capture_ref'],'other-child-retry')
    assert adapter.enqueued == 1


def test_deleted_or_changed_file_authority_cannot_dispatch_retry(tmp_path):
    db = Database(tmp_path/'parent.db'); setup(db)
    adapter = RecordingAdapter(); service = ParentTextbookCaptureService(db,adapter)
    artifact = service.recognize_file('child-4',base64.b64encode(page()).decode(),{'title':'Page'},'file-grant-identity')
    with db.connect() as c:
        # Simulate corrupted storage; normal SQL updates must be rejected.
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE kg_parent_textbook_file_source SET image_sha256='altered'")
        c.execute('DROP TRIGGER kg_parent_textbook_file_source_immutable')
        c.execute("UPDATE kg_parent_textbook_file_source SET request_sha256='altered'"); c.commit()
    with pytest.raises(ValueError,match='source'):
        service.retry_requested('child-4',artifact['capture_ref'],'altered-source-retry')
    assert adapter.enqueued == 1


def test_file_uses_actual_owned_cpu_pipeline_and_can_retry_without_camera(tmp_path):
    from home_study_agent.local_ocr import LocalOcrService
    from home_study_agent.local_ocr.resource_budget import CpuResourceQueue
    from home_study_agent.local_ocr.runtime_adapter import CpuScheduledMediaAdapter
    from test_local_ocr_pipeline import FakeEngine
    db = Database(tmp_path/'parent.db'); setup(db)
    adapter = CpuScheduledMediaAdapter(CpuResourceQueue(tmp_path/'queue.db'), LocalOcrService(db.path,[FakeEngine()]))
    adapter.bind_database(db)
    service = ParentTextbookCaptureService(db,adapter)
    raw = page()
    artifact = service.recognize_file('child-4',base64.b64encode(raw).decode(),{'title':'Page'},
        'actual-file-cpu-pipeline',image_transform={'rotation':90})
    assert artifact['status'] in {'needs_review','ready'}
    if artifact['status'] == 'ready':
        with db.connect() as c:
            c.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='needs_review'"); c.commit()
    restarted = ParentTextbookCaptureService(db,adapter)
    result = restarted.retry_requested('child-4',artifact['capture_ref'],'actual-file-cpu-retry')
    assert result['status'] in {'needs_review','ready'}
    assert restarted.source_image('child-4',artifact['capture_ref'])[0] == raw
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM ocr_job').fetchone()[0] == 2
        assert {r[0] for r in c.execute('SELECT status FROM ocr_job')} == {'completed'}
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_capture_lease').fetchone()[0] == 0


def test_file_backup_restore_and_evidence_erasure_preserve_ownership(tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.privacy_evidence_cleanup import purge_parent_textbook_sources
    db = Database(tmp_path/'parent.db',media_root=tmp_path,data_encryption_key=b'0'*32); setup(db)
    service = ParentTextbookCaptureService(db,RecordingAdapter())
    raw = page()
    artifact = service.recognize_file('child-4',base64.b64encode(raw).decode(),{'title':'Page'},'file-backup-001')
    privacy = PrivacyLifecycleService(db,tmp_path,b'0'*32); privacy.initialize()
    backup = privacy.create_backup(prune=False)
    restore_local(privacy,backup['backup_id'],'确认恢复')
    db = Database(db.path,media_root=tmp_path,data_encryption_key=b'0'*32)
    restarted = ParentTextbookCaptureService(db,RecordingAdapter())
    assert restarted.source_image('child-4',artifact['capture_ref'])[0] == raw
    assert restarted.retry_requested('child-4',artifact['capture_ref'],'file-backup-retry')['status'] == 'needs_review'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        counts = purge_parent_textbook_sources(c,'child-4')
        assert counts['kg_parent_textbook_file_source'] == 1
        c.rollback()
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_file_source').fetchone()[0] == 1
        c.execute('BEGIN IMMEDIATE'); purge_parent_textbook_sources(c,'child-4'); c.commit()
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_file_source').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(ValueError):
        restarted.retry_requested('child-4',artifact['capture_ref'],'file-after-delete-retry')


@pytest.mark.parametrize('raw',[b'not a photograph',b'\x89PNG\r\n\x1a\n'+b'invalid data'*6])
def test_non_image_file_is_rejected_before_any_authority_or_ocr_work(tmp_path,raw):
    db = Database(tmp_path/'parent.db'); setup(db)
    adapter = RecordingAdapter(); service = ParentTextbookCaptureService(db,adapter)
    with pytest.raises(ValueError,match='image'):
        service.recognize_file('child-4',base64.b64encode(raw).decode(),{'title':'Page'},'invalid-file-image')
    assert adapter.enqueued == 0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_file_source').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_ocr_artifact').fetchone()[0] == 0


def test_file_page_cannot_be_relabeled_as_camera_to_bypass_provenance(tmp_path):
    db = Database(tmp_path/'parent.db'); setup(db)
    adapter = RecordingAdapter(); service = ParentTextbookCaptureService(db,adapter)
    artifact = service.recognize_file('child-4',base64.b64encode(page()).decode(),{'title':'Page'},'file-origin-boundary')
    lease = service.acquire('child-4','bound-document-hash',{})['lease_id']
    with db.connect() as c:
        c.execute("UPDATE kg_parent_textbook_ocr_artifact SET source_kind='camera',file_source_id=NULL,lease_id=?", (lease,)); c.commit()
    with pytest.raises(ValueError,match='source'):
        service.retry_requested('child-4',artifact['capture_ref'],'file-pretends-camera-retry')
    assert adapter.enqueued == 1


def test_deleting_child_knowledge_erases_file_authority_and_encrypted_page(tmp_path):
    from pathlib import Path
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db = Database(tmp_path/'parent.db',media_root=tmp_path,data_encryption_key=b'0'*32); setup(db)
    service = ParentTextbookCaptureService(db,RecordingAdapter())
    artifact = service.recognize_file('child-4',base64.b64encode(page()).decode(),{'title':'Page'},'file-privacy-full-delete')
    with db.connect() as c:
        raw_path = Path(c.execute('SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact').fetchone()[0])
    privacy = PrivacyLifecycleService(db,tmp_path,b'0'*32); privacy.initialize()
    assert privacy.delete_child_scope('child-4','knowledge','确认删除','delete-file-knowledge')['status'] == 'completed'
    assert not raw_path.exists()
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_file_source').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_ocr_artifact').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(ValueError):
        service.source_image('child-4',artifact['capture_ref'])
