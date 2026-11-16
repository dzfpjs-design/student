"""Build and audit local atomic-learning candidates without approving them."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.atomic_knowledge import AtomicCandidateStore


def main()->None:
    parser=argparse.ArgumentParser()
    parser.add_argument('--app-db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    database=Database(args.app_db);database.initialize()
    build=AtomicCandidateStore(database).extract_pending(limit=args.limit)
    with database.connect() as connection:
        distributions={}
        for name,sql in {
            'by_source':"SELECT source_kind key,COUNT(*) evidence_rows,COUNT(DISTINCT canonical_signature) unique_atoms FROM kg_atomic_candidate WHERE status='pending' GROUP BY source_kind ORDER BY unique_atoms DESC",
            'by_kind':"SELECT atom_kind key,COUNT(*) evidence_rows,COUNT(DISTINCT canonical_signature) unique_atoms FROM kg_atomic_candidate WHERE status='pending' GROUP BY atom_kind ORDER BY unique_atoms DESC",
            'by_scope':"SELECT COALESCE(json_extract(scope_json,'$.grade'),'') grade,COALESCE(json_extract(scope_json,'$.subject'),'') subject,COUNT(*) evidence_rows,COUNT(DISTINCT canonical_signature) unique_atoms FROM kg_atomic_candidate WHERE status='pending' GROUP BY grade,subject ORDER BY unique_atoms DESC",
        }.items():distributions[name]=[dict(row) for row in connection.execute(sql)]
        totals=dict(connection.execute("""SELECT COUNT(*) evidence_rows,COUNT(DISTINCT canonical_signature) unique_atoms,
            COUNT(DISTINCT resource_id) resources_with_atoms,
            SUM(CASE WHEN segment_id IS NOT NULL THEN 1 ELSE 0 END) segment_grounded_rows
            FROM kg_atomic_candidate WHERE status='pending'""").fetchone())
        samples=[dict(row) for row in connection.execute("""WITH ranked AS (
            SELECT title,action_verb,object_text,atom_kind,mastery_criterion,evidence_text,scope_json,unit_path,
              source_kind,confidence,ROW_NUMBER() OVER(PARTITION BY json_extract(scope_json,'$.grade'),json_extract(scope_json,'$.subject'),atom_kind ORDER BY confidence DESC,title) rank
            FROM kg_atomic_candidate WHERE status='pending') SELECT * FROM ranked WHERE rank<=3 ORDER BY scope_json,atom_kind,title""")]
    report={"boundary":"candidate evidence only; zero candidates are approved or mastery-active",
            "build":build,"totals":totals,"distributions":distributions,"stratified_samples":samples}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({"output":str(args.output),**build,**totals},ensure_ascii=False))


if __name__=='__main__':main()
