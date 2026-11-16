"""Normal local parent PIN/session protected selection; no new credentials."""
from typing import Optional, Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from .curriculum_supply_http import create_parent_dependency, Identifier, _call
from .paper_learning_selection import PaperLearningSelectionService


class SelectionCommand(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    session_id: Identifier
    action_id: Identifier
    expected_version: int=Field(ge=0,strict=True)
    expected_generation: str=Field(min_length=1,max_length=100)
    operation: Literal['replace','append','clear']
    candidate_id: Optional[Identifier]=None
    candidate_version: Optional[int]=Field(default=None,ge=1,strict=True)


def create_paper_learning_selection_router(database,parent_auth):
    service=PaperLearningSelectionService(database)
    router=APIRouter(prefix='/api/parent/children/{child_id}/paper-learning-selection',
                     dependencies=[Depends(create_parent_dependency(parent_auth))])
    @router.get('')
    def options(child_id: Identifier):
        return _call(service.options,child_id)
    @router.post('')
    def change(child_id: Identifier,payload: SelectionCommand):
        values=payload.model_dump();sid=values.pop('session_id')
        return _call(lambda:service.change(child_id,sid,**values))
    return router
