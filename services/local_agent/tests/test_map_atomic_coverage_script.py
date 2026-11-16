import importlib.util
import sqlite3
from pathlib import Path
from typing import Optional

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.schema import ensure_schema


def _script_module():
    path=Path(__file__).parents[1]/'scripts'/'map_atomic_coverage.py'
    spec=importlib.util.spec_from_file_location('map_atomic_coverage_script',path)
    module=importlib.util.module_from_spec(spec);assert spec.loader
    spec.loader.exec_module(module)
    return module


def _insert_job(connection: sqlite3.Connection, job_id: str, *, processed: int, token: Optional[str] = None, lease: Optional[str] = None) -> None:
    connection.execute("""INSERT INTO kg_candidate_mapping_job(
        mapping_job_id,idempotency_key,status,source_ref,total_resources,processed_resources,
        mapped_rows,failed_resources,options_json,created_at,updated_at,run_token,run_lease_expires_at
    ) VALUES(?,?, 'processing','test',9,?,3,0,'{}','now','now',?,?)""",
        (job_id, job_id, processed, token, lease))


def test_consistent_snapshot_includes_wal_and_keeps_job_matrix_and_counts_together(tmp_path: Path) -> None:
    path=tmp_path/'coverage.db';database=Database(path);database.initialize()
    with database.connect() as con:
        ensure_schema(con,'2026-08-28T00:00:00+00:00');con.commit()
    writer=sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,version_label,license_boundary,confidence,review_status,status,created_at,updated_at) VALUES('wal-node','knowledge_point','wal-node','WAL','test','{\"grade\":\"四年级\",\"subject\":\"数学\"}','test','v1','test',.5,'unreviewed','active','now','now')")
        _insert_job(writer,'snapshot-job',processed=1)
        writer.commit()  # Kept open so these committed pages remain in -wal.
        module=_script_module();main_hash=module.sha256_file(path)
        snapshot=module.consistent_snapshot(path,tmp_path)
        snapshot_hash=module.sha256_file(snapshot)

        # A concurrent later commit must not make individual report sections
        # silently refer to a newer live database state.
        writer.execute("UPDATE kg_candidate_mapping_job SET processed_resources=9,status='completed' WHERE mapping_job_id='snapshot-job'")
        writer.execute("INSERT INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,version_label,license_boundary,confidence,review_status,status,created_at,updated_at) VALUES('later-node','knowledge_point','later-node','Later','test','{\"grade\":\"四年级\",\"subject\":\"数学\"}','test','v1','test',.5,'unreviewed','active','now','now')")
        writer.commit()

        report=module.build_report(snapshot,'snapshot-job')
        assert report['snapshot']['database_sha256']==snapshot_hash
        assert module.sha256_file(path)==main_hash  # backup did not checkpoint/mutate the live main file
        assert report['snapshot']['integrity_check']=='ok'
        assert report['job']['processed_resources']==1
        assert report['job']['status']=='processing'
        assert report['snapshot']['counts']['nodes']==1
        assert report['matrix']==[{'grade':'四年级','subject':'数学','track':None,'atomic_nodes':1,'covered_nodes':0,'segments':0}]
        with sqlite3.connect(snapshot) as con:
            assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:
        writer.close()


def test_expired_mapping_lease_is_reported_not_treated_as_active_owner(tmp_path: Path) -> None:
    path=tmp_path/'coverage.db';database=Database(path);database.initialize()
    with database.connect() as con:
        ensure_schema(con,'2026-08-28T00:00:00+00:00')
        _insert_job(con,'expired-job',processed=2,token='interrupted-owner',lease='2000-01-01T00:00:00+00:00')
        con.commit()
    module=_script_module();snapshot=module.consistent_snapshot(path,tmp_path)
    report=module.build_report(snapshot,'expired-job')
    assert report['job']['status']=='processing'
    assert report['job']['active_at_snapshot']==0


def test_public_audit_has_no_readable_transcript_preview() -> None:
    module=_script_module()
    assert not hasattr(module, '_redacted_preview')
