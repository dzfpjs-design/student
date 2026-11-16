"""Read-only route policy from the original process MoneyOwner, never a grant."""
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


def attach_money_voice_policy(application, owner, settings):
    # Same exact outer private path is also preserved by recovery dispatch.
    # POST keeps its original signed command contract; GET has no body/effect.
    routes = application.router.routes
    if any(getattr(route, 'path', None) == '/v1/private/money-egress'
           and 'GET' in (getattr(route, 'methods', None) or ()) for route in routes):
        raise RuntimeError('VOICE_RUNTIME_POLICY_DUPLICATE_ROUTE')
    @application.get('/v1/private/money-egress', include_in_schema=False)
    async def read_voice_policy(request: Request):
        if (not settings.session_token
                or request.headers.get('X-HSC-Session') != settings.session_token
                or request.client is None or request.client.host not in ('127.0.0.1', '::1')):
            raise HTTPException(401, detail={'code': 'VOICE_RUNTIME_POLICY_UNAVAILABLE'},
                                headers={'Cache-Control': 'no-store'})

        def inspect():
            from .money_egress import MoneyOwner, current_owner
            if type(owner) is not MoneyOwner or application.state.money_owner is not owner or current_owner() is not owner:
                return 'blocked'
            first = owner.check()
            if type(first) is not bool:
                return 'blocked'
            audio_allowed = False
            if first is False:
                try:
                    audio_allowed = (application.state.media_ai._cloud_audio_allowed() is True
                        and application.state.privacy_lifecycle.preferences().get('cloud_model_text_allowed') is True)
                except Exception:
                    pass
            if current_owner() is not owner or application.state.money_owner is not owner or owner.check() is not first:
                return 'blocked'
            return 'bounded_segmented' if first or not audio_allowed else 'configured'

        try:
            capture = await run_in_threadpool(inspect)
        except Exception:
            capture = 'blocked'
        return JSONResponse({'version': 1, 'capture': capture}, headers={'Cache-Control': 'no-store'})

    # Root's common money mount owns exact GET/POST precedence on cold recovery.
    # This helper only appends its exact GET, never reorders existing routes.
    return application
