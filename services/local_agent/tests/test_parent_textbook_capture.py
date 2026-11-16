
from local_restore_operation_fixture import restore_local
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database, LocalMediaRuntime
from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
from home_study_agent.knowledge_graph.schema import ensure_schema
from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageInput, TextbookOcrPageService
from home_study_agent.local_ocr.resource_budget import CpuResourceQueue, ResourceSnapshot
from home_study_agent.local_ocr.runtime_adapter import CpuScheduledMediaAdapter
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import utc_now


class Adapter:
    def __init__(self, fail: bool = False): self.enqueued = 0; self.task_id = ''; self.fail = fail
    def enqueue_ocr(self, _image, request_id):
        self.enqueued += 1; self.task_id = f'task:{request_id}'; return {'task': {'task_id': self.task_id}}
    def next(self, _snapshot): return {'task_id': self.task_id, 'task_type': 'ocr'}
    def claim_ocr(self, _scheduled, _snapshot): return {'task_id': self.task_id, 'task_type': 'ocr'}
    def run_ocr_claim(self, _claim, _image):
        if self.fail: raise RuntimeError('cpu unavailable')
        return {'status':'completed','result':{'confidence':.62,'engine':'fake-local','quality':{'passed':False,'issues':['blur']},'lines':[{'text':'Words: apple','confidence':.62}], 'manual_review':{'required':True,'reasons':['blur'],'handwriting':'review_required','math':'review_required'},'privacy':{'raw_image_uploaded':False}}}
    def cancel_ocr(self, _scheduled, _reason): self.cancelled = getattr(self,'cancelled',0) + 1

class Auth:
    def require(self, token):
        if token != 'ok': raise HTTPException(status_code=401, detail='no')


def setup(database: Database, *, initialize: bool = True) -> None:
    if database.local_media_runtime is None:
        database.local_media_runtime=LocalMediaRuntime(database.path.parent,b'0'*32)
    if initialize:
        database.initialize()
    now=utc_now()
    with database.connect() as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child-4','小四','四年级','江苏','active',?,?)",(now,now))
        c.execute("INSERT INTO device_binding(device_type,system_id_hash,label,capabilities_json,verified_at,updated_at) VALUES('document_camera','bound-document-hash','高拍仪','{}',?,?)",(now,now));c.commit()


def test_parent_capture_encrypts_raw_page_and_requires_review_before_page_import(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{'width':1920})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语','page_number':6},'ocr-action-001')
    assert artifact['status']=='needs_review' and artifact['local_path'] is None
    assert artifact['raw_image_retained'] is True and artifact['raw_image_byte_length']==25
    assert artifact['details']['local_pipeline']['privacy']=={
        'raw_image_uploaded':False,'raw_image_retained':True,
        'raw_image_encrypted':True,'cloud_transmission_allowed':False,
    }
    source,media_type=service.source_image('child-4',artifact['capture_ref'])
    assert source==b'hello textbook page image' and media_type=='application/octet-stream'
    assert service.pending('child-4')[0]['capture_ref']==artifact['capture_ref']
    reviewed=service.review('child-4',artifact['capture_ref'],'Words: apple','review-action-001')
    assert reviewed['status']=='ready' and reviewed['extracted_text']=='Words: apple'
    restarted=ParentTextbookCaptureService(Database(tmp_path/'d.db'),Adapter())
    assert restarted.pending('child-4')[0]['status']=='ready'


def test_parent_capture_http_rejects_unbound_or_in_use_device_and_persists_review_queue(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);app=FastAPI();app.state.ocr_workflow=SimpleNamespace(adapter=Adapter());install_knowledge_graph_routes(app,database,Auth())
    with TestClient(app) as client:
        denied=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/leases',json={'system_id_hash':'bound-document-hash'})
        assert denied.status_code==401
        unavailable=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/leases',headers={'X-HSC-Parent':'ok'},json={'system_id_hash':'other-camera'})
        assert unavailable.status_code==404
        lease=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/leases',headers={'X-HSC-Parent':'ok'},json={'system_id_hash':'bound-document-hash'}).json()
        busy=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/leases',headers={'X-HSC-Parent':'ok'},json={'system_id_hash':'bound-document-hash'})
        assert busy.status_code==409
        artifact=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/ocr',headers={'X-HSC-Parent':'ok'},json={'lease_id':lease['lease_id'],'image_base64':'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==','page_context':{'title':'四上英语'},'client_action_id':'ocr-action-001'}).json()['artifact']
        assert 'artifact_id' not in artifact and 'child_id' not in artifact and 'image_sha256' not in artifact and 'page_context_sha256' not in artifact and artifact['status']=='needs_review'
        assert 'raw_image_path' not in artifact and artifact['raw_image_retained'] is True
        source_path=f"/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/{artifact['capture_ref']}/source"
        assert client.get(source_path).status_code==401
        source=client.get(source_path,headers={'X-HSC-Parent':'ok'})
        assert source.status_code==200 and source.content==b'hello textbook page image'
        assert source.headers['cache-control']=='no-store'
        reviewed=client.put(f"/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/{artifact['capture_ref']}/review",headers={'X-HSC-Parent':'ok'},json={'extracted_text':'Words: apple','client_action_id':'review-action-001'})
        assert reviewed.status_code==200 and reviewed.json()['artifact']['status']=='ready'
        skipped=client.post(f"/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/{artifact['capture_ref']}/skip",headers={'X-HSC-Parent':'ok'},json={'client_action_id':'skip-action-001'})
        assert skipped.status_code==200 and skipped.json()['artifact']['status']=='skipped'
        restored=client.post(f"/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/{artifact['capture_ref']}/restore",headers={'X-HSC-Parent':'ok'},json={'client_action_id':'restore-action-001'})
        assert restored.status_code==200 and restored.json()['artifact']['status']=='ready'
        page=client.post('/v1/parent/knowledge-graph/textbook-pages',headers={'X-HSC-Parent':'ok'},json={'title':'四上英语','ocr_text':'Words: apple','page_number':6,'child_id':'child-4','capture_ref':artifact['capture_ref'],'client_action_id':'page-import-action-001'})
        assert page.status_code==200 and 'resource_id' not in page.json()['page']
        assert client.get('/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts',headers={'X-HSC-Parent':'ok'}).json()['items']==[]


def test_parent_capture_original_survives_local_backup_restore_and_can_be_retried(tmp_path: Path) -> None:
    key=b'0'*32
    database=Database(tmp_path/'d.db',media_root=tmp_path,data_encryption_key=key);setup(database)
    service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-backup-001')
    with database.connect() as c:
        raw_path=Path(c.execute("SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE client_action_id='ocr-backup-001'").fetchone()[0])
    privacy=PrivacyLifecycleService(database,tmp_path,key);privacy.initialize()
    backup=privacy.create_backup(prune=False)
    raw_path.unlink()
    restore_local(privacy,backup['backup_id'],'确认恢复')
    restarted_database=Database(tmp_path/'d.db',media_root=tmp_path,data_encryption_key=key)
    restarted=ParentTextbookCaptureService(restarted_database,Adapter())
    source,media_type=restarted.source_image('child-4',artifact['capture_ref'])
    assert source==b'hello textbook page image' and media_type=='application/octet-stream'
    assert restarted.retry_requested('child-4',artifact['capture_ref'],'ocr-backup-retry')['status']=='needs_review'


def test_parent_capture_media_deletion_removes_encrypted_original_but_keeps_ocr_record(tmp_path: Path) -> None:
    key=b'0'*32
    database=Database(tmp_path/'d.db',media_root=tmp_path,data_encryption_key=key);setup(database)
    service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-delete-media')
    with database.connect() as c:
        raw_path=Path(c.execute("SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE client_action_id='ocr-delete-media'").fetchone()[0])
    privacy=PrivacyLifecycleService(database,tmp_path,key);privacy.initialize()
    deleted=privacy.delete_child_scope('child-4','media','确认删除','delete-parent-textbook-media')
    assert deleted['status']=='completed' and not raw_path.exists()
    retained=service.pending('child-4')[0]
    assert retained['capture_ref']==artifact['capture_ref'] and retained['raw_image_retained'] is False
    try:
        service.source_image('child-4',artifact['capture_ref'])
        assert False,'deleted parent source image must not remain readable'
    except ValueError as error:
        assert 'unavailable' in str(error)


def test_parent_capture_original_moves_in_portable_backup_and_uses_target_key(tmp_path: Path) -> None:
    source_dir=tmp_path/'source';target_dir=tmp_path/'target';source_dir.mkdir();target_dir.mkdir()
    old_key,new_key=b'o'*32,b'n'*32
    source_database=Database(source_dir/'agent.db',media_root=source_dir,data_encryption_key=old_key);setup(source_database)
    source_capture=ParentTextbookCaptureService(source_database,Adapter())
    lease=source_capture.acquire('child-4','bound-document-hash',{})
    artifact=source_capture.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-portable-001')
    source_privacy=PrivacyLifecycleService(source_database,source_dir,old_key);source_privacy.initialize()
    portable=source_privacy.create_portable_backup('parent textbook portable passphrase')

    target_database=Database(target_dir/'agent.db',media_root=target_dir,data_encryption_key=new_key);target_database.initialize()
    target_privacy=PrivacyLifecycleService(target_database,target_dir,new_key);target_privacy.initialize()
    target_privacy.restore_portable_backup(portable['path'],'parent textbook portable passphrase','确认迁移恢复')
    target_capture=ParentTextbookCaptureService(target_database,Adapter())
    source,media_type=target_capture.source_image('child-4',artifact['capture_ref'])
    assert source==b'hello textbook page image' and media_type=='application/octet-stream'
    with target_database.connect() as c:
        stored=Path(c.execute("SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE client_action_id='ocr-portable-001'").fetchone()[0])
    assert stored.is_relative_to(target_dir/'media') and stored.is_file()


def test_parent_capture_reconciles_missing_source_without_claiming_it_is_retained(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db',media_root=tmp_path,data_encryption_key=b'0'*32);setup(database)
    service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-missing-source')
    with database.connect() as c:
        raw_path=Path(c.execute("SELECT raw_image_path FROM kg_parent_textbook_ocr_artifact WHERE client_action_id='ocr-missing-source'").fetchone()[0])
    raw_path.unlink()
    restored=ParentTextbookCaptureService(Database(tmp_path/'d.db',media_root=tmp_path,data_encryption_key=b'0'*32),Adapter()).pending('child-4')[0]
    assert restored['capture_ref']==artifact['capture_ref'] and restored['raw_image_retained'] is False
    with database.connect() as c:
        row=c.execute("SELECT raw_image_path,raw_image_byte_length FROM kg_parent_textbook_ocr_artifact WHERE client_action_id='ocr-missing-source'").fetchone()
        events=[item[0] for item in c.execute("SELECT event_type FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=(SELECT artifact_id FROM kg_parent_textbook_ocr_artifact WHERE client_action_id='ocr-missing-source')")]
    assert tuple(row)==(None,0) and 'raw_source_missing' in events


def test_parent_capture_removes_only_stale_unowned_managed_ciphertexts(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db',media_root=tmp_path,data_encryption_key=b'0'*32);setup(database)
    directory=tmp_path/'media'/'child-4'/'parent-textbook';directory.mkdir(parents=True,exist_ok=True)
    stale=directory/'00000000-0000-0000-0000-000000000000.page.enc';stale.write_bytes(b'orphan')
    recent=directory/'11111111-1111-1111-1111-111111111111.page.enc';recent.write_bytes(b'active-stage')
    old=time.time()-900;os.utime(stale,(old,old))
    ParentTextbookCaptureService(database,Adapter()).pending('child-4')
    assert not stale.exists() and recent.exists()


def test_parent_capture_does_not_take_a_document_camera_already_leased_to_a_child(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);now=utc_now()
    with database.connect() as c:
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('child-session','child-4','active','check_in',?,?, 'child-camera-session')",(now,now))
        c.execute("INSERT INTO device_profile(device_id,device_type,system_id_hash,capabilities_json,last_seen_at,status) VALUES('device-doc','document_camera','bound-document-hash','{}',?,'active')",(now,))
        c.execute("INSERT INTO sensor_lease(lease_id,session_id,device_id,device_type,status,opened_at,last_renewed_at,expires_at) VALUES('child-document-lease','child-session','device-doc','document_camera','active',?,?,datetime('now','+3 minutes'))",(now,now));c.commit()
    try:
        ParentTextbookCaptureService(database,Adapter()).acquire('child-4','bound-document-hash',{})
        assert False, 'parent capture must not take the child document camera'
    except ValueError as error:
        assert 'already in use' in str(error)


def test_parent_capture_deduplicates_timeout_retry_before_local_ocr_and_rejects_payload_conflict(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);adapter=Adapter();service=ParentTextbookCaptureService(database,adapter)
    lease=service.acquire('child-4','bound-document-hash',{})
    request=dict(child_id='child-4',lease_id=lease['lease_id'],image_base64='aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',page_context={'title':'四上英语','page_number':6},client_action_id='ocr-timeout-001')
    first=service.recognize(**request); second=service.recognize(**request)
    assert second['capture_ref']==first['capture_ref'] and adapter.enqueued == 1
    try:
        service.recognize(**{**request,'image_base64':'ZGlmZmVyZW50IGltYWdl'})
        assert False, 'same action with another image must not re-run OCR'
    except ValueError as error:
        assert 'conflicts' in str(error)
    assert adapter.enqueued == 1


def test_parent_capture_skip_restore_and_failed_retry_are_durable_and_audited(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-skip-001')
    assert service.skip('child-4',artifact['capture_ref'],'skip-action-001')['status']=='skipped'
    restarted=ParentTextbookCaptureService(Database(tmp_path/'d.db'),Adapter())
    assert restarted.pending('child-4')[0]['status']=='skipped'
    assert restarted.restore('child-4',artifact['capture_ref'],'restore-action-001')['status']=='needs_review'
    failing=Adapter(fail=True); failed_service=ParentTextbookCaptureService(database,failing)
    try:
        failed_service.recognize('child-4',lease['lease_id'],'bmV3IGltYWdl',{'title':'四上英语'},'ocr-fail-001')
        assert False, 'failed local OCR must surface an error'
    except ValueError as error:
        assert 'local OCR failed' in str(error)
    failed=[item for item in failed_service.pending('child-4') if item['status']=='failed'][0]
    retry_adapter=Adapter(); retry_service=ParentTextbookCaptureService(database,retry_adapter)
    retried=retry_service.retry_requested('child-4',failed['capture_ref'],'retry-action-001')
    assert retried['status']=='needs_review' and retry_adapter.enqueued==1 and failing.enqueued==1
    with database.connect() as c:
        events=c.execute("SELECT event_type FROM kg_parent_textbook_ocr_review_event ORDER BY created_at").fetchall()
    assert {row['event_type'] for row in events} >= {'created','skipped','restored','failed','retry_requested'}


def test_parent_capture_schema_v10_artifact_is_rebuilt_without_losing_data_or_index(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db'); setup(database); now=utc_now()
    with database.connect() as c:
        ensure_schema(c, now)
        c.execute("INSERT INTO kg_parent_textbook_capture_lease(lease_id,child_id,system_id_hash,capabilities_json,status,opened_at,last_renewed_at,expires_at) VALUES('lease','child-4','legacy-camera','{}','released',?,?,?)",(now,now,now))
        c.execute('DROP TABLE kg_parent_textbook_ocr_review_event')
        c.execute('DROP INDEX idx_kg_parent_textbook_ocr_artifact_child')
        c.execute('DROP TABLE kg_parent_textbook_ocr_artifact')
        c.execute("""CREATE TABLE kg_parent_textbook_ocr_artifact (
          artifact_id TEXT PRIMARY KEY,child_id TEXT NOT NULL,lease_id TEXT NOT NULL,page_context_json TEXT NOT NULL,
          extracted_text TEXT NOT NULL,confidence REAL NOT NULL,details_json TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('needs_review','ready','imported')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE
        )""")
        c.execute('CREATE INDEX idx_kg_parent_textbook_ocr_artifact_child ON kg_parent_textbook_ocr_artifact(child_id,status,updated_at DESC)')
        c.execute("INSERT INTO kg_parent_textbook_ocr_artifact VALUES('legacy','child-4','lease','{}','apple',.8,'{}','ready',?,?, 'legacy-action')",(now,now))
        c.execute("""CREATE TABLE kg_parent_textbook_ocr_review_event (
          event_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL REFERENCES kg_parent_textbook_ocr_artifact(artifact_id) ON DELETE CASCADE,
          event_type TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL
        )""")
        c.execute("INSERT INTO kg_parent_textbook_ocr_review_event VALUES('legacy-event','legacy','reviewed','{}',?)",(now,))
        ensure_schema(c, now); c.commit()
        row=c.execute("SELECT status,image_sha256,page_context_sha256 FROM kg_parent_textbook_ocr_artifact WHERE artifact_id='legacy'").fetchone()
        index=c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_kg_parent_textbook_ocr_artifact_child'").fetchone()
        event=c.execute("SELECT client_action_id,payload_sha256 FROM kg_parent_textbook_ocr_review_event WHERE event_id='legacy-event'").fetchone()
    assert dict(row)=={'status':'ready','image_sha256':'','page_context_sha256':''} and index and dict(event)=={'client_action_id':'legacy:legacy-event','payload_sha256':'legacy'}


def test_v10_artifact_with_newer_fk_review_event_migrates_without_losing_event(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db'); setup(database); now=utc_now()
    with database.connect() as c:
        ensure_schema(c,now)
        c.execute("INSERT INTO kg_parent_textbook_capture_lease(lease_id,child_id,system_id_hash,capabilities_json,status,opened_at,last_renewed_at,expires_at) VALUES('lease-v10','child-4','legacy-camera','{}','released',?,?,?)",(now,now,now))
        c.execute('DROP TABLE kg_parent_textbook_ocr_review_event')
        c.execute('DROP INDEX idx_kg_parent_textbook_ocr_artifact_child')
        c.execute('DROP TABLE kg_parent_textbook_ocr_artifact')
        c.execute("""CREATE TABLE kg_parent_textbook_ocr_artifact (
          artifact_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),lease_id TEXT NOT NULL REFERENCES kg_parent_textbook_capture_lease(lease_id),page_context_json TEXT NOT NULL,
          extracted_text TEXT NOT NULL,confidence REAL NOT NULL,details_json TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('needs_review','ready','imported')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE
        )""")
        c.execute("INSERT INTO kg_parent_textbook_ocr_artifact VALUES('legacy-new-event','child-4','lease-v10','{}','apple',.8,'{}','ready',?,?, 'legacy-new-action')",(now,now))
        c.execute("""CREATE TABLE kg_parent_textbook_ocr_review_event (
          event_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL REFERENCES kg_parent_textbook_ocr_artifact(artifact_id) ON DELETE CASCADE,
          event_type TEXT NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL
        )""")
        c.execute("INSERT INTO kg_parent_textbook_ocr_review_event VALUES('new-event','legacy-new-event','reviewed','{}','hash-new','new-review-action',?)",(now,))
        ensure_schema(c,now); c.commit()
        event=c.execute("SELECT artifact_id,client_action_id,payload_sha256 FROM kg_parent_textbook_ocr_review_event WHERE event_id='new-event'").fetchone()
    assert dict(event)=={'artifact_id':'legacy-new-event','client_action_id':'new-review-action','payload_sha256':'hash-new'}


def test_parent_capture_review_commands_replay_safely_and_can_undo_correction(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-review-001')
    corrected=service.review('child-4',artifact['capture_ref'],'corrected apple','review-action-001')
    assert service.review('child-4',artifact['capture_ref'],'corrected apple','review-action-001')['status']=='ready'
    try:
        service.review('child-4',artifact['capture_ref'],'other text','review-action-001')
        assert False, 'a replay action must be payload-bound'
    except ValueError as error:
        assert 'conflicts' in str(error)
    undone=service.undo_review('child-4',artifact['capture_ref'],'undo-review-001')
    assert undone['status']=='needs_review' and undone['extracted_text']=='Words: apple'
    assert service.undo_review('child-4',artifact['capture_ref'],'undo-review-001')['status']=='needs_review'
    assert corrected['extracted_text']=='corrected apple'


def test_parent_capture_recovers_stale_processing_and_cleans_own_unclaimed_cpu_task(tmp_path: Path) -> None:
    class MismatchAdapter(Adapter):
        def claim_ocr(self, _scheduled, _snapshot): return None
    database=Database(tmp_path/'d.db');setup(database);adapter=MismatchAdapter();service=ParentTextbookCaptureService(database,adapter)
    lease=service.acquire('child-4','bound-document-hash',{})
    try:
        service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-claim-001')
        assert False, 'a different queue claim must not run the capture OCR'
    except ValueError as error:
        assert 'original page is retained' in str(error)
    assert adapter.cancelled == 1
    with database.connect() as c:
        c.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='processing',updated_at='2000-01-01T00:00:00+00:00'")
        c.commit()
    restarted=ParentTextbookCaptureService(Database(tmp_path/'d.db'),Adapter())
    assert restarted.pending('child-4')[0]['status']=='failed'
    with database.connect() as c:
        recovery=c.execute("SELECT 1 FROM kg_parent_textbook_ocr_review_event WHERE event_type='recovery_required'").fetchone()
    assert recovery


def test_parent_capture_http_returns_stable_capacity_code_for_discarded_frame(tmp_path: Path) -> None:
    class CapacityUnavailableAdapter(Adapter):
        def claim_ocr(self, _scheduled, _snapshot): return None
    database=Database(tmp_path/'d.db');setup(database);app=FastAPI();app.state.ocr_workflow=SimpleNamespace(adapter=CapacityUnavailableAdapter());install_knowledge_graph_routes(app,database,Auth())
    with TestClient(app) as client:
        headers={'X-HSC-Parent':'ok'}
        lease=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/leases',headers=headers,json={'system_id_hash':'bound-document-hash'}).json()
        response=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/ocr',headers=headers,json={
            'lease_id':lease['lease_id'],'image_base64':'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',
            'page_context':{'title':'四上英语'},'client_action_id':'ocr-capacity-001',
        })
    assert response.status_code==409 and response.json()['detail']['code']=='TEXTBOOK_OCR_CAPACITY_UNAVAILABLE'


def test_cpu_adapter_cancellation_finishes_the_enqueued_task_without_retaining_media(tmp_path: Path) -> None:
    class QueueOcr:
        def __init__(self): self.cancelled: list[str] = []
        def enqueue(self, _image, request_id, preferred_route): return {'job_id':f'job:{request_id}'}
        def cancel(self, job_id): self.cancelled.append(job_id); return {'status':'cancelled'}
    queue=CpuResourceQueue(tmp_path/'cpu.db'); ocr=QueueOcr(); adapter=CpuScheduledMediaAdapter(queue,ocr)
    scheduled=adapter.enqueue_ocr(b'frame bytes',request_id='capture-action-001')
    adapter.cancel_ocr(scheduled,'claim_mismatch')
    with queue._connect() as c:
        task=c.execute("SELECT status,payload_json FROM cpu_task WHERE task_id=?",(scheduled['task']['task_id'],)).fetchone()
    assert ocr.cancelled==['job:capture-action-001'] and task['status']=='cancelled' and 'frame bytes' not in task['payload_json']


def test_parent_capture_concurrent_identical_action_reserves_once_and_returns_one_artifact(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);adapter=Adapter();service=ParentTextbookCaptureService(database,adapter)
    lease=service.acquire('child-4','bound-document-hash',{})
    request=dict(child_id='child-4',lease_id=lease['lease_id'],image_base64='aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',page_context={'title':'四上英语'},client_action_id='ocr-concurrent-001')
    with ThreadPoolExecutor(max_workers=2) as pool:
        artifacts=list(pool.map(lambda _: service.recognize(**request), range(2)))
    assert adapter.enqueued == 1 and {artifact['capture_ref'] for artifact in artifacts}.__len__() == 1
    assert {artifact['status'] for artifact in artifacts} <= {'processing','needs_review'}


def test_known_ocr_claim_never_consumes_an_earlier_unrelated_queue_task(tmp_path: Path) -> None:
    class QueueOcr:
        def enqueue(self, _image, request_id, preferred_route): return {'job_id':f'job:{request_id}'}
        def cancel(self, _job_id): return {'status':'cancelled'}
    queue=CpuResourceQueue(tmp_path/'cpu.db'); adapter=CpuScheduledMediaAdapter(queue,QueueOcr())
    earlier=queue.enqueue('asr',{'client_action_id':'earlier-task'},task_id='asr:earlier-task')
    scheduled=adapter.enqueue_ocr(b'frame bytes',request_id='own-capture-001')
    claim=adapter.claim_ocr(scheduled,ResourceSnapshot(cpu_percent=0,memory_mb=0))
    with queue._connect() as c:
        prior=c.execute("SELECT status FROM cpu_task WHERE task_id=?",(earlier['task_id'],)).fetchone()
    assert claim and claim['task_id']==scheduled['task']['task_id'] and prior['status']=='queued'
    adapter.cancel_ocr(scheduled,'test_cleanup')


def test_capture_import_replays_after_page_commit_and_http_errors_are_semantic(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);app=FastAPI();app.state.ocr_workflow=SimpleNamespace(adapter=Adapter());install_knowledge_graph_routes(app,database,Auth())
    with TestClient(app) as client:
        headers={'X-HSC-Parent':'ok'}
        lease=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/leases',headers=headers,json={'system_id_hash':'bound-document-hash'}).json()
        body={'lease_id':lease['lease_id'],'image_base64':'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==','page_context':{'title':'四上英语'},'client_action_id':'ocr-import-001'}
        artifact=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/ocr',headers=headers,json=body).json()['artifact']
        client.put(f"/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/{artifact['capture_ref']}/review",headers=headers,json={'extracted_text':'Words: apple','client_action_id':'review-import-001'})
        page_body={'title':'四上英语','ocr_text':'Words: apple','grade_scope':'四年级','subject_scope':'英语','page_number':6,'child_id':'child-4','capture_ref':artifact['capture_ref'],'client_action_id':'page-import-001'}
        # Simulate the crash window: page transaction committed but artifact has not yet been finalized.
        TextbookOcrPageService(database).import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple',grade_scope='四年级',subject_scope='英语',page_number=6,source_ref=f"local_parent_textbook_capture:{artifact['capture_ref']}"))
        first=client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json=page_body)
        replay=client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json=page_body)
        assert first.status_code==200 and replay.status_code==200
        changed_import=client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json={**page_body,'title':'四上英语（错误改写）'})
        assert changed_import.status_code==409
        assert client.get('/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts',headers=headers).json()['items']==[]
        conflict=client.post('/v1/parent/knowledge-graph/textbook-capture/child-4/ocr',headers=headers,json={**body,'image_base64':'ZGlmZmVyZW50IGltYWdlIGZvciBjb25mbGljdA=='})
        assert conflict.status_code==409
        missing=client.put('/v1/parent/knowledge-graph/textbook-capture/child-4/artifacts/not-a-real-ref/review',headers=headers,json={'extracted_text':'x','client_action_id':'review-missing-001'})
        assert missing.status_code==404
        incomplete=client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json={'title':'四上英语','ocr_text':'x','capture_ref':artifact['capture_ref']})
        assert incomplete.status_code==422
        bypass=client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json={
            'title':'伪造的高拍仪页','ocr_text':'wrong text','page_number':99,
            'source_ref':f"local_parent_textbook_capture:{artifact['capture_ref']}",
        })
        assert bypass.status_code==422
        assert client.get('/v1/parent/knowledge-graph/textbook-pages',headers=headers).json()['items'][0]['title']=='四上英语'
        # Real vertical slice: reviewed textbook page -> conservative candidate
        # -> parent confirmation -> assessable atom and child-visible material.
        # A browser-provided mistake ID is not host evidence and cannot update mastery.
        page_ref=first.json()['page']['page_ref']
        candidates=client.post(f'/v1/parent/knowledge-graph/textbook-pages/{page_ref}/language-candidates/extract',headers=headers).json()['items']
        apple=next(item for item in candidates if item['normalized']=='apple')
        confirmed=client.put(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}",headers=headers,json={'term':'apple','focus_kind':'word','proposed':{'senses':['苹果']},'status':'confirmed','client_action_id':'capture-vertical-candidate-001'})
        assert confirmed.status_code==200 and confirmed.json()['item']['status']=='confirmed'
        with database.connect() as c:
            atom=c.execute("SELECT node_id FROM kg_node WHERE canonical_key='language:english:word:apple:meaning_in_context'").fetchone()['node_id']
        mistake=client.post('/v1/children/child-4/knowledge-graph/mistakes',json={'node_id':atom,'mistake_id':'apple-context-001','note':'教材页词义判断错误'})
        path=client.get('/v1/children/child-4/knowledge-graph/learning-path',params={'node_id':atom})
        search=client.get('/v1/children/child-4/knowledge-graph/materials/search',params={'query':'apple'})
        plan=client.post('/v1/children/child-4/knowledge-graph/review-plans',json={'client_action_id':'apple-review-plan-001','max_items':3})
        assert mistake.status_code==409 and mistake.json()['detail']['code']=='HOST_MISTAKE_REQUIRED'
        assert path.status_code==200 and path.json()['materials'] and path.json()['mastery']['evidence_count']==0
        assert plan.status_code==200 and plan.json()['source_evidence_count']==0 and plan.json()['items']==[]
        assert path.json()['materials'][0]['mapping_source_ref'].startswith('textbook_focus_manifest_v1:candidate:')
        assert search.status_code==200 and search.json()['items'][0]['text']=='Words: apple'

    # Parent confirmation, child retrieval and the truthful empty plan all come
    # back after a server/database restart rather than living in request state.
    restarted=FastAPI();restarted.state.ocr_workflow=SimpleNamespace(adapter=Adapter());install_knowledge_graph_routes(restarted,Database(tmp_path/'d.db'),Auth())
    with TestClient(restarted) as client:
        restored=client.get('/v1/children/child-4/knowledge-graph/learning-path',params={'node_id':atom})
        latest=client.get('/v1/children/child-4/knowledge-graph/review-plans/latest')
        assert restored.status_code==200 and restored.json()['mastery']['evidence_count']==0 and restored.json()['materials']
        assert latest.status_code==200 and latest.json()['plan']['source_evidence_count']==0 and latest.json()['plan']['items']==[]
        withdrawn=client.post(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}/withdraw",headers={'X-HSC-Parent':'ok'},json={'client_action_id':'capture-vertical-withdraw-001'})
        hidden=client.get('/v1/children/child-4/knowledge-graph/materials/search',params={'query':'apple'})
        assert withdrawn.status_code==200 and withdrawn.json()['item']['status']=='withdrawn' and hidden.json()['items']==[]

    after_withdraw=FastAPI();after_withdraw.state.ocr_workflow=SimpleNamespace(adapter=Adapter());install_knowledge_graph_routes(after_withdraw,Database(tmp_path/'d.db'),Auth())
    with TestClient(after_withdraw) as client:
        assert client.get('/v1/children/child-4/knowledge-graph/materials/search',params={'query':'apple'}).json()['items']==[]
        assert client.get('/v1/children/child-4/knowledge-graph/review-plans/latest').json()['plan']['items']==[]


def test_host_application_registers_the_parent_capture_router_behind_parent_pin(tmp_path: Path) -> None:
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'host.db',maintenance_enabled=False,data_encryption_key=b'0'*32)
    with TestClient(create_app(settings)) as client:
        denied=client.get('/v1/parent/knowledge-graph/textbook-capture/child-primary-4/artifacts')
        token=client.post('/v1/parent/setup',json={'pin':'2468'}).json()['parent_token']
        allowed=client.get('/v1/parent/knowledge-graph/textbook-capture/child-primary-4/artifacts',headers={'X-HSC-Parent':token})
    assert denied.status_code==401 and allowed.status_code==200 and allowed.json()['items']==[]


def test_page_import_reservation_rejects_concurrent_different_actions_before_page_write(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-reservation-001')
    service.review('child-4',artifact['capture_ref'],'Words: apple','review-reservation-001')
    source=service.page_source_ref(artifact['capture_ref'])
    winner={'title':'四上英语','ocr_text':'Words: apple','page_number':6,'source_ref':source}
    loser={**winner,'title':'错误覆盖页','ocr_text':'wrong text'}
    def reserve(action: str, request: dict) -> str:
        try: return 'replay' if service.reserve_page_import('child-4',artifact['capture_ref'],action,request) else 'reserved'
        except ValueError: return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda pair: reserve(*pair), [('page-action-one',winner),('page-action-two',loser)]))
    assert sorted(results)==['conflict','reserved']
    with database.connect() as c:
        resource_count=c.execute("SELECT COUNT(*) FROM kg_resource WHERE corpus_id='family-textbook-ocr-pages-v1'").fetchone()[0]
        importing=c.execute("SELECT status,import_request_json FROM kg_parent_textbook_ocr_artifact").fetchone()
    assert resource_count==0 and importing['status']=='importing' and json.loads(importing['import_request_json']) in (winner,loser)


def test_stale_importing_recovers_by_stable_source_or_returns_to_ready(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);service=ParentTextbookCaptureService(database,Adapter())
    lease=service.acquire('child-4','bound-document-hash',{})
    artifact=service.recognize('child-4',lease['lease_id'],'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==',{'title':'四上英语'},'ocr-import-recovery-001')
    service.review('child-4',artifact['capture_ref'],'Words: apple','review-import-recovery-001')
    source=service.page_source_ref(artifact['capture_ref']); request={'title':'四上英语','ocr_text':'Words: apple','page_number':6,'source_ref':source}
    service.reserve_page_import('child-4',artifact['capture_ref'],'page-import-recovery-001',request)
    TextbookOcrPageService(database).import_page(TextbookOcrPageInput(**request))
    with database.connect() as c:
        c.execute("UPDATE kg_parent_textbook_ocr_artifact SET updated_at='2000-01-01T00:00:00+00:00' WHERE artifact_id IN (SELECT artifact_id FROM kg_parent_textbook_ocr_artifact)");c.commit()
    restarted=ParentTextbookCaptureService(Database(tmp_path/'d.db'),Adapter())
    assert restarted.pending('child-4')==[]
    with database.connect() as c:
        status=c.execute("SELECT status FROM kg_parent_textbook_ocr_artifact").fetchone()['status']
    assert status=='imported'


def test_concurrent_parent_camera_acquire_allows_only_one_active_lease(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);service=ParentTextbookCaptureService(database,Adapter())
    def acquire(_: int) -> str:
        try: return service.acquire('child-4','bound-document-hash',{})['lease_id']
        except ValueError: return 'busy'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(acquire,range(2)))
    assert results.count('busy')==1 and len([item for item in results if item!='busy'])==1
    with database.connect() as c:
        active=c.execute("SELECT COUNT(*) FROM kg_parent_textbook_capture_lease WHERE status='active'").fetchone()[0]
    assert active==1


def test_legacy_duplicate_active_camera_leases_are_normalized_before_unique_index(tmp_path: Path) -> None:
    database=Database(tmp_path/'d.db');setup(database);now=utc_now()
    with database.connect() as c:
        # Replace the isolated empty v66 table with its pre-unique-index shape.
        c.execute("DROP TABLE kg_parent_textbook_capture_lease")
        c.execute("""CREATE TABLE kg_parent_textbook_capture_lease (
          lease_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),system_id_hash TEXT NOT NULL,
          capabilities_json TEXT NOT NULL,status TEXT NOT NULL,opened_at TEXT NOT NULL,last_renewed_at TEXT NOT NULL,expires_at TEXT NOT NULL,released_at TEXT,release_reason TEXT
        )""")
        c.execute("INSERT INTO kg_parent_textbook_capture_lease VALUES('legacy-lease-1','child-4','bound-document-hash','{}','active',?,?,datetime('now','+3 minutes'),NULL,NULL)",(now,now))
        c.execute("INSERT INTO kg_parent_textbook_capture_lease VALUES('legacy-lease-2','child-4','bound-document-hash','{}','active',?,?,datetime('now','+3 minutes'),NULL,NULL)",(now,now))
        ensure_schema(c,now);c.commit()
        leases=c.execute("SELECT status,release_reason FROM kg_parent_textbook_capture_lease ORDER BY lease_id").fetchall()
    assert [row['status'] for row in leases].count('active')==1 and any(row['release_reason']=='duplicate_before_unique_lease_migration' for row in leases)
