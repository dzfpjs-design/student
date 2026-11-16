"""Authenticated HTTP seam; app assembly owns the actual local API authority."""
import asyncio
import base64
import binascii
import inspect
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .local_ocr.cache_lifecycle import OcrActionConflict, OcrSourceInvalid
from .local_ocr.workflow import OcrWorkflowDeferredError, OcrWorkflowFailedError
from .mistake_learning import MistakeLearningError


class LearningInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    child_id: StrictStr
    session_id: Optional[StrictStr] = None
    client_action_id: StrictStr
    expected_revision: Optional[StrictInt] = None
    practice_item_id: Optional[StrictStr] = None
    utterance: Optional[StrictStr] = None
    style: Literal['direct', 'socratic'] = 'socratic'
    count: StrictInt = 3
    native_turn_id: Optional[StrictStr] = None
    original_review: StrictBool = False
    voice_input_id: Optional[StrictStr] = Field(default=None, min_length=1, max_length=120)
    coach_turn_id: Optional[StrictStr] = Field(default=None, min_length=1, max_length=120)
    source_revision: Optional[StrictStr] = Field(default=None, min_length=64, max_length=64)


class OriginalReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    child_id: StrictStr = Field(min_length=1, max_length=120)
    session_id: StrictStr = Field(min_length=1, max_length=120)
    client_action_id: StrictStr = Field(min_length=1, max_length=180)
    expected_revision: Optional[StrictInt] = None
    source_revision: Optional[StrictStr] = Field(default=None, min_length=64, max_length=64)
    modality: Literal['paper', 'voice']
    utterance: Optional[StrictStr] = Field(default=None, max_length=4000)
    artifact_id: Optional[StrictStr] = Field(default=None, min_length=1, max_length=120)
    voice_input_id: Optional[StrictStr] = Field(default=None, min_length=1, max_length=120)


class OriginalReviewCapture(BaseModel):
    model_config = ConfigDict(extra='forbid')
    child_id: StrictStr = Field(min_length=1, max_length=120)
    lease_id: StrictStr = Field(min_length=1, max_length=120)
    image_base64: StrictStr = Field(min_length=32, max_length=24_000_000)
    client_action_id: StrictStr = Field(min_length=8, max_length=120)


def create_router(service, *, authorize_request, native_guard=None, ocr_workflow=None):
    router = APIRouter()

    async def authorize(request):
        ok = authorize_request(request)
        if inspect.isawaitable(ok):
            ok = await ok
        if ok is not True:
            raise HTTPException(403, detail={'code': 'MISTAKE_LEARNING_AUTHORITY_REQUIRED'})

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MistakeLearningError as e:
            raise HTTPException(e.status, detail={'code': e.code}) from None

    @router.get('/v1/children/{child_id}/mistake-learning')
    async def library(child_id: str, request: Request):
        await authorize(request)
        return await asyncio.to_thread(invoke, service.library, child_id)

    @router.get('/v1/children/{child_id}/mistake-learning/print')
    async def print_notebook(child_id: str, request: Request, response: Response):
        await authorize(request)
        response.headers['Cache-Control'] = 'no-store'
        from .mistake_print import snapshot
        return await asyncio.to_thread(invoke, snapshot, service, child_id)

    @router.get('/v1/mistakes/{mistake_id}/learning')
    async def state(mistake_id: str, child_id: str, request: Request, session_id: Optional[str] = None):
        await authorize(request)
        return await asyncio.to_thread(invoke, service.state, mistake_id, child_id=child_id, session_id=session_id)

    @router.get('/v1/mistakes/{mistake_id}/learning/actions/{client_action_id}')
    async def action(mistake_id: str, client_action_id: str, child_id: str, request: Request):
        await authorize(request)
        return invoke(service.read_action, client_action_id, child_id=child_id, mistake_id=mistake_id)

    @router.post('/v1/mistakes/{mistake_id}/learning/review')
    async def original_review(mistake_id: str, body: OriginalReviewInput, request: Request):
        await authorize(request)
        try:
            return await service.review_with_cloud(mistake_id, **body.model_dump())
        except MistakeLearningError as error:
            raise HTTPException(error.status, detail={'code': error.code}) from None

    @router.post('/v1/sessions/{session_id}/mistake-review/ocr')
    async def original_review_ocr(session_id: str, body: OriginalReviewCapture, request: Request):
        await authorize(request)
        if ocr_workflow is None:
            raise HTTPException(503, detail={'code': 'MISTAKE_REVIEW_OCR_UNAVAILABLE'})
        try:
            image = base64.b64decode(body.image_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, detail={'code': 'MISTAKE_REVIEW_IMAGE_INVALID'}) from None
        try:
            return await service.review_ocr(ocr_workflow, session_id=session_id, image_bytes=image,
                                            **body.model_dump(exclude={'image_base64'}))
        except MistakeLearningError as error:
            raise HTTPException(error.status, detail={'code': error.code}) from None
        except OcrWorkflowDeferredError:
            raise HTTPException(409, detail={'code': 'MISTAKE_REVIEW_OCR_BUSY'}) from None
        except (OcrActionConflict, OcrSourceInvalid):
            raise HTTPException(409, detail={'code': 'MISTAKE_REVIEW_CAPTURE_CHANGED'}) from None
        except OcrWorkflowFailedError:
            raise HTTPException(422, detail={'code': 'MISTAKE_REVIEW_OCR_UNREADABLE'}) from None

    @router.post('/v1/mistakes/{mistake_id}/learning/{operation}')
    async def mutate(mistake_id: str, operation: str, body: LearningInput, request: Request):
        await authorize(request)
        if operation not in {'analyze', 'practice', 'coach', 'answer', 'focus'}:
            raise HTTPException(404, detail={'code': 'MISTAKE_LEARNING_OPERATION_INVALID'})
        if body.voice_input_id and (body.native_turn_id or operation not in ('coach', 'answer')):
            raise HTTPException(422, detail={'code': 'MISTAKE_LEARNING_ARGUMENT_INVALID'})
        if not body.voice_input_id and (body.coach_turn_id or body.source_revision):
            raise HTTPException(422, detail={'code': 'MISTAKE_LEARNING_ARGUMENT_INVALID'})
        guard = None
        if body.native_turn_id:
            if native_guard is None or not body.session_id:
                raise HTTPException(403, detail={'code': 'MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED'})
            def guard(c):
                return native_guard(c, native_turn_id=body.native_turn_id, child_id=body.child_id,
                                    session_id=body.session_id, mistake_id=mistake_id, operation=operation)
        kw = body.model_dump(exclude={'native_turn_id'}) | {'guard': guard}
        if operation == 'focus':
            kw = {k: kw[k] for k in ('child_id', 'session_id', 'client_action_id', 'expected_revision', 'guard')}
        try:
            value = getattr(service, operation)(mistake_id, **kw)
            return await value if inspect.isawaitable(value) else value
        except MistakeLearningError as e:
            raise HTTPException(e.status, detail={'code': e.code}) from None
    return router
