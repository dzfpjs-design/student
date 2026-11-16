"""Opt-in local PET evidence router; main app owns final mounting and privacy.

All endpoints require the injected local-session dependency. Parent-only source
review/preview additionally use the existing ParentAuth, never request roles.
There is no upload, cloud TTS, ASR, scoring, or child-voice download. Explicit
parent-only material creation uses audited original text and offline synthesis.
"""

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .parent_auth import ParentSessionRequiredError
from .pet_media_evidence import PetMediaEvidenceError


def _parent_listening_prompts(connection, child_id):
    """Project original snapshots without calling diagnostic read's write path."""
    from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version

    if not connection.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'",
                              (child_id,)).fetchone():
        raise PetMediaEvidenceError("child_unavailable")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
        return []
    rows = connection.execute("""SELECT p.prompt_id,p.child_id,p.origin_session_id,p.diagnostic_item_id,
        p.item_version,p.item_json,p.status,p.review_task_id,p.review_version,p.attempt_id,p.created_at,
        p.request_json,p.prepare_action_id,
        s.status AS session_status,s.state AS session_state,
        kn.code AS node_code,di.knowledge_node_id AS current_node_id
        FROM diagnostic_prompt p JOIN daily_session s
          ON s.session_id=p.origin_session_id AND s.child_id=p.child_id
        LEFT JOIN diagnostic_item di ON di.diagnostic_item_id=p.diagnostic_item_id
        LEFT JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
        WHERE p.child_id=? ORDER BY p.created_at DESC,p.rowid DESC""", (child_id,))
    prompts = []
    for raw in rows:
        row = dict(raw)
        try:
            saved = json.loads(row["item_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(saved, dict) or saved.get("track") not in {'pet', 'school'}:
            continue
        listening = saved.get("answer_type") == "listening_text" or (
            saved.get("knowledge_node_id") == row["current_node_id"]
            and (row["node_code"] or "").upper().startswith("PET.LISTENING."))
        if not listening:
            continue
        if saved['track'] == 'school':
            from .listening_modality import school_listening_node
            node = connection.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
                                      (saved.get('knowledge_node_id'),)).fetchone()
            if not node or not school_listening_node(node) or saved.get('answer_type') != 'listening_text':
                continue
        status = row["status"]
        item = DiagnosticPromptService._item(connection, child_id, row["diagnostic_item_id"])
        source_valid = (item is not None and _version(item) == row["item_version"]
                        and saved == {key: item[key] for key in _ITEM_FIELDS})
        from .exam_listening_delivery import exam_owner,prompt_source
        exam = exam_owner(row)
        if exam and source_valid:
            try:
                prompt_source(connection,row)
                from .exam_listening_delivery import VERSION,frozen
                if VERSION in json.loads(row['request_json']):
                    _,_,_,unit = frozen(connection,row['prompt_id'])
                    if unit['prompt_id'] != row['prompt_id']:
                        continue
            except ValueError:
                source_valid = False
        if not source_valid:
            status = "invalidated"
        elif status in {"waiting_answer", "answer_saved"}:
            if (row["session_status"] != "active" or row["session_state"] in {"completed", "evolving"}
                    or DiagnosticPromptService._scheduled_source_reason(connection, row)):
                status = "invalidated"
        if row["review_task_id"] and status not in {"cancelled", "invalidated"}:
            review = DiagnosticPromptService._review(connection, child_id, row["review_task_id"],
                allowed_statuses=("pending", "completed") if status == "answered" else ("pending",))
            if review is None or _version(review) != row["review_version"]:
                status = "invalidated"
        if status == "answered" and DiagnosticPromptService._receipt(connection, row) is None:
            status = "invalidated"
        prompt = {key: row[key] for key in ("child_id", "origin_session_id", "prompt_id",
            "item_version", "diagnostic_item_id", "created_at")}
        prompt.update(status=status, item={"track": saved['track'], "prompt": saved.get("prompt", ""),
            "stimulus_text": saved.get("stimulus_text"), "pet_audio_modality": "listening"})
        if exam:
            binding = json.loads(row['request_json']).get('exam_slot')
            if isinstance(binding,dict):
                prompt['exam_slot'] = binding
                context = connection.execute("""SELECT r.track,r.subject,r.status AS exam_status,r.delivery_phase,
                    i.section_id,se.title AS section_title FROM specialized_exam_run r
                    JOIN specialized_exam_item i USING(run_id) LEFT JOIN specialized_exam_section_state se
                    ON se.run_id=i.run_id AND se.section_id=i.section_id WHERE r.run_id=? AND i.position=?""",
                    (binding['run_id'],binding['position'])).fetchone()
                prompt['exam_context'] = dict(context) if context else None
        prompts.append(prompt)
        if len(prompts) == 100:
            break
    return prompts


def create_parent_pet_prompts_router(service, parent_auth, *, local_session_dependency):
    """Mount separately; reuse PetMediaEvidenceService and real ParentAuth.

    GET /v1/parent/children/{child_id}/pet-prompts is a no-store, read-only
    projection of at most 100 original listening prompts, newest first.
    Invalidated is projected, not written back. No attempt/answer/asset paths.
    """
    if not callable(local_session_dependency) or not callable(getattr(parent_auth, "require", None)):
        raise ValueError("PET_MEDIA_AUTH_DEPENDENCIES_REQUIRED")

    def parent(request: Request):
        try:
            parent_auth.require(request.headers.get("X-HSC-Parent"))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={"code": "PARENT_SESSION_REQUIRED"},
                                headers={"Cache-Control": "no-store"}) from None

    router = APIRouter(prefix="/v1/parent/children/{child_id}",
                       dependencies=[Depends(local_session_dependency), Depends(parent)])

    @router.get("/pet-prompts")
    def prompts(child_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with service.database.connect() as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            try:
                result = _parent_listening_prompts(connection, child_id)
            except PetMediaEvidenceError:
                raise HTTPException(404, detail={"code": "PET_MEDIA_CHILD_UNAVAILABLE"},
                                    headers={"Cache-Control": "no-store"}) from None
            finally:
                connection.rollback()
        return {"child_id": child_id, "prompts": result,
                "local_only": True, "cloud_transmission_allowed": False}

    return router


class PetMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_action_id: str = Field(min_length=1, max_length=200)


class PetMediaPrompt(PetMediaRequest):
    prompt_id: str = Field(min_length=1, max_length=200)
    item_version: str = Field(min_length=1, max_length=200)


class PetListeningBegin(PetMediaPrompt):
    artifact_id: str = Field(min_length=1, max_length=200)


class PetMaterialize(PetMediaRequest):
    item_version: str = Field(min_length=1, max_length=200)


class PetAgentControl(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_turn_id: str = Field(min_length=1, max_length=200)


class PetSpeakingBind(PetMediaPrompt):
    voice_input_id: str = Field(min_length=1, max_length=200)


class PetPlaybackBinding(PetMediaRequest):
    media_id: str = Field(min_length=1, max_length=200)
    generation: int = Field(ge=1, le=2**53 - 1)
    expected_revision: int = Field(ge=1, le=2**53 - 1)


class PetPlaybackControl(PetPlaybackBinding):
    command: Literal["pause", "resume", "restart", "cancel"]


class PetPlaybackReceipt(PetPlaybackBinding):
    event: Literal["started", "ended"]
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frame_position: int = Field(ge=0, le=2**53 - 1)
    monotonic_ms: int = Field(ge=0, le=2**53 - 1)


class PetSourceReview(PetMediaRequest):
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["approve", "retract"]
    full_spoken_reference: bool = Field(default=False, strict=True)


class PetAssessmentReference(PetMediaRequest):
    assessment_id: str = Field(min_length=1, max_length=200)


def create_pet_media_router(service, parent_auth, *, local_session_dependency):
    """Return an unmounted router; no optional/disabled auth fallback.

    local_session_dependency must validate the host's X-HSC-Session token.
    parent_auth is the host's existing ParentAuth instance (tokens are in memory).
    JSON responses: {media, local_only: true, cloud_transmission_allowed: false}.
    GET /prompt/{prompt_id}?item_version= returns the collection directly, with
    listening/speaking arrays and no automatic selection. Playback POST returns
    202 for pending_receipt; use its saved pending_playback_receipt for retry.
    Audio responses: full pinned WAV bytes, no ranges/cache/redirect/file paths.
    """
    if not callable(local_session_dependency) or not callable(getattr(parent_auth, "require", None)):
        raise ValueError("PET_MEDIA_AUTH_DEPENDENCIES_REQUIRED")

    def private(response: Response):
        response.headers["Cache-Control"] = "no-store"

    router = APIRouter(prefix="/v1/sessions/{session_id}/pet-media",
                       dependencies=[Depends(local_session_dependency), Depends(private)])

    def parent(request: Request):
        try:
            parent_auth.require(request.headers.get("X-HSC-Parent"))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={"code": "PARENT_SESSION_REQUIRED"},
                                headers={"Cache-Control": "no-store"}) from None

    def call(method, session_id, **values):
        try:
            return getattr(service, method)(session_id, **values)
        except PetMediaEvidenceError as exc:
            code = str(exc)
            status = 404 if code == "unknown_session" or "ownership" in code else 409
            raise HTTPException(status, detail={"code": "PET_MEDIA_" + code.upper()},
                                headers={"Cache-Control": "no-store"}) from None

    def envelope(method, session_id, **values):
        return {"media": call(method, session_id, **values), "local_only": True, "cloud_transmission_allowed": False}

    def audio(method, session_id, media_id, audio_sha256, request, **scope):
        if request.headers.get("Range") is not None:
            raise HTTPException(416, detail={"code": "PET_MEDIA_FULL_AUDIO_REQUIRED"},
                                headers={"Cache-Control": "no-store", "Accept-Ranges": "none"})
        content = call(method, session_id, media_id=media_id, audio_sha256=audio_sha256, **scope)
        return Response(content, media_type="audio/wav", headers={
            "Cache-Control": "no-store", "Accept-Ranges": "none", "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin", "Content-Security-Policy": "default-src 'none'",
            "Content-Disposition": 'inline; filename="pet-original.wav"', "X-HSC-Audio-SHA256": audio_sha256,
        })

    @router.post("/listening/begin")
    def begin(session_id: str, payload: PetListeningBegin):
        return envelope("begin_listening", session_id, **payload.model_dump())

    @router.post("/prompt/{prompt_id}/materialize", dependencies=[Depends(parent)])
    def materialize(session_id: str, prompt_id: str, payload: PetMaterialize):
        return {"material": call("materialize_listening", session_id, prompt_id=prompt_id, **payload.model_dump()),
                "local_only": True, "cloud_transmission_allowed": False}

    @router.post("/speaking/bind")
    def bind(session_id: str, payload: PetSpeakingBind):
        return envelope("bind_speaking", session_id, **payload.model_dump())

    @router.post("/listening/control")
    def control(session_id: str, payload: PetPlaybackControl):
        return envelope("control_listening", session_id, **payload.model_dump())

    @router.post("/listening/agent-control")
    def agent_control(session_id: str, payload: PetAgentControl):
        return call("control_from_agent", session_id, **payload.model_dump())

    @router.post("/listening/playback")
    def playback(session_id: str, payload: PetPlaybackReceipt, response: Response):
        result = envelope("record_playback", session_id, **payload.model_dump())
        if result["media"]["status"] == "pending_receipt":
            response.status_code = 202
        return result

    @router.get("/{media_id}")
    def read(session_id: str, media_id: str):
        return envelope("read", session_id, media_id=media_id)

    @router.get("/{media_id}/action-receipt")
    def action_receipt(session_id: str, media_id: str,
                       client_action_id: str = Query(..., min_length=1, max_length=200)):
        return call("playback_action_receipt", session_id, media_id=media_id, client_action_id=client_action_id)

    @router.get("/{media_id}/speaking-readiness")
    def speaking_readiness(session_id: str, media_id: str):
        return call("speaking_readiness", session_id, media_id=media_id)

    @router.get("/{media_id}/speaking-review", dependencies=[Depends(parent)])
    def speaking_review(session_id: str, media_id: str,
                         assessment_id: str = Query(..., min_length=1, max_length=200)):
        return call("speaking_review", session_id, media_id=media_id, assessment_id=assessment_id)

    @router.get("/{media_id}/speaking-review-audio", dependencies=[Depends(parent)])
    def speaking_review_audio(session_id: str, media_id: str, request: Request,
                               assessment_id: str = Query(..., min_length=1, max_length=200),
                               audio_sha256: str = Query(..., pattern=r"^[0-9a-f]{64}$")):
        return audio("speaking_review_audio", session_id, media_id, audio_sha256, request, assessment_id=assessment_id)

    @router.get("/prompt/{prompt_id}")
    def prompt_media(session_id: str, prompt_id: str,
                     item_version: str = Query(..., min_length=1, max_length=200)):
        return call("list_for_prompt", session_id, prompt_id=prompt_id, item_version=item_version)

    @router.post("/{media_id}/source-review", dependencies=[Depends(parent)])
    def review(session_id: str, media_id: str, payload: PetSourceReview):
        return envelope("review_listening_source", session_id, media_id=media_id,
                        reviewer_id="household-parent", reviewer_role="parent", **payload.model_dump())

    @router.post("/{media_id}/assessment")
    def assessment(session_id: str, media_id: str, payload: PetAssessmentReference):
        return envelope("attach_assessment", session_id, media_id=media_id, **payload.model_dump())

    @router.get("/{media_id}/control-reference")
    def control_reference(session_id: str, media_id: str, request: Request,
                          audio_sha256: str = Query(..., pattern=r"^[0-9a-f]{64}$")):
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, detail={"code": "LOCAL_CONTROL_LOOPBACK_REQUIRED"})
        return call("playback_control_reference", session_id, media_id=media_id, audio_sha256=audio_sha256)

    @router.get("/{media_id}/audio")
    def listening_audio(session_id: str, media_id: str, request: Request,
                        audio_sha256: str = Query(..., pattern=r"^[0-9a-f]{64}$")):
        return audio("read_listening_audio", session_id, media_id, audio_sha256, request)

    @router.get("/{media_id}/review-audio", dependencies=[Depends(parent)])
    def review_audio(session_id: str, media_id: str, request: Request,
                     audio_sha256: str = Query(..., pattern=r"^[0-9a-f]{64}$")):
        return audio("review_listening_audio", session_id, media_id, audio_sha256, request)

    return router
