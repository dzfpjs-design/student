"""Synthetic actual HTTP answer/evaluation fixture, never a certified score."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'services/local_agent/src'), str(ROOT / 'services/local_agent/tests')]
parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', required=True)
parser.add_argument('--facts', action='store_true')
args = parser.parse_args()
data = Path(args.data_dir).resolve()
if not any(p.name.startswith('hsc-pet-task-native-') for p in data.parents):
    raise ValueError('Only owned synthetic native directories')
path = data / 'pet-task.db'

def facts():
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('owned_pet_facts', ROOT / 'scripts/seed-pet-task-native.py')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.facts(data)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        result['learning_hashes'] = {}
        for table in ('diagnostic_attempt', 'learning_evidence', 'learning_evaluation_execution', 'rubric_assessment', 'pet_media_binding'):
            rows = sorted([list(r) for r in c.execute('SELECT * FROM ' + table)], key=repr)
            result['learning_hashes'][table] = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
        result['safety'] = [dict(r) for r in c.execute('SELECT child_id,session_id,agent_turn_id,risk_level FROM child_safety_event ORDER BY created_at')]
        result['recovery_turns'] = [dict(r) for r in c.execute("""SELECT t.agent_turn_id,t.child_id,t.session_id,t.tool_status,t.intent,
            p.objective_id,s.capability_name,s.status AS step_status FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
            JOIN agent_step s USING(plan_id) WHERE json_extract(t.action_json,'$.local_recovery_control.version')='local_voice_recovery_v1'""")]
    return result

if args.facts:
    print(json.dumps(facts()))
else:
    if path.exists():
        raise ValueError('Never overwrite an existing native fixture')
    data.mkdir(parents=True, exist_ok=True)
    os.environ['HSC_DATA_DIR'] = str(data)
    os.environ['HSC_DATABASE_PATH'] = str(path)
    from fastapi.testclient import TestClient
    from pytest import MonkeyPatch
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.database import Database
    from test_agent_continuation import Gateway
    from test_pending_evaluation_voice_capture_http import speaking_receipt
    from test_voice_activity_capture_lifecycle import parent
    settings = Settings(data_dir=data, database_path=path, maintenance_enabled=False)
    db = Database(path)
    with contextlib.redirect_stdout(sys.stderr), MonkeyPatch.context() as patch:
        with TestClient(create_app(settings, model_gateway=Gateway({}))) as client:
            parent(client)
            sid, prompt, evaluation_id, _ = speaking_receipt(client, settings, db, patch)
    print(json.dumps({'skill': 'reading', 'mode': 'fresh', 'session_id': sid, 'child_id': 'child-primary-4',
        'evaluation_turn_id': evaluation_id, 'prompt': prompt,
        'boundary': 'Actual synthetic speaking answer + completed local evaluation; review not approved, no reliable mastery claim'}))
