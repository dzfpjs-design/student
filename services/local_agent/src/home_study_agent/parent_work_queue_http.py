"""Host mounting: create_parent_work_queue_router(ParentWorkQueueService(db), parent_auth)."""
from __future__ import annotations

import ipaddress
import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .parent_auth import ParentSessionRequiredError
from .parent_work_queue import ParentWorkQueueService, WorkQueueConflict, WorkQueueNotFound


Kind = Literal['safety', 'ocr', 'variant', 'specialized', 'rubric', 'report_review', 'delivery_unknown', 'curriculum', 'supply_followup', 'original_question']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Evidence(StrictModel):
    label: str
    text: str


class OriginalSourceDecision(StrictModel):
    action_id: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=100)
    decision: Literal['approve', 'revoke']
    item_id: Optional[str] = Field(default=None, max_length=160)
    item_version: Optional[str] = Field(default=None, max_length=120)
    original_quote: str = Field(max_length=1500)
    reference_quote: str = Field(max_length=500)
    note: str = Field(min_length=8, max_length=1500)


class Target(StrictModel):
    panel: str
    object_id: str
    related_id: Optional[str]


class WorkItem(StrictModel):
    work_id: str
    child_id: str
    child_name: str
    kind: Kind
    object_id: str
    version: str
    title: str
    impact: str
    priority: int
    urgency: Literal['immediate', 'today', 'review']
    created_at: str
    evidence: list[Evidence]
    target: Target
    bulk_action: Optional[Literal['ocr_skip']]


class Source(StrictModel):
    child_id: str
    kind: Kind
    status: Literal['ready', 'unavailable']
    total: Optional[int]
    error_code: Optional[str]


class Child(StrictModel):
    child_id: str
    nickname: str


class WorkPage(StrictModel):
    items: list[WorkItem]
    sources: list[Source]
    children: list[Child]
    total: Optional[int]
    known_total: int
    system_verification_total: Optional[int]
    revision: str
    next_cursor: Optional[str]
    read_at: str
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]


class SupplyGap(StrictModel):
    child_id: str
    job_id: str
    status: str
    version: str
    error_code: Optional[str]
    evidence: list[Evidence]
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]


class SupplyFollowup(StrictModel):
    original_question: Optional[str] = None
    subject: Optional[str] = None
    material_object_id: str
    child_id: str
    root_obligation_id: str
    original_session_id: str
    original_date: str
    title: str
    original_reason: str
    decision_id: str
    current_session_id: Optional[str]
    plan_item_id: Optional[str]
    status: Literal['needs_plan','ready','awaiting_resources','scheduled','active','paused',
                    'practice_completed','cancelled','source_changed','missing_source','needs_replan','waiting_constraints']
    deferral_reason: str
    next_action: str
    version: str
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]
    mastery_claim_allowed: Literal[False]


class PeriodSupplyHistory(StrictModel):
    root_obligation_id: str
    original_session_id: str
    original_date: str
    title: str
    original_reason: str
    decision_id: str
    last_plan_date: str
    decision_version: str
    historical_status: Literal['practice_completed','cancelled','explicitly_deferred','unfulfilled']
    deferral_reason: str
    mastery_claim_allowed: Literal[False]


class PeriodSupplyItem(StrictModel):
    historical: PeriodSupplyHistory
    current: Optional[SupplyFollowup]


class PeriodSupplyCurrent(StrictModel):
    child_id: str
    snapshot_id: Optional[str]
    period_report_id: str
    as_of: Optional[str]
    status: Literal['ready','unavailable']
    unfulfilled_count: Optional[int]
    items: list[PeriodSupplyItem]
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]
    mastery_claim_allowed: Literal[False]


def create_parent_work_queue_router(service: ParentWorkQueueService, parent_auth) -> APIRouter:
    def parent_required(request: Request, response: Response):
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={'code': 'PARENT_SESSION_REQUIRED'}, headers={'Cache-Control':'no-store'}) from None
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail={'code': 'WORK_LOCAL_ONLY'}, headers={'Cache-Control':'no-store'})
        allowed = {'child_id', 'limit', 'cursor', 'audience'}
        if request.scope['route'].path.endswith('/original-checks/{commitment_id}'):
            allowed.add('search')
        if set(request.query_params) - allowed or len(request.query_params.multi_items()) != len(request.query_params):
            raise HTTPException(422, detail={'code': 'WORK_QUERY_INVALID'}, headers={'Cache-Control':'no-store'})
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Vary'] = 'X-HSC-Parent'

    router = APIRouter(dependencies=[Depends(parent_required)])

    def original_operation(operation):
        try:
            return operation()
        except (ValueError, KeyError, TypeError) as error:
            code = 'SOURCE_REVIEW_NOT_FOUND' if str(error) == 'SOURCE_REVIEW_NOT_FOUND' else 'SOURCE_REVIEW_CHANGED'
            raise HTTPException(404 if code.endswith('NOT_FOUND') else 409,
                detail={'code':code}, headers={'Cache-Control':'no-store'}) from None
        except sqlite3.Error:
            raise HTTPException(503, detail={'code':'WORK_SOURCE_UNAVAILABLE'}, headers={'Cache-Control':'no-store'}) from None

    @router.get('/v1/parent/children/{child_id}/original-checks/{commitment_id}')
    def original_check(child_id: str, commitment_id: str, search: str = Query('', max_length=120)):
        from .document_source_review import DocumentSourceReview
        return original_operation(lambda: DocumentSourceReview(service.database).read(child_id, commitment_id, search))

    @router.post('/v1/parent/children/{child_id}/original-checks/{commitment_id}')
    def original_decision(child_id: str, commitment_id: str, payload: OriginalSourceDecision):
        from .document_source_review import DocumentSourceReview
        return original_operation(lambda: DocumentSourceReview(service.database).review(child_id, commitment_id, payload.model_dump()))

    @router.get('/v1/parent/children/{child_id}/original-checks/{commitment_id}/receipts/{action_id}')
    def original_receipt(child_id: str, commitment_id: str, action_id: str):
        from .document_source_review import DocumentSourceReview
        return original_operation(lambda: DocumentSourceReview(service.database).receipt(child_id, commitment_id, action_id))

    @router.get('/v1/parent/children/{child_id}/period-reports/{period_report_id}/supply-followups', response_model=PeriodSupplyCurrent)
    def period_supply_by_report(child_id: str, period_report_id: str):
        try:
            return service.period_supply_followups(child_id, period_report_id=period_report_id)
        except WorkQueueNotFound:
            raise HTTPException(404, detail={'code':'WORK_OBJECT_NOT_FOUND'}, headers={'Cache-Control':'no-store'}) from None
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise HTTPException(503, detail={'code':'WORK_SOURCE_UNAVAILABLE'}, headers={'Cache-Control':'no-store'}) from None

    @router.get('/v1/parent/children/{child_id}/period-supply-followups/{snapshot_id}', response_model=PeriodSupplyCurrent)
    def period_supply_followups(child_id: str, snapshot_id: str):
        try:
            return service.period_supply_followups(child_id, snapshot_id)
        except WorkQueueNotFound:
            raise HTTPException(404, detail={'code':'WORK_OBJECT_NOT_FOUND'}, headers={'Cache-Control':'no-store'}) from None
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise HTTPException(503, detail={'code':'WORK_SOURCE_UNAVAILABLE'}, headers={'Cache-Control':'no-store'}) from None

    @router.get('/v1/parent/children/{child_id}/supply-followups/{root_id}', response_model=SupplyFollowup)
    def supply_followup(child_id: str, root_id: str):
        try:
            return service.supply_followup(child_id, root_id)
        except WorkQueueNotFound:
            raise HTTPException(404, detail={'code':'WORK_OBJECT_NOT_FOUND'}, headers={'Cache-Control':'no-store'}) from None
        except (sqlite3.Error, ValueError, KeyError):
            raise HTTPException(503, detail={'code':'WORK_SOURCE_UNAVAILABLE'}, headers={'Cache-Control':'no-store'}) from None

    @router.get('/v1/parent/children/{child_id}/work-supply-gaps/{job_id}', response_model=SupplyGap)
    def supply_gap(child_id: str, job_id: str):
        try:
            return service.supply_gap(child_id, job_id)
        except WorkQueueNotFound:
            raise HTTPException(404, detail={'code':'WORK_OBJECT_NOT_FOUND'}, headers={'Cache-Control':'no-store'}) from None
        except sqlite3.Error:
            raise HTTPException(503, detail={'code':'WORK_SOURCE_UNAVAILABLE'}, headers={'Cache-Control':'no-store'}) from None

    @router.get('/v1/parent/work-queue', response_model=WorkPage)
    def work_queue(child_id: Optional[str] = Query(None, min_length=1, max_length=120),
                   limit: int = Query(20, ge=1, le=50), cursor: Optional[str] = Query(None, max_length=400),
                   audience: Literal['all', 'parent_decisions'] = Query('all')):
        try:
            return service.list(child_id=child_id, limit=limit, cursor=cursor, audience=audience)
        except WorkQueueNotFound:
            raise HTTPException(404, detail={'code': 'WORK_CHILD_NOT_FOUND'}, headers={'Cache-Control':'no-store'}) from None
        except WorkQueueConflict:
            raise HTTPException(409, detail={'code': 'WORK_QUEUE_CHANGED'}, headers={'Cache-Control':'no-store'}) from None
        except ValueError:
            raise HTTPException(422, detail={'code': 'WORK_QUERY_INVALID'}, headers={'Cache-Control':'no-store'}) from None

    return router
