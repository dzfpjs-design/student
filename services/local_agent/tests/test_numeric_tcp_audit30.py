"""Loopback TCP and real app lifespans against isolated synthetic SQLite data."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn

import test_numeric_independent_audit30 as audit
from test_diagnostic_prompt_http import environment
from test_mistake_gate_reliability import flow


class TCPClient:
    def __init__(self, app):
        self.app = app

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

    def post(self, path, **kwargs):
        start = time.perf_counter()
        response = self.client.post(path, **kwargs)
        print({'method': 'POST', 'path': path, 'status': response.status_code,
               'elapsed_ms': round((time.perf_counter() - start) * 1000, 2)})
        return response

    def get(self, path, **kwargs):
        return self.client.get(path, **kwargs)

    def put(self, path, **kwargs):
        return self.client.put(path, **kwargs)

    def __exit__(self, *exc):
        self.client.close()
        self.server.should_exit = True
        self.worker.join(15)
        self.socket.close()
        assert not self.worker.is_alive(), 'TCP worker not drained'


@pytest.fixture(autouse=True)
def local_tcp_only(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {'127.0.0.1', '::1'}, 'External socket forbidden'
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(audit, 'TestClient', TCPClient)


def test_tcp_original_and_same_key_survive_backend_restart(environment, monkeypatch):
    audit.test_actual_http_ambiguous_original_never_becomes_strong_child_evidence(environment, monkeypatch)
    print({'persisted_db': str(environment[1].path), 'owned_worker': 0})


@pytest.mark.parametrize('transcript', ['答案是两三', '答案是6!'])
def test_tcp_original_agent_speech(environment, monkeypatch, transcript):
    audit.test_original_agent_speech_cannot_gain_a_numeric_score_by_lossy_extraction(environment, monkeypatch, transcript)
    print({'persisted_db': str(environment[1].path), 'owned_worker': 0})


def test_tcp_legacy_lossy_receipt_after_restart(environment, monkeypatch):
    audit.test_legacy_lossy_agent_receipt_loses_current_authority_without_erasure(environment, monkeypatch)
    print({'persisted_db': str(environment[1].path), 'owned_worker': 0})


@pytest.mark.parametrize('previously_approved', [False, True])
def test_tcp_integer_representation_cannot_be_approved_or_opened_after_restart(environment, monkeypatch, previously_approved):
    settings, db = environment
    def generated(stage='parallel', index=0):
        value = audit.numeric_generated(stage, index)
        if stage == 'parallel':
            value['prompt'] = '将数字六写成三位编号，前面补零。'
            value['scoring_contract']['expected_answer']['exact_answers'] = ['006']
        return value
    monkeypatch.setattr(audit.closed, 'generated', generated)
    gateway = audit.closed.ClosedGateway()
    with TCPClient(audit.create_app(settings, model_gateway=gateway)) as client:
        with monkeypatch.context() as old:
            old.setattr(audit.spoken_numeric, 'task_supported', lambda *_: True)
            headers, rows = audit.generated_http(client, db)
            row = next(r for r in rows if r['stage'] == 'parallel')
            request = {'decision': 'approve', 'note': 'Historical synthetic representation review', 'client_action_id': 'old-representation30'}
            url = f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review"
            if previously_approved:
                saved = client.post(url, headers=headers, json=request)
                assert saved.status_code == 200, saved.text
                item = saved.json()['diagnostic_item_id']
    with TCPClient(audit.create_app(settings, model_gateway=gateway)) as client:
        if previously_approved:
            sid = audit.start(client)
            response = client.post(audit.endpoint(sid, 'prepare'), json={'track': 'competition_math',
                'preferred_item_id': item, 'client_action_id': 'no-numeric-open30'})
            # The requested historical item must not be offered as a qualified scalar task.
            if response.status_code == 200:
                assert (response.json().get('prompt') or {}).get('diagnostic_item_id') != item
            else:
                assert response.status_code in {409, 422}, response.text
        else:
            unlocked = client.post('/v1/parent/unlock', json={'pin': '2846'})
            assert unlocked.status_code == 200
            headers = {'X-HSC-Parent': unlocked.json()['parent_token']}
            response = client.post(url, headers=headers, json=request)
            assert response.status_code == 422 and 'NUMERIC_CONTRACT_INVALID' in response.text
        with db.connect() as c:
            current = c.execute('SELECT status,review_action_id FROM specialized_supply_candidate WHERE candidate_id=?', (row['candidate_id'],)).fetchone()
            assert current['status'] == ('promoted' if previously_approved else row['status'])
            assert bool(current['review_action_id']) == previously_approved
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert audit.closed.counting(db)['qualified_stage'] is None
    assert len(gateway.requests) == 1
    print({'persisted_db': str(db.path), 'owned_worker': 0, 'old_approval_preserved': previously_approved})


def test_tcp_mistake_unknown_is_durable_across_same_key_and_restart(flow, monkeypatch):
    import json
    from home_study_agent import learning_state
    from home_study_agent.config import Settings
    from home_study_agent.child_model import ChildModelService
    from test_mistake_gate_reliability import prepare, NODE
    db, learning, _, _ = flow
    item_id = 'audit30-tcp-original-books'
    rubric = {'local_rule': 'local_exact_v1', 'answer_format': audit.spoken_numeric.FORMAT,
              'numeric_kind': 'integer', 'exact_answers': ['23']}
    monkeypatch.setattr(learning_state, 'ITEMS', (*learning_state.ITEMS,
        (item_id, NODE, '二十本书再放入三本，共有多少本？请说出整数答案。', 'text', rubric, '二十加三等于二十三。', 2)))
    learning.seed_catalog()
    _, case = prepare(flow)
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False)
    gateway = audit.closed.ClosedGateway()
    request = {'child_id': audit.CHILD, 'stage': 'near_transfer', 'diagnostic_item_id': item_id,
               'answer': '两三', 'hint_level': 0, 'client_action_id': 'tcp-mistake30'}
    with TCPClient(audit.create_app(settings, model_gateway=gateway)) as client:
        response = client.post(f'/v1/mistakes/{case}/attempts', json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['attempt_result']['correctness'] is None
        assert not result['attempt_result']['independent'] and result['status'] != 'mastered'
        with db.connect() as c:
            row = dict(c.execute('SELECT * FROM mistake_attempt WHERE client_action_id=?', (request['client_action_id'],)).fetchone())
            assert row['submitted_answer'] == '两三' and row['correctness'] is None
    with TCPClient(audit.create_app(settings, model_gateway=gateway)) as client:
        replay = client.post(f'/v1/mistakes/{case}/attempts', json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()['status'] != 'mastered'
        with db.connect() as c:
            saved = [dict(r) for r in c.execute('SELECT * FROM mistake_attempt WHERE client_action_id=?', (request['client_action_id'],))]
            assert saved == [row]
        model = ChildModelService(db).snapshot(audit.CHILD)['payload']['learning']['reliable_feedback']
        assert not any(row['evidence_id'] in json.dumps(node) for node in model['nodes'])
        other = client.post(f'/v1/mistakes/{case}/attempts', json={**request, 'child_id': 'child-junior-3'})
        assert other.status_code in {404, 409, 422}, other.text
    assert len(gateway.requests) == 0
    print({'persisted_db': str(db.path), 'owned_worker': 0, 'attempt_id': row['attempt_id'], 'score': None})
