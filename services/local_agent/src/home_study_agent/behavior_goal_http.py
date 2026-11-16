"""Authenticated local corrections to evidence-bound behavior opportunities."""
import hashlib
import ipaddress
import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .behavior_model.goal_confirmation import BehaviorActorProvenance, GoalBehaviorConflict
from .parent_auth import ParentSessionRequiredError
from .database import ExternalEffectBusyError


class BehaviorGoalConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    opportunity_id: str = Field(min_length=1, max_length=200)
    expected_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    opportunity_present: Optional[StrictBool]
    criteria: dict[str, Optional[StrictBool]]
    initiated_by: Literal['child', 'parent', 'agent', 'unknown'] = 'unknown'
    assistance_level: Literal['none', 'prompted', 'assisted', 'unknown'] = 'unknown'
    client_action_id: str = Field(min_length=1, max_length=200)
    operation: Literal['confirm', 'retract'] = 'confirm'
    reason: str = Field(default='', max_length=500)


def create_behavior_goal_router(service, parent_auth):
    def authorized(child_id: str, request: Request, response: Response):
        token = request.headers.get('X-HSC-Parent')
        try:
            parent_auth.require(token)
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={'code': 'PARENT_SESSION_REQUIRED'},
                                headers={'Cache-Control': 'no-store'}) from None
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail={'code': 'BEHAVIOR_GOAL_LOCAL_ONLY'})
        if request.query_params:
            raise HTTPException(422, detail={'code': 'BEHAVIOR_GOAL_QUERY_NOT_SUPPORTED'})
        response.headers['Cache-Control'] = 'no-store'
        # One household credential is one observer, regardless of login sessions.
        return BehaviorActorProvenance('parent', 'household_parent', child_id,
                                       hashlib.sha256(token.encode()).hexdigest())

    def call(function, *args, **kwargs):
        try:
            return {**function(*args, **kwargs), 'local_only': True, 'cloud_transmission_allowed': False}
        except GoalBehaviorConflict as error:
            code = str(error)
            status = 404 if code == 'BEHAVIOR_GOAL_NOT_OWNED' else 409
            if code in {'BEHAVIOR_CONFIRMATION_INVALID', 'BEHAVIOR_CRITERIA_OR_TIME_INVALID'}:
                status = 422
            raise HTTPException(status, detail={'code': code}, headers={'Cache-Control': 'no-store'}) from None
        except ExternalEffectBusyError:
            raise HTTPException(503, detail={'code': 'BEHAVIOR_GOAL_BUSY'},
                                headers={'Retry-After': '1', 'Cache-Control': 'no-store'}) from None
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error).lower():
                raise
            raise HTTPException(503, detail={'code': 'BEHAVIOR_GOAL_BUSY'},
                                headers={'Retry-After': '1', 'Cache-Control': 'no-store'}) from None

    router = APIRouter()
    root = '/v1/parent/children/{child_id}/behavior/habit-experiments/{experiment_id}'

    @router.get(root + '/opportunities')
    def opportunities(child_id: str, experiment_id: str, actor=Depends(authorized)):
        return call(service.goal_behavior_opportunities, child_id, experiment_id)

    @router.post(root + '/confirmation')
    def confirm(child_id: str, experiment_id: str, body: BehaviorGoalConfirmation, actor=Depends(authorized)):
        return call(service.confirm_goal_behavior, child_id, experiment_id,
                    actor_provenance=actor, **body.model_dump())

    return router
