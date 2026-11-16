"""Authenticated historical parent review; no current workspace dependency."""
from typing import Literal, Optional
from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .local_supply import LocalSupplyError
from .parent_auth import ParentSessionRequiredError


class Scope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision_id: str = Field(min_length=1,max_length=128)
    region_version: str = Field(pattern='^[0-9a-f]{64}$')
    facts_version: Optional[str] = Field(pattern='^[0-9a-f]{64}$')
    client_action_id: str = Field(min_length=1,max_length=128)


class Storage(Scope):
    purpose: Literal['local_visual_material_review_v1']


class Decision(Scope):
    facts_version: str = Field(pattern='^[0-9a-f]{64}$')


class Confirm(Decision):
    display_receipt_id: str = Field(min_length=1,max_length=128)


class Displayed(Scope):
    display_token: str = Field(min_length=1,max_length=6000)
    sha256: str = Field(pattern='^[0-9a-f]{64}$')
    width: StrictInt = Field(ge=1,le=4096)
    height: StrictInt = Field(ge=1,le=4096)


def create_visual_material_router(service,parent_auth,*,local_session_dependency):
    if not callable(local_session_dependency):
        raise ValueError('local_session_dependency_required')

    def parent(request: Request):
        try:
            local = bool(request.client and ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403,detail={'code':'VISUAL_MATERIAL_LOCAL_ONLY'},headers={'Cache-Control':'no-store'})
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
        except ParentSessionRequiredError as error:
            raise HTTPException(401,detail={'code':'PARENT_SESSION_REQUIRED'},headers={'Cache-Control':'no-store'}) from error

    router=APIRouter(prefix='/v1/parent/children/{child_id}/visual-materials',
        dependencies=[Depends(local_session_dependency),Depends(parent)])

    def call(fn,*args,**kwargs):
        try:
            result = fn(*args,**kwargs)
            if isinstance(result, dict):
                from .visual_material import FLAGS
                return {**result, **FLAGS}
            return result
        except KeyError as error:
            raise HTTPException(404,detail={'code':'VISUAL_MATERIAL_NOT_FOUND'},headers={'Cache-Control':'no-store'}) from error
        except LocalSupplyError as error:
            code=str(error)
            if not code.startswith('VISUAL_MATERIAL_'):
                raise
            raise HTTPException(409,detail={'code':code,'message':'这份图形材料暂时无法核对，请重新读取原材料。'},
                headers={'Cache-Control':'no-store'}) from error

    @router.get('')
    def listing(child_id: str,response: Response,offset: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100)):
        response.headers['Cache-Control']='no-store'
        return call(service.listing,child_id,offset=offset,limit=limit)

    path='/{artifact_id}/regions/{region_id}'

    @router.get(path)
    def detail(child_id: str,artifact_id: str,region_id: str,revision_id: str,region_version: str,response: Response):
        response.headers['Cache-Control']='no-store'
        return call(service.read,child_id,artifact_id,region_id,revision_id=revision_id,region_version=region_version)

    @router.get(path+'/image')
    def image(child_id: str,artifact_id: str,region_id: str,revision_id: str,region_version: str,sha256: str,request: Request):
        if request.headers.get('Range') is not None:
            raise HTTPException(416,detail={'code':'VISUAL_MATERIAL_RANGE_FORBIDDEN'},headers={'Cache-Control':'no-store'})
        raw,token=call(service.image,child_id,artifact_id,region_id,revision_id=revision_id,region_version=region_version,sha256=sha256)
        return Response(raw,media_type='image/png',headers={'Cache-Control':'no-store',
            'X-HSC-Visual-Display-Token':token,'X-Content-Type-Options':'nosniff'})

    @router.post(path+'/storage')
    def storage(child_id: str,artifact_id: str,region_id: str,payload: Storage,response: Response):
        response.headers['Cache-Control']='no-store'
        return call(service.storage,child_id,artifact_id,region_id,**payload.model_dump())

    @router.post(path+'/displayed')
    def displayed(child_id: str,artifact_id: str,region_id: str,payload: Displayed,response: Response):
        response.headers['Cache-Control']='no-store'
        return call(service.displayed,child_id,artifact_id,region_id,**payload.model_dump())

    @router.post(path+'/confirm')
    def confirm(child_id: str,artifact_id: str,region_id: str,payload: Confirm,response: Response):
        response.headers['Cache-Control']='no-store'
        return call(service.decide,child_id,artifact_id,region_id,decision='confirm',**payload.model_dump())

    @router.post(path+'/retract')
    def retract(child_id: str,artifact_id: str,region_id: str,payload: Scope,response: Response):
        response.headers['Cache-Control']='no-store'
        return call(service.decide,child_id,artifact_id,region_id,decision='retract',**payload.model_dump())

    @router.get(path+'/commands/{client_action_id}')
    def command(child_id: str,artifact_id: str,region_id: str,client_action_id: str,response: Response):
        response.headers['Cache-Control']='no-store'
        return call(service.command,child_id,artifact_id,region_id,client_action_id)

    return router
