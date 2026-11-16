"""Read-only deployment gate for the local knowledge graph."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.runtime_budget import MinimumRuntimeBudget


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument('--app-db',type=Path,required=True)
    parser.add_argument('--minimum-free-gb',type=int,default=20); arguments=parser.parse_args()
    database=Database(arguments.app_db); budget=MinimumRuntimeBudget(minimum_free_gb=arguments.minimum_free_gb)
    with database.connect() as connection:
        integrity=connection.execute('PRAGMA integrity_check').fetchone()[0]
        counts=dict(connection.execute("""SELECT (SELECT COUNT(*) FROM kg_resource) resources,(SELECT COUNT(*) FROM kg_segment) segments,
            (SELECT COUNT(*) FROM kg_node) nodes,(SELECT COUNT(*) FROM kg_edge) edges,
            (SELECT COUNT(*) FROM kg_ingest_failure WHERE status='open') open_failures,
            (SELECT COUNT(*) FROM kg_ingest_job WHERE status IN ('processing','pending')) active_jobs,
            (SELECT MAX(version) FROM kg_schema_migration) kg_schema_version,
            (SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='kg_review_plan') review_plan_table,
            (SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='kg_mapping_review_event') review_event_table""").fetchone())
        roots=[dict(row) for row in connection.execute('SELECT corpus_id,transcript_root,video_root FROM kg_corpus ORDER BY corpus_id')]
    for item in roots:
        item['transcript_root_available']=Path(item['transcript_root']).is_dir()
        item['video_root_available']=bool(item['video_root'] and Path(item['video_root']).is_dir())
        # Paths prove source availability only; no media bytes are read or copied.
    status=database.latest_upgrade_status()
    budget_status=budget.storage_check(arguments.app_db.parent)
    passed=integrity=='ok' and counts['open_failures']==0 and counts['active_jobs']==0 and counts['kg_schema_version'] >= 3 and counts['review_plan_table']==1 and counts['review_event_table']==1 and budget_status['sufficient']
    print(json.dumps({'passed':passed,'integrity':integrity,'counts':counts,'media_roots':roots,
        'upgrade_snapshot':status,'storage':budget_status},ensure_ascii=False))
    if not passed: raise SystemExit(2)


if __name__=='__main__': main()
