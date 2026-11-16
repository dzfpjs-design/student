"""Loopback session-only figure reads; never parent display/score receipts."""
from ipaddress import ip_address
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


class _ReadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def read(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={'code': 'DIAGNOSTIC_VISUAL_INPUT_INVALID'}, headers=HEADERS)
        return read


class ImageDescriptor(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['bounded_diagram_region']
    artifact_id: str
    revision_id: str
    region_id: str
    region_version: str = Field(pattern='^[0-9a-f]{64}$')
    sha256: str = Field(pattern='^[0-9a-f]{64}$')
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=4096)
    mime_type: Literal['image/png']


class Material(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    material_key: str
    image_descriptor: ImageDescriptor


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal['diagnostic_visual_v1']
    child_id: str
    session_id: str
    prompt_id: str
    item_version: str
    materials: List[Material] = Field(max_length=8)
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]
    mastery_claim_allowed: Literal[False]


def create_diagnostic_visual_router(service, *, local_session_dependency):
    if not callable(local_session_dependency):
        raise ValueError('local_session_dependency_required')

    def local(request: Request):
        try:
            allowed = bool(request.client and ip_address(request.client.host).is_loopback)
        except ValueError:
            allowed = False
        if not allowed:
            raise HTTPException(403, detail={'code': 'DIAGNOSTIC_VISUAL_LOCAL_ONLY'}, headers=HEADERS)
        if request.headers.get('Range') is not None:
            raise HTTPException(416, detail={'code': 'DIAGNOSTIC_VISUAL_RANGE_FORBIDDEN'}, headers=HEADERS)
        try:
            local_session_dependency(request)
        except HTTPException as error:
            error.headers = {**(error.headers or {}), **HEADERS}
            raise

    router = APIRouter(prefix='/v1/sessions/{session_id}/diagnostic-visual',
        dependencies=[Depends(local)], route_class=_ReadRoute)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError, TypeError, OSError) as error:
            # Known malformed/withdrawn sources must not expose paths or stored text.
            # SQLite failures deliberately remain server errors, not empty materials.
            raise HTTPException(409, detail={'code': 'DIAGNOSTIC_VISUAL_UNAVAILABLE',
                'status': 'unavailable', 'message': '原题图形暂时无法读取，请核对当前题目和材料。'}, headers=HEADERS) from error

    @router.get('', response_model=Manifest)
    def manifest(session_id: str, response: Response,
                 prompt_id: str = Query(..., min_length=1, max_length=128),
                 item_version: str = Query(..., min_length=1, max_length=200)):
        response.headers.update(HEADERS)
        return call(service.manifest, session_id, prompt_id=prompt_id, item_version=item_version)

    @router.get('/image')
    def image(session_id: str, prompt_id: str = Query(..., min_length=1, max_length=128),
              item_version: str = Query(..., min_length=1, max_length=200),
              material_key: str = Query(..., pattern='^[A-Za-z][A-Za-z0-9_-]{0,39}$'),
              sha256: str = Query(..., pattern='^[0-9a-f]{64}$')):
        raw = call(service.image, session_id, prompt_id=prompt_id, item_version=item_version,
            material_key=material_key, sha256=sha256)
        return Response(raw, media_type='image/png', headers=HEADERS)

    return router
