"""Main-owned native cloud requests. No key or private provider body is returned."""
import asyncio
import base64
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

DOMAIN = 'native_realtime_capability_request_v1'
FIELDS = ('version', 'operation', 'session_id', 'voice_input_id', 'connection_id', 'connection_generation', 'body_sha256', 'issued_at')
OPERATIONS = {'catalog', 'context', 'input', 'proposal', 'receipt', 'dialogue', 'client'}
_request_proof = ContextVar('native_cloud_request_proof', default=None)


class NativeRealtimeRequestConflict(ValueError):
    pass


def _fail():
    raise NativeRealtimeRequestConflict('NATIVE_REALTIME_REQUEST_UNCONFIRMED')


def verify_native_realtime_request(key, header, session_id, operation, raw, *, now=None):
    if (type(key) is not str or not re.fullmatch('[a-f0-9]{64}', key)
            or type(header) is not str or len(header) > 8192 or not re.fullmatch('[A-Za-z0-9_-]+', header)):
        _fail()
    try:
        value = json.loads(base64.urlsafe_b64decode(header + '=' * (-len(header) % 4)))
        if type(value) is not dict or set(value) != set(FIELDS) | {'signature'}:
            _fail()
        if type(value['version']) is not int or value['version'] != 1 or value['operation'] not in OPERATIONS:
            _fail()
        if value['operation'] != operation or value['session_id'] != session_id:
            _fail()
        for name in ('session_id', 'connection_id'):
            if type(value[name]) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,160}', value[name]):
                _fail()
        if type(value['connection_generation']) is not int or not 1 <= value['connection_generation'] <= 9007199254740991:
            _fail()
        voice = value['voice_input_id']
        if operation in {'catalog', 'context', 'input'}:
            if voice != '' or (operation != 'input' and raw != b''):
                _fail()
        elif type(voice) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,160}', voice):
            _fail()
        if value['body_sha256'] != hashlib.sha256(raw).hexdigest():
            _fail()
        issued = datetime.fromisoformat(value['issued_at'].replace('Z', '+00:00'))
        if issued.tzinfo is None or not -5 <= ((now or datetime.now(timezone.utc)) - issued).total_seconds() <= 90:
            _fail()
        encoded = json.dumps([DOMAIN] + [value[k] for k in FIELDS], ensure_ascii=False, separators=(',', ':')).encode()
        expected = hmac.new(bytes.fromhex(key), encoded, hashlib.sha256).hexdigest()
        if type(value['signature']) is not str or not hmac.compare_digest(value['signature'], expected):
            _fail()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        _fail()
    return {k: value[k] for k in FIELDS}


def _current(proof):
    return bool(proof and proof.get('host_current') and proof['host_current']())


def authorize_native_realtime_input(connection, session_id, child_id, envelope, voice):
    """A request-local Main proof supplements the adapter's own SQLite scope checks."""
    proof = _request_proof.get()
    return bool((_current(proof) or bool(proof and proof.get('retirement_completion'))) and proof['operation'] in {'proposal', 'receipt', 'dialogue', 'client'}
                and proof['session_id'] == session_id
                and proof['voice_input_id'] == envelope.get('voice_input_id') == voice.get('voice_input_id')
                and all(envelope.get(k) == v for k, v in proof['body_identity'].items())
                and voice.get('session_id') == session_id and voice.get('child_id') == child_id)


def authorize_native_realtime_context(connection, session_id, child_id):
    proof = _request_proof.get()
    return bool(_current(proof) and proof['operation'] == 'context' and proof['session_id'] == session_id)


def authorize_native_realtime_admission(connection, session_id, child_id):
    proof = _request_proof.get()
    return bool(_current(proof) and proof['operation'] == 'input' and proof['session_id'] == session_id)


def authorize_native_realtime_receipt(connection, session_id, child_id, envelope, voice):
    """Read-only provenance never grants execution of the packet being read."""
    proof = _request_proof.get()
    if (not (_current(proof) or bool(proof and proof.get('retirement_completion'))) or proof['session_id'] != session_id or voice.get('session_id') != session_id
            or voice.get('child_id') != child_id or envelope.get('voice_input_id') != voice.get('voice_input_id')):
        return False
    if proof['operation'] == 'context':
        return True
    if proof['operation'] == 'proposal':
        # admit has already established semantic replay of this same voice.
        return proof['voice_input_id'] == voice.get('voice_input_id')
    return proof['operation'] in {'receipt','client'} and authorize_native_realtime_input(
        connection, session_id, child_id, envelope, voice)


def native_client_host_owner():
    proof=_request_proof.get()
    if not proof or proof.get('operation')!='client' or not _current(proof):return None
    return {key:proof[key] for key in ('connection_id','connection_generation','session_id')}


def native_admission_host_owner():
    proof = _request_proof.get()
    if not proof or proof.get('operation') != 'input' or not _current(proof):
        return None
    return {key: proof[key] for key in ('connection_id', 'connection_generation', 'session_id')}


def create_router(settings, service, *, context_provider, input_recorder=None, input_followup=None):
    router = APIRouter()
    # Only Main signs generations. A newly negotiated owner fences late requests
    # at each SQLite authorization, including work already queued in a thread.
    latest_host = (0, '', '')
    from .realtime_native_client import register_claim_fence
    if hasattr(service,'database'):
        register_claim_fence(service.database,lambda: {'connection_generation':latest_host[0],'connection_id':latest_host[1],'session_id':latest_host[2]})

    async def run(session_id, request, operation):
        nonlocal latest_host
        if (not settings.session_token or request.headers.get('X-HSC-Session') != settings.session_token
                or request.client is None or request.client.host not in ('127.0.0.1', '::1')):
            raise HTTPException(401, detail={'code': 'LOCAL_SESSION_REQUIRED'})
        try:
            async def read_body():
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 128000:
                        _fail()
                return bytes(raw)
            raw = await asyncio.wait_for(read_body(), 2)
            proof = verify_native_realtime_request(settings.native_launch_attestation_key,
                request.headers.get('X-HSC-Native-Voice-Proof'), session_id, operation, raw)
            body = {} if operation in {'catalog', 'context'} else json.loads(raw)
            if type(body) is not dict or (operation not in {'catalog', 'context', 'input'}
                    and body.get('voice_input_id') != proof['voice_input_id']):
                _fail()
            retired=body.get('completion_after_retirement') is True
            if 'completion_after_retirement' in body and not (operation=='client' and body.get('phase')=='complete' and retired):_fail()
            # Only Main's verified completion-only lease can sign this marker.
            # It never advances or borrows latest execution authority.
            proof['retirement_completion']=retired
            host = (proof['connection_generation'], proof['connection_id'], session_id)
            if not retired:
                if host[0] < latest_host[0] or (host[0] == latest_host[0] and host != latest_host):_fail()
                latest_host = host
            proof['host_current'] = lambda: latest_host == host
            proof['body_identity'] = {k: body[k] for k in (
                'voice_input_id', 'supplier_session_id', 'input_item_id', 'response_id', 'call_id') if k in body}
        except (NativeRealtimeRequestConflict, ValueError, TypeError, asyncio.TimeoutError):
            raise HTTPException(409, detail={'code': 'NATIVE_REALTIME_REQUEST_UNCONFIRMED'}) from None
        token = _request_proof.set(proof)
        admitted = None
        try:
            if operation == 'catalog':
                result = await run_in_threadpool(service.catalog, session_id)
            elif operation == 'context':
                result = await run_in_threadpool(context_provider, session_id)
            elif operation == 'input':
                if input_recorder is None:
                    _fail()
                result = await run_in_threadpool(input_recorder, session_id, body)
                if input_followup is not None:
                    result = await input_followup(session_id, result)
            elif operation == 'receipt':
                result = await run_in_threadpool(service.receipt_for_identity, session_id, body)
            elif operation == 'client':
                result = await service.client(session_id, body)
            elif operation == 'dialogue':
                result = await run_in_threadpool(service.save_dialogue, session_id, body)
            else:
                admitted = await run_in_threadpool(service.admit, session_id, body)
                if (admitted.get('execution', {}).get('status', 'pending') != 'pending'
                        or admitted.get('proposal_status')=='replayed' or 'client_dispatch' in admitted):
                    result = admitted
                else:
                    result = await service.execute(session_id, admitted['action_id'])
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except ValueError as error:
            code = str(error)
            if not re.fullmatch(r'(?:REALTIME_[A-Z0-9_]{1,80}|NATIVE_REALTIME_REQUEST_UNCONFIRMED)', code):
                code = 'NATIVE_CAPABILITY_REJECTED'
            if operation in {'proposal', 'receipt'} and admitted is None:
                try:
                    # A read distinguishes a definitely absent call from a lost
                    # execution response. It never retries an effect.
                    await run_in_threadpool(service.receipt_for_identity, session_id, body)
                except ValueError as missing:
                    if str(missing) == 'REALTIME_CAPABILITY_RECEIPT_NOT_FOUND':
                        public = {'status': 'not_executed', 'reason_code': code}
                        if operation == 'proposal':
                            try:
                                catalog = await run_in_threadpool(service.catalog, session_id)
                                spec = next((s for s in catalog['capabilities']
                                    if s['name'] == body.get('capability_name')), None)
                                if spec:
                                    public['arguments_schema'] = spec['arguments_schema']
                            except (ValueError, KeyError, TypeError):
                                pass
                        return JSONResponse({'version': 1, 'proposal_status': 'rejected', 'not_admitted': True,
                            'execution': {'status': 'failed', 'location': 'server'}, 'public_result': public},
                            headers={'Cache-Control': 'no-store'})
            raise HTTPException(409, detail={'code': code}, headers={'Cache-Control': 'no-store'}) from None
        finally:
            _request_proof.reset(token)

    @router.get('/v1/sessions/{session_id}/voice/realtime/capabilities')
    async def catalog(session_id: str, request: Request):
        return await run(session_id, request, 'catalog')

    @router.get('/v1/sessions/{session_id}/voice/realtime/native-context')
    async def context(session_id: str, request: Request):
        return await run(session_id, request, 'context')

    @router.post('/v1/sessions/{session_id}/voice/realtime/capability-proposals')
    async def proposal(session_id: str, request: Request):
        return await run(session_id, request, 'proposal')

    @router.post('/v1/sessions/{session_id}/voice/realtime/native-input')
    async def input_admission(session_id: str, request: Request):
        return await run(session_id, request, 'input')

    @router.post('/v1/sessions/{session_id}/voice/realtime/capability-receipt')
    async def receipt(session_id: str, request: Request):
        return await run(session_id, request, 'receipt')

    @router.post('/v1/sessions/{session_id}/voice/realtime/native-dialogue')
    async def dialogue(session_id: str, request: Request):
        return await run(session_id, request, 'dialogue')

    @router.post('/v1/sessions/{session_id}/voice/realtime/native-client')
    async def client_dispatch(session_id: str, request: Request):
        return await run(session_id, request, 'client')

    return router
