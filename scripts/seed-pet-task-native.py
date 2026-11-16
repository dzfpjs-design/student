"""Owned synthetic generated-plan fixture and read-only durability facts.

Never imports or runs test command(), which marks unrelated tasks completed.
The awaiting fixture injects one failed timer call before native startup only.
"""
import argparse
import contextlib
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'services/local_agent/src'), str(ROOT / 'services/local_agent/tests')]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def checked_data(value):
    data = Path(value).resolve()
    if not any(p.name.startswith('hsc-pet-task-native-') for p in data.parents):
        raise ValueError('An owned hsc-pet-task-native-* temporary directory is required')
    return data


def facts(data):
    path = data / 'pet-task.db'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        other = {}
        # Actual foreign child rows only, not a whole-database dump or data_version.
        for table in sorted(tables):
            columns = {r[1] for r in c.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')}
            if 'child_id' in columns:
                rows = [dict(r) for r in c.execute('SELECT * FROM "' + table.replace('"', '""') + '" WHERE child_id=?', ('child-junior-3',))]
                if rows:
                    other[table] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
        scalar = lambda sql: c.execute(sql).fetchone()[0]
        return {
            'plans': [dict(r) for r in c.execute('SELECT plan_item_id,source_type,source_ref,status,started_at,active_segment_started_at FROM plan_item ORDER BY plan_item_id')],
            'prompts': [dict(r) for r in c.execute('SELECT prompt_id,child_id,origin_session_id,diagnostic_item_id,item_version,status,answer_action_id,attempt_id FROM diagnostic_prompt ORDER BY prompt_id')],
            'started_events': [dict(r) for r in c.execute("SELECT plan_item_id,event_type,COUNT(*) AS count FROM plan_item_event WHERE event_type='started' GROUP BY plan_item_id,event_type")],
            'attempts': scalar('SELECT COUNT(*) FROM diagnostic_attempt'),
            'model_calls': scalar('SELECT COUNT(*) FROM model_call'),
            'completed_tasks': scalar("SELECT COUNT(*) FROM plan_item WHERE status='completed'"),
            'media_playback_events': scalar("SELECT COUNT(*) FROM pet_media_event WHERE operation IN ('playback','playback_settled')") if 'pet_media_event' in tables else 0,
            'other_child_fingerprint': digest(other),
            'other_child_tables': {table: {'count': len(rows), 'hash': digest(rows)} for table, rows in other.items()},
            'activities': [dict(r) for r in c.execute('SELECT activity_id,child_id,session_id,status,turn_count,ends_at FROM companion_activity')] if 'companion_activity' in tables else [],
            'activity_reply_receipts': [dict(r) for r in c.execute("""SELECT t.agent_turn_id,t.child_id,t.session_id,t.tool_status,
                s.capability_name,s.status AS step_status FROM agent_turn t
                JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
                WHERE t.intent='companion_activity_reply' ORDER BY t.created_at""")],
        }


def recover_receipt(data):
    """Recover launcher identity only; never recreate a prompt or plan."""
    path = data / 'pet-task.db'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute('SELECT * FROM diagnostic_prompt').fetchall()
        if len(rows) != 1:
            raise ValueError('Expected exactly one retained original prompt')
        p = dict(rows[0])
        item = json.loads(p['item_json'])
        plans = c.execute("""SELECT pi.plan_item_id,pi.status FROM plan_item pi
            JOIN daily_plan dp ON dp.plan_id=pi.plan_id
            WHERE dp.session_id=? AND pi.source_type='pet_daily' AND pi.source_ref=?""",
            (p['origin_session_id'], p['diagnostic_item_id'])).fetchall()
        if len(plans) != 1 or plans[0]['status'] != 'pending' or p['status'] != 'waiting_answer':
            raise ValueError('Recovery is limited to the retained unstarted original')
        if p['child_id'] != 'child-primary-4' or item['track'] != 'pet':
            raise ValueError('Retained fixture scope mismatch')
        prompt = {key: p[key] for key in ('prompt_id', 'child_id', 'origin_session_id', 'diagnostic_item_id',
                                         'item_version', 'review_task_id', 'created_at')}
        prompt['item'] = item
        return {'synthetic_only': True, 'mode': 'awaiting', 'skill': 'listening' if item['answer_type'] == 'listening_text' else 'reading',
                'child_id': p['child_id'], 'session_id': p['origin_session_id'], 'plan_item_id': plans[0]['plan_item_id'],
                'diagnostic_item_id': p['diagnostic_item_id'], 'knowledge_node_id': item['knowledge_node_id'],
                'question': item['prompt'], 'stimulus': item.get('stimulus_text'), 'original_prompt': prompt,
                'database_path': str(path), 'recovered_launcher_receipt': True, 'facts': facts(data)}


def seed(data, skill, state):
    # Importing the HTTP fixture can construct the default app against explicit
    # HSC_DATABASE_PATH. Check ownership before those production imports.
    if (data / 'pet-task.db').exists():
        raise ValueError('Seed refuses to overwrite a retained fixture')
    data.mkdir(parents=True, exist_ok=True)
    from fastapi.testclient import TestClient
    from test_pet_task_execution import generated, CHILD
    from test_learning_runtime_http import NoCloud
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.repository import StudyRepository
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.parent_auth import ParentAuth

    db, sid, pid, node, original = generated(data, skill)
    repo = StudyRepository(db)
    current = repo.get_workspace(sid)
    if current['session']['state'] == 'plan_review':
        repo.transition(sid, 'plan_review', 'study', 'plan_confirmed', 'native-confirm-generated-plan')
    elif current['session']['state'] != 'study':
        raise ValueError('Unexpected generated session state; do not bypass transition guards')
    unfinished = [r for r in repo.get_workspace(sid)['plan_items'] if r['status'] == 'pending']
    if not unfinished or unfinished[0]['plan_item_id'] != pid:
        raise ValueError('PET is not the first real pending task; preserve fixture for inspection')
    privacy = PrivacyLifecycleService(db, data)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': False, 'cloud_audio_allowed': False,
                                'raw_media_storage_allowed': False})
    ParentAuth(db).setup('2468')
    result = {'synthetic_only': True, 'skill': skill, 'mode': state, 'child_id': CHILD,
              'session_id': sid, 'plan_item_id': pid, 'diagnostic_item_id': original['diagnostic_item_id'],
              'knowledge_node_id': node, 'question': original['prompt'], 'stimulus': original.get('stimulus_text'),
              'database_path': str(db.path), 'original_prompt': None}
    settings = Settings(data_dir=data, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        if state == 'awaiting':
            planned = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': '开始任务', 'client_action_id': 'native-prestart-original-command'})
            if planned.status_code != 200:
                raise RuntimeError('Original local planning failed: ' + planned.text)
            turn = planned.json()
            if (turn.get('runtime_capability') or turn.get('action', {}).get('capability')) != 'task.start':
                raise RuntimeError('Actual local command did not select task.start: ' + json.dumps(turn))
            with patch.object(StudyRepository, 'start_plan_item', side_effect=RuntimeError('NATIVE_SEED_TIMER_NOT_COMMITTED')):
                failed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            if failed.status_code < 400:
                raise RuntimeError('The injected timer failure was not observed')
            read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt')
            if read.status_code != 200 or read.json()['status'] != 'awaiting_task_start':
                raise RuntimeError('Missing durable awaiting_task_start after timer failure: ' + read.text)
            result['original_prompt'] = read.json()['prompt']
            result['failed_turn_id'] = turn['agent_turn_id']
            result['fault_injection'] = 'one seed-only start_plan_item exception; no success receipt or status rewrite'
    result['facts'] = facts(data)
    if result['facts']['attempts'] or result['facts']['completed_tasks'] or result['facts']['started_events']:
        raise RuntimeError('Seed invented completed learning or started timers')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--skill', choices=('reading', 'listening'), default='reading')
    parser.add_argument('--mode', choices=('fresh', 'awaiting'), default='fresh')
    parser.add_argument('--facts', action='store_true')
    parser.add_argument('--recover-receipt', action='store_true')
    args = parser.parse_args()
    owned = checked_data(args.data_dir)
    with contextlib.redirect_stdout(sys.stderr):
        if args.recover_receipt and (owned.parent / 'seed.json').exists():
            raise ValueError('Existing launcher receipt must not be overwritten')
        result = facts(owned) if args.facts else recover_receipt(owned) if args.recover_receipt else seed(owned, args.skill, args.mode)
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if not args.facts:
        # Preserve the fixture identity even if the launcher loses stdout.
        (owned.parent / 'seed.json').write_text(serialized + '\n')
    print(serialized)
