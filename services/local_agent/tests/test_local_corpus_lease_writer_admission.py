"""Real SQLite locks and original synthetic ingestion, no real F07 corpus/model."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, current_thread
from time import sleep
import sqlite3
import sys
import threading

import pytest
from home_study_agent.database import Database, StudyConnection
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor, IngestRunAlreadyOwned


@pytest.fixture
def corpus(tmp_path):
    root=tmp_path.resolve(strict=True);db=Database(root/'study.db');db.initialize()
    transcripts=root/'transcripts';transcripts.mkdir();path=transcripts/'one.txt'
    path.write_text('写事务续租的有限合成文本。',encoding='utf-8')
    spec=CorpusSpec('writer-admission','合成事务','seyk',transcripts,None,'local','user_local')
    ingestor=LocalCorpusIngestor(db);job=ingestor.start(spec,idempotency_key='synthetic-once')
    ingestor.run_lease_seconds=.05
    return db,ingestor,spec,job,path


def claim(db,ingestor,job):
    with db.connect()as con:token=ingestor._claim_run(con,job)
    assert token
    return token


def durable(db,job):
    with db.connect()as con:
        row=dict(con.execute('SELECT status,processed_count,failed_count,checkpoint_cursor,run_token,run_lease_expires_at FROM kg_ingest_job WHERE job_id=?',(job,)).fetchone())
        count=con.execute('SELECT count(*) FROM kg_resource').fetchone()[0]
    return row,count


def assert_completed_once(db,job):
    row,count=durable(db,job)
    assert row=={'status':'completed','processed_count':1,'failed_count':0,'checkpoint_cursor':'one','run_token':None,'run_lease_expires_at':None}
    assert count==1


class SQLTrace:
    """Delegate the same SQL; record only its first real lease/commit per actor."""
    def __init__(self,monkeypatch):
        self.heartbeat_attempt=Event();self.heartbeat_commit=Event();self.checkpoint_attempt=Event()
        self.records=[];self.pending={};self.on_heartbeat_commit=None;self.before_checkpoint=None
        execute=StudyConnection.execute;commit=StudyConnection.commit
        def observed(con,sql,*args,**kwargs):
            caller=sys._getframe(1).f_code.co_name;s=' '.join(sql.split());parameters=args[0]if args else ()
            if caller=='renew'and(s=='BEGIN IMMEDIATE'or s.startswith('UPDATE kg_ingest_job SET run_lease_expires_at=')):
                self.heartbeat_attempt.set()
            is_checkpoint=caller=='run'and(s=='BEGIN IMMEDIATE'or s.startswith('UPDATE kg_ingest_job SET checkpoint_cursor='))
            if is_checkpoint:
                self.checkpoint_attempt.set()
                if self.before_checkpoint:self.before_checkpoint()
            result=execute(con,sql,*args,**kwargs)
            if s.startswith('UPDATE kg_ingest_job SET run_lease_expires_at='):
                self.pending[id(con)]={'caller':caller,'expiry':parameters[0],'rows':result.rowcount}
            elif s.startswith('UPDATE kg_ingest_job SET checkpoint_cursor='):
                self.pending[id(con)]={'caller':caller,'expiry':parameters[-3],'rows':result.rowcount}
            return result
        def committed(con,*args,**kwargs):
            result=commit(con,*args,**kwargs);record=self.pending.pop(id(con),None)
            if record:
                self.records.append(record)
                if record['caller']=='renew':
                    if self.on_heartbeat_commit:self.on_heartbeat_commit()
                    self.heartbeat_commit.set()
            return result
        monkeypatch.setattr(StudyConnection,'execute',observed);monkeypatch.setattr(StudyConnection,'commit',committed)


def test_original_late_heartbeat_cannot_regress_resource_transaction_lease(corpus,monkeypatch):
    db,ingestor,spec,job,path=corpus;owner=claim(db,ingestor,job);trace=SQLTrace(monkeypatch)
    entered=Event();release=Event();holder={}
    def before_commit():entered.set();assert release.wait(5)
    ingestor._before_resource_commit=before_commit
    with ThreadPoolExecutor(max_workers=1)as pool:
        future=pool.submit(ingestor._upsert_file,spec,job,owner,'one',path,None,{},False)
        stop=heartbeat=None
        try:
            assert entered.wait(5);ingestor.heartbeat_interval_seconds=.01
            stop,heartbeat=ingestor._start_heartbeat(job,owner);holder['stop']=stop
            trace.on_heartbeat_commit=stop.set
            assert trace.heartbeat_attempt.wait(5)
            sleep(.12);released=datetime.now(timezone.utc);release.set();future.result(5)
            assert trace.heartbeat_commit.wait(5)
        finally:
            release.set()
            if stop:stop.set()
            if heartbeat:heartbeat.join(5);assert not heartbeat.is_alive()
    resource=[r for r in trace.records if r['caller']=='_upsert_file'][-1]
    renewed=[r for r in trace.records if r['caller']=='renew'][0]
    assert datetime.fromisoformat(renewed['expiry'])>=datetime.fromisoformat(resource['expiry'])
    assert datetime.fromisoformat(renewed['expiry'])>released
    row,count=durable(db,job);assert bool(row['run_token']==owner)and count==1
    assert row['run_lease_expires_at']==renewed['expiry']and renewed['rows']==1


def test_original_checkpoint_lease_is_computed_after_sqlite_writer_admission(corpus,monkeypatch):
    db,ingestor,spec,job,_=corpus;trace=SQLTrace(monkeypatch);committed=Event();writer_ready=Event()
    original=ingestor._resolve_failure
    def resolved(*args):original(*args);committed.set();assert writer_ready.wait(5)
    ingestor._resolve_failure=resolved
    with ThreadPoolExecutor(max_workers=1)as pool:
        future=pool.submit(ingestor.run,spec,job)
        try:
            assert committed.wait(5)
            with db.connect()as con:
                con.execute('BEGIN IMMEDIATE');writer_ready.set();assert trace.checkpoint_attempt.wait(5)
                sleep(.12);released=datetime.now(timezone.utc);con.commit()
            result=future.result(5)
        finally:writer_ready.set()
    checkpoint=[r for r in trace.records if r['caller']=='run'][-1]
    assert datetime.fromisoformat(checkpoint['expiry'])>released
    assert result['status']=='completed';assert_completed_once(db,job)


def test_original_recovery_token_is_unchanged_by_late_old_heartbeat(corpus,monkeypatch):
    db,ingestor,spec,job,_=corpus;old=claim(db,ingestor,job);sleep(.12);new=claim(db,ingestor,job)
    assert bool(old!=new);before,_=durable(db,job);trace=SQLTrace(monkeypatch)
    ingestor.heartbeat_interval_seconds=.01;stop,heartbeat=ingestor._start_heartbeat(job,old)
    trace.on_heartbeat_commit=stop.set
    try:assert trace.heartbeat_commit.wait(5)
    finally:stop.set();heartbeat.join(5);assert not heartbeat.is_alive()
    after,_=durable(db,job);assert before==after
    assert [r for r in trace.records if r['caller']=='renew'][0]['rows']==0
    sleep(.12);ingestor.heartbeat_interval_seconds=20
    assert ingestor.run(spec,job)['status']=='completed';assert_completed_once(db,job)


def test_actual_recovery_handoff_fences_old_checkpoint_and_finishes_one_durable_resource(corpus,monkeypatch):
    db,ingestor,spec,job,_=corpus;trace=SQLTrace(monkeypatch);resource_committed=Event();allow_old=Event();seen=[]
    original=ingestor._resolve_failure
    def resolved(*args):
        original(*args)
        if current_thread().name.startswith('old-original'):
            resource_committed.set();assert allow_old.wait(5)
    ingestor._resolve_failure=resolved
    claim_original=ingestor._claim_run
    def observed_claim(*args):
        result=claim_original(*args)
        if result:seen.append(result)
        return result
    ingestor._claim_run=observed_claim
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='old-original')as pool:
        first=pool.submit(ingestor.run,spec,job)
        try:
            assert resource_committed.wait(5);sleep(.12)
            second=ingestor.run(spec,job);assert second['status']=='completed'
            assert len(seen)==2 and bool(seen[0]!=seen[1]);allow_old.set()
            with pytest.raises(IngestRunAlreadyOwned,match='^ingest checkpoint lease was taken by a recovery worker$'):first.result(5)
        finally:allow_old.set()
    assert_completed_once(db,job)


def test_sqlite_writer_rejection_is_not_swallowed_and_original_heartbeat_stops(corpus,monkeypatch):
    db,ingestor,_,job,_=corpus;owner=claim(db,ingestor,job);before,_=durable(db,job)
    with db.connect()as con:
        con.execute("CREATE TRIGGER synthetic_reject_renew BEFORE UPDATE OF run_lease_expires_at ON kg_ingest_job BEGIN SELECT RAISE(FAIL,'synthetic writer rejected'); END");con.commit()
    failed=Event();errors=[]
    def exception(args):errors.append(args.exc_value);failed.set()
    monkeypatch.setattr(threading,'excepthook',exception);ingestor.heartbeat_interval_seconds=.01
    stop,heartbeat=ingestor._start_heartbeat(job,owner)
    try:assert failed.wait(5)
    finally:stop.set();heartbeat.join(5);assert not heartbeat.is_alive()
    assert len(errors)==1 and type(errors[0])is sqlite3.IntegrityError and str(errors[0])=='synthetic writer rejected'
    after,_=durable(db,job);assert before==after
    with db.connect()as con:con.execute('BEGIN IMMEDIATE');con.rollback()
