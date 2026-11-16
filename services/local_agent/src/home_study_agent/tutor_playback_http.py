"""Local playback checkpoints; no model calls and no mastery updates."""
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .tutor_playback import TutorPlaybackError, TutorPlaybackService


class Begin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str = Field(min_length=1, max_length=200)
    client_action_id: str = Field(min_length=1, max_length=200)
    presentation: Optional[dict] = None
    source_version: Optional[str] = None


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    playback_id: str = Field(min_length=1, max_length=200)
    source_version: str = Field(min_length=1, max_length=200)
    generation: int = Field(strict=True, ge=1)
    expected_revision: int = Field(strict=True, ge=1)
    client_action_id: str = Field(min_length=1, max_length=200)


class Control(Binding):
    command: Literal["pause", "resume", "previous", "restart", "cancel"]


class Segment(Binding):
    segment_id: str = Field(min_length=1, max_length=200)
    event: Literal["started", "ended"]


def create_tutor_playback_router(database):
    router = APIRouter(prefix="/v1/sessions/{session_id}/tutor-playback")

    def call(method, session_id, **values):
        try:
            result = getattr(TutorPlaybackService(database), method)(session_id, **values)
            return {"playback": result, "local_only": True, "cloud_transmission_allowed": False}
        except TutorPlaybackError as error:
            raise HTTPException(409, detail={"code": error.code}) from None
        except KeyError:
            raise HTTPException(404, detail={"code": "TUTOR_PLAYBACK_NOT_FOUND"}) from None

    @router.get("")
    def read(session_id: str, playback_id: Optional[str] = None):
        return call("read", session_id, playback_id=playback_id)

    @router.post("/begin")
    def begin(session_id: str, payload: Begin):
        return call("begin", session_id, **payload.model_dump())

    @router.post("/control")
    def control(session_id: str, payload: Control):
        return call("control", session_id, **payload.model_dump())

    @router.post("/segment")
    def segment(session_id: str, payload: Segment):
        return call("record_segment", session_id, **payload.model_dump())

    return router
