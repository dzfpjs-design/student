"""Checked response receipt. No grading, model call, or mastery mutation."""
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from .tutor_check_response import TutorCheckError,TutorCheckResponseService

class CheckBinding(BaseModel):
    model_config=ConfigDict(extra='forbid')
    turn_id:str=Field(min_length=1,max_length=200)
    source_version:str=Field(min_length=1,max_length=200)

class Response(BaseModel):
    model_config=ConfigDict(extra='forbid')
    binding:CheckBinding
    response_text:str=Field(min_length=1,max_length=2000)
    client_action_id:str=Field(min_length=8,max_length=128)

def create_tutor_check_response_router(database):
    router=APIRouter(prefix='/v1/sessions/{session_id}/tutor-check')
    service=TutorCheckResponseService(database)
    @router.get('')
    def read(session_id:str):return service.read(session_id)
    @router.post('/responses')
    def respond(session_id:str,payload:Response):
        try:return service.save(session_id,**payload.model_dump())
        except TutorCheckError as error:raise HTTPException(409,detail={'code':str(error)}) from None
    return router
