"""Parent-authenticated local retention initialization; no caller actor fields."""
from ipaddress import ip_address
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .parent_auth import ParentSessionRequiredError
from .visual_retention import VisualRetentionError


class PolicyCommand(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    action: Literal['enable','update','revoke']
    expected_revision: Optional[str] = Field(max_length=128)
    client_action_id: str = Field(min_length=1,max_length=128)
    session_id: Optional[str] = Field(default=None,min_length=1,max_length=128)
    retention_days: Optional[StrictInt] = Field(default=None,ge=1,le=30)
    authorization_days: Optional[StrictInt] = Field(default=None,ge=1,le=365)
    max_bytes: Optional[StrictInt] = Field(default=None,ge=1048576,le=268435456)

    @model_validator(mode='after')
    def complete_scope(self):
        if self.action=='revoke':
            if self.model_fields_set & {'session_id','retention_days','authorization_days','max_bytes'}:
                raise ValueError('revoke accepts only identity')
        elif any(v is None for v in (self.retention_days,self.authorization_days,self.max_bytes)):
            raise ValueError('explicit retention bounds required')
        return self


def create_visual_retention_router(service,parent_auth,*,local_session_dependency):
    def parent(request:Request):
        try:
            local=bool(request.client and ip_address(request.client.host).is_loopback)
        except ValueError:
            local=False
        if not local:
            raise HTTPException(403,detail={'code':'VISUAL_RETENTION_LOCAL_ONLY'})
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
        except ParentSessionRequiredError as error:
            raise HTTPException(401,detail={'code':'PARENT_SESSION_REQUIRED'}) from error

    router=APIRouter(prefix='/v1/parent/children/{child_id}/visual-retention',
        dependencies=[Depends(local_session_dependency),Depends(parent)])

    def call(fn,*a,**kw):
        try:
            return fn(*a,**kw)
        except KeyError as error:
            raise HTTPException(404,detail={'code':'VISUAL_RETENTION_NOT_FOUND'},headers={'Cache-Control':'no-store'}) from error
        except VisualRetentionError as error:
            raise HTTPException(409,detail={'code':str(error),'message':'保留设置已变化，请重新读取原设置。'},headers={'Cache-Control':'no-store'}) from error

    @router.get('')
    def read(child_id:str,response:Response):
        response.headers['Cache-Control']='no-store'
        return call(service.read,child_id)

    @router.post('')
    def command(child_id:str,payload:PolicyCommand,response:Response):
        response.headers['Cache-Control']='no-store'
        return call(service.command,child_id,**payload.model_dump())

    return router
