"""Parent purpose permission and explicit paid-unknown recovery, never child scoring."""
import json
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .parent_auth import ParentSessionRequiredError
from .pet_audio_source import PetAudioAuthorityError
from .pet_audio_gateway import PetAudioProtocolError


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    allowed: StrictBool
    expires_at: str
    day_limit_micros: int = Field(strict=True,gt=0,le=100_000_000)
    request_limit_micros: int = Field(strict=True,gt=0,le=100_000_000)
    expected_revision: int = Field(strict=True,ge=0)
    action_id: str = Field(min_length=1,max_length=200)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_generation: int = Field(strict=True,ge=0)
    action_id: str = Field(min_length=1,max_length=200)
    acknowledge_possible_charge: StrictBool


def create_router(service, parent_auth, *, local_session_dependency):
    def parent(request: Request):
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
        except ParentSessionRequiredError:
            raise HTTPException(401,detail={'code':'PARENT_SESSION_REQUIRED'}) from None
    router=APIRouter(prefix='/v1/parent/children/{child_id}/pet-audio',
        dependencies=[Depends(local_session_dependency),Depends(parent)])

    def safe_grant(g):
        return {**{key:g[key] for key in ('grant_id','revision','expires_at','day_limit_micros','request_limit_micros','action_id')},
                'allowed':bool(g['allowed'])} if g else None

    @router.get('/authorization')
    def authorization(child_id: str,response: Response):
        response.headers['Cache-Control']='no-store'
        with service.database.connect() as c:
            c.execute('BEGIN')
            if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'",(child_id,)).fetchone():
                raise HTTPException(404,detail={'code':'CHILD_UNAVAILABLE'})
            g=c.execute('SELECT * FROM pet_audio_grant WHERE child_id=? ORDER BY revision DESC LIMIT 1',(child_id,)).fetchone()
            try:
                service.require_new_use()
                service._grant(c,child_id)
                current=True
            except (ValueError,TypeError): current=False
        return {'child_id':child_id,'purpose':'pet_original_audio_formative_v1','configured':service.gateway is not None,'current':current,
            'grant':safe_grant(g),'budget_accounting':'full_conservative_ceiling_held_after_dispatch_including_unknown',
            'mastery_claim_allowed':False}

    @router.post('/authorization')
    def authorize(child_id: str,payload: GrantRequest,response: Response):
        response.headers['Cache-Control']='no-store'
        try:
            return {'child_id':child_id,'grant':safe_grant(service.grant(child_id,**payload.model_dump())),'mastery_claim_allowed':False}
        except (ValueError,TypeError) as error:
            code=str(error) if isinstance(error,(PetAudioAuthorityError,PetAudioProtocolError)) else 'PET_AUDIO_GRANT_INVALID'
            raise HTTPException(409,detail={'code':code}) from None

    @router.get('/authorization/actions/{action_id}')
    def grant_receipt(child_id: str,action_id: str,response: Response):
        response.headers['Cache-Control']='no-store'
        with service.database.connect() as c:
            g=c.execute('SELECT * FROM pet_audio_grant WHERE child_id=? AND action_id=?',(child_id,action_id)).fetchone()
        return {'child_id':child_id,'state':'applied' if g else 'missing','grant':safe_grant(g)}

    @router.get('/work')
    def works(child_id: str,response: Response):
        response.headers['Cache-Control']='no-store'
        with service.database.connect() as c:
            c.execute('BEGIN')
            rows=c.execute('SELECT * FROM pet_audio_work WHERE child_id=? ORDER BY created_at DESC,work_id DESC LIMIT 20',(child_id,)).fetchall()
            items=[service._public(c,row,live=False) for row in rows]
        return {'child_id':child_id,'items':items}

    @router.get('/work/{work_id}')
    def work(child_id: str,work_id: str,response: Response):
        response.headers['Cache-Control']='no-store'
        try: return service.read(child_id,work_id)
        except PetAudioAuthorityError as error:
            raise HTTPException(409,detail={'code':str(error)}) from None

    @router.post('/work/{work_id}/retry')
    async def retry(child_id: str,work_id: str,payload: RetryRequest,response: Response):
        response.headers['Cache-Control']='no-store'
        try:
            service.retry(child_id,work_id,**payload.model_dump())
            result=await service.run(child_id,work_id,expected_generation=payload.expected_generation+1)
            return {**result,'retry_action_id':payload.action_id,'retry_generation':payload.expected_generation+1}
        except PetAudioAuthorityError as error:
            raise HTTPException(409,detail={'code':str(error)}) from None

    @router.get('/work/{work_id}/retry/actions/{action_id}')
    def retry_receipt(child_id: str,work_id: str,action_id: str,response: Response):
        response.headers['Cache-Control']='no-store'
        with service.database.connect() as c:
            row=c.execute('SELECT request_json FROM pet_audio_retry WHERE child_id=? AND work_id=? AND action_id=?',
                (child_id,work_id,action_id)).fetchone()
            try:
                value=json.loads(row['request_json']) if row else None
                applied=isinstance(value,list) and len(value)==3 and value[0]==work_id and type(value[1]) is int and value[1]>=0 and value[2] is True
            except (ValueError,TypeError):
                applied=False
        return {'child_id':child_id,'work_id':work_id,'action_id':action_id,'state':'applied' if applied else 'missing',
            'generation':value[1]+1 if applied else None}

    return router
