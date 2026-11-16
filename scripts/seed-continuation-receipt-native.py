"""Real local planner and receipt writes with synthetic model transport only."""
import json
import os
from pathlib import Path
import sys

db_path, receipt_path = (Path(arg).resolve() for arg in sys.argv[1:3])
os.environ.update(HSC_DATA_DIR=str(db_path.parent), HSC_DATABASE_PATH=str(db_path), HSC_MAINTENANCE_ENABLED='0')
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root/'services/local_agent/src'), str(root/'services/local_agent/tests')]


def local_only(event, args):
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] not in ('127.0.0.1', '::1', 'localhost'):
        raise RuntimeError('External socket forbidden')


sys.addaudithook(local_only)
import httpx
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_agent_continuation import plan, next_plan

settings = Settings(data_dir=db_path.parent, database_path=db_path, maintenance_enabled=False,
    model_provider='qwen', model_name='isolated-intent', model_api_key='synthetic-not-a-key',
    model_base_url='https://continuation.invalid/v1')
quote = '精神很好，记下来再告诉我摄像头状态'
original, following = plan().to_dict(), next_plan()
step = {'capability': 'device.status', 'arguments': {'device': 'camera'}}
original['continuation_scope'] = [{**step, 'request_quote': '告诉我摄像头状态'}]
original['objective']['summary'] = quote
original['next_step'] = {k: original['next_step'][k] for k in ('capability', 'arguments', 'reason', 'expected_evidence')}
original['utterance_binding'] = {'role': 'task_request', 'quote': quote}
following['next_step'] = step
posts = []


def transport(request):
    posts.append(request.url.path)
    assert len(posts) <= 2
    content = original if len(posts) == 1 else following
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content, ensure_ascii=False)}}]})


with TestClient(create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))) as client:
    def post(path, body=None):
        response = client.post(path, json=body)
        assert response.status_code in (200, 201), (path, response.status_code)
        return response.json()
    parent = post('/v1/parent/setup', {'pin': '2846'})
    assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': parent['parent_token']},
        json={'cloud_model_text_allowed': True}).status_code == 200
    sid = post('/v1/sessions', {'child_id': 'child-primary-4', 'client_action_id': 'native-continuation-session'})['session_id']
    turn = post(f'/v1/sessions/{sid}/agent-turn', {'transcript': quote, 'client_action_id': 'native-continuation-root'})
    assert turn['runtime_capability'] == 'checkin.update_context'
    parent_id = turn['agent_turn_id']
    post(f'/v1/agent-turns/{parent_id}/execute')
    prepared = post(f'/v1/sessions/{sid}/agent-turns/{parent_id}/continue')
    assert prepared['status'] == 'ready'
    next_id = prepared['turn']['agent_turn_id']
    admitted = post(f'/v1/sessions/{sid}/agent-turns/{next_id}/continuation-admission')
    assert admitted['status'] == 'admitted'
    receipt = client.get(f'/v1/sessions/{sid}/agent-continuation').json()
    assert receipt['status'] == 'awaiting_receipt' and not receipt['replay_allowed']
assert len(posts) == 2
receipt_path.write_text(json.dumps({'session_id': sid, 'parent_turn_id': parent_id, 'next_turn_id': next_id,
    'synthetic_model_posts': len(posts), 'device_calls': 0}, ensure_ascii=False), encoding='utf-8')
