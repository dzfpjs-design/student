"""Parent-authenticated, local-only adapter; the host owns mounting and privacy.

    app.include_router(create_curriculum_supply_router(service, parent_auth))

Uses the host's existing ParentAuth instance and X-HSC-Parent sessions. There is
one household parent principal, not fabricated independent human identities.
No gateway, remote destination, actor override, file write or cloud export exists.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import sqlite3
from typing import Annotated, Literal, Optional, Protocol, Union

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .curriculum_supply import CurriculumSupplyService
from .database import ExternalEffectBusyError
from .family_clock import family_today
from .local_supply import LocalSupplyError, validate_item
from .parent_auth import ParentSessionRequiredError


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=1200, strip_whitespace=True)]
Note = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
Version = Annotated[int, Field(strict=True, ge=0)]
Stage = Literal["parallel", "near_transfer", "representation_transfer", "discrimination", "transfer", "far_transfer"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Scope(StrictModel):
    grade: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    subject: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    edition: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    semester: Literal["上学期", "下学期"]
    standard_scope: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    textbook_source_id: Identifier


class Command(StrictModel):
    action_id: Identifier
    expected_version: Version


class SelectRequest(Command):
    scope: Scope


class SourceSpec(StrictModel):
    seed_item_id: Identifier
    stage: Stage
    excerpt_id: Identifier
    requirement_id: Identifier
    standard_page: Annotated[int, Field(strict=True, ge=1, le=100000)]
    standard_quote: Annotated[str, StringConstraints(min_length=8, max_length=1200, strip_whitespace=True)]
    validator: Identifier
    explanation: Text
    difficulty: Annotated[int, Field(strict=True, ge=1, le=5)]


class GenerateRequest(Command):
    expected_version: Annotated[int, Field(strict=True, ge=0, le=0)]
    expected_scope_version: Annotated[int, Field(strict=True, ge=1)]
    source: SourceSpec
    supersedes: Optional[Identifier] = None
    supersedes_version: Optional[Version] = None

    @model_validator(mode="after")
    def revision_pair(self):
        if (self.supersedes is None) != (self.supersedes_version is None):
            raise ValueError("revision requires both supersedes and supersedes_version")
        return self


class FirstSourceSpec(StrictModel):
    knowledge_node_id: Identifier
    excerpt_id: Identifier
    requirement_id: Identifier
    standard_page: Annotated[int, Field(strict=True, ge=1, le=100000)]
    standard_quote: Annotated[str, StringConstraints(min_length=8, max_length=1200, strip_whitespace=True)]
    explanation: Text
    difficulty: Annotated[int, Field(strict=True, ge=1, le=5)]


class FirstGenerateRequest(Command):
    expected_version: Annotated[int, Field(strict=True, ge=0, le=0)]
    expected_scope_version: Annotated[int, Field(strict=True, ge=1)]
    source: FirstSourceSpec


class StageReview(StrictModel):
    schema_: Literal['specialized_stage_review_v1'] = Field(alias='schema')
    stage: Literal['near_transfer', 'far_transfer']
    binding_version: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    source_quote: Annotated[str, StringConstraints(min_length=4, max_length=1200)]
    candidate_quote: Annotated[str, StringConstraints(min_length=4, max_length=1200)]
    same_skill_rationale: Annotated[str, StringConstraints(min_length=12, max_length=1200)]
    changed_context_rationale: Annotated[str, StringConstraints(min_length=12, max_length=1200)]


class ReviewRequest(Command):
    decision: Literal["approve", "reject"]
    note: Note
    stage_review: Optional[StageReview] = None


class WithdrawRequest(Command):
    note: Note


class LocalResponse(StrictModel):
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]


class SelectionResponse(LocalResponse):
    version: int
    scope: Scope


class PetExactReference(StrictModel):
    local_rule: Literal["local_exact_v1"]
    exact_answers: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=200)]], Field(min_length=1, max_length=24)]


class PetCompiledReference(PetExactReference):
    answer_format: Literal["pet_exact_text_v1"]


class ReviewedCompiledReference(PetExactReference):
    answer_format: Literal['reviewed_exact_text_v1']


class ReviewedNumericTupleReference(PetExactReference):
    answer_format: Literal['reviewed_spoken_numeric_tuple_v1']
    numeric_kind: Literal['integer','rational']
    tuple_size: Annotated[int,Field(strict=True,ge=2,le=8)]


class ObjectiveSource(StrictModel):
    source_schema: Literal['curriculum_objective_source_v1'] = Field(alias='schema')
    subject: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    grade: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    knowledge_node_id: Identifier
    assessment_mode: Literal['objective']
    answer_type: Literal['text']
    prompt: Text
    expected_answer: Union[PetExactReference,ReviewedNumericTupleReference]


class PetListeningSource(StrictModel):
    source_schema: Literal["pet_listening_source_v1"] = Field(alias="schema")
    modality: Literal["listening"]
    assessment_mode: Literal["objective"]
    prompt: Text
    stimulus_text: Annotated[str, StringConstraints(min_length=1, max_length=3000)]
    instruction: Text
    required_play_count: Annotated[int, Field(strict=True, ge=1, le=3)]
    expected_answer: PetExactReference


class SchoolListeningSource(PetListeningSource):
    source_schema: Literal['school_listening_source_v1'] = Field(alias='schema')
    subject: Literal['英语']
    grade: Literal['初三']
    knowledge_node_id: Identifier


class ListeningDeliveryUnit(StrictModel):
    unit_key: Annotated[str,StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$')]
    member_keys: Annotated[list[Annotated[str,StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$')]],Field(min_length=1,max_length=50)]
    original_audio_key: Annotated[str,StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$')]
    required_play_count: Annotated[int,Field(strict=True,ge=1,le=3)]


class PetListeningSourceV2(PetListeningSource):
    source_schema: Literal['pet_listening_source_v2'] = Field(alias='schema')
    question_key: Annotated[str,StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$')]
    delivery_unit: ListeningDeliveryUnit


class SchoolListeningSourceV2(PetListeningSourceV2):
    source_schema: Literal['school_listening_source_v2'] = Field(alias='schema')
    subject: Literal['英语']
    grade: Literal['初三']
    knowledge_node_id: Identifier


# The legacy envelope name remains compatible; schemas keep school/PET authority distinct.
ListeningSource = Union[PetListeningSource, SchoolListeningSource,PetListeningSourceV2,SchoolListeningSourceV2]


class WritingReference(StrictModel):
    task_points: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=400)]], Field(min_length=1, max_length=12)]
    min_words: Annotated[int, Field(strict=True, ge=1, le=1500)]
    max_words: Annotated[int, Field(strict=True, ge=1, le=1500)]
    review_profile: Literal['pet_writing_text_v1']


class PetWritingSource(StrictModel):
    source_schema: Literal['pet_writing_source_v1'] = Field(alias='schema')
    modality: Literal['writing']
    assessment_mode: Literal['human_rubric']
    subject: Literal['英语']
    grade: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    knowledge_node_id: Identifier
    prompt: Text
    task_points: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=400)]], Field(min_length=1, max_length=12)]
    min_words: Annotated[int, Field(strict=True, ge=1, le=1500)]
    max_words: Annotated[int, Field(strict=True, ge=1, le=1500)]


class OpenResponseReference(StrictModel):
    task_points: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=400)]], Field(min_length=1, max_length=12)]
    review_profile: Identifier
    profile_version: Literal['school_open_response_profiles_v1', 'school_primary_open_response_profiles_v1']
    task_family: Identifier
    response_mode: Literal['written', 'spoken_explanation']
    context_requirement: Literal['self_contained', 'external_material']


class OralStimulusBinding(StrictModel):
    diagnostic_item_id: Identifier
    source_version: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
    stimulus_asset: Identifier


class PetOralSource(StrictModel):
    source_schema: Literal['pet_speaking_source_v1'] = Field(alias='schema')
    subject: Literal['英语']
    grade: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    knowledge_node_id: Identifier
    answer_type: Literal['speaking', 'read_aloud']
    assessment_mode: Literal['pet_reference_rubric']
    task_family: Literal['topic_response', 'picture_description', 'read_aloud', 'interaction',
                         'extended_turn', 'daily_dialogue', 'retell']
    prompt: Text
    task_points: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=400)]], Field(min_length=1, max_length=12)]
    stimulus_text: Optional[Annotated[str, StringConstraints(min_length=1, max_length=6000)]]
    stimulus_binding: Optional[OralStimulusBinding]

    @model_validator(mode='after')
    def valid_original(self):
        from .pet_oral_source import parse
        parse(self.model_dump_json(by_alias=True))
        return self


class OralReference(StrictModel):
    task_points: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=400)]], Field(min_length=1, max_length=12)]
    task_family: Identifier
    source_schema: Literal['pet_speaking_source_v1']
    reference_scope: Literal['oral_content_reference_only']


class OpenResponseSource(StrictModel):
    source_schema: Literal['reviewed_open_response_source_v1'] = Field(alias='schema')
    subject: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    grade: Literal['初三', '四年级']
    knowledge_node_id: Identifier
    answer_type: Literal['open_response']
    assessment_mode: Literal['open_response_rubric']
    task_family: Identifier
    response_mode: Literal['written', 'spoken_explanation']
    prompt: Text
    task_points: Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=400)]], Field(min_length=1, max_length=12)]
    context_requirement: Literal['self_contained', 'external_material']

    @model_validator(mode='after')
    def valid_original(self):
        from .open_response_source import parse
        parse(self.model_dump_json(by_alias=True))
        return self


class OpenMaterialBinding(StrictModel):
    material_key: Annotated[str, StringConstraints(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,39}$')]
    source_id: Identifier
    source_version: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
    excerpt_id: Identifier
    excerpt_hash: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
    span_start: Annotated[int, Field(strict=True, ge=0, le=0)]
    span_end: Annotated[int, Field(strict=True, ge=1, le=1000)]
    text_hash: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]


class OpenMaterialSource(OpenResponseSource):
    source_schema: Literal['reviewed_open_response_source_v2'] = Field(alias='schema')
    context_requirement: Literal['external_material']
    material_semantics: Literal['text_only', 'image_required']
    material_bindings: Annotated[list[OpenMaterialBinding], Field(min_length=1, max_length=8)]


class OpenMaterialReference(OpenResponseReference):
    context_requirement: Literal['external_material']
    material_schema: Literal['reviewed_open_response_source_v2']
    material_semantics: Literal['text_only', 'image_required']
    material_bindings: Annotated[list[OpenMaterialBinding], Field(min_length=1, max_length=8)]


class VisualBinding(StrictModel):
    artifact_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    revision_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    region_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    region_version: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
    facts_version: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
    confirmation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class VisualMaterialBinding(OpenMaterialBinding):
    visual: VisualBinding


class OpenVisualSource(OpenResponseSource):
    source_schema: Literal['reviewed_open_response_source_v3'] = Field(alias='schema')
    grade: Literal['四年级', '初三']
    context_requirement: Literal['external_material']
    material_semantics: Literal['image_required']
    material_bindings: Annotated[list[VisualMaterialBinding], Field(min_length=1, max_length=8)]

    @model_validator(mode='after')
    def valid_original(self):
        from .open_response_source import parse
        parse(self.model_dump_json(by_alias=True))
        return self


class OpenVisualReference(OpenResponseReference):
    profile_version: Literal['school_open_response_profiles_v1', 'school_visual_response_profiles_v1']
    context_requirement: Literal['external_material']
    material_schema: Literal['reviewed_open_response_source_v3']
    material_semantics: Literal['image_required']
    material_bindings: Annotated[list[VisualMaterialBinding], Field(min_length=1, max_length=8)]


class DraftPayload(SourceSpec):
    prompt: Text
    expected_answer: Optional[str]
    pet_listening: Optional[ListeningSource] = None
    objective_source: Optional[ObjectiveSource] = None
    pet_writing: Optional[PetWritingSource] = None
    pet_oral: Optional[PetOralSource] = None
    open_response: Optional[Union[OpenResponseSource, OpenMaterialSource, OpenVisualSource]] = None


class FirstDraftPayload(FirstSourceSpec):
    seed_item_id: None
    stage: Literal['parallel']
    validator: Literal['curriculum_objective_exact_source_v1']
    prompt: Text
    expected_answer: None
    objective_source: ObjectiveSource


class FirstOpenDraftPayload(FirstSourceSpec):
    seed_item_id: None
    stage: Literal['parallel']
    validator: Literal['reviewed_open_response_rubric_source_v1']
    prompt: Text
    expected_answer: None
    open_response: Union[OpenResponseSource, OpenMaterialSource, OpenVisualSource]


class PackageSummary(StrictModel):
    candidate_id: str
    state: Literal["draft", "blocked", "validated", "reviewed", "published", "withdrawn", "rejected"]
    version: int
    eligible: bool
    blockers: list[str]
    next_action: str


class CheckResult(StrictModel):
    validator: str
    computed_answer: str
    question_hash: str
    payload_hash: str
    expected_answer: Optional[Union[PetCompiledReference, ReviewedCompiledReference, ReviewedNumericTupleReference, WritingReference, OpenResponseReference, OpenMaterialReference, OpenVisualReference, OralReference]] = None
    pet_listening: Optional[ListeningSource] = None
    pet_writing: Optional[PetWritingSource] = None
    pet_oral: Optional[PetOralSource] = None
    qualification: Optional[Literal["original_reference_checked_not_computed_or_skill_certified"]] = None
    open_response: Optional[Union[OpenResponseSource, OpenMaterialSource, OpenVisualSource]] = None
    answer_type: Optional[Literal['open_response']] = None
    assessment_mode: Optional[Literal['open_response_rubric']] = None
    answer_verification: Optional[Literal['source_reference_review_only']] = None
    mastery_claim_allowed: Optional[Literal[False]] = None
    stage_qualification_allowed: Optional[Literal[False]] = None
    local_only: Optional[Literal[True]] = None
    cloud_transmission_allowed: Optional[Literal[False]] = None


class EventResult(StrictModel):
    stage_review: Optional[StageReview] = None
    state: str
    blockers: list[str]
    check: Optional[CheckResult] = None
    decision: Optional[Literal["approve", "reject"]] = None
    note: Optional[str] = None


class Event(StrictModel):
    version: int
    kind: str
    actor: str
    created_at: str
    result: EventResult


class TextbookEvidence(StrictModel):
    source_id: str
    title: str
    source_ref: str
    version_label: str
    review_status: str
    excerpt_id: str
    content_text: str


class StandardEvidence(StrictModel):
    source_id: str
    official_url: str
    page_number: int
    requirement_text: str
    quote: str


class ParentMaterialText(StrictModel):
    material_key: str
    title: str
    version_label: str
    heading: Optional[str]
    text: Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class ParentCurrentMaterials(StrictModel):
    status: Literal['ready', 'blocked']
    reason: Optional[str]
    materials: Annotated[list[ParentMaterialText], Field(max_length=8)]
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]
    mastery_claim_allowed: Literal[False]


class ReviewPacket(StrictModel):
    fingerprint: str
    scope: Scope
    textbook: TextbookEvidence
    standard: StandardEvidence
    review_scope: str
    pet_listening: Optional[ListeningSource] = None
    pet_writing: Optional[PetWritingSource] = None
    pet_oral: Optional[PetOralSource] = None
    open_response: Optional[Union[OpenResponseSource, OpenMaterialSource, OpenVisualSource]] = None
    current_materials: Optional[ParentCurrentMaterials] = None


class PackageResponse(PackageSummary, LocalResponse):
    stage_review_context: Optional[dict] = None
    child_id: str
    supersedes: Optional[str]
    diagnostic_item_id: Optional[str]
    payload: Union[DraftPayload, FirstDraftPayload, FirstOpenDraftPayload]
    review_packet: Optional[ReviewPacket]
    events: list[Event]


class SelectedScope(StrictModel):
    version: int
    scope: Scope


class AuditPackageSummary(PackageSummary):
    prompt: str


class AuditResponse(LocalResponse):
    child_id: str
    current_curriculum: list[SelectedScope]
    blocked_job_counts: dict[str, int]
    eligible_curriculum_items: int
    qualified_stages: list[Stage]
    packages: list[AuditPackageSummary]
    thinking_or_transfer_coverage_claim_allowed: Literal[False]


class SourceOption(StrictModel):
    source_id: str
    title: str
    subject: str
    edition: Optional[str]
    available: bool
    blockers: list[str]
    excerpt_count: int


class OptionsResponse(LocalResponse):
    child_id: str
    grade: str
    semesters: list[str]
    sources: list[SourceOption]


class ExcerptOption(StrictModel):
    excerpt_id: str
    sequence: int
    text: str
    current_materials: Optional[ParentCurrentMaterials] = None


class StandardOption(StrictModel):
    requirement_id: str
    title: str
    stage_scope: str
    requirement_text: str
    source_title: str
    page_number: int
    quote: str
    available: bool
    blockers: list[str]


class SeedOption(StrictModel):
    seed_item_id: str
    title: str
    prompt: str
    validator: str
    stages: list[Stage]


class FirstSourceOption(StrictModel):
    knowledge_node_id: str
    title: str
    excerpt_id: str
    prompt: str


class SourceOptionsResponse(LocalResponse):
    child_id: str
    source_id: str
    excerpts: list[ExcerptOption]
    standards: list[StandardOption]
    seeds: list[SeedOption]
    first_sources: list[FirstSourceOption] = Field(default_factory=list)


class ParentAuthPort(Protocol):
    def require(self, token: Optional[str]) -> None: ...


@dataclass(frozen=True)
class ParentPrincipal:
    actor: str = "parent"


def create_parent_dependency(parent_auth: ParentAuthPort):
    """Inject an actual host auth provider, not a principal from request data."""
    def parent_required(request: Request, response: Response) -> ParentPrincipal:
        try:
            parent_auth.require(request.headers.get("X-HSC-Parent"))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail={"code": "PARENT_SESSION_REQUIRED"},
                                headers={"Cache-Control": "no-store"}) from None
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail={"code": "CURRICULUM_LOCAL_ONLY"})
        if request.query_params:
            raise HTTPException(422, detail={"code": "CURRICULUM_QUERY_NOT_SUPPORTED"})
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return ParentPrincipal()
    return parent_required


def _call(operation, *args, **kwargs):
    try:
        return {**operation(*args, **kwargs), "local_only": True, "cloud_transmission_allowed": False}
    except ExternalEffectBusyError:
        raise HTTPException(503, detail={"code": "CURRICULUM_BUSY"}, headers={"Retry-After": "1", "Cache-Control": "no-store"}) from None
    except KeyError:
        raise HTTPException(404, detail={"code": "CURRICULUM_RESOURCE_NOT_FOUND"}) from None
    except LocalSupplyError as error:
        code = str(error)
        status = 409 if code.endswith("CONFLICT") else 422
        raise HTTPException(status, detail={"code": code}, headers={"Cache-Control": "no-store"}) from None
    except sqlite3.OperationalError as error:
        if "locked" not in str(error).lower():
            raise
        raise HTTPException(503, detail={"code": "CURRICULUM_BUSY"}, headers={"Retry-After": "1"}) from None
    except OSError:
        raise HTTPException(503, detail={"code": "CURRICULUM_STORAGE_UNAVAILABLE"},
                            headers={"Retry-After": "1", "Cache-Control": "no-store"}) from None


def create_curriculum_supply_router(service: CurriculumSupplyService, parent_auth: ParentAuthPort) -> APIRouter:
    router = APIRouter(prefix="/api/parent/children/{child_id}/curriculum-supply", tags=["curriculum-supply"])
    parent_required = create_parent_dependency(parent_auth)

    def owned_child(child_id: Identifier, principal: ParentPrincipal = Depends(parent_required)) -> str:
        with service.database.connect() as c:
            if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
                raise HTTPException(404, detail={"code": "CURRICULUM_RESOURCE_NOT_FOUND"})
        return child_id

    def source_owned(child_id, *, source_id=None, excerpt_id=None, seed_item_id=None):
        with service.database.connect() as c:
            if source_id:
                row = c.execute("SELECT child_id FROM knowledge_source WHERE source_id=?", (source_id,)).fetchone()
                if not row or row[0] != child_id:
                    raise HTTPException(404, detail={"code": "CURRICULUM_RESOURCE_NOT_FOUND"})
            if excerpt_id:
                row = c.execute("SELECT ks.child_id FROM knowledge_excerpt ke JOIN knowledge_source ks USING(source_id) WHERE excerpt_id=?", (excerpt_id,)).fetchone()
                # Missing material may become a blocked draft; another child's may not.
                if row and row[0] != child_id:
                    raise HTTPException(404, detail={"code": "CURRICULUM_RESOURCE_NOT_FOUND"})
            if seed_item_id:
                try:
                    validate_item(c, child_id, seed_item_id)
                except LocalSupplyError:
                    raise HTTPException(404, detail={"code": "CURRICULUM_RESOURCE_NOT_FOUND"}) from None

    @router.get("/options", response_model=OptionsResponse)
    def options(child: str = Depends(owned_child)):
        with service.database.connect() as c:
            grade = c.execute("SELECT grade FROM child_profile WHERE child_id=?", (child,)).fetchone()[0]
            sources = []
            today = family_today().isoformat()
            for row in c.execute("""SELECT ks.*,(SELECT COUNT(*) FROM knowledge_excerpt ke WHERE ke.source_id=ks.source_id) AS excerpt_count
                FROM knowledge_source ks WHERE child_id=? AND grade_band=? ORDER BY imported_at DESC,source_id""", (child, grade)):
                blockers = []
                if row["review_status"] != "approved" or row["license_status"] not in {"public", "family_owned_index"}:
                    blockers.append("CURRICULUM_TEXTBOOK_UNREVIEWED")
                if not row["version_label"] or row["source_type"] not in {"textbook_index", "school_material", "curriculum"}:
                    blockers.append("CURRICULUM_SCOPE_MISMATCH")
                if (row["effective_from"] and row["effective_from"] > today) or (row["effective_to"] and row["effective_to"] < today):
                    blockers.append("CURRICULUM_TEXTBOOK_EXPIRED")
                if not row["excerpt_count"]:
                    blockers.append("CURRICULUM_TEXTBOOK_UNAVAILABLE")
                sources.append({"source_id": row["source_id"], "title": row["title"], "subject": row["subject"],
                    "edition": row["version_label"], "available": not blockers, "blockers": blockers, "excerpt_count": row["excerpt_count"]})
        return {"child_id": child, "grade": grade, "semesters": ["上学期", "下学期"], "sources": sources,
                "local_only": True, "cloud_transmission_allowed": False}

    @router.get("/oral-authoring")
    def oral_authoring(child: str = Depends(owned_child)):
        from .pet_oral_authoring import authoring_options
        with service.database.connect() as c:
            c.execute('BEGIN')
            return _call(authoring_options, c, child)

    @router.get("/sources/{source_id}/options", response_model=SourceOptionsResponse)
    def source_options(source_id: Identifier, child: str = Depends(owned_child)):
        source_owned(child, source_id=source_id)
        with service.database.connect() as c:
            c.execute('BEGIN')
            source = c.execute("SELECT * FROM knowledge_source WHERE source_id=? AND child_id=?", (source_id, child)).fetchone()
            if source is None:
                raise HTTPException(404, detail={"code": "CURRICULUM_RESOURCE_NOT_FOUND"})
            excerpts = [{"excerpt_id": row["excerpt_id"], "sequence": row["sequence"], "text": row["content_text"]}
                        for row in c.execute("SELECT * FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence", (source_id,))]
            standards = []
            for row in c.execute("""SELECT req.*,css.title AS source_title,css.status AS source_status,
                page.page_number,page.content_text FROM curriculum_standard_requirement req
                JOIN curriculum_standard_source css USING(source_id)
                LEFT JOIN curriculum_standard_page page ON page.source_id=req.source_id
                    AND page.page_number BETWEEN req.page_start AND req.page_end
                WHERE req.subject_title=? ORDER BY req.requirement_id,page.page_number""", (source["subject"],)):
                import json
                from .manual_standard_scope import ManualScopeError, manual_requirement
                linked = (c.execute("SELECT * FROM curriculum_manual_requirement WHERE requirement_id=?",
                                    (row["requirement_id"],)).fetchone()
                          if c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_manual_requirement'").fetchone() else None)
                if linked is not None:
                    if linked["child_id"] != child:
                        continue
                    approved = json.loads(linked["payload_json"])
                    if row["page_number"] != approved["page_number"]:
                        continue
                    quote, blockers = approved["quote"], []
                    try:
                        manual_requirement(c, child, row["requirement_id"], row["page_number"], quote,
                                           knowledge_node_id=approved["knowledge_node_id"])
                    except ManualScopeError as e:
                        blockers.append(e.code)
                else:
                    quote = next((text for text in (row["title"], row["requirement_text"])
                                  if 8 <= len(text) <= 1200 and text in (row["content_text"] or "")), "")
                    blockers = []
                    if row["maturity"] != "verified":
                        blockers.append("CURRICULUM_STANDARD_UNREVIEWED")
                    if not quote or row["source_status"] not in {"ocr_ready", "structured"}:
                        blockers.append("CURRICULUM_STANDARD_QUOTE_MISMATCH")
                standards.append({"requirement_id": row["requirement_id"], "title": row["title"],
                    "stage_scope": row["stage_scope"], "requirement_text": row["requirement_text"],
                    "source_title": row["source_title"], "page_number": row["page_number"] or row["page_start"],
                    "quote": quote, "available": not blockers, "blockers": blockers})
            seeds = []
            from . import curriculum_objective_source as objective
            from . import pet_writing_source as writing
            from . import open_response_source as opened
            from . import pet_oral_source as oral
            objective_nodes = set()
            writing_nodes = set()
            open_sources = {}
            oral_sources = {}
            for excerpt in excerpts:
                try:
                    original_oral = oral.parse(excerpt['text'])
                    if (original_oral['subject'], original_oral['grade']) == (source['subject'], source['grade_band']):
                        oral_sources.setdefault(original_oral['knowledge_node_id'], []).append(original_oral)
                except LocalSupplyError:
                    pass
                try:
                    original_open = opened.parse(excerpt['text'])
                    if (original_open['subject'],original_open['grade']) == (source['subject'],source['grade_band']):
                        open_sources.setdefault(original_open['knowledge_node_id'],[]).append(original_open)
                        if original_open['schema'] == opened.MATERIAL_SCHEMA:
                            from .open_response_materials import parent_source_materials
                            excerpt['current_materials'] = parent_source_materials(c, child_id=child, source=original_open)
                except LocalSupplyError:
                    pass
                try:
                    original_writing = writing.parse(excerpt['text'])
                    if (original_writing['subject'], original_writing['grade']) == (source['subject'], source['grade_band']):
                        writing_nodes.add(original_writing['knowledge_node_id'])
                except LocalSupplyError:
                    pass
                try:
                    original = objective.parse(excerpt['text'])
                except LocalSupplyError:
                    continue
                if (original['subject'], original['grade']) == (source['subject'], source['grade_band']):
                    objective_nodes.add(original['knowledge_node_id'])
            for row in c.execute("""SELECT di.diagnostic_item_id,di.prompt,di.answer_type,kn.knowledge_node_id,
                kn.title,kn.track,kn.code,kn.subject,kn.grade_band,kn.status FROM diagnostic_item di
                JOIN knowledge_node kn USING(knowledge_node_id)
                WHERE kn.subject=?
                  AND (kn.grade_band=? OR kn.grade_band LIKE ? ESCAPE '\\')
                  AND di.status='active'
                ORDER BY di.diagnostic_item_id""", (
                    source["subject"], source["grade_band"],
                    source["grade_band"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "-%",
                )):
                try:
                    validate_item(c, child, row["diagnostic_item_id"])
                except LocalSupplyError:
                    continue
                from .pet_reviewed_source import VALIDATOR
                validators = []
                if row['knowledge_node_id'] in oral_sources:
                    compatible = False
                    for original_oral in oral_sources[row['knowledge_node_id']]:
                        try:
                            if not oral.node_allowed(original_oral, row, row):
                                continue
                            seed = validate_item(c, child, row['diagnostic_item_id'])
                            oral.check(dict(validator=oral.VALIDATOR, stage='parallel',
                                prompt=original_oral['prompt'], expected_answer=None, pet_oral=original_oral), seed)
                            oral.materials(c, child, original_oral)
                            compatible = True
                        except LocalSupplyError:
                            pass
                    if compatible:
                        validators.append(oral.VALIDATOR)
                if row['knowledge_node_id'] in open_sources:
                    compatible = False
                    for original_open in open_sources[row['knowledge_node_id']]:
                        try:
                            opened.check(dict(validator=opened.VALIDATOR,stage='parallel',prompt=original_open['prompt'],
                                expected_answer=None,open_response=original_open),dict(row),node=dict(row),
                                connection=c,child_id=child)
                            if original_open['schema'] == opened.VISUAL_SCHEMA:
                                from .visual_material_host import assert_host_visual_current
                                for material in original_open['material_bindings']:
                                    assert_host_visual_current(c,child,material['visual'],require_managed=True)
                            compatible = True
                        except LocalSupplyError:
                            pass
                    if compatible:
                        validators.append(opened.VALIDATOR)
                if row['knowledge_node_id'] in objective_nodes and objective.node_allowed(row, row):
                    validators.append(objective.VALIDATOR)
                if (row['knowledge_node_id'] in writing_nodes and row['track'] == 'pet'
                      and row['code'].startswith('PET.WRITING.') and row['answer_type'] == 'writing'):
                    validators.append(writing.VALIDATOR)
                if not validators:
                    if row['knowledge_node_id'] == 'node-p4-math-arithmetic':
                        validators.append('arithmetic_expression_v1')
                    elif ((row['track'] == 'pet' and row['code'].startswith('PET.LISTENING.'))
                          or (row['track'] == 'school' and row['code'] == 'J3.ENGLISH.LISTENING'
                              and row['grade_band'] == '初三' and row['subject'] == '英语')):
                        validators.append(VALIDATOR)
                # A textbook can contain several question types for one skill.
                # Keep every compatible route; the selected excerpt determines
                # which validator is submitted and publication rechecks proof.
                for validator in validators:
                    seeds.append({"seed_item_id": row["diagnostic_item_id"], "title": row["title"], "prompt": row["prompt"],
                                  "validator": validator, "stages": ["parallel", "near_transfer", "far_transfer"] if validator in {writing.VALIDATOR, opened.VALIDATOR} else ["parallel"]})
            from .curriculum_first_source import options as first_source_options
            first_sources = first_source_options(c, child, source_id)
        return {"child_id": child, "source_id": source_id, "excerpts": excerpts, "standards": standards,
                "first_sources": first_sources,
                "seeds": seeds, "local_only": True, "cloud_transmission_allowed": False}

    @router.post("/select", response_model=SelectionResponse)
    def select(payload: SelectRequest, child: str = Depends(owned_child), principal: ParentPrincipal = Depends(parent_required)):
        source_owned(child, source_id=payload.scope.textbook_source_id)
        return _call(service.select_curriculum, child, payload.scope.model_dump(), actor=principal.actor,
                     action_id=payload.action_id, expected_version=payload.expected_version)

    @router.post("/generate", response_model=PackageResponse)
    def generate(payload: GenerateRequest, child: str = Depends(owned_child)):
        source_owned(child, excerpt_id=payload.source.excerpt_id, seed_item_id=payload.source.seed_item_id)
        return _call(service.generate_draft, child, payload.source.model_dump(),
                     author="local:reviewed_source_extractor_v1", action_id=payload.action_id,
                     expected_scope_version=payload.expected_scope_version,
                     supersedes=payload.supersedes, supersedes_version=payload.supersedes_version)

    @router.post('/first-sources', response_model=PackageResponse)
    def first_generate(payload: FirstGenerateRequest, child: str = Depends(owned_child)):
        from .curriculum_first_source import CurriculumFirstSourceService
        source_owned(child, excerpt_id=payload.source.excerpt_id)
        return _call(CurriculumFirstSourceService(service.database).generate, child, payload.source.model_dump(),
                     action_id=payload.action_id, expected_scope_version=payload.expected_scope_version)

    @router.get("/audit", response_model=AuditResponse)
    def audit(child: str = Depends(owned_child)):
        return _call(service.audit, child)

    @router.get("/{candidate_id}", response_model=PackageResponse)
    def read(candidate_id: Identifier, child: str = Depends(owned_child)):
        return _call(service.get, child, candidate_id)

    @router.get("/{candidate_id}/export", response_model=PackageResponse)
    def export(candidate_id: Identifier, request: Request, response: Response, child: str = Depends(owned_child)):
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail={"code": "CURRICULUM_LOCAL_EXPORT_ONLY"})
        response.headers["Content-Disposition"] = f'attachment; filename="curriculum-{candidate_id}.json"'
        return _call(service.get, child, candidate_id)

    @router.post("/{candidate_id}/validate", response_model=PackageResponse)
    def validate(candidate_id: Identifier, payload: Command, child: str = Depends(owned_child)):
        from .open_response_source import VALIDATOR
        package = _call(service.get,child,candidate_id)
        actor = 'local:'+VALIDATOR if package['payload']['validator'] == VALIDATOR else 'local:arithmetic_expression_v1'
        return _call(service.validate, child, candidate_id, actor=actor,
                     **payload.model_dump())

    @router.post("/{candidate_id}/review", response_model=PackageResponse)
    def review(candidate_id: Identifier, payload: ReviewRequest, child: str = Depends(owned_child), principal: ParentPrincipal = Depends(parent_required)):
        return _call(service.review, child, candidate_id, actor=principal.actor, **payload.model_dump(exclude_none=True, by_alias=True))

    @router.post("/{candidate_id}/publish", response_model=PackageResponse)
    def publish(candidate_id: Identifier, payload: Command, child: str = Depends(owned_child), principal: ParentPrincipal = Depends(parent_required)):
        return _call(service.publish, child, candidate_id, actor=principal.actor, **payload.model_dump())

    @router.post("/{candidate_id}/withdraw", response_model=PackageResponse)
    def withdraw(candidate_id: Identifier, payload: WithdrawRequest, child: str = Depends(owned_child), principal: ParentPrincipal = Depends(parent_required)):
        return _call(service.withdraw, child, candidate_id, actor=principal.actor, **payload.model_dump())

    return router
