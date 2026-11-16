"""Parent-authenticated, local-only routes for explicit report rebinding."""
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from .report_adjustment_rebinding import ReportRebindingService
from .report_followup import ReportFollowupConflict


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    binding: dict
    terms: dict
    client_action_id: str


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt
    decision: str
    review_hash: str
    evidence_seen: StrictBool
    client_action_id: str


class Cancel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_snapshot_id: str


def register_report_rebinding_routes(application, database, parent_auth):
    service = ReportRebindingService(database)
    service.initialize()
    root = '/v1/parent/children/{child_id}/report-rebindings'

    def call(request, response, function, *args, **kwargs):
        parent_auth.require(request.headers.get('X-HSC-Parent'))
        response.headers['Cache-Control'] = 'no-store'
        try:
            return function(*args, **kwargs)
        except KeyError as error:
            raise HTTPException(404, '找不到当前孩子的关联记录。') from error
        except ReportFollowupConflict as error:
            raise HTTPException(409, str(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    @application.get(root + '/contexts/{snapshot_id}')
    def report_rebinding_contexts(child_id: str, snapshot_id: str, request: Request, response: Response):
        return call(request, response, service.contexts, child_id, snapshot_id)

    @application.get(root + '/snapshots/{snapshot_id}')
    def report_rebinding_list(child_id: str, snapshot_id: str, request: Request, response: Response):
        return call(request, response, service.list, child_id, snapshot_id)

    @application.get(root + '/commands/{client_action_id}')
    def report_rebinding_command(child_id: str, client_action_id: str, request: Request, response: Response):
        return call(request, response, service.command, child_id, client_action_id)

    @application.post(root + '/proposals')
    def report_rebinding_propose(child_id: str, payload: Proposal, request: Request, response: Response):
        return call(request, response, service.propose, child_id, **payload.model_dump())

    @application.post(root + '/commands/{client_action_id}/cancel')
    def report_rebinding_cancel(child_id: str, client_action_id: str, payload: Cancel, request: Request, response: Response):
        return call(request, response, service.cancel_command, child_id, client_action_id, payload.target_snapshot_id)

    @application.post(root + '/{adjustment_id}/review')
    def report_rebinding_review(child_id: str, adjustment_id: str, payload: Review, request: Request, response: Response):
        return call(request, response, service.review, child_id, adjustment_id, **payload.model_dump())
