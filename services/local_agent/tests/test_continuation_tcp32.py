"""Loopback app replies deliberately lost after real committed TCP responses."""
import socket
import json
from dataclasses import replace
import threading
import time

import httpx
import pytest
import uvicorn

from home_study_agent.app import create_app
from test_continuation_lifecycle32 import prepared_root
from test_continuation_receipt32 import expire
from test_diagnostic_prompt_http import environment, CHILD


class TCPClient:
    def __init__(self, app):
        self.app, self.drop = app, None

    def __enter__(self):
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(128)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='error', access_log=False))
        self.worker = threading.Thread(target=self.server.run, kwargs={'sockets': [self.socket]})
        self.worker.start()
        deadline = time.monotonic() + 15
        while not self.server.started and self.worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        if not self.server.started:
            self.server.should_exit = True
            self.worker.join(10)
            self.socket.close()
            raise AssertionError('TCP app failed to start')
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.socket.getsockname()[1]}', trust_env=False, timeout=15)
        return self

    def request(self, method, path, **kwargs):
        start = time.monotonic()
        response = self.client.request(method, path, **kwargs)
        print({'method': method, 'path': path, 'http': response.status_code,
            'elapsed_ms': round((time.monotonic() - start) * 1000, 2), 'drop_after_response': self.drop == path})
        if self.drop == path:
            self.drop = None
            assert response.status_code == 200, response.text
            raise httpx.ReadError('Synthetic client delivery loss AFTER actual server 200 commit')
        return response

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def put(self, path, **kwargs):
        return self.request('PUT', path, **kwargs)

    def __exit__(self, *exc):
        self.client.close()
        self.server.should_exit = True
        self.worker.join(15)
        self.socket.close()
        assert not self.worker.is_alive(), 'TCP worker not drained'


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {'127.0.0.1', '::1'}, 'External socket forbidden'
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)


@pytest.mark.parametrize('lost', ['plan', 'execute', 'admission'])
def test_real_tcp_loss_restart_reads_original_work_without_replaying_effect(tmp_path, monkeypatch, lost):
    db, sid, settings, gateway = prepared_root(tmp_path, device=lost == 'admission')
    continued = f'/v1/sessions/{sid}/agent-turns/root32/continue'
    with TCPClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.post('/v1/agent-turns/root32/execute').status_code == 200
        if lost == 'plan':
            client.drop = continued
            with pytest.raises(httpx.ReadError):
                client.post(continued)
            with db.connect() as c:
                following = c.execute("SELECT next_turn_id FROM agent_continuation WHERE parent_turn_id='root32'").fetchone()[0]
        else:
            following = client.post(continued).json()['turn']['agent_turn_id']
            path = (f'/v1/agent-turns/{following}/execute' if lost == 'execute' else
                    f'/v1/sessions/{sid}/agent-turns/{following}/continuation-admission')
            client.drop = path
            with pytest.raises(httpx.ReadError):
                client.post(path)
        with db.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM session_transition')]
            turns_before = [tuple(r) for r in c.execute('SELECT * FROM agent_turn')]
    if lost != 'plan':
        expire(monkeypatch)
    requests = len(gateway.requests)
    with TCPClient(create_app(settings, model_gateway=gateway)) as client:
        read = client.get(f'/v1/sessions/{sid}/agent-continuation?parent_turn_id=root32').json()
        expected = {'plan': 'not_confirmed', 'execute': 'completed', 'admission': 'awaiting_receipt'}[lost]
        assert read['status'] == expected, read
        assert read['next_turn_id'] == following and read['root_turn_id'] == 'root32'
        assert not read['replay_allowed'] and not read['mastery_update_allowed']
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM session_transition')] == before
            assert [tuple(r) for r in c.execute('SELECT * FROM agent_turn')] == turns_before
        if lost == 'plan':
            saved = client.post(continued)
            assert saved.json()['status'] == 'ready' and saved.json()['turn']['agent_turn_id'] == following
            assert client.post(f'/v1/agent-turns/{following}/execute').status_code == 200
            assert client.get(f'/v1/sessions/{sid}/agent-continuation').json()['status'] == 'completed'
        elif lost == 'admission':
            assert client.post(f'/v1/sessions/{sid}/agent-turns/{following}/continuation-admission').json()['status'] != 'admitted'
        else:
            assert client.post(continued).json()['status'] == 'completed'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM session_transition').fetchone()[0] == (0 if lost == 'admission' else 1)
            assert c.execute("SELECT count(*) FROM agent_turn WHERE intent='runtime_continuation'").fetchone()[0] == 1
    assert len(gateway.requests) == requests == 1
    print({'db': str(db.path), 'loss': lost, 'owned_workers': 0, 'model_calls': len(gateway.requests),
        'no_device_call': True, 'history_status': expected})


@pytest.mark.parametrize('lost', ['plan', 'execute', 'admission'])
def test_production_planner_tcp_loss_restart_from_original_child_request(environment, monkeypatch, lost):
    from home_study_agent.model_gateway import ModelGateway
    from test_agent_continuation import plan, next_plan
    settings, db = environment
    settings = replace(settings, model_provider='qwen', model_name='isolated-intent',
        model_api_key='synthetic-fa77d7bdb2ae64fbd28ade4f', model_base_url='https://continuation.invalid/v1')
    root, following_plan = plan().to_dict(), next_plan()
    quote = '精神很好，记下来再安排学习'
    if lost == 'admission':
        quote = '精神很好，记下来再告诉我摄像头状态'
        follow = {'capability': 'device.status', 'arguments': {'device': 'camera'}}
        root['continuation_scope'] = [{**follow, 'request_quote': '告诉我摄像头状态'}]
        root['objective']['summary'] = quote
        following_plan['next_step'] = follow
    root['next_step'] = {k: root['next_step'][k] for k in ('capability', 'arguments', 'reason', 'expected_evidence')}
    root['utterance_binding'] = {'role': 'task_request', 'quote': quote}
    posts = []
    def transport(request):
        posts.append(json.loads(request.content))
        value = root if len(posts) == 1 else following_plan
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}}]})
    def application():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TCPClient(application()) as client:
        setup = client.post('/v1/parent/setup', json={'pin': '2846'})
        assert setup.status_code == 200, setup.text
        assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': setup.json()['parent_token']},
            json={'cloud_model_text_allowed': True}).status_code == 200
        opened = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'production-session32'})
        assert opened.status_code == 201, opened.text
        sid = opened.json()['session_id']
        first = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'client_action_id': 'production-root32'})
        assert first.status_code == 200 and first.json().get('runtime_capability') == 'checkin.update_context', first.text
        root_id = first.json()['agent_turn_id']
        assert client.post(f'/v1/agent-turns/{root_id}/execute').status_code == 200
        continued = f'/v1/sessions/{sid}/agent-turns/{root_id}/continue'
        if lost == 'plan':
            client.drop = continued
            with pytest.raises(httpx.ReadError):
                client.post(continued)
        else:
            prepared = client.post(continued)
            assert prepared.status_code == 200 and prepared.json()['status'] == 'ready', prepared.text
        with db.connect() as c:
            next_id = c.execute('SELECT next_turn_id FROM agent_continuation WHERE parent_turn_id=?', (root_id,)).fetchone()[0]
            assert next_id
        if lost != 'plan':
            path = (f'/v1/agent-turns/{next_id}/execute' if lost == 'execute' else
                    f'/v1/sessions/{sid}/agent-turns/{next_id}/continuation-admission')
            client.drop = path
            with pytest.raises(httpx.ReadError):
                client.post(path)
        with db.connect() as c:
            original_turns = [tuple(r) for r in c.execute('SELECT * FROM agent_turn')]
            original_transitions = [tuple(r) for r in c.execute('SELECT * FROM session_transition')]
    assert len(posts) == 2
    if lost != 'plan':
        expire(monkeypatch)
    with TCPClient(application()) as client:
        receipt = client.get(f'/v1/sessions/{sid}/agent-continuation?parent_turn_id={root_id}')
        expected = {'plan': 'not_confirmed', 'execute': 'completed', 'admission': 'awaiting_receipt'}[lost]
        assert receipt.status_code == 200 and receipt.json()['status'] == expected, receipt.text
        assert receipt.json()['next_turn_id'] == next_id and receipt.json()['root_turn_id'] == root_id
        assert not receipt.json()['replay_allowed'] and not receipt.json()['mastery_update_allowed']
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM agent_turn')] == original_turns
            assert [tuple(r) for r in c.execute('SELECT * FROM session_transition')] == original_transitions
        if lost == 'plan':
            ready = client.post(continued)
            assert ready.json()['status'] == 'ready' and ready.json()['turn']['agent_turn_id'] == next_id
            assert client.post(f'/v1/agent-turns/{next_id}/execute').status_code == 200
        elif lost == 'execute':
            assert client.post(continued).json()['status'] == 'completed'
        else:
            refused = client.post(f'/v1/sessions/{sid}/agent-turns/{next_id}/continuation-admission')
            assert refused.json()['status'] != 'admitted'
        with db.connect() as c:
            count = c.execute("SELECT count(*) FROM session_transition WHERE session_id=? AND to_state='classroom_recall'", (sid,)).fetchone()[0]
            assert count == (0 if lost == 'admission' else 1)
            assert c.execute("SELECT count(*) FROM agent_turn WHERE intent='runtime_continuation'").fetchone()[0] == 1
            assert c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?', (root_id,)).fetchone()[0] == quote
    assert len(posts) == 2
    print({'production_planner': True, 'db': str(db.path), 'loss': lost, 'history_status': expected,
        'mock_posts': len(posts), 'transition_count': count, 'owned_workers': 0, 'no_device_call': True})
