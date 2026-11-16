"""Parent-local W24 API. Runtime confirmations are deliberately not public writes."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .companion_activity_outcome import (
    CompanionActivityOutcomeService, ActivityConflict, ActivityInputError, UnknownActivity,
)
from .parent_auth import ParentSessionRequiredError


class OpenActivity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_kind: Literal['companion', 'controlled_game', 'controlled_content']
    source_id: str = Field(min_length=1, max_length=200)
    intent: str = Field(min_length=1, max_length=2000)
    success_description: str = Field(min_length=1, max_length=2000)
    client_action_id: str = Field(min_length=1, max_length=200)


class VersionedAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1)
    client_action_id: str = Field(min_length=1, max_length=200)


class ActivityFeedback(VersionedAction):
    execution: Literal['completed', 'partial', 'not_executed', 'unknown', 'refused', 'stopped']
    experience: Literal['comfortable', 'neutral', 'fatigued', 'uncomfortable', 'unknown']
    note: str = Field(min_length=1, max_length=2000)
    evidence_basis: Literal['parent_observed', 'parent_report']


class ActivityRetraction(VersionedAction):
    target_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class ResumeDecision(VersionedAction):
    note: str = Field(min_length=1, max_length=2000)


def create_companion_activity_outcome_router(service: CompanionActivityOutcomeService, parent_auth):
    def authorized(request: Request, response: Response):
        try:
            parent_auth.require(request.headers.get('x-hsc-parent'))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={'code':'PARENT_SESSION_REQUIRED'},
                                headers={'Cache-Control':'no-store'}) from None
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(prefix='/v1/parent/children/{child_id}/companion-activity-outcomes',
                       dependencies=[Depends(authorized)])

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (UnknownActivity, ActivityConflict, ActivityInputError) as error:
            status = 404 if isinstance(error, UnknownActivity) else 409 if isinstance(error, ActivityConflict) else 422
            raise HTTPException(status, detail={'code':str(error).strip("'")},
                                headers={'Cache-Control':'no-store'}) from None

    @router.get('')
    def listing(child_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100)):
        return call(service.list, child_id, offset=offset, limit=limit)

    @router.post('')
    def register(child_id: str, body: OpenActivity):
        return call(service.open, child_id, actor='parent', **body.model_dump())

    @router.get('/{outcome_id}')
    def detail(child_id: str, outcome_id: str):
        return call(service.get, child_id, outcome_id)

    @router.post('/{outcome_id}/feedback')
    def feedback(child_id: str, outcome_id: str, body: ActivityFeedback):
        return call(service.feedback, child_id, outcome_id, actor='parent', **body.model_dump())

    @router.post('/{outcome_id}/retract')
    def retract(child_id: str, outcome_id: str, body: ActivityRetraction):
        return call(service.retract, child_id, outcome_id, actor='parent', **body.model_dump())

    @router.post('/{outcome_id}/resume-request')
    def resume_request(child_id: str, outcome_id: str, body: ResumeDecision):
        return call(service.request_resume, child_id, outcome_id, actor='parent', **body.model_dump())

    @router.post('/{outcome_id}/resume-defer')
    def resume_defer(child_id: str, outcome_id: str, body: ResumeDecision):
        return call(service.defer_resume, child_id, outcome_id, actor='parent', **body.model_dump())

    return router
