"""Resume strict local candidate mapping and emit a compact coverage/audit matrix."""
from __future__ import annotations
import argparse, hashlib, json, sqlite3, tempfile
from datetime import datetime, timezone
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.coverage_mapping import AtomicCoverageMapper

def sha256_file(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        while block:=handle.read(1024*1024): digest.update(block)
    return digest.hexdigest()

def consistent_snapshot(path: Path, temporary: Path) -> Path:
    """Copy one committed SQLite view, including committed WAL pages, to ``temporary``."""
    snapshot = temporary / 'coverage-snapshot.db'
    with sqlite3.connect(path) as source:
        # A read transaction pins the source view for the entire backup.  It is
        # deliberately allowed to capture a running mapper: the report labels
        # that state from this same snapshot, rather than racing a pre-check.
        source.execute('BEGIN')
        try:
            with sqlite3.connect(snapshot) as target:
                source.backup(target)
        finally:
            source.rollback()
    with sqlite3.connect(snapshot) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('coverage snapshot integrity check failed')
    return snapshot


def build_report(snapshot_path: Path, job_id: str) -> dict:
    """Read every report field from the one already-verified snapshot file."""
    snapshot_db = Database(snapshot_path)
    with snapshot_db.connect() as con:
        matrix=[dict(row) for row in con.execute("""SELECT json_extract(n.scope_json,'$.grade') grade,json_extract(n.scope_json,'$.subject') subject,json_extract(n.scope_json,'$.track') track,COUNT(DISTINCT n.node_id) atomic_nodes,COUNT(DISTINCT CASE WHEN rn.review_status IN ('unreviewed','approved') THEN n.node_id END) covered_nodes,COUNT(DISTINCT rn.segment_id) segments FROM kg_node n LEFT JOIN kg_resource_node rn ON rn.node_id=n.node_id AND rn.review_status IN ('unreviewed','approved') WHERE n.node_type='knowledge_point' GROUP BY grade,subject,track ORDER BY grade,subject,track""")]
        uncovered=[dict(row) for row in con.execute("""SELECT n.title knowledge_point,json_extract(n.scope_json,'$.grade') grade,json_extract(n.scope_json,'$.subject') subject,json_extract(n.scope_json,'$.track') track FROM kg_node n WHERE n.node_type='knowledge_point' AND NOT EXISTS(SELECT 1 FROM kg_resource_node rn WHERE rn.node_id=n.node_id AND rn.review_status IN ('unreviewed','approved')) ORDER BY grade,subject,track,n.title LIMIT 300""")]
        distribution=[dict(row) for row in con.execute("""SELECT CASE WHEN c=0 THEN '0' WHEN c=1 THEN '1' WHEN c<=3 THEN '2-3' WHEN c<=10 THEN '4-10' ELSE '11+' END bucket,COUNT(*) atomic_nodes FROM (SELECT n.node_id,COUNT(DISTINCT rn.segment_id) c FROM kg_node n LEFT JOIN kg_resource_node rn ON rn.node_id=n.node_id AND rn.review_status IN ('unreviewed','approved') WHERE n.node_type='knowledge_point' GROUP BY n.node_id) GROUP BY bucket ORDER BY bucket""")]
        audit_rows=[dict(row) for row in con.execute("""WITH ranked AS (SELECT n.title knowledge_point,r.title resource_title,s.text evidence_text,s.text_sha256 evidence_sha256,rn.confidence,rn.source_ref,json_extract(n.scope_json,'$.grade') grade,json_extract(n.scope_json,'$.subject') subject,json_extract(n.scope_json,'$.track') track,ROW_NUMBER() OVER(PARTITION BY json_extract(n.scope_json,'$.grade'),json_extract(n.scope_json,'$.subject'),json_extract(n.scope_json,'$.track') ORDER BY n.title,rn.confidence DESC,s.segment_id) pos FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id JOIN kg_resource r ON r.resource_id=rn.resource_id JOIN kg_segment s ON s.segment_id=rn.segment_id WHERE rn.review_status IN ('unreviewed','approved')) SELECT * FROM ranked WHERE pos<=5 ORDER BY grade,subject,track,pos""")]
        audit=[]
        for raw_row in audit_rows:
            row=dict(raw_row)
            row.pop('evidence_text')
            audit.append(row)
        audit_strata={(item['grade'],item['subject'],item['track']) for item in audit}
        gaps=[{'grade':item['grade'],'subject':item['subject'],'track':item['track'],'reason':'no candidate segment available for sampling'} for item in matrix if (item['grade'],item['subject'],item['track']) not in audit_strata]
        counts=dict(con.execute("SELECT (SELECT COUNT(*) FROM kg_resource) resources,(SELECT COUNT(*) FROM kg_segment) segments,(SELECT COUNT(*) FROM kg_node) nodes,(SELECT COUNT(*) FROM kg_resource_node) mappings").fetchone())
        corpora=[dict(row) for row in con.execute("SELECT c.corpus_id,c.version_label,c.source_kind,COUNT(*) resources FROM kg_corpus c JOIN kg_resource r USING(corpus_id) GROUP BY c.corpus_id,c.version_label,c.source_kind ORDER BY c.corpus_id")]
        schema_version=con.execute("SELECT MAX(version) FROM kg_schema_migration").fetchone()[0]
        job=con.execute("""SELECT idempotency_key,status,checkpoint_resource_id,total_resources,
            processed_resources,mapped_rows,failed_resources,source_ref,created_at,updated_at,completed_at,
            CASE WHEN run_token IS NOT NULL AND run_lease_expires_at IS NOT NULL
                 AND julianday(run_lease_expires_at)>julianday('now') THEN 1 ELSE 0 END active_at_snapshot
            FROM kg_candidate_mapping_job WHERE mapping_job_id=?""",(job_id,)).fetchone()
        if not job:
            raise RuntimeError('mapping job is absent from coverage snapshot')
        return {
            'snapshot': {
                'database_sha256':sha256_file(snapshot_path),
                'generated_at':datetime.now(timezone.utc).isoformat(),
                'consistency':'sqlite_backup_read_transaction',
                'integrity_check':'ok',
                'schema_version':schema_version,
                'counts':counts,
                'corpora':corpora,
            },
            'job':dict(job),
            'matrix':matrix,
            'uncovered_atomic_nodes':uncovered,
            'candidate_count_distribution':distribution,
            'label_free_precision_proxy':'exact lexical evidence only; not human precision',
            'label_free_recall_proxy':'covered_nodes / atomic_nodes',
            'stratified_audit_samples':audit,
            'audit_strata_without_samples':gaps,
            '_private_audit_samples':audit_rows,
        }

def main() -> None:
    parser=argparse.ArgumentParser();parser.add_argument('--app-db',type=Path,required=True);parser.add_argument('--idempotency-key',required=True);parser.add_argument('--batch-size',type=int,default=25);parser.add_argument('--max-batches',type=int,default=1);parser.add_argument('--report-output',type=Path);parser.add_argument('--private-audit-output',type=Path)
    args=parser.parse_args();db=Database(args.app_db);mapper=AtomicCoverageMapper(db);job=mapper.start(idempotency_key=args.idempotency_key)
    for _ in range(max(1,args.max_batches)):
        state=mapper.run(job,max_resources=max(1,min(args.batch_size,100)))
        if state['status']=='completed':break
    with tempfile.TemporaryDirectory(prefix='hsc-kg-coverage-') as temporary:
        snapshot_path=consistent_snapshot(args.app_db,Path(temporary))
        result=build_report(snapshot_path,job)
    private_samples=result.pop('_private_audit_samples')
    if args.private_audit_output:
        args.private_audit_output.parent.mkdir(parents=True,exist_ok=True)
        private_payload={'snapshot':result['snapshot'],'job':result['job'],'stratified_audit_samples':private_samples}
        args.private_audit_output.write_text(json.dumps(private_payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        result['private_audit']={'status':'written_local_untracked','sha256':sha256_file(args.private_audit_output),'path_status':'local_data_audit'}
    else:
        result['private_audit']={'status':'not_written','path_status':'local_data_audit'}
    if args.report_output: args.report_output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__': main()
