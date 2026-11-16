"""Validate curriculum import against an isolated SQLite backup on its target host."""
from __future__ import annotations
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/local_agent/src'))
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.content_bundle import import_content
from home_study_agent.knowledge_graph.local_course_context import LocalCourseContextService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--scratch-db', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.scratch_db.exists() or args.output.exists(): raise ValueError('evidence path already exists')
    args.scratch_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.source_db.resolve().as_uri()+'?mode=ro', uri=True)) as source, closing(sqlite3.connect(args.scratch_db)) as target:
        source.backup(target)
        assert target.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        before = {table: target.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ['child_profile','daily_session','agent_turn','diagnostic_attempt','mistake_case']}
    result = import_content(args.bundle, args.scratch_db, expected_sha256=args.sha256)
    service = LocalCourseContextService(Database(args.scratch_db))
    with closing(sqlite3.connect(args.scratch_db)) as connection:
        children = dict(connection.execute("SELECT grade,child_id FROM child_profile WHERE status='active'"))
    cases = [('四年级','数学','三角形内角和'),('四年级','英语','What time is it'),('四年级','语文','观潮'),
             ('初三','数学','一元二次方程'),('初三','物理','欧姆定律'),('初三','化学','氧气')]
    probes = []
    for grade, subject, query in cases:
        started = time.perf_counter()
        response = service.context_for_child(child_id=children[grade], query=query, subject=subject)
        assert response['evidence'], (grade, subject, query, 'no source evidence')
        assert response['cloud_transmission_allowed'] is False and response['mastery_update_allowed'] is False
        probes.append({'grade':grade,'subject':subject,'query':query,'hits':len(response['evidence']),
                       'titles':[item['title'] for item in response['evidence']], 'latency_ms':round((time.perf_counter()-started)*1000)})
    assert import_content(args.bundle, args.scratch_db, expected_sha256=args.sha256)['already_imported']
    with closing(sqlite3.connect(args.scratch_db)) as connection:
        after = {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in before}
        assert before == after
        assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        events = connection.execute('SELECT count(*) FROM kg_local_course_context_event').fetchone()[0]
        assert events >= len(cases), 'local curriculum usage audit did not persist'
        grades = [list(row) for row in connection.execute('SELECT grade_scope,subject_scope,count(*) FROM kg_resource GROUP BY grade_scope,subject_scope')]
    report = {'status':'passed','import':result,'probes':probes,'original_family_counts_unchanged':True,
              'restart_read_events':events,'grade_subject_resources':grades,
              'boundaries':['isolated backup, not live import','no cloud model call','not child learning outcome','existing source labels not upgraded']}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'passed','probes':len(probes),'resources':result['tables']['kg_resource'],'evidence':str(args.output)}))


if __name__ == '__main__': main()
