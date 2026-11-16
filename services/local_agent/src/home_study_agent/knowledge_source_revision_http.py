"""Authenticated local-only revision workflow; actor is never request-controlled."""
from typing import Literal
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .knowledge_base import InvalidKnowledgeSourceError
from .knowledge_source_revision import SourceRevisionConflict


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    content_text: str = Field(min_length=2, max_length=100_000)


class Revise(Preview):
    expected_version: str = Field(pattern='^[a-f0-9]{64}$')
    client_action_id: str = Field(min_length=1, max_length=128)
    confirmation: Literal['保留旧稿并创建待审核新版本']


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_version: str = Field(pattern='^[a-f0-9]{64}$')
    client_action_id: str = Field(min_length=1, max_length=128)
    confirmation: Literal['已核对新版完整内容与来源']


def create_knowledge_source_revision_router(service, parent_auth):
    def authorized(request: Request, response: Response):
        parent_auth.require(request.headers.get('X-HSC-Parent'))
        try:
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail={'code': 'SOURCE_REVISION_LOCAL_ONLY'})
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(prefix='/v1/parent/children/{child_id}/knowledge-sources/{source_id}/revision',
                       dependencies=[Depends(authorized)])

    def call(method, *args, **kwargs):
        try:
            return {**method(*args, **kwargs), 'local_only': True, 'cloud_transmission_allowed': False}
        except KeyError:
            raise HTTPException(404, detail={'code': 'KNOWLEDGE_SOURCE_NOT_FOUND'}, headers={'Cache-Control': 'no-store'}) from None
        except SourceRevisionConflict as error:
            raise HTTPException(409, detail={'code': str(error), 'message': '原资料或操作版本已变化，请重新核对。没有覆盖旧稿。'}, headers={'Cache-Control': 'no-store'}) from None
        except InvalidKnowledgeSourceError:
            raise HTTPException(422, detail={'code': 'SOURCE_REVISION_INVALID_DOCUMENT'}, headers={'Cache-Control': 'no-store'}) from None

    @router.get('')
    def get(child_id: str, source_id: str):
        return call(service.get, child_id, source_id)

    @router.post('/preview')
    def preview(child_id: str, source_id: str, body: Preview):
        return call(service.preview, child_id, source_id, body.content_text)

    @router.post('')
    def revise(child_id: str, source_id: str, body: Revise):
        return call(service.revise, child_id, source_id, **body.model_dump(exclude={'confirmation'}))

    @router.post('/review')
    def review(child_id: str, source_id: str, body: Review):
        return call(service.review, child_id, source_id, **body.model_dump(exclude={'confirmation'}))

    return router
