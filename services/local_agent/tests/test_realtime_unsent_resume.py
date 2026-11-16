"""Original saved speech resumes through the real Runtime, not browser text."""
import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.agent_request_ownership import claim
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER


WORDS = '帮我看看纸上这道题吧'
INPUT = 'resume-original-input'


def capture(db, sid):
    service = VoiceCaptureBindingService(db)
    service.begin_realtime(sid, INPUT, 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
    return service.complete_realtime(sid, INPUT, WORDS)


def root(sid):
    return f'/v1/sessions/{sid}/voice/realtime/actions/{INPUT}/resume'


def test_saved_unsent_original_survives_restart_and_is_interpreted_once(environment):
    settings, db = environment
    planner = Planner('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        voice = capture(db, sid)
        preview = client.get(root(sid), params={'child_id': CHILD})
        assert preview.status_code == 200, preview.text
        assert preview.json()['status'] == 'ready'
        assert preview.headers['cache-control'] == 'no-store'
        assert WORDS not in preview.text and not planner.requests
    with TestClient(create_app(settings, model_gateway=planner)) as restarted:
        payload = {'child_id': CHILD, 'voice_input_id': voice['voice_input_id']}
        resumed = restarted.post(root(sid), json=payload)
        assert resumed.status_code == 200, resumed.text
        turn = resumed.json()
        assert turn['transcript'] == WORDS
        assert turn['action']['voice_input_id'] == voice['voice_input_id']
        assert len(planner.requests) == 1
        repeated = restarted.post(root(sid), json=payload)
        assert repeated.status_code == 409, repeated.text
        assert len(planner.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
            assert c.execute('SELECT client_action_id FROM agent_turn').fetchone()[0] == 'realtime-' + voice['voice_input_id']


def test_unknown_claim_never_replays_and_cancelled_capture_cannot_resume(environment):
    settings, db = environment
    planner = Planner('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        voice = capture(db, sid)
        action = 'realtime-' + voice['voice_input_id']
        claim(db, sid, action, WORDS, voice_input_id=voice['voice_input_id'])
        preview = client.get(root(sid), params={'child_id': CHILD})
        assert preview.status_code == 200, preview.text
        assert preview.json()['status'] == 'blocked'
        response = client.post(root(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert response.status_code == 409, response.text
        assert not planner.requests
        assert client.post(f'/v1/sessions/{sid}/asr/actions/{INPUT}/cancel').status_code == 200
        response = client.post(root(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert response.status_code == 409, response.text
        assert not planner.requests


@pytest.mark.parametrize('change', ['child', 'voice', 'transcript', 'closed'])
def test_resume_cannot_rebind_original(environment, change):
    settings, db = environment
    planner = Planner('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        voice = capture(db, sid)
        payload = {'child_id': CHILD, 'voice_input_id': voice['voice_input_id']}
        if change == 'child': payload['child_id'] = OTHER
        if change == 'voice': payload['voice_input_id'] = 'different-source'
        if change == 'transcript': payload['transcript'] = '新口令绝不能替代原答案'
        if change == 'closed':
            with db.connect() as c:
                c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
                c.commit()
        response = client.post(root(sid), json=payload)
        assert response.status_code in (409, 422), response.text
        assert not planner.requests


def test_pre_model_failure_permit_resumes_original_after_restart(environment, monkeypatch):
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    from home_study_agent.agent_request_ownership import USED
    settings, db = environment
    planner = Planner('learning.capture_question', {}, WORDS)
    async def fail_locally(self, *args, **kwargs):
        raise RuntimeError('synthetic before-model failure')
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        sid = start(client)
        voice = capture(db, sid)
        with monkeypatch.context() as patch:
            patch.setattr(AgentOrchestrator, '_interpret_owned', fail_locally)
            with pytest.raises(RuntimeError, match='before-model'):
                client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': WORDS,
                    'voice_input_id': voice['voice_input_id'], 'client_action_id': 'realtime-' + voice['voice_input_id']})
        assert not planner.requests
    with TestClient(create_app(settings, model_gateway=planner)) as restarted:
        assert restarted.get(root(sid), params={'child_id': CHILD}).json()['status'] == 'ready'
        result = restarted.post(root(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (USED,)).fetchone()[0] == 1
        assert len(planner.requests) == 1


def test_concurrent_resume_claim_and_cancel_after_preview_are_rechecked(environment, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from home_study_agent.agent_request_ownership import AgentRequestOwnershipError
    from home_study_agent.realtime_unsent_resume import RealtimeUnsentResume
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        voice = capture(db, sid)
        def consume():
            try:
                return claim(db, sid, 'realtime-' + voice['voice_input_id'], WORDS,
                    voice_input_id=voice['voice_input_id'], allow_local_resume=True, require_unsent_resume=True)
            except AgentRequestOwnershipError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: consume(), range(2))) == [False, True]
    # New disposable original, then revoke between preview and actual claim.
    planner = Planner('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        service = VoiceCaptureBindingService(db)
        service.begin_realtime(sid, 'resume-race-input', 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
        later = service.complete_realtime(sid, 'resume-race-input', WORDS)
        original = RealtimeUnsentResume._snapshot
        def revoked(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            service.cancel(sid, 'resume-race-input')
            return result
        monkeypatch.setattr(RealtimeUnsentResume, '_snapshot', revoked)
        response = client.post(root(sid).replace(INPUT, 'resume-race-input'),
            json={'child_id': CHILD, 'voice_input_id': later['voice_input_id']})
        assert response.status_code == 409, response.text
        assert not planner.requests


def test_resume_endpoints_require_local_auth_and_never_serve_transcripts(environment):
    settings, db = environment
    settings = replace(settings, session_token='synthetic-session-secret')
    with TestClient(create_app(settings), headers={'X-HSC-Session': settings.session_token}) as client:
        sid = start(client)
        voice = capture(db, sid)
        for method in ('GET', 'POST'):
            kwargs = {'params': {'child_id': CHILD}} if method == 'GET' else {'json': {'child_id': CHILD, 'voice_input_id': voice['voice_input_id']}}
            response = client.request(method, root(sid), headers={'X-HSC-Session': 'incorrect'}, **kwargs)
            assert response.status_code == 401, response.text
            assert WORDS not in response.text
        preview = client.get(root(sid), params={'child_id': CHILD})
        assert preview.status_code == 200 and WORDS not in preview.text


def test_actual_model_interruption_is_not_replayed_via_resume_after_restart(environment):
    settings, db = environment
    class Interrupted(Planner):
        async def complete(self, request):
            self.requests.append(request)
            raise asyncio.CancelledError()
    interrupted = Interrupted('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=interrupted)) as client:
        sid = start(client)
        voice = capture(db, sid)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(client.app.state.agent_orchestrator.interpret(sid, WORDS,
                'realtime-' + voice['voice_input_id'], True, voice_input_id=voice['voice_input_id']))
        assert len(interrupted.requests) == 1
    successor = Planner('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=successor)) as restarted:
        assert restarted.get(root(sid), params={'child_id': CHILD}).json()['status'] == 'blocked'
        response = restarted.post(root(sid), json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
        assert response.status_code == 409, response.text
        assert not successor.requests
