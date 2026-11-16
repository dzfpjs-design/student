"""Household-parent review of local, versioned open-answer evidence."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .learning_rubric import RubricEvidenceService
from .agent_runtime import AgentRuntimeStore


class Dimension(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    dimension_id: str = Field(min_length=1, max_length=80)
    score: int = Field(ge=0, le=3)
    response_quotes: list[str] = Field(max_length=20)
    source_quotes: Optional[list[str]] = Field(default=None, max_length=20)
    rationale: str = Field(min_length=1, max_length=800)


class Criterion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    criterion: str = Field(min_length=1, max_length=1000)
    response_quote: str = Field(min_length=1, max_length=2000)


class AudioObservation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    dimension_id: Literal['audio_text_alignment', 'intelligibility', 'pronunciation', 'prosody', 'fluency']
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    heard_text: str = Field(min_length=1, max_length=800)
    rationale: str = Field(min_length=1, max_length=800)


class AudioReferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_schema: Literal['pet_speaking_audio_references_v1'] = Field(alias='schema')
    binding_version: str = Field(min_length=1, max_length=200)
    audio_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    observations: list[AudioObservation] = Field(min_length=5, max_length=5)


class AudioEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    media_id: str = Field(min_length=1, max_length=200)
    review: AudioReferences


class WritingTaskPoint(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    task_point: str = Field(min_length=1, max_length=400)
    response_quote: str = Field(min_length=1, max_length=12000)


class WritingEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    binding_version: str = Field(pattern=r'^[0-9a-f]{64}$')
    task_point_evidence: list[WritingTaskPoint] = Field(min_length=1, max_length=12)


class VisualDisplay(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    material_key: str = Field(min_length=1, max_length=200)
    display_receipt_id: str = Field(min_length=1, max_length=200)


class VisualEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    assessment_id: str = Field(min_length=1, max_length=200)
    assessment_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    binding_version: str = Field(pattern=r'^[0-9a-f]{64}$')
    displays: list[VisualDisplay] = Field(min_length=1, max_length=8)


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=0)
    client_action_id: str = Field(min_length=1, max_length=200)
    operation: Literal['approve', 'retract']
    reason: str = Field(min_length=1, max_length=2000)
    dimensions: Optional[list[Dimension]] = Field(default=None, max_length=12)
    criterion_evidence: Optional[list[Criterion]] = Field(default=None, max_length=50)
    independent: Optional[bool] = None
    hint_level: Optional[int] = Field(default=None, ge=0, le=5)
    review_profile: Optional[Literal['pet_speaking_audio_v1', 'pet_writing_text_v1', 'school_open_written_v1']] = None
    audio_evidence: Optional[AudioEvidence] = None
    writing_evidence: Optional[WritingEvidence] = None
    visual_evidence: Optional[VisualEvidence] = None


def create_learning_rubric_router(database, parent_auth, runtime=None):
    service = RubricEvidenceService(database)
    service.initialize()
    runtime = runtime or AgentRuntimeStore(database)

    def authorized(request: Request, response: Response):
        parent_auth.require(request.headers.get('x-hsc-parent'))
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(prefix='/v1/parent/children/{child_id}/learning-rubrics', dependencies=[Depends(authorized)])

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (KeyError, ValueError) as error:
            code = str(error).strip("'")
            raise HTTPException(409, detail={'code': code if code.startswith('RUBRIC_') else 'RUBRIC_REVIEW_CONFLICT',
                'message': '原题、回答或审核版本已变化，请重新核对；没有把本次审核用于另一份回答。'}) from None

    def owned_child(child_id):
        with database.connect() as c:
            if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
                raise HTTPException(404, detail={'code': 'CHILD_NOT_FOUND'})

    @router.get('')
    def listing(child_id: str, offset: int = 0, limit: int = 30):
        owned_child(child_id)
        if not 0 <= offset or not 1 <= limit <= 100:
            raise HTTPException(422, detail={'code': 'RUBRIC_PAGE_INVALID'})
        with database.connect() as c:
            c.execute('BEGIN')
            total = c.execute('SELECT COUNT(*) FROM rubric_assessment WHERE child_id=?', (child_id,)).fetchone()[0]
            ids = c.execute('SELECT assessment_id FROM rubric_assessment WHERE child_id=? ORDER BY created_at DESC,assessment_id LIMIT ? OFFSET ?',
                            (child_id, limit, offset)).fetchall()
            items = [{**call(service.get, child_id, row[0], connection=c),
                      'qualification': call(service.qualification, child_id, row[0], connection=c)} for row in ids]
        return {'child_id': child_id, 'items': items, 'total': total, 'offset': offset,
                'local_only': True, 'cloud_transmission_allowed': False}

    @router.get('/{assessment_id}')
    def detail(child_id: str, assessment_id: str):
        result = call(service.get, child_id, assessment_id)
        return {'assessment': result, 'history': call(service.history, child_id, assessment_id),
                'qualification': call(service.qualification, child_id, assessment_id),
                'audio_review': call(service.audio_context, child_id, assessment_id),
                'writing_review': call(service.writing_context, child_id, assessment_id),
                'local_only': True, 'cloud_transmission_allowed': False}

    @router.get('/{assessment_id}/audio')
    def original_audio(child_id: str, assessment_id: str, request: Request, media_id: str, audio_sha256: str):
        if request.headers.get('range'):
            raise HTTPException(416, detail={'code': 'RUBRIC_FULL_ORIGINAL_AUDIO_REQUIRED'})
        from .pet_speaking_review import read_speaking_review_audio
        from .database import local_media_scope
        with database.connect() as c:
            c.execute('BEGIN')
            current = call(service.get, child_id, assessment_id, connection=c)
            scope = local_media_scope(c)
            if not scope:
                raise HTTPException(409, detail={'code': 'RUBRIC_ORIGINAL_AUDIO_RUNTIME_UNAVAILABLE'})
            content = call(read_speaking_review_audio, c, current['session_id'], media_id=media_id,
                assessment_id=assessment_id, audio_sha256=audio_sha256, **scope)
        return Response(content, media_type='audio/wav', headers={'Cache-Control': 'no-store',
            'Accept-Ranges': 'none', 'X-Content-Type-Options': 'nosniff', 'Cross-Origin-Resource-Policy': 'same-origin',
            'Content-Security-Policy': "default-src 'none'", 'X-HSC-Audio-SHA256': audio_sha256})

    @router.post('/{assessment_id}/review')
    def review(child_id: str, assessment_id: str, payload: Review):
        event = call(service.review, child_id, assessment_id,
                     reviewer_id='household-parent', reviewer_role='parent', **payload.model_dump(exclude_none=True, by_alias=True))
        # The review is durable before reconciliation. A lost response can use
        # the same key to recover it and retry the projection, never a new score.
        runtime.reconcile_learning_state(child_id)
        current = detail(child_id, assessment_id)
        snapshot = current['assessment']
        transition = {'status': 'not_applied', 'local_only': True, 'cloud_transmission_allowed': False}
        if (payload.operation == 'approve' and snapshot['score_status'] == 'reviewed_human'
                and snapshot.get('source_kind') != 'exam_open_review'
                and event['review_revision'] == snapshot['review_revision']):
            gate = call(runtime.inspect_learning_gate, child_id, snapshot['objective_id'])
            transition = call(runtime.apply_learning_gate, child_id, snapshot['objective_id'],
                expected_gate_fingerprint=gate['gate_fingerprint'],
                expected_objective_updated_at=gate['expected_objective_updated_at'])
        return {'event': event, **current, 'learning_transition': transition}

    return router
