"""Authenticated parent decisions for one writing question and cloud purpose."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .writing_question_authorization import WritingQuestionAuthorizationService


class LicenseBasis(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    license_id: Literal['CC0-1.0', 'original_author', 'explicit_cloud_processing_permission']
    reference: str = Field(min_length=1, max_length=1024)
    evidence: str = Field(min_length=20, max_length=2000)


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=0)
    client_action_id: str = Field(min_length=1, max_length=200)


class MaterialLicenseBasis(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    material_key: str = Field(min_length=1, max_length=200)
    source_version: str = Field(min_length=1, max_length=200)
    text_hash: str = Field(min_length=1, max_length=200)
    license_basis: LicenseBasis


class GrantRequest(RevokeRequest):
    item_version: str = Field(min_length=1, max_length=200)
    source_version: str = Field(min_length=1, max_length=200)
    curriculum_version: str = Field(min_length=1, max_length=200)
    license_basis: LicenseBasis
    material_license_bases: Optional[list[MaterialLicenseBasis]] = Field(default=None, max_length=20)


def create_writing_question_authorization_router(database, parent_auth):
    service = WritingQuestionAuthorizationService(database)
    service.initialize()

    def authorized(request: Request, response: Response):
        parent_auth.require(request.headers.get('x-hsc-parent'))
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(prefix='/v1/parent/children/{child_id}/writing-question-authorizations',
                       dependencies=[Depends(authorized)])

    def call(method, child_id, item_id, **kwargs):
        with database.connect() as c:
            if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
                raise HTTPException(404, detail={'code': 'CHILD_NOT_FOUND'})
        try:
            return method(child_id, diagnostic_item_id=item_id, **kwargs)
        except ValueError as error:
            code = str(error)
            if not code.startswith('WRITING_QUESTION_'):
                raise
            raise HTTPException(409, detail={'code': code,
                'message': '题目、来源或授权版本已变化，请重新核对。没有扩大云端处理范围。'}) from None

    @router.get('/{item_id}')
    def read(child_id: str, item_id: str):
        return call(service.read, child_id, item_id)

    @router.post('/{item_id}/grant')
    def grant(child_id: str, item_id: str, body: GrantRequest):
        return call(service.grant, child_id, item_id, actor_id='parent_ui', **body.model_dump(exclude_none=True))

    @router.post('/{item_id}/revoke')
    def revoke(child_id: str, item_id: str, body: RevokeRequest):
        return call(service.revoke, child_id, item_id, actor_id='parent_ui', **body.model_dump())

    return router
