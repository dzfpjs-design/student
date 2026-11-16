from fastapi import APIRouter, HTTPException, Request

from .realtime_output_authority import RealtimeOutputAuthority, RealtimeOutputConflict


def create_router(database, settings, *, page_source_store=None):
    router = APIRouter()
    from .public_reply_projection import approved_routes
    authority = RealtimeOutputAuthority(database, settings.native_launch_attestation_key,
        page_source_store=page_source_store, approved_model_routes=approved_routes(settings))

    @router.get('/v1/sessions/{session_id}/voice-output-context/{turn_id}')
    def context(session_id: str, turn_id: str, request: Request):
        if not settings.session_token or request.headers.get('X-HSC-Session') != settings.session_token:
            raise HTTPException(401, detail={'code': 'LOCAL_SESSION_REQUIRED'})
        try:
            from .privacy_operation_receipt import generation
            from .realtime_output_authority import _hash
            with database.connect() as c:
                row = c.execute('SELECT child_id FROM agent_turn WHERE agent_turn_id=? AND session_id=?', (turn_id, session_id)).fetchone()
                if not row:
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_SOURCE_NOT_CURRENT')
                if authority._event(c, 'realtime-output-' + _hash(turn_id)):
                    return {'status': 'already_attempted', 'child_id': row[0]}
                authority._source(c, dict(agent_turn_id=turn_id, session_id=session_id, child_id=row[0]))
                from fastapi.responses import JSONResponse
                return JSONResponse({'status': 'eligible', 'child_id': row[0], 'storage_generation': generation(c)},
                    headers={'Cache-Control': 'no-store'})
        except RealtimeOutputConflict as error:
            raise HTTPException(409, detail={'code': str(error)}) from None

    @router.post('/v1/sessions/{session_id}/voice-output-authority')
    async def execute(session_id: str, request: Request):
        if not settings.session_token or request.headers.get('X-HSC-Session') != settings.session_token:
            raise HTTPException(401, detail={'code': 'LOCAL_SESSION_REQUIRED'})
        try:
            # No renderer-supplied text or permission fields are accepted.
            if int(request.headers.get('Content-Length', '0')) > 4096:
                raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 4096:
                    raise RealtimeOutputConflict('REALTIME_OUTPUT_PROOF_INVALID')
                raw.extend(chunk)
            import json
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('session_id') != session_id:
                raise RealtimeOutputConflict('REALTIME_OUTPUT_SCOPE_CHANGED')
            from starlette.concurrency import run_in_threadpool
            result = await run_in_threadpool(authority.execute, value)
        except RealtimeOutputConflict as error:
            raise HTTPException(409, detail={'code': str(error)}, headers={'Cache-Control': 'no-store'}) from None
        except (ValueError, TypeError):
            raise HTTPException(400, detail={'code': 'REALTIME_OUTPUT_PROOF_INVALID'}, headers={'Cache-Control': 'no-store'}) from None
        from fastapi.responses import JSONResponse
        return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    return router
