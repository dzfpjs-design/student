"""Authenticated FastAPI journeys against a fresh, real SQLite database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import socket
import subprocess
import sys
import time

import httpx

from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.companion_activity_outcome_http import create_companion_activity_outcome_router
from home_study_agent.database import Database
from home_study_agent.parent_auth import ParentAuth
from test_companion_activity_outcome import activity, legacy_activity


ROOT = '/v1/parent/children/child-game/companion-activity-outcomes'


def client_for(db, auth):
    app = FastAPI()
    app.include_router(create_companion_activity_outcome_router(CompanionActivityOutcomeService(db), auth))
    return TestClient(app)


def test_http_feedback_reopen_withdraw_old_key_and_child_isolation(activity):
    db, runtime, service, oid, session, _ = activity
    auth = ParentAuth(db)
    token = auth.setup('7391')
    client = client_for(db, auth)
    path = ROOT + '/' + oid
    body = dict(expected_revision=1, client_action_id='http-feedback', execution='completed',
                experience='comfortable', note='Observed slow shoulder stretches', evidence_basis='parent_observed')
    assert client.get(path).status_code == 401
    assert client.post(path + '/feedback', json=body).status_code == 401
    client.headers['X-HSC-Parent'] = token
    assert client.get(ROOT).json()['total'] == 1
    assert client.get(path.replace('child-game', 'other')).status_code == 404
    assert client.post(path.replace('child-game', 'other') + '/feedback', json=body).status_code == 404
    assert client.get(ROOT.replace('child-game','other')).json()['items'] == []
    assert client.get(ROOT + '?limit=101').status_code == 422
    assert client.post(path + '/feedback', json={**body,'actor':'parent'}).status_code == 422
    assert client.post(path + '/feedback', json={**body,'evidence_basis':'child_report'}).status_code == 422
    assert client.post(path + '/feedback', json={**body,'expected_revision':True}).status_code == 422
    runtime.end_companion_activity(session, 'turn_limit')
    response = client.post(path + '/feedback', json=body)
    assert response.status_code == 200, response.text[:400]
    receipt = response.json()
    assert response.headers['cache-control'] == 'no-store'
    assert receipt['event']['actor'] == 'parent'
    assert receipt['activity']['execution_status'] == 'observed_completed'
    assert not receipt['activity']['habit_evidence_eligible']
    assert client.post(path + '/feedback', json={**body,'client_action_id':'stale'}).status_code == 409
    reopened = Database(db.path)
    new_auth = ParentAuth(reopened)
    restarted = client_for(reopened, new_auth)
    restarted.headers['X-HSC-Parent'] = token
    assert restarted.get(path).status_code == 401
    restarted.headers['X-HSC-Parent'] = new_auth.unlock('7391')
    assert restarted.get(path).json() == service.get('child-game', oid)
    retract = restarted.post(path + '/retract', json=dict(expected_revision=2, client_action_id='retract-http',
                           target_revision=2, reason='Correction: only watched, did not perform'))
    assert retract.status_code == 200
    replay = restarted.post(path + '/feedback', json=body).json()
    assert replay['event'] == receipt['event']
    assert replay['activity']['revision'] == 3
    assert replay['activity']['execution_status'] == 'unknown'
    assert replay['local_only'] and not replay['cloud_transmission_allowed']
    auth._tokens[token] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.get(path).status_code == 401
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_http_register_real_source_concurrent_revision_and_explicit_resume(activity):
    db, runtime, s, oid, session, _ = activity
    auth = ParentAuth(db)
    client = client_for(db, auth)
    client.headers['X-HSC-Parent'] = auth.setup('7391')
    runtime.end_companion_activity(session, 'turn_limit')
    source_id = legacy_activity(db, session, 'item-game')
    body = dict(source_kind='companion', source_id=source_id, intent='Clear workspace',
                success_description='Put the loose pencils into the box', client_action_id='tidy-open')
    response = client.post(ROOT, json=body)
    assert response.status_code == 200, response.text[:400]
    new_id = response.json()['activity']['outcome_id']
    assert client.post(ROOT, json=body).json()['activity']['outcome_id'] == new_id
    assert client.post(ROOT.replace('child-game','other'), json=body).status_code == 404
    path = ROOT + '/' + new_id
    request = dict(expected_revision=1, client_action_id='resume-before-end', note='Ready')
    assert client.post(path + '/resume-request', json=request).status_code == 409
    runtime.end_companion_activity(session, 'turn_limit')
    def write(key):
        with client_for(db, auth) as concurrent:
            return concurrent.post(path + '/feedback', headers={'X-HSC-Parent':client.headers['X-HSC-Parent']},
                json=dict(expected_revision=1, client_action_id=key, execution='partial',
                          experience='neutral', note='Two pencils put away', evidence_basis='parent_observed')).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ['concurrent-a','concurrent-b'])) == [200,409]
    resume = client.post(path + '/resume-request', json={**request,'expected_revision':2})
    assert resume.status_code == 200
    assert resume.json()['activity']['resume']['status'] == 'requested'
    # The parent endpoint cannot invent an executor acknowledgement.
    assert client.post(path + '/resume-confirm', json={'plan_item_event_id':'invented'}).status_code == 404
    deferred = client.post(path + '/resume-defer', json=dict(expected_revision=3, client_action_id='defer', note='Rest first'))
    assert deferred.status_code == 200
    assert deferred.json()['activity']['resume']['status'] == 'deferred'


def test_real_loopback_http_process_restart_keeps_feedback_and_retraction(activity, tmp_path):
    db, runtime, _, oid, session, _ = activity
    runtime.end_companion_activity(session, 'turn_limit')
    ParentAuth(db).setup('7391')
    # A test-only host: real router/auth, no production app, cloud, or family DB.
    script = '''import sys
from pathlib import Path
from fastapi import FastAPI
import uvicorn
from home_study_agent.database import Database
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.companion_activity_outcome_http import create_companion_activity_outcome_router
db=Database(Path(sys.argv[1]))
auth=ParentAuth(db)
app=FastAPI()
app.include_router(create_companion_activity_outcome_router(CompanionActivityOutcomeService(db),auth))
@app.post('/_isolated_fixture/unlock')
def unlock():
    return {'token':auth.unlock('7391')}
uvicorn.run(app, fd=int(sys.argv[2]), log_level='error', access_log=False)
'''
    path = ROOT + '/' + oid
    body = dict(expected_revision=1, client_action_id='socket-feedback', execution='partial',
                experience='neutral', note='Two stretches observed', evidence_basis='parent_observed')
    process = None
    with socket.socket() as listener, (tmp_path / 'isolated-http.log').open('wb') as log:
        listener.bind(('127.0.0.1', 0))
        listener.listen(32)
        address = f'http://127.0.0.1:{listener.getsockname()[1]}'
        with httpx.Client(base_url=address, trust_env=False, timeout=1) as client:
            def start():
                child = subprocess.Popen([sys.executable, '-c', script, str(db.path), str(listener.fileno())],
                    pass_fds=(listener.fileno(),), stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if child.poll() is not None:
                            raise AssertionError('isolated HTTP process exited at startup')
                        try:
                            if client.get(path).status_code == 401:
                                return child
                        except httpx.TransportError:
                            pass
                        time.sleep(0.02)
                    raise AssertionError('isolated HTTP process did not become ready')
                except BaseException:
                    child.terminate()
                    child.wait(timeout=5)
                    raise
            try:
                process = start()
                old_token = client.post('/_isolated_fixture/unlock').json()['token']
                headers = {'X-HSC-Parent':old_token}
                receipt = client.post(path + '/feedback', json=body, headers=headers)
                assert receipt.status_code == 200
                process.terminate()
                process.wait(timeout=5)
                process = start()
                assert client.get(path, headers=headers).status_code == 401
                headers = {'X-HSC-Parent':client.post('/_isolated_fixture/unlock').json()['token']}
                assert client.get(path, headers=headers).json()['activity']['revision'] == 2
                result = client.post(path + '/retract', headers=headers, json=dict(expected_revision=2,
                    client_action_id='socket-retract', target_revision=2, reason='Correcting the observation'))
                assert result.status_code == 200
                replay = client.post(path + '/feedback', headers=headers, json=body).json()
                assert replay['event'] == receipt.json()['event']
                assert replay['activity']['revision'] == 3
                assert replay['activity']['execution_status'] == 'unknown'
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
