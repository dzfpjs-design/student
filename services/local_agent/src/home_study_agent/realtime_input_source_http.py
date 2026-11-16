import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from .realtime_input_source import RealtimeInputSource, RealtimeInputSourceConflict


def create_router(database, settings, *, page_source_store=None):
    router = APIRouter()
    source = RealtimeInputSource(database, settings.native_launch_attestation_key, page_source_store=page_source_store)

    @router.post('/v1/sessions/{session_id}/voice-input-source')
    async def read(session_id: str, request: Request):
        if not settings.session_token or request.headers.get('X-HSC-Session') != settings.session_token:
            raise HTTPException(401, detail={'code': 'LOCAL_SESSION_REQUIRED'})
        try:
            if int(request.headers.get('Content-Length', '0')) > 4096:
                raise RealtimeInputSourceConflict('REALTIME_INPUT_PROOF_INVALID')
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 4096:
                    raise RealtimeInputSourceConflict('REALTIME_INPUT_PROOF_INVALID')
                raw.extend(chunk)
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('session_id') != session_id:
                raise RealtimeInputSourceConflict('REALTIME_INPUT_SCOPE_CHANGED')
            result = await run_in_threadpool(source.read, value)
        except RealtimeInputSourceConflict as error:
            raise HTTPException(409, detail={'code': str(error)}, headers={'Cache-Control': 'no-store'}) from None
        except (ValueError, TypeError):
            raise HTTPException(400, detail={'code': 'REALTIME_INPUT_PROOF_INVALID'}, headers={'Cache-Control': 'no-store'}) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    return router
