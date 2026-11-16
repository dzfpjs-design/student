"""Parent-only decisions for generating variants from one confirmed paper question."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .paper_variant_authorization import PaperVariantAuthorizationService


class PaperLicenseBasis(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    license_id: Literal['CC0-1.0', 'original_author', 'explicit_cloud_processing_permission']
    reference: str = Field(min_length=1, max_length=1024)
    evidence: str = Field(min_length=20, max_length=2000)


class PaperRevokeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=0)
    client_action_id: str = Field(min_length=1, max_length=200)


class PaperGrantRequest(PaperRevokeRequest):
    question_version: str = Field(min_length=1, max_length=200)
    source_version: str = Field(min_length=1, max_length=200)
    license_basis: PaperLicenseBasis


def create_paper_variant_authorization_router(database, parent_auth):
    service = PaperVariantAuthorizationService(database)
    service.initialize()

    def authorized(request: Request, response: Response):
        parent_auth.require(request.headers.get('x-hsc-parent'))
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(prefix='/v1/parent/children/{child_id}/paper-variant-authorizations',
                       dependencies=[Depends(authorized)])

    def call(method, child_id, mistake_id, **kwargs):
        with database.connect() as c:
            if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
                raise HTTPException(404, detail={'code': 'CHILD_NOT_FOUND'})
        try:
            return method(child_id, mistake_id=mistake_id, **kwargs)
        except KeyError as error:
            if error.args != ('PAPER_VARIANT_NOT_FOUND',):
                raise
            raise HTTPException(404, detail={'code': 'PAPER_VARIANT_NOT_FOUND'}) from None
        except ValueError as error:
            code = str(error)
            if not code.startswith('PAPER_VARIANT_'):
                raise
            raise HTTPException(409, detail={'code': code,
                'message': '原题、来源或授权依据已变化，请重新核对；没有自动扩大云端处理范围。'}) from None

    @router.get('/{mistake_id}')
    def read(child_id: str, mistake_id: str):
        return call(service.read, child_id, mistake_id)

    @router.post('/{mistake_id}/grant')
    def grant(child_id: str, mistake_id: str, body: PaperGrantRequest):
        return call(service.grant, child_id, mistake_id, actor_id='parent_ui', **body.model_dump())

    @router.post('/{mistake_id}/revoke')
    def revoke(child_id: str, mistake_id: str, body: PaperRevokeRequest):
        return call(service.revoke, child_id, mistake_id, actor_id='parent_ui', **body.model_dump())

    return router
