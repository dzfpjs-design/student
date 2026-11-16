"""Local authenticated parent command for one owned native source."""
import ipaddress
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field


class DeleteKnowledgeSource(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    confirmation: Literal['确认删除']
    client_action_id: str = Field(min_length=1, max_length=200)


def create_knowledge_source_deletion_router(service, parent_auth):
    def authorized(request: Request, response: Response):
        parent_auth.require(request.headers.get('x-hsc-parent'))
        try:
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail={'code':'KNOWLEDGE_SOURCE_LOCAL_ONLY'})
        if request.query_params:
            raise HTTPException(422, detail={'code':'KNOWLEDGE_SOURCE_QUERY_NOT_SUPPORTED'})
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(dependencies=[Depends(authorized)])

    @router.post('/v1/parent/children/{child_id}/knowledge-sources/{source_id}/delete')
    async def delete(child_id: str, source_id: str, body: DeleteKnowledgeSource,request:Request):
        try:
            from .parent_restore_privacy import admitted_parent_delete
            async with admitted_parent_delete(service.database,parent_auth,request.headers.get('x-hsc-parent'),
                    method=request.method,path=request.url.path,body=body.model_dump()):
                return {**service.delete_knowledge_source(child_id, source_id, body.confirmation, body.client_action_id),
                        'local_only':True, 'cloud_transmission_allowed':False}
        except KeyError:
            raise HTTPException(404, detail={'code':'KNOWLEDGE_SOURCE_NOT_FOUND'}) from None

    return router
