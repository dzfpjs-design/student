"""Local request/receipt binding; never a source-content planning request."""
from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import document_goal_handoff as handoff


class ConfirmationRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    confirmation_id: str = Field(min_length=1, max_length=120)
    revision: int = Field(ge=1, strict=True)


def register(application, database, runtime):
    @application.get('/v1/sessions/{session_id}/document-goals/{root_turn_id}/review')
    async def read_document_goal_review(session_id: str, root_turn_id: str):
        try:
            return handoff.review(database, root_turn_id, session_id)
        except (ValueError, TypeError, KeyError) as error:
            code = str(error) if isinstance(error, handoff.DocumentGoalError) else 'DOCUMENT_GOAL_SOURCE_CHANGED'
            raise HTTPException(409, detail={'code': code,
                'message': '原题核对暂不能恢复，原记录仍保留，没有重新拍摄或另起讲解。'}) from None

    @application.post('/v1/sessions/{session_id}/document-goals/{root_turn_id}/confirm')
    async def confirm_document_goal(session_id: str, root_turn_id: str, payload: ConfirmationRequest):
        try:
            return handoff.prepare(database, runtime, root_turn_id, session_id, payload.confirmation_id, payload.revision)
        except handoff.DocumentGoalError as error:
            raise HTTPException(409, detail={'code': str(error),
                'message': '原题或当前目标已变化，已保存内容保留，请核对要继续的这道题。'}) from None
