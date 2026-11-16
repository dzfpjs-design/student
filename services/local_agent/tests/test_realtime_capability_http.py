import asyncio
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
from functools import wraps
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from home_study_agent.realtime_capability_http import (
    DOMAIN, FIELDS, NativeRealtimeRequestConflict, authorize_native_realtime_context,
    authorize_native_realtime_input, create_router, verify_native_realtime_request,
)

KEY = 'ab' * 32
SID = 'session-public'
BODY = {'version': 1, 'voice_input_id': 'voice-public', 'supplier_session_id': 'supplier-public',
        'input_item_id': 'input-public', 'response_id': 'response-public', 'call_id': 'call-public',
        'capability_name': 'learning.prepare_request', 'args': {'subject': '地理'}}


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def header(raw, operation='proposal', session=SID, *, issued=None, generation=1, host='host-connection-public'):
    value = {'version': 1, 'operation': operation, 'session_id': session,
             'voice_input_id': '' if operation in {'context', 'catalog', 'input'} else BODY['voice_input_id'],
             'connection_id': host, 'connection_generation': generation, 'body_sha256': hashlib.sha256(raw).hexdigest(),
             'issued_at': (issued or datetime.now(timezone.utc)).isoformat()}
    encoded = json.dumps([DOMAIN] + [value[k] for k in FIELDS], ensure_ascii=False, separators=(',', ':')).encode()
    value['signature'] = hmac.new(bytes.fromhex(KEY), encoded, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')


def test_exact_unicode_body_bytes_and_domain_are_bound():
    raw = json.dumps(BODY, ensure_ascii=False, separators=(',', ':')).encode()
    proof = verify_native_realtime_request(KEY, header(raw), SID, 'proposal', raw)
    assert proof['body_sha256'] == hashlib.sha256(raw).hexdigest()
    assert proof['connection_id'] == 'host-connection-public'
    for key, session, operation, altered in [
        ('cd' * 32, SID, 'proposal', raw), (KEY, 'session-other', 'proposal', raw),
        (KEY, SID, 'dialogue', raw), (KEY, SID, 'proposal', raw + b' '),
    ]:
        with pytest.raises(NativeRealtimeRequestConflict):
            verify_native_realtime_request(key, header(raw), session, operation, altered)


@pytest.mark.parametrize('age', [91, -6])
def test_expired_or_future_host_proof_is_not_authority(age):
    raw = json.dumps(BODY).encode(); now = datetime.now(timezone.utc)
    with pytest.raises(NativeRealtimeRequestConflict):
        verify_native_realtime_request(KEY, header(raw, issued=now - timedelta(seconds=age)), SID, 'proposal', raw, now=now)


class Service:
    def __init__(self): self.calls = []; self.body = None
    def catalog(self, sid):
        assert sid == SID
        return {'version': 1, 'capabilities': []}
    def admit(self, sid, body):
        voice = {'voice_input_id': body['voice_input_id'], 'session_id': sid, 'child_id': 'child-public'}
        assert authorize_native_realtime_input(None, sid, 'child-public', body, voice)
        changed = {**body, 'response_id': 'another-response'}
        assert not authorize_native_realtime_input(None, sid, 'child-public', changed, voice)
        self.calls.append('admit'); self.body = body
        return {'action_id': 'action-public'}
    async def execute(self, sid, action):
        voice = {'voice_input_id': BODY['voice_input_id'], 'session_id': sid, 'child_id': 'child-public'}
        # Executor sees persisted identity only, not mutable proposal arguments.
        identity = {k: self.body[k] for k in ('voice_input_id', 'supplier_session_id', 'input_item_id', 'response_id', 'call_id')}
        assert authorize_native_realtime_input(None, sid, 'child-public', identity, voice)
        self.calls.append('execute')
        return {'version': 1, 'execution': {'status': 'completed'}, 'public_result': {'request_saved': True}}
    def receipt_for_identity(self, sid, body):
        return {'version': 1, 'execution': {'status': 'unknown'}, 'public_result': {}}
    def save_dialogue(self, sid, body):
        return {'version': 1, 'status': 'saved', 'verified': False}


def application(service):
    app = FastAPI()
    def context(sid):
        assert authorize_native_realtime_context(None, sid, 'child-public')
        return {'version': 1, 'revision': 'public-revision', 'recent_dialogue': [], 'runtime_facts': {}, 'pending_calls': []}
    app.include_router(create_router(SimpleNamespace(session_token='local-public-token', native_launch_attestation_key=KEY),
                                     service, context_provider=context))
    return app


@async_test
async def test_current_request_proof_survives_thread_and_execution_but_does_not_leak_to_the_next_request():
    service = Service(); app = application(service); raw = json.dumps(BODY, ensure_ascii=False).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1234)), base_url='http://local') as c:
        response = await c.post(f'/v1/sessions/{SID}/voice/realtime/capability-proposals', content=raw,
            headers={'X-HSC-Session': 'local-public-token', 'X-HSC-Native-Voice-Proof': header(raw)})
        assert response.status_code == 200
        assert response.json()['public_result'] == {'request_saved': True}
    assert service.calls == ['admit', 'execute']
    assert not authorize_native_realtime_input(None, SID, 'child-public', BODY,
        {'voice_input_id': BODY['voice_input_id'], 'session_id': SID, 'child_id': 'child-public'})
    assert not authorize_native_realtime_context(None, SID, 'child-public')


@async_test
async def test_context_read_has_a_separate_purpose_and_never_executes_a_tool():
    service = Service(); app = application(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1234)), base_url='http://local') as c:
        response = await c.get(f'/v1/sessions/{SID}/voice/realtime/native-context',
            headers={'X-HSC-Session': 'local-public-token', 'X-HSC-Native-Voice-Proof': header(b'', 'context')})
        assert response.status_code == 200
        wrong = await c.get(f'/v1/sessions/{SID}/voice/realtime/native-context',
            headers={'X-HSC-Session': 'local-public-token', 'X-HSC-Native-Voice-Proof': header(b'', 'catalog')})
        assert wrong.status_code == 409
    assert service.calls == []


@pytest.mark.parametrize('tamper', ['body', 'session_token', 'remote_client', 'oversize'])
@async_test
async def test_invalid_transport_never_reaches_the_executor(tamper):
    service = Service(); app = application(service); raw = json.dumps(BODY).encode()
    signed = header(raw)
    if tamper == 'body': raw += b' '
    if tamper == 'oversize': raw = b'x' * 128001
    client = ('192.0.2.3', 1234) if tamper == 'remote_client' else ('127.0.0.1', 1234)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url='http://local') as c:
        response = await c.post(f'/v1/sessions/{SID}/voice/realtime/capability-proposals', content=raw,
            headers={'X-HSC-Session': 'wrong' if tamper == 'session_token' else 'local-public-token', 'X-HSC-Native-Voice-Proof': signed})
        assert response.status_code in (401, 409)
    assert service.calls == []


@async_test
async def test_new_host_context_fences_older_signed_requests_and_inflight_authority():
    service = Service(); app = application(service); raw = json.dumps(BODY).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1234)), base_url='http://local') as c:
        fresh = await c.get(f'/v1/sessions/{SID}/voice/realtime/native-context', headers={
            'X-HSC-Session': 'local-public-token', 'X-HSC-Native-Voice-Proof': header(b'', 'context', generation=2, host='new-host')})
        assert fresh.status_code == 200
        old = await c.post(f'/v1/sessions/{SID}/voice/realtime/capability-proposals', content=raw, headers={
            'X-HSC-Session': 'local-public-token', 'X-HSC-Native-Voice-Proof': header(raw)})
        assert old.status_code == 409
    assert service.calls == []
