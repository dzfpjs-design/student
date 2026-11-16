
from local_restore_operation_fixture import restore_local
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.repository import utc_now
from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
from home_study_agent.knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
from home_study_agent.knowledge_graph.parent_textbook_capture import CaptureConflictError, ParentTextbookCaptureService
from test_parent_textbook_capture import Adapter, Auth, setup


class BoundDraftService(ParentTextbookDraftService):
    def __init__(self, database):
        super().__init__(database)
        self.origin_generation = self.read('child-4','capture')['storage_generation']

    def save(self, *args, **kwargs):
        return super().save(*args, expected_generation=kwargs.pop('expected_generation',self.origin_generation), **kwargs)


@pytest.fixture
def store(tmp_path):
    database = Database(tmp_path / 'drafts.db')
    setup(database)
    with database.connect() as con:
        con.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child-9','九','初三','江苏','active',?,?)", (utc_now(), utc_now()))
        con.commit()
    return database, BoundDraftService(database)


def payload(**changes):
    return {'page': {'title': '校对后的教材', 'ocr_text': 'private parent draft 苹果'},
            'capture_ref': '', 'capture_version': '', 'review_drafts': {}, 'action_ids': {}, **changes}


def test_encrypted_save_read_restart_and_no_knowledge_promotion(store):
    db, service = store
    saved = service.save('child-4', 'capture', 0, 'draft-command-001', payload())
    assert saved['revision'] == 1 and saved['payload'] == payload()
    assert saved['cloud_transmission_allowed'] is False
    restarted = ParentTextbookDraftService(Database(db.path, media_root=db.path.parent, data_encryption_key=b'0'*32))
    assert restarted.read('child-4', 'capture') == saved
    assert service.read('child-9', 'capture')['payload'] is None
    assert service.read('child-4', 'language')['payload'] is None
    with db.connect() as con:
        row = con.execute('SELECT * FROM kg_parent_textbook_form_draft').fetchone()
        assert b'private parent draft' not in row['encrypted_payload']
        assert con.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 0


def test_retry_unknown_receipt_same_key_and_content_does_not_increment(store):
    _, service = store
    first = service.save('child-4', 'capture', 0, 'draft-command-001', payload())
    assert service.save('child-4', 'capture', 0, 'draft-command-001', payload()) == first
    with pytest.raises(CaptureConflictError):
        service.save('child-4', 'capture', 0, 'draft-command-001', payload(page={'title':'different'}))
    with pytest.raises(CaptureConflictError):
        service.save('child-4', 'capture', 0, 'draft-command-old', payload())
    assert service.read('child-4', 'capture') == first
    with service.database.connect() as con:
        rows = con.execute("SELECT summary_json FROM audit_event WHERE action='parent_textbook_draft_saved'").fetchall()
        assert len(rows) == 1 and 'private parent draft' not in rows[0][0]


def test_clear_tombstone_blocks_old_window_and_wrong_generation(store):
    _, service = store
    service.save('child-4', 'capture', 0, 'draft-command-001', payload())
    cleared = service.save('child-4', 'capture', 1, 'draft-clear-001', None)
    assert cleared['revision'] == 2 and cleared['payload'] is None
    with pytest.raises(CaptureConflictError):
        service.save('child-4', 'capture', 1, 'draft-late-window', payload())
    assert service.read('child-4', 'capture') == cleared


@pytest.mark.parametrize('value', [
    {'unknown':'not allowed'}, payload(page={'image_base64':'not allowed'}),
    payload(page={'ocr_text':'x'*200001}), payload(action_ids={'one':'x'*129}),
    payload(capture_ref='unknown', capture_version='unknown'),
    payload(review_drafts={'unknown':{'text':'secret','source_version':'unknown'}}),
])
def test_invalid_payload_not_saved(store, value):
    _, service = store
    with pytest.raises(ValueError): service.save('child-4', 'capture', 0, 'draft-invalid-001', value)
    assert service.read('child-4', 'capture')['revision'] == 0


def test_corrupted_cipher_or_wrong_key_is_not_blank_recovery(store):
    db, service = store
    service.save('child-4', 'capture', 0, 'draft-command-001', payload())
    wrong = ParentTextbookDraftService(Database(db.path, media_root=db.path.parent, data_encryption_key=b'1'*32))
    with pytest.raises(ValueError): wrong.read('child-4', 'capture')
    with pytest.raises(ValueError): wrong.save('child-4', 'capture', 1, 'draft-overwrite-001', payload())
    assert service.read('child-4', 'capture')['payload'] == payload()
    with db.connect() as con:
        con.execute("UPDATE kg_parent_textbook_form_draft SET encrypted_payload=?", (b'damaged',)); con.commit()
    with pytest.raises(ValueError): service.read('child-4', 'capture')
    with pytest.raises(ValueError): service.save('child-4', 'capture', 1, 'draft-overwrite-002', None)
    with db.connect() as con: assert bytes(con.execute('SELECT encrypted_payload FROM kg_parent_textbook_form_draft').fetchone()[0]) == b'damaged'


def test_reference_ownership_and_version_are_local_authority(store):
    db, service = store
    capture = ParentTextbookCaptureService(db, Adapter())
    lease = capture.acquire('child-4', 'bound-document-hash', {})
    artifact = capture.recognize('child-4', lease['lease_id'], 'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==', {'title':'教材'}, 'draft-ocr-001')
    value = payload(review_drafts={artifact['capture_ref']:{'text':'corrected 苹果', 'source_version':artifact['source_version']}})
    service.save('child-4', 'capture', 0, 'draft-command-001', value)
    with pytest.raises(ValueError): service.save('child-9', 'capture', 0, 'draft-cross-child', value)
    capture.review('child-4', artifact['capture_ref'], 'reviewed', 'review-command-001')
    restored = service.read('child-4', 'capture')
    assert restored['payload'] == value and restored['stale_refs'] == [artifact['capture_ref']]
    with pytest.raises(CaptureConflictError): service.save('child-4', 'capture', 1, 'draft-stale-001', value)
    assert service.save('child-4', 'capture', 1, 'draft-clear-001', None)['payload'] is None


def test_parent_only_http_strict_payload_no_store_and_active_child(store):
    db, _ = store
    app = FastAPI(); app.state.ocr_workflow = SimpleNamespace(adapter=Adapter())
    install_knowledge_graph_routes(app, db, Auth())
    path = '/v1/parent/knowledge-graph/textbook-capture/child-4/drafts/capture'
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        headers = {'X-HSC-Parent':'ok'}
        assert client.get(path, headers=headers).headers['cache-control'] == 'no-store'
        request = {'expected_revision':0,'expected_generation':client.get(path,headers=headers).json()['draft']['storage_generation'], 'client_action_id':'draft-command-001','payload':payload()}
        saved = client.put(path, headers=headers, json=request)
        assert saved.status_code == 200
        assert client.put(path, headers=headers, json={**request,'extra':'forbidden'}).status_code == 422
        assert client.put(path, headers=headers, json={**request,'client_action_id':'draft-old-window'}).status_code == 409
        assert client.get(path.replace('child-4','unknown'), headers=headers).status_code == 404
        assert client.get(path.replace('/capture','/unknown'), headers=headers).status_code == 404
        assert client.get(path, headers=headers).json()['draft'] == saved.json()['draft']


def test_real_backup_restore_and_deletion_do_not_revive_old_windows(store):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, service = store
    service.save('child-4', 'capture', 0, 'draft-command-001', payload())
    sibling = service.save('child-9', 'capture', 0, 'draft-sibling-001', payload(page={'title':'sibling'}))
    privacy = PrivacyLifecycleService(db, db.path.parent, b'0'*32); privacy.initialize()
    assert privacy.deletion_preview('child-4')['parent_textbook_drafts'] == 1
    export = privacy.export_child('child-4')
    exported = json.loads(Path(export['path']).read_text())
    assert exported['parent_textbook_drafts'][0]['payload'] == payload()
    assert exported['cloud_transmission_allowed'] is False
    assert 'sibling' not in Path(export['path']).read_text()
    backup = privacy.create_backup(prune=False)
    service.save('child-4', 'capture', 1, 'draft-clear-001', None)
    restore_local(privacy,backup['backup_id'], '确认恢复')
    assert service.read('child-4', 'capture')['payload'] == payload()
    with pytest.raises(CaptureConflictError): service.save('child-4','capture',1,'draft-pre-restore',payload())
    service = BoundDraftService(db)
    result = privacy.delete_child_scope('child-4','knowledge','确认删除','draft-privacy-delete')
    assert result['status'] == 'completed'
    assert service.read('child-4', 'capture')['payload'] is None
    assert service.read('child-9', 'capture')['payload'] == sibling['payload']
    with pytest.raises(CaptureConflictError): service.save('child-4','capture',1,'draft-deleted-old',payload())
    # An open but previously empty window also cannot revive deleted content.
    with pytest.raises(CaptureConflictError): service.save('child-4','language',0,'draft-empty-old',payload())


def test_action_receipt_is_read_only_owned_and_tamper_evident(store):
    db, _ = store
    capture = ParentTextbookCaptureService(db, Adapter())
    lease = capture.acquire('child-4','bound-document-hash',{})
    artifact = capture.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'教材'},'receipt-ocr-001')
    capture.review('child-4',artifact['capture_ref'],'real corrected text','receipt-review-001')
    app = FastAPI();app.state.ocr_workflow=SimpleNamespace(adapter=Adapter())
    install_knowledge_graph_routes(app,db,Auth())
    path = f"/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/{artifact['capture_ref']}/actions/receipt-review-001"
    with TestClient(app) as client:
        headers={'X-HSC-Parent':'ok'}
        assert client.get(path).status_code == 401
        response=client.get(path,headers=headers)
        assert response.headers['cache-control']=='no-store'
        assert response.json()['request']=={'extracted_text':'real corrected text'}
        assert response.json()['action']=='reviewed' and response.json()['cloud_transmission_allowed'] is False
        assert client.get(path.replace('child-4','child-9'),headers=headers).status_code == 404
        assert client.get(path.replace('receipt-review-001','unknown-action-001'),headers=headers).json()['status']=='unknown'
        with db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM kg_parent_textbook_ocr_review_event').fetchone()[0]==2
            con.execute("UPDATE kg_parent_textbook_ocr_review_event SET payload_json=? WHERE client_action_id='receipt-review-001'",(json.dumps({'request':{'extracted_text':'invented'}}),));con.commit()
        assert client.get(path,headers=headers).status_code == 422
