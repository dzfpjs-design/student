"""Read-only local inspection of actual scoring qualification and its blockers."""
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .parent_auth import ParentSessionRequiredError


class AuthorityImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    envelope: dict
    signature: str = Field(min_length=1, max_length=512)
    client_action_id: str = Field(min_length=1, max_length=200)


class AuthorityRevoke(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str
    receipt_id: str
    expected_hash: str
    client_action_id: str = Field(min_length=1, max_length=200)


class ProtocolFreeze(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scorer: dict
    scope: dict
    holdout_receipt_id: str
    client_action_id: str = Field(min_length=1, max_length=200)


class ProtocolCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    client_action_id: str = Field(min_length=1, max_length=200)


class ProtocolCase(ProtocolCommand):
    assessment_id: str
    blind_receipt_ids: list[str]


class PrintedCase(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str = Field(min_length=1, max_length=200)
    blind_receipt_ids: list[str] = Field(min_length=2, max_length=2)


class PrintedCalibration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scorer: dict
    scope: dict
    holdout_receipt_id: str = Field(min_length=1, max_length=200)
    cases: list[PrintedCase] = Field(min_length=1, max_length=10000)


def create_rubric_calibration_router(service, parent_auth, evaluator=None, printed=None):
    def authorized(request: Request, response: Response):
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={'code': 'PARENT_SESSION_REQUIRED'},
                                headers={'Cache-Control': 'no-store'}) from None
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail={'code': 'RUBRIC_CALIBRATION_LOCAL_ONLY'})
        if request.query_params:
            raise HTTPException(422, detail={'code': 'RUBRIC_CALIBRATION_QUERY_NOT_SUPPORTED'})
        response.headers['Cache-Control'] = 'no-store'

    def read(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError:
            raise HTTPException(404, detail={'code': 'RUBRIC_CALIBRATION_NOT_FOUND'},
                                headers={'Cache-Control': 'no-store'}) from None
        except ValueError as error:
            code = str(error)
            if not code.startswith('RUBRIC_CALIBRATION_'):
                code = 'RUBRIC_CALIBRATION_AUTHORITY_INVALID'
            raise HTTPException(409, detail={'code': code, 'message': '证明无法核验或已失效，未授予评分资格。'},
                                headers={'Cache-Control': 'no-store'}) from None

    router = APIRouter(dependencies=[Depends(authorized)])
    root = '/v1/parent/children/{child_id}/rubric-calibration'

    @router.post(root + '/printed/evaluate')
    def evaluate_printed(child_id: str, body: PrintedCalibration):
        if printed is None:
            raise HTTPException(409, detail={'code': 'RUBRIC_CALIBRATION_PRINTED_UNAVAILABLE'})
        return read(printed.evaluate, child_id, **body.model_dump())

    @router.get(root + '/printed/{artifact_id}')
    def read_printed(child_id: str, artifact_id: str):
        if printed is None:
            raise HTTPException(409, detail={'code': 'RUBRIC_CALIBRATION_PRINTED_UNAVAILABLE'})
        return read(printed.current, child_id, artifact_id)

    @router.get(root + '/evaluations/{turn_id}/request-spec')
    def request_spec(child_id: str, turn_id: str):
        from .rubric_calibration_host import describe_execution
        if evaluator is None:
            raise HTTPException(409, detail={'code': 'RUBRIC_CALIBRATION_HOST_UNAVAILABLE'})
        return read(describe_execution, evaluator, child_id, turn_id)

    def authority_write(function, child_id, values):
        with service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            result = function(c, child_id=child_id, **values)
            c.commit()
            return result

    @router.post(root + '/authority/import')
    def import_authority(child_id: str, body: AuthorityImport):
        from .rubric_calibration_authority import import_receipt
        return read(authority_write, import_receipt, child_id, body.model_dump())

    @router.post(root + '/authority/revoke')
    def revoke_authority(child_id: str, body: AuthorityRevoke):
        from .rubric_calibration_authority import revoke
        return read(authority_write, revoke, child_id, body.model_dump())

    @router.post(root + '/protocols/freeze')
    def freeze_protocol(child_id: str, body: ProtocolFreeze):
        return read(service.freeze, child_id, **body.model_dump())

    @router.post(root + '/{protocol_id}/cases')
    def record_case(child_id: str, protocol_id: str, body: ProtocolCase):
        return read(service.record_case, child_id, protocol_id, **body.model_dump())

    @router.post(root + '/{protocol_id}/evaluate')
    def evaluate(child_id: str, protocol_id: str, body: ProtocolCommand):
        return read(service.evaluate, child_id, protocol_id, **body.model_dump())

    @router.post(root + '/{protocol_id}/revoke')
    def revoke_protocol(child_id: str, protocol_id: str, body: ProtocolCommand):
        return read(service.revoke, child_id, protocol_id, **body.model_dump())

    @router.get(root)
    def list_protocols(child_id: str):
        return read(service.list_for_child, child_id)

    @router.get(root + '/{protocol_id}')
    def detail(child_id: str, protocol_id: str):
        return read(service.read, child_id, protocol_id)

    return router
