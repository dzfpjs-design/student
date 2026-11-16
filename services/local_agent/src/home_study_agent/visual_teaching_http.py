"""Exact saved Tutor figures, separate from cloud-facing turn text."""
from ipaddress import ip_address
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from .diagnostic_visual_http import HEADERS, Manifest as DiagnosticManifest, _ReadRoute


class Manifest(DiagnosticManifest):
    version: Literal['visual_teaching_v1']
    turn_id: str
    source_version: str
    teaching_scope: Literal['observation_scaffold']


def create_visual_teaching_router(service, *, local_session_dependency):
    if not callable(local_session_dependency):
        raise ValueError('local_session_dependency_required')

    def local(request: Request):
        try:
            allowed = bool(request.client and ip_address(request.client.host).is_loopback)
        except ValueError:
            allowed = False
        if not allowed:
            raise HTTPException(403, detail={'code': 'TUTOR_VISUAL_LOCAL_ONLY'}, headers=HEADERS)
        if request.headers.get('Range') is not None:
            raise HTTPException(416, detail={'code': 'TUTOR_VISUAL_RANGE_FORBIDDEN'}, headers=HEADERS)
        try:
            local_session_dependency(request)
        except HTTPException as error:
            error.headers = {**(error.headers or {}), **HEADERS}
            raise

    router = APIRouter(prefix='/v1/sessions/{session_id}/tutor-visual',
        dependencies=[Depends(local)], route_class=_ReadRoute)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise HTTPException(409, detail={'code': 'TUTOR_VISUAL_UNAVAILABLE', 'status': 'unavailable',
                'message': '这轮讲解的原图暂时无法读取，请核对原题材料。'}, headers=HEADERS) from error

    @router.get('', response_model=Manifest)
    def manifest(session_id: str, response: Response,
                 turn_id: str = Query(..., min_length=1, max_length=128)):
        response.headers.update(HEADERS)
        return call(service.manifest, session_id, turn_id=turn_id)

    @router.get('/image')
    def image(session_id: str, turn_id: str = Query(..., min_length=1, max_length=128),
              material_key: str = Query(..., pattern='^[A-Za-z][A-Za-z0-9_-]{0,39}$'),
              sha256: str = Query(..., pattern='^[0-9a-f]{64}$')):
        raw = call(service.image, session_id, turn_id=turn_id, material_key=material_key, sha256=sha256)
        return Response(raw, media_type='image/png', headers=HEADERS)

    return router
