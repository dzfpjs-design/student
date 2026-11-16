from pathlib import Path
import pytest
from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService

def seeded(db):
    db.initialize()
    with db.connect() as c:
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name IN('rubric_assessment','pet_media_binding')").fetchone()
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)VALUES('schema-child','public synthetic','初三','江苏南京','active',datetime('now'),datetime('now'))")
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)VALUES('schema-session','schema-child','active','check_in',datetime('now'),datetime('now'),'schema-start')")
        c.commit()

@pytest.mark.parametrize('scope',['session','privacy'])
def test_core_database_empty_job_has_no_optional_table_delete_dependency(tmp_path,scope):
    db=Database(tmp_path/'study.db')
    try:
        seeded(db)
        if scope=='session':
            with db.connect() as c:
                c.execute("DELETE FROM daily_session WHERE session_id='schema-session'");c.commit()
                assert c.execute('SELECT COUNT(*)FROM daily_session').fetchone()[0]==0
        else:
            service=PrivacyLifecycleService(db,tmp_path/'private');service.initialize()
            service.delete_child_scope('schema-child','all_history','确认删除','schema-erase')
        with db.connect()as c:
            assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        pass  # Every original connection is already context-closed; no runtime client was begun.

# All PCM, transcripts and deployment values below are original public synthetic
# fixtures. They prove source/CAS/deletion and migration, not model installation.
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from home_study_agent import pet_speaking_candidate_jobs as job_schema
from home_study_agent import pet_multipa_offline as offline
from home_study_agent.parent_restore_assets import validate_schema as validate_restore_schema
from home_study_agent.pet_speaking_candidate_jobs import quarantine_after_restore
from test_pet_media_evidence import media,CHILD
from test_pet_speaking_candidate_jobs import prepared,_fixture_register,drop_empty_schema92_receipts,owned_worker_host,public_host,worker
from test_pet_speaking_model_candidates import output

def install_original90(db):
    with db.connect()as c:
        c.execute('BEGIN IMMEDIATE')
        # A real old90 fixture has no newer immutable receipt history. Never
        # delete nonempty schema92 receipts to pretend that history was old90.
        drop_empty_schema92_receipts(c)
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'pet_multipa_execution_%'").fetchone()
        rows=[tuple(r)for r in c.execute('SELECT * FROM pet_speaking_candidate_job ORDER BY job_id')]
        c.execute('DROP TABLE pet_speaking_candidate_job')
        for _,_,sql in job_schema.OBJECTS_90:c.execute(sql)
        if rows:c.executemany('INSERT INTO pet_speaking_candidate_job VALUES('+','.join('?'for _ in rows[0])+')',rows)
        c.execute('DELETE FROM schema_migration WHERE version>=91');c.commit()
        job_schema.validate_schema(c,source_version=90)
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==90
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert [tuple(r)for r in c.execute('SELECT * FROM pet_speaking_candidate_job ORDER BY job_id')]==rows
    return rows

def test_real_90_empty_optional_closure_upgrades_and_deletes(tmp_path):
    db=Database(tmp_path/'v90.db');seeded(db);install_original90(db)
    reopened=Database(db.path);reopened.initialize()
    assert reopened.latest_upgrade_status()['status']=='completed'
    with reopened.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==92
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name IN('rubric_assessment','pet_media_binding')").fetchone()
        assert {r[2]for r in c.execute('PRAGMA foreign_key_list(pet_speaking_candidate_job)')}=={'child_profile','daily_session','pet_speaking_model_candidate'}
        c.execute("DELETE FROM daily_session WHERE session_id='schema-session'");c.commit()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()

@pytest.mark.parametrize('state',['queued','claimed','completed','unknown'])
def test_real90_rows_history_index_trigger_preserved_and_no_auto_inference(media,monkeypatch,state,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    if state in {'claimed','unknown'}:jobs._claim()
    if state=='unknown':
        from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs
        SpeakingCandidateJobs(jobs.database,jobs.runtime,media_root=jobs.media_root).recover_interrupted()
    if state=='completed':
        # Historical completed90: genuine raw child and original legacy store,
        # no schema92 cap/receipt and no current claimed-worker bypass.
        from home_study_agent.pet_multipa_candidate import prepare_input
        from home_study_agent.pet_speaking_candidate_jobs import _Claim,validate_claim,complete_claim
        claimed=jobs._claim();claim=_Claim(claimed)
        with jobs.database.connect()as c:
            binding,content=prepare_input(c,media[2],**args)
        raw=worker.predict_in_worker('TEST_ONLY',content)
        answer=jobs.runtime.speaking_candidate_assessor.store.record(CHILD,media[2],binding=binding,
            result=raw,asset_hashes=worker.configured('TEST_ONLY')['asset_hashes'],client_action_id=claimed['client_action_id'])
        with jobs.database.connect()as c:
            c.execute('BEGIN IMMEDIATE')
            validate_claim(c,claim,CHILD,media[2],binding,worker.configured('TEST_ONLY')['asset_hashes'])
            complete_claim(c,claim,answer['current']['candidate_id']);c.commit()
        assert jobs.read(CHILD,row['job_id'])['state']=='completed'
    before=install_original90(jobs.database);call_count=len(calls)
    with jobs.database.connect()as c:numeric=[tuple(r)for r in c.execute('SELECT * FROM pet_speaking_model_candidate')]
    reopened=Database(jobs.database.path);reopened.initialize()
    with reopened.connect()as c:
        assert [tuple(r)for r in c.execute('SELECT * FROM pet_speaking_candidate_job ORDER BY job_id')]==before
        assert [tuple(r)for r in c.execute('SELECT * FROM pet_speaking_model_candidate')]==numeric
        job_schema.validate_schema(c,source_version=91);validate_restore_schema(c)
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        with pytest.raises(sqlite3.IntegrityError,match='PET_SPEAKING_JOB_IDENTITY_OR_STATE'):
            c.execute("UPDATE pet_speaking_candidate_job SET child_id='other'WHERE job_id=?",(row['job_id'],))
    assert len(calls)==call_count

@pytest.mark.parametrize('damage',['index','trigger','incoming'])
def test_unknown90_contract_is_not_silently_repaired(tmp_path,damage):
    db=Database(tmp_path/'unknown90.db');seeded(db);install_original90(db)
    with db.connect()as c:
        if damage=='index':c.execute('CREATE INDEX unreviewed_job_index ON pet_speaking_candidate_job(updated_at)')
        elif damage=='trigger':
            c.execute('DROP TRIGGER pet_speaking_job_identity');c.execute("CREATE TRIGGER pet_speaking_job_identity BEFORE UPDATE ON pet_speaking_candidate_job BEGIN SELECT 1;END")
        else:c.execute('CREATE TABLE unknown_job_child(id TEXT REFERENCES pet_speaking_candidate_job(job_id) ON DELETE CASCADE)')
        c.commit();before=[tuple(r)for r in c.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE '%job%'ORDER BY name")]
    with pytest.raises(Exception):Database(db.path).initialize()
    with db.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==90
        assert [tuple(r)for r in c.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE '%job%'ORDER BY name")]==before
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_speaking_candidate_job_schema91_stage'").fetchone()


def test_real_upgrade_failure_restores_original90_snapshot_and_rows(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);_fixture_register(jobs,media[2],turn);before=install_original90(jobs.database)
    original=job_schema._migrate_90
    def fail_after_rebuild(c):
        original(c)
        raise RuntimeError('SYNTHETIC_AFTER_REBUILD_FAILURE')
    monkeypatch.setattr(job_schema,'_migrate_90',fail_after_rebuild)
    reopened=Database(jobs.database.path)
    with pytest.raises(RuntimeError,match='SYNTHETIC_AFTER_REBUILD_FAILURE'):reopened.initialize()
    assert reopened.latest_upgrade_status()['status']=='restored_after_failure'
    with reopened.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==90
        assert [tuple(r)for r in c.execute('SELECT * FROM pet_speaking_candidate_job ORDER BY job_id')]==before
        job_schema.validate_schema(c,source_version=90)
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_speaking_candidate_job_schema91_stage'").fetchone()
    assert calls==[]

@pytest.mark.parametrize('version',[90,91])
def test_restore_source_contract_version_then_quarantine_without_rerun(media,monkeypatch,version,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn);jobs._claim()
    if version==90:install_original90(jobs.database)
    else:
        with jobs.database.connect()as c:
            drop_empty_schema92_receipts(c)
            c.execute('DELETE FROM schema_migration WHERE version>=92');c.commit()
            assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==91
            job_schema.validate_schema(c,source_version=91)
    with jobs.database.connect()as c:
        validate_restore_schema(c);c.execute('BEGIN IMMEDIATE');quarantine_after_restore(c);c.commit()
        assert c.execute('SELECT state,owner_token,owner_epoch FROM pet_speaking_candidate_job').fetchone()[:]==('blocked',None,None)
    reopened=Database(jobs.database.path);reopened.initialize()
    with reopened.connect()as c:
        validate_restore_schema(c);assert c.execute('SELECT state FROM pet_speaking_candidate_job').fetchone()[0]=='blocked'
    assert calls==[]

@pytest.mark.parametrize('source',['assessment','media'])
def test_deleted_optional_source_never_runs_queued_job(media,monkeypatch,source,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    with jobs.database.connect()as c:
        if source=='assessment':c.execute('DELETE FROM rubric_assessment WHERE assessment_id=?',(args['assessment_id'],))
        else:c.execute('DELETE FROM pet_media_binding WHERE media_id=?',(args['media_id'],))
        c.commit()
    assert jobs.process_one()['state']=='blocked'and calls==[]
    assert jobs.read(CHILD,row['job_id'])['candidate_id']is None

@pytest.mark.parametrize('scope',['media','learning'])
def test_actual_inflight_deletion_cannot_commit_or_revive_candidate(media,monkeypatch,scope,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);_fixture_register(jobs,media[2],turn);entered=threading.Event();release=threading.Event()
    communicate=worker._communicate_owned
    def original_result_inflight(*a,**kw):
        result=communicate(*a,**kw)
        entered.set();assert release.wait(5)
        return result
    monkeypatch.setattr(worker,'_communicate_owned',original_result_inflight)
    with ThreadPoolExecutor(max_workers=1)as pool:
        future=pool.submit(jobs.process_one)
        try:
            assert entered.wait(5)
            privacy=PrivacyLifecycleService(jobs.database,jobs.database.path.parent);privacy.initialize()
            privacy.delete_child_scope(CHILD,scope,'确认删除','delete-inflight-'+scope)
        finally:release.set()
        result=future.result(timeout=5)
    assert result['state']=='cancelled'
    with jobs.database.connect()as c:
        assert not c.execute('SELECT 1 FROM pet_speaking_candidate_job WHERE child_id=?',(CHILD,)).fetchone()
        assert not c.execute('SELECT 1 FROM pet_speaking_model_candidate WHERE child_id=?',(CHILD,)).fetchone()
    assert jobs.process_one()is None


def test_uppercase_incoming_reference_rejected_without_cascading_public_rows(media,monkeypatch,owned_worker_host):
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn);before=install_original90(jobs.database)
    with jobs.database.connect()as c:
        c.execute('CREATE TABLE unknown_uppercase_job_child(id TEXT REFERENCES PET_SPEAKING_CANDIDATE_JOB(job_id) ON DELETE CASCADE)')
        c.execute('INSERT INTO unknown_uppercase_job_child VALUES(?)',(row['job_id'],));c.commit()
    with pytest.raises(ValueError,match='MIGRATION_INCOMING_REFERENCE'):Database(jobs.database.path).initialize()
    with jobs.database.connect()as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==90
        assert [tuple(r)for r in c.execute('SELECT * FROM pet_speaking_candidate_job ORDER BY job_id')]==before
        assert c.execute('SELECT id FROM unknown_uppercase_job_child').fetchone()[0]==row['job_id']
    assert calls==[]


def test_sqlitex_user_table_incoming_reference_preserves_entire_original90_database(media,monkeypatch,owned_worker_host):
    import hashlib
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn);before_jobs=install_original90(jobs.database)
    with jobs.database.connect()as c:
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        c.execute('CREATE TABLE sqliteXjob_child(id TEXT REFERENCES pet_speaking_candidate_job(job_id) ON DELETE CASCADE)')
        c.execute('INSERT INTO sqliteXjob_child VALUES(?)',(row['job_id'],));c.commit()
        before=hashlib.sha256(repr(tuple(c.iterdump())).encode()).hexdigest()
    reopened=Database(jobs.database.path)
    with pytest.raises(ValueError,match='MIGRATION_INCOMING_REFERENCE'):reopened.initialize()
    assert reopened.latest_upgrade_status()['status']=='restored_after_failure'
    with jobs.database.connect()as c:
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0]==90
        assert [tuple(r)for r in c.execute('SELECT * FROM pet_speaking_candidate_job ORDER BY job_id')]==before_jobs
        assert c.execute('SELECT id FROM sqliteXjob_child').fetchone()[0]==row['job_id']
        assert hashlib.sha256(repr(tuple(c.iterdump())).encode()).hexdigest()==before
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_speaking_candidate_job_schema91_stage'").fetchone()
    assert calls==[]


def test_real_source_withdrawal_and_cross_child_cancel_never_revive_job(media,monkeypatch,owned_worker_host):
    import json
    from home_study_agent.learning_evidence_withdrawal import record_withdrawal
    jobs,turn,args,calls=prepared(media,monkeypatch);row=_fixture_register(jobs,media[2],turn)
    with pytest.raises(ValueError,match='NOT_FOUND'):jobs.cancel('child-junior-3',row['job_id'])
    assert jobs.read(CHILD,row['job_id'])==row
    with jobs.database.connect()as c:
        evidence=json.loads(c.execute('SELECT payload_json FROM rubric_assessment WHERE assessment_id=?',(args['assessment_id'],)).fetchone()[0])['source_evidence_id']
        c.execute('BEGIN IMMEDIATE')
        record_withdrawal(c,child_id=CHILD,session_id=media[2],evidence_id=evidence,client_action_id='schema91-public-withdrawal',request={'operation':'withdraw-public-synthetic'})
        c.commit()
    assert jobs.process_one()['state']=='blocked'and calls==[]
    assert jobs.process_one()is None
    with jobs.database.connect()as c:
        assert c.execute('SELECT COUNT(*)FROM pet_speaking_model_candidate').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*)FROM learning_evidence_withdrawal WHERE evidence_id=?',(evidence,)).fetchone()[0]==1
    assert _fixture_register(jobs,media[2],turn)is None
