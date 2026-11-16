"""Loopback/session-bound ingestion of bounded observation claims."""
from ipaddress import ip_address
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field


class SegmentRequest(BaseModel):
    child_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    epoch: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0, strict=True)
    seq: int = Field(ge=0, strict=True)
    segment_id: str = Field(min_length=1, max_length=128)
    started_at: str = Field(min_length=1, max_length=64)
    ended_at: str = Field(min_length=1, max_length=64)
    visible_ms: int = Field(gt=0, le=15000, strict=True)


def create_document_observation_router(service, *, local_session_dependency):
    if not callable(local_session_dependency):
        raise ValueError('local_session_dependency_required')

    def local(request: Request):
        try:
            allowed = bool(request.client and ip_address(request.client.host).is_loopback)
        except ValueError:
            allowed = False
        if not allowed:
            raise HTTPException(403, detail='LOCAL_ONLY')
        local_session_dependency(request)

    router = APIRouter(prefix='/v1/sessions/{session_id}/document-observation-segments', dependencies=[Depends(local)])

    @router.get('/epoch')
    def epoch(session_id: str):
        return {'epoch': service.epoch, 'local_only': True}

    @router.post('')
    def record(session_id: str, payload: SegmentRequest):
        try:
            return service.record(session_id, **payload.model_dump())
        except ValueError as error:
            raise HTTPException(409, detail=str(error)) from error

    return router
