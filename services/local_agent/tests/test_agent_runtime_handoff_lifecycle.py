"""Real Runtime, HTTP and managed restore; synthetic interrupted model only."""
import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.agent_request_ownership import ACTION
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from privacy_restore_operation import restore_operation
from test_native_privacy_permit_http import certificate, signed_header
from test_controlled_launch_intent_http import KEY as SYNTHETIC_NATIVE_KEY
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment, start, CHILD


def test_unsent_terminal_failure_can_resume_once_after_restart(environment, monkeypatch):
    from home_study_agent.agent_request_ownership import RETRY, USED
    from home_study_agent.agent_orchestrator import AgentOrchestrator
    settings, db = environment
    gateway = Planner('learning.capture_question', {}, WORDS)
    original = AgentOrchestrator._interpret_owned
    async def unsent(self, *args, **kwargs):
        raise RuntimeError('synthetic-local-failure-before-model')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        request = body(db, sid)
        with monkeypatch.context() as patch:
            patch.setattr(AgentOrchestrator, '_interpret_owned', unsent)
            with pytest.raises(RuntimeError, match='synthetic-local-failure'):
                client.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (RETRY,)).fetchone()[0] == 1
    with TestClient(create_app(settings, model_gateway=gateway)) as restarted:
        repaired = restarted.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        assert repaired.status_code == 200, repaired.text
        repeated = restarted.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['agent_turn_id'] == repaired.json()['agent_turn_id']
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (USED,)).fetchone()[0] == 1


def test_local_only_recovery_does_not_allocate_a_new_model_call(environment):
    from home_study_agent.agent_request_ownership import execution_scope, LocalHandoffDispatchDenied
    from home_study_agent.repository import StudyRepository
    settings, db = environment
    db.initialize()
    repository = StudyRepository(db)
    with execution_scope(False):
        with pytest.raises(LocalHandoffDispatchDenied):
            repository.start_model_call('session', CHILD, 'test', 'synthetic', 'synthetic', 3,
                'test', {}, 'low', 'synthetic', 'test', None)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


WORDS = '帮我看看纸上这道题吧'


class Interrupted(Planner):
    async def complete(self, request):
        self.requests.append(request)
        raise asyncio.CancelledError()


def body(db, sid):
    service = VoiceCaptureBindingService(db)
    service.begin_realtime(sid, 'source', 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
    voice = service.complete_realtime(sid, 'source', WORDS)
    return {'transcript': WORDS, 'voice_input_id': voice['voice_input_id'], 'client_action_id': 'original-action'}


def test_interrupted_model_handoff_is_not_resent_after_restart(environment):
    settings, db = environment
    gateway = Interrupted('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        request = body(db, sid)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(client.app.state.agent_orchestrator.interpret(sid, WORDS,
                request['client_action_id'], True, voice_input_id=request['voice_input_id']))
        assert len(gateway.requests) == 1
        original = client.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        assert original.status_code == 409, original.text
        assert original.json()['code'] == 'AGENT_HANDOFF_UNCONFIRMED'
    successor = Planner('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=successor)) as restarted:
        original = restarted.post(f'/v1/sessions/{sid}/agent-turn', json=request)
        assert original.status_code == 409, original.text
        assert original.json()['code'] == 'AGENT_HANDOFF_UNCONFIRMED'
        changed_engine = restarted.post(f'/v1/sessions/{sid}/agent-turn',
            json={**request, 'client_action_id': 'new-engine-action'})
        assert changed_engine.status_code == 409, changed_engine.text
        assert changed_engine.json()['code'] == 'AGENT_HANDOFF_SOURCE_CONFLICT'
    assert not successor.requests
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (ACTION,)).fetchone()[0] == 2


def test_restore_from_before_handoff_cannot_remove_consumed_source_fences(environment):
    settings, db = environment
    # Only this legitimate synthetic-native restore case has issuer authority.
    settings = replace(settings, native_launch_attestation_key=SYNTHETIC_NATIVE_KEY)
    gateway = Interrupted('learning.capture_question', {}, WORDS)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        setup = client.post('/v1/parent/setup', json={'pin': '5837'})
        assert setup.status_code == 200, setup.text
        client.headers['X-HSC-Parent'] = setup.json()['parent_token']
        sid = start(client)
        request = body(db, sid)
        backup = client.post('/v1/parent/backups')
        assert backup.status_code == 200, backup.text
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(client.app.state.agent_orchestrator.interpret(sid, WORDS,
                request['client_action_id'], True, voice_input_id=request['voice_input_id']))
        with db.connect() as c:
            fences = [tuple(row) for row in c.execute('SELECT * FROM audit_event WHERE action=? ORDER BY audit_id', (ACTION,))]
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
            c.commit()
        path = '/v1/parent/backups/restore'
        restore_body = {
            'backup_id': backup.json()['backup_id'], 'confirmation': '确认恢复',
            'operation': restore_operation(client)}
        # Full original body, current generation and one nonce; real verifier/HTTP.
        proof = certificate(path, restore_body, restore_body['operation'],
                            kind='restore', child_id=None, scope=None)
        restored = client.post(path, json=restore_body, headers=signed_header(proof))
        assert restored.status_code == 200, restored.text
        permit = client.post('/v1/parent/native-privacy-permits/read', json=proof)
        assert permit.status_code == 200 and permit.json()['status'] == 'finished', permit.text
        assert permit.json()['nonce'] == proof['nonce']
        replay = client.post(path, json=restore_body, headers=signed_header(proof))
        assert replay.status_code == 409 and 'NATIVE_PRIVACY_GENERATION_CHANGED' in replay.text, replay.text
        with db.connect() as c:
            assert [tuple(row) for row in c.execute('SELECT * FROM audit_event WHERE action=? ORDER BY audit_id', (ACTION,))] == fences
            assert all(WORDS not in row[4] for row in fences)
            assert c.execute('SELECT COUNT(*) FROM agent_turn').fetchone()[0] == 0
        retry = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': WORDS, 'client_action_id': request['client_action_id']})
        assert retry.status_code == 409, retry.text
        assert retry.json()['code'] == 'AGENT_HANDOFF_SOURCE_CONFLICT'
        assert len(gateway.requests) == 1
