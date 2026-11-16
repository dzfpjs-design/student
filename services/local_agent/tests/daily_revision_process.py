"""Separate-process, synthetic-only HTTP persistence witness; no listening socket."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

payload = json.loads(sys.stdin.read())
data = Path(payload['data_dir']).resolve()
database = Path(payload['database_path']).resolve()
temporary_roots = {Path(tempfile.gettempdir()).resolve()}
if os.name == 'posix':
    temporary_roots.add(Path('/tmp').resolve())
assert any(data.is_relative_to(root) and database.is_relative_to(root) for root in temporary_roots)
os.environ.clear()
os.environ.update(PATH='/usr/bin:/bin', HOME=str(data / 'home'), HSC_DATA_DIR=str(data),
    HSC_DATABASE_PATH=str(database), HSC_MODEL_PROVIDER='disabled', HSC_ASR_PROVIDER='disabled',
    HSC_TTS_PROVIDER='disabled', HSC_MAINTENANCE_ENABLED='0', PYTHONDONTWRITEBYTECODE='1')
sys.dont_write_bytecode = True
source = Path(payload['source_dir']).resolve()
assert source == Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(source))

def deny(*args, **kwargs):
    raise AssertionError('Synthetic process has no network permission')

socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = deny
from datetime import datetime
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent import repository, learning_input_replan, learning_state
from test_paused_homework_revision_runtime_http import SemanticGateway, say

now = datetime.fromisoformat(payload['now'])
repository.utc_now = learning_state.utc_now = lambda: now.isoformat()
learning_input_replan.utc_now = lambda: now
gateway = SemanticGateway(revision=payload.get('revision'))
results = []
with TestClient(create_app(Settings(data_dir=data, database_path=database,
        maintenance_enabled=False), model_gateway=gateway)) as client:
    for operation in payload['operations']:
        if operation['kind'] == 'say':
            turn, result = say(client, payload['session_id'], operation['text'], operation['key'])
            results.append({'turn': turn, 'result': result})
        else:
            response = client.request(operation.get('method', 'GET'), operation['path'], json=operation.get('json'))
            assert response.status_code == 200, response.text
            results.append(response.json())
print('HSC_PROCESS_RESULT ' + json.dumps({'pid': os.getpid(), 'results': results,
      'model_requests': len(gateway.requests)}, ensure_ascii=False))
