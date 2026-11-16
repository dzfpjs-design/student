from datetime import datetime, timedelta, timezone
from threading import Event, Thread
import json
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.local_ocr import LocalOcrService, LocalOcrWorkflow
from home_study_agent.local_ocr.resource_budget import CpuResourceQueue, ResourceSnapshot
from home_study_agent.local_ocr.runtime_adapter import CpuScheduledMediaAdapter
from home_study_agent.repository import StudyRepository, utc_now
from test_local_ocr_pipeline import FakeEngine, image_bytes


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / 'main.db')
    db.initialize()
    DocumentMonitoringService(db)
    with db.connect() as c:
        for child in ('A', 'B'):
            c.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                VALUES(?,?,'grade4','synthetic','active',?,?)""", (child, child, utc_now(), utc_now()))
        c.commit()
    repo = StudyRepository(db)
    session = repo.start_session('A', 'start-A')['session_id']
    ocr = LocalOcrService(db.path, [FakeEngine()])
    queue = CpuResourceQueue(tmp_path / 'queue.db')
    adapter = CpuScheduledMediaAdapter(queue, ocr)
    media = LocalMediaAI(db, tmp_path, tmp_path / 'models')
    workflow = LocalOcrWorkflow(adapter, media)
    return db, repo, session, ocr, queue, adapter, workflow


def test_invalid_session_has_no_persisted_ocr_body(env):
    db, _, _, _, _, _, workflow = env
    with pytest.raises(Exception):
        workflow.process('absent-session', image_bytes(), 'invalid', False)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM ocr_job').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ocr_cache').fetchone()[0] == 0


def test_exact_claim_never_strands_another_asr_and_success_replay_is_readable(env):
    db, _, sid, _, queue, adapter, workflow = env
    adapter.enqueue_asr('other-owner')
    artifact = workflow.process(sid, image_bytes(), 'my-ocr', False)
    again = workflow.process(sid, image_bytes(), 'my-ocr', False)
    assert again['artifact_id'] == artifact['artifact_id']
    with queue._connect() as c:
        assert c.execute("SELECT status FROM cpu_task WHERE task_type='asr'").fetchone()[0] == 'queued'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM capture_artifact').fetchone()[0] == 1


def test_same_key_other_image_or_other_session_is_a_conflict(env):
    db, repo, sid, _, _, _, workflow = env
    workflow.process(sid, image_bytes(), 'shared-key', False)
    with pytest.raises(ValueError, match='CONFLICT'):
        workflow.process(sid, image_bytes(width=460), 'shared-key', False)
    next_sid = repo.switch_active_session_child(sid, 'B', 'switch-B')['session_id']
    with pytest.raises(ValueError, match='CONFLICT'):
        workflow.process(next_sid, image_bytes(), 'shared-key', False)
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM capture_artifact WHERE child_id='B'").fetchone()[0] == 0


def test_crash_after_ocr_commit_retries_same_owner_not_other_child(env):
    db, repo, sid, ocr, queue, adapter, workflow = env
    data = image_bytes()
    scheduled = adapter.enqueue_ocr(data, 'pipeline:crash', session_id=sid)
    claim = adapter.claim_ocr(scheduled, ResourceSnapshot(cpu_percent=0, memory_mb=0))
    ocr.run(claim['payload']['ocr_job_id'], data)
    queue.recover_orphaned(now=datetime.now(timezone.utc) + timedelta(seconds=60))
    result = workflow.process(sid, data, 'crash', False)
    assert result['extracted_text']
    other = repo.switch_active_session_child(sid, 'B', 'switch')['session_id']
    with pytest.raises(ValueError, match='CONFLICT'):
        workflow.process(other, image_bytes(width=460), 'crash', False)


def test_single_artifact_delete_fences_replay_and_keeps_other_child(env):
    from home_study_agent.local_ocr.cache_lifecycle import delete_scope
    db, repo, sid, ocr, _, _, workflow = env
    first = workflow.process(sid, image_bytes(), 'A-page', False)
    other = repo.switch_active_session_child(sid, 'B', 'switch')['session_id']
    second = workflow.process(other, image_bytes(), 'B-page', False)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        counts = delete_scope(c, 'A', artifact_id=first['artifact_id'])
        c.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (first['artifact_id'],))
        c.commit()
        assert counts['ocr_jobs'] == 1
        assert c.execute("SELECT count(*) FROM ocr_job WHERE child_id='A'").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM ocr_job WHERE child_id='B' AND result_json IS NOT NULL").fetchone()[0] == 1
    assert workflow.process(other, image_bytes(), 'B-page', False)['artifact_id'] == second['artifact_id']
    with pytest.raises(ValueError, match='DELETED'):
        workflow.process(other, image_bytes(), 'A-page', False)


def test_deletion_during_ocr_cannot_republish_body_or_artifact(env):
    from home_study_agent.local_ocr.cache_lifecycle import delete_scope
    db, _, sid, ocr, _, _, workflow = env
    entered, release = Event(), Event()
    original = ocr._process
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    ocr._process = delayed
    outcomes = []
    def work():
        try:
            workflow.process(sid, image_bytes(), 'late', False)
        except Exception as error:
            outcomes.append(error)
    thread = Thread(target=work)
    thread.start()
    try:
        assert entered.wait(5)
        with db.external_effect_guard(), db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            delete_scope(c, 'A')
            c.commit()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and outcomes
    with db.connect() as c:
        for table in ('ocr_job', 'ocr_cache', 'capture_artifact'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_restore_quarantines_jobs_without_creating_new_success(env):
    from home_study_agent.local_ocr.cache_lifecycle import quarantine_after_restore
    db, _, sid, _, _, _, workflow = env
    workflow.process(sid, image_bytes(), 'old-success', False)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        quarantine_after_restore(c, 'restore-1')
        quarantine_after_restore(c, 'restore-1')
        c.commit()
    with pytest.raises(ValueError, match='RESTORE|QUARANTINED'):
        workflow.process(sid, image_bytes(), 'old-success', False)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM capture_artifact').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ocr_cache').fetchone()[0] == 0


def test_schema_and_delete_can_rollback_in_borrowed_transaction(env):
    from home_study_agent.local_ocr.cache_lifecycle import delete_scope, ensure_schema
    db, _, sid, _, _, _, workflow = env
    workflow.process(sid, image_bytes(), 'rollback', False)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        ensure_schema(c)
        delete_scope(c, 'A')
        assert c.in_transaction
        c.rollback()
    assert workflow.process(sid, image_bytes(), 'rollback', False)['extracted_text']


def test_lost_publication_reply_reuses_artifact_and_actual_privacy_cleans_body(env, monkeypatch):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, _, sid, _, _, _, workflow = env
    original = workflow._attach_pipeline
    def crash(*args):
        with db.connect() as c:
            row = c.execute('SELECT artifact_id,details_json FROM capture_artifact').fetchone()
            details = json.loads(row['details_json'])
            assert 'question_blocks' in details['local_pipeline']
            assert details['local_pipeline']['manual_review']['auto_accept_allowed'] is False
            assert c.execute('SELECT artifact_id FROM ocr_job').fetchone()[0] == row['artifact_id']
        raise RuntimeError('synthetic lost publication response')
    monkeypatch.setattr(workflow, '_attach_pipeline', crash)
    with pytest.raises(RuntimeError):
        workflow.process(sid, image_bytes(), 'publish-crash', False)
    monkeypatch.setattr(workflow, '_attach_pipeline', original)
    result = workflow.process(sid, image_bytes(), 'publish-crash', False)
    privacy = PrivacyLifecycleService(db, db.path.parent)
    privacy.initialize()
    assert privacy.delete_capture_artifact(result['artifact_id'], '确认删除', 'delete-one')['status'] == 'completed'
    with db.connect() as c:
        for table in ('ocr_job', 'ocr_cache', 'capture_artifact'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    with pytest.raises(ValueError, match='DELETED'):
        workflow.process(sid, image_bytes(), 'publish-crash', False)


def test_delete_after_artifact_commit_before_pointer_attach(env, monkeypatch):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, _, sid, _, _, _, workflow = env
    def crash(*args):
        raise RuntimeError('synthetic crash after durable artifact')
    monkeypatch.setattr(workflow, '_attach_pipeline', crash)
    with pytest.raises(RuntimeError):
        workflow.process(sid, image_bytes(), 'unattached', False)
    with db.connect() as c:
        artifact_id = c.execute('SELECT artifact_id FROM capture_artifact').fetchone()[0]
    privacy = PrivacyLifecycleService(db, db.path.parent)
    privacy.initialize()
    assert privacy.delete_capture_artifact(artifact_id, '确认删除', 'delete-unattached')['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM ocr_job').fetchone()[0] == 0


def test_pipeline_pointer_failure_rolls_back_artifact_and_same_job_can_resume(env):
    db, _, sid, _, _, _, workflow = env
    with db.connect() as c:
        c.execute('''CREATE TRIGGER fail_pipeline_pointer BEFORE UPDATE OF artifact_id ON ocr_job
            WHEN NEW.artifact_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'synthetic pointer failure'); END''')
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic pointer failure'):
        workflow.process(sid, image_bytes(), 'atomic-pointer-fault', False)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0
        assert c.execute('SELECT artifact_id FROM ocr_job').fetchone()[0] is None
        c.execute('DROP TRIGGER fail_pipeline_pointer')
        c.commit()
    result = workflow.process(sid, image_bytes(), 'atomic-pointer-fault', False)
    assert result['details']['local_pipeline']['question_blocks']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 1
        assert c.execute('SELECT artifact_id FROM ocr_job').fetchone()[0] == result['artifact_id']


def test_legacy_schema_bodies_are_quarantined_not_replayed(tmp_path):
    import json
    import sqlite3
    from hashlib import sha256
    from home_study_agent.local_ocr.service import OCR_PIPELINE_VERSION
    path = tmp_path / 'legacy.db'
    data = image_bytes()
    digest = sha256(data).hexdigest()
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE ocr_job(job_id TEXT PRIMARY KEY,request_id TEXT UNIQUE,image_hash TEXT,preferred_route TEXT,status TEXT,created_at TEXT,started_at TEXT,completed_at TEXT,result_json TEXT,error_code TEXT)')
        c.execute('CREATE TABLE ocr_cache(image_hash TEXT PRIMARY KEY,result_json TEXT,created_at TEXT)')
        body = json.dumps({'pipeline_version': OCR_PIPELINE_VERSION, 'lines': [{'text': 'unowned synthetic body'}]})
        c.execute('INSERT INTO ocr_job VALUES(?,?,?,?,?,?,NULL,NULL,?,NULL)', ('old', 'old-key', digest, 'cpu', 'completed', utc_now(), body))
        c.execute('INSERT INTO ocr_cache VALUES(?,?,?)', (digest, body, utc_now()))
    service = LocalOcrService(path, [FakeEngine()])
    assert service.get('old')['result'] is None
    with pytest.raises(ValueError, match='LEGACY'):
        service.enqueue(data, 'old-key')
    assert service.enqueue(data, 'fresh-key')['status'] == 'queued'
    with sqlite3.connect(path) as c:
        assert 'unowned synthetic body' in c.execute('SELECT result_json FROM ocr_cache_legacy_quarantine').fetchone()[0]


def test_legacy_purge_preserves_other_child_and_reports_unattributed(env, tmp_path):
    import json
    import sqlite3
    from home_study_agent.local_ocr.cache_lifecycle import register_legacy_store, purge_legacy_scope
    db, repo, sid, _, _, _, workflow = env
    a = workflow.process(sid, image_bytes(), 'legacy-A', False)
    sid_b = repo.switch_active_session_child(sid, 'B', 'legacy-switch')['session_id']
    b = workflow.process(sid_b, image_bytes(width=460), 'legacy-B', False)
    legacy = tmp_path / 'old-local-ocr.db'
    with sqlite3.connect(legacy) as side, db.connect() as main:
        side.execute('CREATE TABLE ocr_job(job_id TEXT PRIMARY KEY,request_id TEXT,image_hash TEXT,result_json TEXT)')
        side.execute('CREATE TABLE ocr_cache(image_hash TEXT PRIMARY KEY,result_json TEXT)')
        for job in main.execute('SELECT job_id,request_id,image_hash,result_json FROM ocr_job'):
            side.execute('INSERT INTO ocr_job VALUES(?,?,?,?)', tuple(job))
            side.execute('INSERT INTO ocr_cache VALUES(?,?)', (job['image_hash'], job['result_json']))
        side.execute("INSERT INTO ocr_job VALUES('orphan','orphan-key','orphan-hash','unowned body')")
        side.execute("INSERT INTO ocr_cache VALUES('orphan-hash','unowned body')")
    status = register_legacy_store(db, legacy)
    assert status == {'legacy_ocr_unattributed_jobs': 1, 'legacy_ocr_unattributed_cache': 1}
    with db.connect() as c:
        c.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (a['artifact_id'],))
        c.commit()
    result = purge_legacy_scope(db, legacy, 'A', a['artifact_id'])
    assert result['legacy_ocr_jobs_deleted'] == 1 and result['legacy_ocr_cache_deleted'] == 1
    assert result['legacy_ocr_unattributed_jobs'] == 1
    assert purge_legacy_scope(db, legacy, 'A', a['artifact_id'])['legacy_ocr_jobs_deleted'] == 0
    with sqlite3.connect(legacy) as c:
        assert {r[0] for r in c.execute('SELECT request_id FROM ocr_job')} == {'pipeline:legacy-B', 'orphan-key'}
    with db.connect() as c:
        assert c.execute('SELECT extracted_text FROM capture_artifact WHERE artifact_id=?', (b['artifact_id'],)).fetchone()[0]


def test_real_session_switch_inside_media_processing_cannot_publish(env, monkeypatch):
    db, repo, sid, _, _, _, workflow = env
    original = workflow.media_ai._merge_ocr_quality
    def switch_after_initial_session_check(*args, **kwargs):
        repo.switch_active_session_child(sid, 'B', 'during-publication')
        return original(*args, **kwargs)
    monkeypatch.setattr(workflow.media_ai, '_merge_ocr_quality', switch_after_initial_session_check)
    with pytest.raises(ValueError, match='SESSION_NOT_ACTIVE'):
        workflow.process(sid, image_bytes(), 'late-switch', False)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM capture_artifact').fetchone()[0] == 0
