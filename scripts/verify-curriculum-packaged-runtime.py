"""Real packaged HTTP curriculum/supply acceptance in a disposable profile."""
import argparse
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'services/local_agent/src'))
from home_study_agent.knowledge_graph.content_bundle import import_content

spec = importlib.util.spec_from_file_location('daily_runtime', Path(__file__).with_name('verify-daily-learning-runtime.py'))
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert os.name == 'nt' and args.exe.is_file() and not args.output.exists()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='hsc-curriculum-packaged-'))
    runtime = daily.Runtime(args.exe.resolve(), root)
    report = {'status':'running','synthetic_profile':True,'real_child_evidence':False,'cloud_calls':0,'probes':[]}
    failure = None
    try:
        report['phase'] = 'initial_start'
        runtime.start()
        runtime.stop()
        report['phase'] = 'import'
        report['import'] = import_content(args.bundle, runtime.database, expected_sha256=args.sha256)
        report['phase'] = 'restart_after_import'
        runtime.start()
        report['phase'] = 'retrieval_and_learning'
        parent = runtime.api('/v1/parent/setup', {'pin':'2468'})['parent_token']
        for _ in range(2): runtime.api('/v1/parent/local-supply/refresh', {}, parent=parent, timeout=90)
        for child, subject, query in [('child-primary-4','数学','三角形内角和'),('child-primary-4','英语','What time is it'),
                                      ('child-primary-4','语文','观潮'),('child-junior-3','数学','一元二次方程'),
                                      ('child-junior-3','物理','欧姆定律'),('child-junior-3','化学','氧气')]:
            start = time.perf_counter()
            result = runtime.api('/v1/children/'+child+'/knowledge-graph/course-context?'+urlencode({'query':query,'subject':subject}))
            assert result['evidence'] and result['cloud_transmission_allowed'] is False
            report['probes'].append({'query':query,'subject':subject,'hits':len(result['evidence']),'ms':round((time.perf_counter()-start)*1000)})
        report['daily_learning'] = []
        for fixture in daily.FIXTURES:
            record = daily.create_learning_day(runtime, fixture, parent)
            daily.daily_summary(runtime, record)
            report['daily_learning'].append({'child':fixture[0],'review_task_bound':bool(record['immediate_review_id'])})
        runtime.stop()
        runtime.start()
        parent = runtime.api('/v1/parent/unlock', {'pin':'2468'})['parent_token']
        report['supply'] = {}
        for child in ['child-primary-4','child-junior-3']:
            supply = runtime.api('/v1/parent/children/'+child+'/specialized-supply/candidates?status=promoted', parent=parent)
            report['supply'][child] = len(supply['items'])
            assert not runtime.api('/v1/parent/children/'+child+'/model-calls', parent=parent)['items']
        with closing(sqlite3.connect(runtime.database.as_uri()+'?mode=ro',uri=True)) as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            report['persisted_course_events'] = db.execute('SELECT count(*) FROM kg_local_course_context_event').fetchone()[0]
            assert report['persisted_course_events'] >= 6
            report['diagnostic_item_count'] = db.execute('SELECT count(*) FROM diagnostic_item').fetchone()[0]
            report['qualified_local_items'] = db.execute("SELECT count(*) FROM diagnostic_item WHERE source_ref LIKE 'local_verified_%'").fetchone()[0]
            assert report['qualified_local_items'] > 0
        report['phase'] = 'owned_crash_recovery'
        assert runtime.process is not None and runtime.process.poll() is None
        subprocess.run(['taskkill', '/PID', str(runtime.process.pid), '/T', '/F'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        runtime.process.wait(timeout=15)
        runtime.forced_stops += 1
        runtime.stop()
        bootstrap = runtime.start()
        assert bootstrap['recovery']['unclean_restart_detected'] is True
        for fixture in daily.FIXTURES:
            summary = runtime.api('/v1/children/' + fixture[0] + '/daily-learning-summary')
            assert summary['sessions'] and summary['sessions'][0]['child_id'] == fixture[0]
        report['owned_crash_recovery'] = True
        report['status'] = 'passed'
        report['startup_seconds'] = runtime.startup_seconds
    except Exception as error:
        failure = error
        report.update(status='failed',error=str(error),error_type=type(error).__name__)
    finally:
        try:
            runtime.stop()
            runtime.log.close()
            if failure:
                tail = (root / 'runtime.log').read_text(encoding='utf-8', errors='replace').splitlines()[-35:]
                report['isolated_runtime_log_tail'] = [line.replace(runtime.token, '[session]').replace(runtime.encryption_key, '[encryption-key]') for line in tail]
            shutil.rmtree(root)
            report['owned_profile_removed'] = True
        except Exception as error:
            failure = failure or error
            report.update(status='failed',cleanup_error=str(error),retained_profile=str(root))
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'status':report['status'],'probes':len(report['probes']),'evidence':str(args.output),'error':str(failure) if failure else None}))
    return 1 if failure else 0


if __name__ == '__main__': raise SystemExit(main())
