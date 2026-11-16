from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .mistake_learning_intake import MistakeIntakeError


class CardRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=12000)
    submitted_answer: str = Field(default='', max_length=8000)
    subject: str = Field(default='', max_length=40)
    session_id: Optional[str] = Field(default=None, max_length=160)
    client_action_id: str = Field(min_length=1, max_length=200)


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=1, max_length=160)
    expected_generation: int = Field(ge=1)


def create_router(intake, *, authorize_request, learning_service=None):
    router = APIRouter()

    @router.post('/v1/children/{child_id}/mistake-learning/cards')
    def create_card(child_id: str, payload: CardRequest, request: Request):
        if authorize_request(request) is not True:
            raise HTTPException(403, detail={'code': 'MISTAKE_INTAKE_SESSION_REQUIRED', 'message': '请从当前客户端保存题目。'})
        try:
            row = intake.create(child_id=child_id, **payload.model_dump())
        except MistakeIntakeError as error:
            code = str(error)
            status = 404 if code.endswith(('NOT_FOUND', 'SOURCE_CHANGED')) else 409 if code.endswith(('CONFLICT', 'CHANGED')) else 422
            raise HTTPException(status, detail={'code': code, 'message': '题目尚未保存。请核对当前孩子和题目后重试；已保存的内容仍在。'}) from None
        return {'version': 1, 'child_id': child_id, 'mistake_id': row['mistake_id'], 'saved': True}

    @router.post('/v1/children/{child_id}/mistake-learning/focus/release')
    def release_focus(child_id: str, payload: ReleaseRequest, request: Request):
        if authorize_request(request) is not True or learning_service is None:
            raise HTTPException(403, detail={'code': 'MISTAKE_INTAKE_SESSION_REQUIRED'})
        try:
            learning_service.invalidate_focus(payload.session_id, child_id, payload.expected_generation)
        except ValueError:
            # A retired session/focus cannot invalidate a newer owner.
            return {'released': False}
        return {'released': True}

    return router
