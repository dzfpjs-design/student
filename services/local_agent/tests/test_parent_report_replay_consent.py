"""Real reflection HTTP and durable report recovery, with no external model calls."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_report_delivery import _cloud_wording, _pending_report_job


SESSION = 'pending-report-session'
URL = '/v1/sessions/' + SESSION + '/reflection/complete'
PAYLOAD = {'learned_text': '会用列式', 'difficult_text': '', 'feeling': '正常',
           'next_action': '明天复习第一步', 'skipped': False,
           'client_action_id': 'http-original-reflection'}


@pytest.fixture
def report_case(tmp_path):
    db = Database(tmp_path / 'report.db')
    db.initialize()
    _pending_report_job(db)
    # Reuse its isolated family/session, but leave completion to the HTTP route.
    with db.connect() as c:
        c.execute('DELETE FROM report_job WHERE session_id=?', (SESSION,))
        c.execute('DELETE FROM daily_reflection WHERE session_id=?', (SESSION,))
        c.execute("UPDATE daily_session SET status='active',state='reflection',ended_at=NULL WHERE session_id=?", (SESSION,))
        c.commit()
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        data_encryption_key=b'R' * 32, model_provider='deepseek', model_api_key='fixture-only',
        model_base_url='https://report.invalid')
    return db, privacy, settings


def app_with_recorded_transport(settings, posts, first_post=None, release_post=None):
    async def transport(request):
        assert request.url.host == 'report.invalid'
        assert request.method == 'POST'
        posts.append(json.loads(request.content))
        if first_post is not None and len(posts) == 1:
            first_post.set()
            assert await asyncio.to_thread(release_post.wait, 30), 'Test did not release the first POST.'
        return httpx.Response(200, json={'choices': [{'message': {
            'content': json.dumps(_cloud_wording(), ensure_ascii=False)}}]})
    return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))


def persisted(db):
    with db.connect() as c:
        reports = [dict(r) for r in c.execute('SELECT * FROM daily_report WHERE session_id=?', (SESSION,))]
        snapshots = [dict(r) for r in c.execute("SELECT * FROM parent_report_snapshot WHERE source_kind='daily_report' AND child_id='child-primary-4'")]
        job = c.execute("SELECT status,last_error FROM report_job WHERE session_id=? AND report_type='daily'", (SESSION,)).fetchone()
        return {'reports': reports, 'snapshots': snapshots, 'job': dict(job) if job else None,
            'reflections': c.execute('SELECT count(*) FROM daily_reflection WHERE session_id=?', (SESSION,)).fetchone()[0],
            'other_reports': c.execute("SELECT count(*) FROM daily_report WHERE child_id='child-junior-3'").fetchone()[0]}


def assert_single_durable_report(state):
    assert len(state['reports']) == len(state['snapshots']) == 1
    assert state['snapshots'][0]['report_id'] == state['reports'][0]['report_id']
    assert state['reflections'] == 1 and state['other_reports'] == 0
    assert state['job']['status'] == 'completed'


def test_missing_parent_consent_completes_http_with_local_report_and_no_post(report_case):
    db, _, settings = report_case
    posts = []
    with TestClient(app_with_recorded_transport(settings, posts)) as client:
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM consent_record WHERE scope='cloud_model_text_allowed'").fetchone()[0] == 0
        response = client.post(URL, json=PAYLOAD)
        assert response.status_code == 200, response.text
        state = persisted(db)
    assert_single_durable_report(state)
    assert (len(posts), state['reports'][0]['source'].startswith('local_')) == (0, True)


@pytest.mark.parametrize('restart_after_lost_receipt', [False, True])
def test_completed_http_original_key_replay_does_not_make_second_post(report_case, restart_after_lost_receipt):
    db, privacy, settings = report_case
    privacy.update_preferences({'cloud_model_text_allowed': True})
    posts = []
    with TestClient(app_with_recorded_transport(settings, posts)) as client:
        first = client.post(URL, json=PAYLOAD)
        assert first.status_code == 200, first.text
        before = persisted(db)
        assert_single_durable_report(before)
        assert len(posts) == 1
        if not restart_after_lost_receipt:
            replay = client.post(URL, json=PAYLOAD)
            assert replay.status_code == 200, replay.text
    if restart_after_lost_receipt:
        # Discard the first response. The caller knows only its original payload/key.
        with TestClient(app_with_recorded_transport(settings, posts)) as client:
            replay = client.post(URL, json=PAYLOAD)
            assert replay.status_code == 200, replay.text
    after = persisted(db)
    assert_single_durable_report(after)
    assert after['reports'] == before['reports']
    assert after['snapshots'] == before['snapshots']
    assert len(posts) == 1, 'Replaying a persisted completion must not buy another report.'


def test_daily_saved_snapshot_tail_failure_recovers_without_regeneration(report_case, monkeypatch):
    db, privacy, settings = report_case
    privacy.update_preferences({'cloud_model_text_allowed': True})
    posts, failures = [], []
    original = ParentReportService.create_from_daily

    def fail_first_snapshot(self, report):
        if not failures:
            failures.append(report['report_id'])
            raise RuntimeError('isolated_snapshot_tail_failure')
        return original(self, report)

    monkeypatch.setattr(ParentReportService, 'create_from_daily', fail_first_snapshot)
    with TestClient(app_with_recorded_transport(settings, posts)) as client:
        first = client.post(URL, json=PAYLOAD)
        assert first.status_code == 200, first.text
        interrupted = persisted(db)
        assert len(interrupted['reports']) == 1 and interrupted['snapshots'] == []
        assert interrupted['job'] == {'status': 'pending', 'last_error': 'RuntimeError'}
        assert len(posts) == 1 and failures == [interrupted['reports'][0]['report_id']]
        replay = client.post(URL, json=PAYLOAD)
        assert replay.status_code == 200, replay.text
        recovered = persisted(db)
        assert_single_durable_report(recovered)
        assert recovered['reports'] == interrupted['reports']
    with TestClient(app_with_recorded_transport(settings, posts)) as client:
        replay = client.post(URL, json=PAYLOAD)
        assert replay.status_code == 200, replay.text
    restarted = persisted(db)
    assert_single_durable_report(restarted)
    assert restarted['reports'] == recovered['reports']
    assert restarted['snapshots'] == recovered['snapshots']
    assert len(posts) == 1, 'Recover the missing snapshot, not another cloud generation.'


def test_same_app_concurrent_original_completion_uses_one_post(report_case):
    db, privacy, settings = report_case
    privacy.update_preferences({'cloud_model_text_allowed': True})
    posts, arrivals = [], []
    first_post, release_post, second_arrival = threading.Event(), threading.Event(), threading.Event()
    application = app_with_recorded_transport(settings, posts, first_post, release_post)

    @application.middleware('http')
    async def record_arrival(request, call_next):
        if request.url.path == URL and request.method == 'POST':
            arrivals.append(request.url.path)
            if len(arrivals) == 2:
                second_arrival.set()
        return await call_next(request)

    with TestClient(application) as client, ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(client.post, URL, json=PAYLOAD)
        try:
            assert first_post.wait(30), 'First request never reached the isolated model transport.'
            second = workers.submit(client.post, URL, json=PAYLOAD)
            assert second_arrival.wait(30), 'Second HTTP request never entered this app.'
        finally:
            release_post.set()
        for future in (first, second):
            response = future.result(timeout=60)
            assert response.status_code == 200, response.text
    assert len(arrivals) == 2
    assert_single_durable_report(persisted(db))
    assert len(posts) == 1, 'Same-process concurrent completion must share the persisted report.'
