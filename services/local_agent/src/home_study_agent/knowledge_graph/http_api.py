"""Narrow, local-only HTTP adapter for the reviewed knowledge-graph journey.

The host owns authentication and route registration.  Keeping this adapter here
prevents knowledge-graph code from taking ownership of the large shared app module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict

from ..database import Database
from .application_service import KnowledgeGraphApplicationService
from .atomic_companion import AtomicGraphUnavailableError
from .language_candidate_service import LanguageCandidateConflictError, TextbookFocusCandidateService
from .textbook_page_service import TextbookOcrPageInput, TextbookOcrPageService
from .parent_textbook_capture import CaptureCapacityUnavailableError, CaptureConflictError, CaptureNotFoundError, ParentTextbookCaptureService
from .parent_textbook_drafts import ParentTextbookDraftService


class ParentAuthPort(Protocol):
    def require(self, token: str | None) -> None: ...


class DiagnosticResultRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=120)
    correctness: float = Field(ge=0, le=1)
    independent: bool
    client_action_id: str = Field(min_length=8, max_length=128)
    note: str = Field(default="", max_length=500)


class MistakeConfirmationRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=120)
    mistake_id: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


class ReviewPlanRequest(BaseModel):
    client_action_id: str = Field(min_length=8, max_length=128)
    max_items: int = Field(default=5, ge=1, le=12)


class ExerciseResultRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=120)
    exercise_id: str = Field(min_length=1, max_length=120)
    correctness: float = Field(ge=0, le=1)
    independent: bool
    client_action_id: str = Field(min_length=8, max_length=128)
    note: str = Field(default='', max_length=500)


class MappingReviewRequest(BaseModel):
    resource_id: str = Field(min_length=1, max_length=120)
    segment_id: str = Field(min_length=1, max_length=120)
    node_id: str = Field(min_length=1, max_length=120)
    relation_type: str = Field(pattern="^(covers|explains|exemplifies|assesses)$")
    decision: str = Field(pattern="^(approved|rejected|needs_split)$")
    reviewer: str = Field(min_length=1, max_length=80)
    client_action_id: str = Field(min_length=8, max_length=128)
    note: str = Field(default="", max_length=1000)


class MappingUndoRequest(BaseModel):
    resource_id: str = Field(min_length=1, max_length=120)
    segment_id: str = Field(min_length=1, max_length=120)
    node_id: str = Field(min_length=1, max_length=120)
    relation_type: str = Field(pattern="^(covers|explains|exemplifies|assesses)$")
    reviewer: str = Field(min_length=1, max_length=80)
    client_action_id: str = Field(min_length=8, max_length=128)
    note: str = Field(default="", max_length=1000)

class ResourceMappingReviewRequest(BaseModel):
    resource_id: str = Field(min_length=1, max_length=120)
    node_id: str = Field(min_length=1, max_length=120)
    relation_type: str = Field(pattern="^(covers|explains|exemplifies|assesses)$")
    reviewer: str = Field(min_length=1, max_length=80)
    client_action_id: str = Field(min_length=8, max_length=128)
    decision: Optional[str] = Field(default=None, pattern="^(approved|rejected|needs_split)$")
    note: str = Field(default="", max_length=1000)

class LanguageCandidateSaveRequest(BaseModel):
    term: str = Field(min_length=1, max_length=80)
    focus_kind: str = Field(pattern="^(word|character|phrase)$")
    proposed: dict = Field(default_factory=dict)
    status: str = Field(pattern="^(pending|confirmed|deleted)$")
    client_action_id: str = Field(min_length=8, max_length=128)


class LanguageCandidateWithdrawRequest(BaseModel):
    client_action_id: str = Field(min_length=8, max_length=128)


class LanguageCandidateCreateRequest(BaseModel):
    language: str = Field(pattern="^(english|chinese)$")
    term: str = Field(min_length=1, max_length=80)
    focus_kind: str = Field(pattern="^(word|character|phrase)$")
    proposed: dict = Field(default_factory=dict)


class TextbookOcrPageRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    ocr_text: str = Field(min_length=1, max_length=200_000)
    material_kind: str = Field(default="textbook", pattern="^(textbook|workbook)$")
    grade_scope: str = Field(default="", max_length=80)
    subject_scope: str = Field(default="", max_length=80)
    semester_scope: str = Field(default="", max_length=80)
    textbook_version: str = Field(default="", max_length=120)
    volume: str = Field(default="", max_length=80)
    unit: str = Field(default="", max_length=120)
    lesson: str = Field(default="", max_length=160)
    page_number: int = Field(default=1, ge=1, le=20_000)
    source_ref: str = Field(default="local_ocr_capture", min_length=1, max_length=300)
    ocr_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    child_id: Optional[str] = Field(default=None, max_length=120)
    capture_ref: Optional[str] = Field(default=None, max_length=80)
    client_action_id: Optional[str] = Field(default=None, min_length=8, max_length=128)

class ParentTextbookLeaseRequest(BaseModel):
    system_id_hash: str = Field(min_length=8,max_length=160)
    capabilities: dict = Field(default_factory=dict)
class ParentTextbookOcrRequest(BaseModel):
    lease_id: str = Field(min_length=8,max_length=120)
    image_base64: str = Field(min_length=32,max_length=24_000_000)
    page_context: dict = Field(default_factory=dict)
    image_transform: Optional[dict] = None
    client_action_id: str = Field(min_length=8,max_length=128)
class ParentTextbookReviewRequest(BaseModel):
    extracted_text: str = Field(min_length=1,max_length=200_000)
    client_action_id: str = Field(min_length=8,max_length=128)
class ParentTextbookFileOcrRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    image_base64: str = Field(min_length=32,max_length=24_000_000)
    page_context: dict = Field(default_factory=dict)
    image_transform: Optional[dict] = None
    client_action_id: str = Field(min_length=8,max_length=128)
class ParentTextbookActionRequest(BaseModel):
    client_action_id: str = Field(min_length=8,max_length=128)

class ParentTextbookDraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    expected_generation: str = Field(min_length=8, max_length=128)
    client_action_id: str = Field(min_length=8, max_length=128)
    payload: Optional[dict] = None


class PaperObjectiveDraftIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: int = Field(ge=1)
    storage_generation: str = Field(min_length=8,max_length=128)
    last_action_id: str = Field(min_length=8,max_length=128)

class PaperObjectiveSeedChoice(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    diagnostic_item_id: str = Field(min_length=1,max_length=160)
    binding: str = Field(pattern='^[a-f0-9]{64}$')


class PaperObjectiveSubmitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    draft_identity: PaperObjectiveDraftIdentity
    seed_choice: Optional[PaperObjectiveSeedChoice] = None
    expected_selection_version: int = Field(ge=1)
    standard_key: str = Field(min_length=1,max_length=160)
    standard_binding: str = Field(pattern='^[a-f0-9]{64}$')
    expected_node_binding: str = Field(pattern='^[a-f0-9]{64}$')
    answer_semantics: dict
    originals_confirmed: bool
    license_status: str = Field(pattern='^(public|family_owned_index)$')
    difficulty: int = Field(ge=1,le=5)
    action_id: str = Field(min_length=8,max_length=120)


class PaperObjectiveResumeRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_generation: str = Field(min_length=8,max_length=128)


def install_knowledge_graph_routes(
    application: FastAPI,
    database: Database,
    parent_auth: ParentAuthPort,
    atomic_graph_db_path: Path | str | None = None,
) -> None:
    """Register the reviewed child flow and the parent-only review queue once."""
    if getattr(application.state, "knowledge_graph_routes_installed", False):
        return
    facade = KnowledgeGraphApplicationService(database, atomic_graph_db_path=atomic_graph_db_path)
    language_candidates = TextbookFocusCandidateService(database)
    textbook_pages = TextbookOcrPageService(database)
    child = APIRouter(prefix="/v1/children/{child_id}/knowledge-graph", tags=["knowledge-graph"])
    parent = APIRouter(prefix="/v1/parent/knowledge-graph", tags=["knowledge-graph-parent"])

    def active_child_or_404(child_id: str) -> None:
        try:
            facade._require_active_child(child_id)
        except Exception as error:
            raise HTTPException(status_code=404, detail={"code":"CHILD_NOT_FOUND", "message":"找不到可用的孩子档案。"}) from error

    def parent_or_401(request: Request) -> None:
        parent_auth.require(request.headers.get("X-HSC-Parent"))
    def capture_service() -> ParentTextbookCaptureService:
        service = getattr(application.state, "knowledge_graph_parent_capture_service", None)
        if service is None:
            service = ParentTextbookCaptureService(database, application.state.ocr_workflow.adapter)
            application.state.knowledge_graph_parent_capture_service = service
        return service

    def capture_error(error: ValueError, code: str) -> HTTPException:
        if isinstance(error, CaptureNotFoundError):
            return HTTPException(status_code=404, detail={"code":code, "message":str(error)})
        if isinstance(error, CaptureCapacityUnavailableError):
            return HTTPException(status_code=409, detail={"code":"TEXTBOOK_OCR_CAPACITY_UNAVAILABLE", "message":str(error)})
        if isinstance(error, CaptureConflictError):
            return HTTPException(status_code=409, detail={"code":code, "message":str(error)})
        return HTTPException(status_code=422, detail={"code":code, "message":str(error)})

    def capture_device_error(error: ValueError) -> HTTPException:
        if isinstance(error, CaptureNotFoundError):
            return HTTPException(status_code=404, detail={"code":"TEXTBOOK_CAMERA_NOT_FOUND", "message":str(error)})
        return HTTPException(status_code=409, detail={"code":"TEXTBOOK_CAMERA_UNAVAILABLE", "message":str(error)})

    @parent.get('/textbook-capture/{child_id}/drafts/{scope}')
    def read_textbook_draft(child_id: str, scope: str, request: Request, response: Response) -> dict:
        parent_or_401(request)
        response.headers['Cache-Control'] = 'no-store'
        try:
            return {'draft': ParentTextbookDraftService(database).read(child_id, scope)}
        except ValueError as error:
            raise capture_error(error, 'TEXTBOOK_DRAFT_UNAVAILABLE') from error

    @parent.put('/textbook-capture/{child_id}/drafts/{scope}')
    def save_textbook_draft(child_id: str, scope: str, payload: ParentTextbookDraftRequest, request: Request, response: Response) -> dict:
        parent_or_401(request)
        response.headers['Cache-Control'] = 'no-store'
        try:
            return {'draft': ParentTextbookDraftService(database).save(child_id, scope, payload.expected_revision, payload.client_action_id, payload.payload, expected_generation=payload.expected_generation)}
        except ValueError as error:
            raise capture_error(error, 'TEXTBOOK_DRAFT_CONFLICT') from error

    def paper_bridge_call(callback):
        from ..local_supply import LocalSupplyError
        try:
            return callback()
        except LocalSupplyError as error:
            # These are module-owned enum codes, never raw media or arbitrary text.
            raise HTTPException(409,detail={'code':str(error)}) from None
        except (ValueError,KeyError,OSError):
            raise HTTPException(409,detail={'code':'PAPER_OBJECTIVE_SOURCE_CHANGED'}) from None

    @parent.get('/textbook-capture/{child_id}/objective-review/options')
    def paper_objective_options(child_id: str,request: Request,response: Response):
        parent_or_401(request);response.headers['Cache-Control']='no-store'
        from .paper_objective_bridge import PaperObjectiveBridge
        return paper_bridge_call(lambda:PaperObjectiveBridge(database).options(child_id))

    @parent.post('/textbook-capture/{child_id}/objective-review')
    def paper_objective_submit(child_id: str,payload: PaperObjectiveSubmitRequest,request: Request,response: Response):
        parent_or_401(request);response.headers['Cache-Control']='no-store'
        from .paper_objective_bridge import PaperObjectiveBridge
        return paper_bridge_call(lambda:PaperObjectiveBridge(database).submit(child_id,**payload.model_dump()))

    @parent.get('/textbook-capture/{child_id}/objective-review/submissions')
    def paper_objective_submissions(child_id: str,request: Request,response: Response):
        parent_or_401(request);response.headers['Cache-Control']='no-store'
        from .paper_objective_bridge import PaperObjectiveBridge
        return paper_bridge_call(lambda:PaperObjectiveBridge(database).submissions(child_id))

    @parent.post('/textbook-capture/{child_id}/objective-review/{receipt_id}/generate')
    def paper_objective_generate(child_id: str,receipt_id: str,payload: PaperObjectiveResumeRequest,request: Request,response: Response):
        parent_or_401(request);response.headers['Cache-Control']='no-store'
        from .paper_objective_bridge import PaperObjectiveBridge
        return paper_bridge_call(lambda:PaperObjectiveBridge(database).generate(child_id,receipt_id,**payload.model_dump()))

    @parent.get('/textbook-capture/{child_id}/objective-review/{receipt_id}')
    def paper_objective_receipt(child_id: str,receipt_id: str,request: Request,response: Response):
        parent_or_401(request);response.headers['Cache-Control']='no-store'
        from .paper_objective_bridge import PaperObjectiveBridge
        return paper_bridge_call(lambda:PaperObjectiveBridge(database).read(child_id,receipt_id))

    @child.get("/learning-path")
    def learning_path(child_id: str, node_id: str = Query(min_length=1, max_length=120)) -> dict:
        active_child_or_404(child_id)
        try:
            return facade.learning_path(child_id, node_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"KNOWLEDGE_POINT_UNAVAILABLE", "message":str(error)}) from error

    @child.get("/materials/search")
    def search_materials(
        child_id: str,
        query: str = Query(min_length=1, max_length=300),
        subject: Optional[str] = Query(default=None, max_length=80),
        limit: int = Query(default=8, ge=1, le=20),
    ) -> dict:
        """Retrieve only local, provenance-bearing explanation material for a child."""
        active_child_or_404(child_id)
        return {"items":facade.search_materials_for_child(child_id, query, subject=subject, limit=limit),
                "truthfulness":{"local_only":True,"source_selection":"hybrid_fts_vector",
                                "profile_grade_enforced":True,"approved_mapping_only":True}}

    @child.get("/course-context")
    def course_context(child_id: str, query: str = Query(min_length=1, max_length=500),
                       subject: Optional[str] = Query(default=None, max_length=40),
                       limit: int = Query(default=3, ge=1, le=5)) -> dict:
        active_child_or_404(child_id)
        return facade.local_courses.context_for_child(child_id=child_id, query=query, subject=subject, limit=limit)

    @child.get("/atomic-context")
    def atomic_context(child_id: str, query: str = Query(min_length=1, max_length=240),
                       subject: Optional[str] = Query(default=None, min_length=1, max_length=40),
                       limit: int = Query(default=6, ge=1, le=8)) -> dict:
        """Bounded local context used by the companion for questions and hypotheses."""
        active_child_or_404(child_id)
        try:
            return facade.atomic_context(child_id=child_id, query=query, subject=subject, limit=limit)
        except AtomicGraphUnavailableError as error:
            raise HTTPException(status_code=503, detail={
                "code":"ATOMIC_GRAPH_UNAVAILABLE", "message":str(error), "retryable":True,
            }) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail={
                "code":"ATOMIC_GRAPH_QUERY_INVALID", "message":str(error),
            }) from error

    @child.post("/mistakes")
    def confirm_mistake(child_id: str, payload: MistakeConfirmationRequest) -> dict:
        active_child_or_404(child_id)
        # A client-provided ID is not evidence.  Host mistake capture is the
        # system of record and its bridge is invoked by the host route.
        raise HTTPException(status_code=409, detail={"code":"HOST_MISTAKE_REQUIRED", "message":"请先在错题本确认真实错题；客户端不能直接写入掌握度。"})

    @child.post("/diagnostic-results")
    def confirm_diagnostic_result(child_id: str, payload: DiagnosticResultRequest) -> dict:
        active_child_or_404(child_id)
        raise HTTPException(status_code=409, detail={"code":"HOST_GRADED_ATTEMPT_REQUIRED", "message":"请通过本地诊断作答提交；客户端正确率不能直接更新掌握度。"})

    @child.get("/retest-items")
    def retest_items(child_id: str, node_id: str = Query(min_length=1, max_length=120), limit: int = Query(default=3, ge=1, le=10)) -> dict:
        active_child_or_404(child_id)
        try:
            items=facade.similar_exercises_for_child(child_id, node_id, limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"KNOWLEDGE_POINT_UNAVAILABLE", "message":str(error)}) from error
        return {"items":items, "truthfulness":{
            "exercise_and_assessment_relation":"approved_parent_or_product_only",
            "unreviewed_exercises_hidden":True,
            "grading":"submit an independently or locally graded result; this endpoint does not claim to score free text",
        }}

    @child.post("/exercise-results")
    def record_exercise_result(child_id: str, payload: ExerciseResultRequest) -> dict:
        active_child_or_404(child_id)
        raise HTTPException(status_code=409, detail={"code":"HOST_GRADED_ATTEMPT_REQUIRED", "message":"此复测尚无宿主可验证判分记录；需要人工确认，未更新掌握度。"})

    @child.post("/review-plans")
    def create_review_plan(child_id: str, payload: ReviewPlanRequest) -> dict:
        active_child_or_404(child_id)
        return facade.review_plan(child_id=child_id, action_id=payload.client_action_id, max_items=payload.max_items)

    @child.get("/review-plans/latest")
    def latest_review_plan(child_id: str) -> dict:
        active_child_or_404(child_id)
        return {"plan":facade.latest_review_plan(child_id)}

    @parent.get("/review-queue")
    def review_queue(request: Request, per_track: int = Query(default=8, ge=1, le=50)) -> dict:
        parent_or_401(request)
        return {"items":facade.quality.review_queue(per_track=per_track), "operator_only":True}

    @parent.get("/resource-review-queue")
    def resource_review_queue(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> dict:
        parent_or_401(request)
        return {"items":facade.quality.resource_review_queue(limit), "summary":facade.quality.resource_review_summary(),
                "operator_only":True,"evidence_text_default":"hidden"}

    @parent.get("/resource-review-evidence")
    def resource_review_evidence(request: Request, resource_id: str = Query(min_length=1, max_length=120),
                                 node_id: str = Query(min_length=1, max_length=120),
                                 relation_type: str = Query(pattern="^(covers|explains|exemplifies|assesses)$")) -> dict:
        parent_or_401(request)
        return {"items":facade.quality.resource_review_evidence(resource_id=resource_id,node_id=node_id,relation_type=relation_type),
                "local_preview":True,"full_transcript_available":False}

    @parent.get("/resource-review-history")
    def resource_review_history(request: Request, reviewer: str = Query(min_length=1, max_length=80),
                                limit: int = Query(default=12, ge=1, le=50)) -> dict:
        parent_or_401(request)
        return {"items":facade.quality.resource_review_history(reviewer, limit), "operator_only":True}

    @parent.post("/resource-mapping-reviews")
    def record_resource_mapping_review(request: Request, payload: ResourceMappingReviewRequest) -> dict:
        parent_or_401(request)
        try:
            if not payload.decision: raise ValueError('decision is required')
            return {"review":facade.quality.record_resource_review(**payload.model_dump())}
        except ValueError as error: raise HTTPException(status_code=422, detail={"code":"RESOURCE_MAPPING_REVIEW_INVALID","message":str(error)}) from error

    @parent.post("/resource-mapping-reviews/undo")
    def undo_resource_mapping_review(request: Request, payload: ResourceMappingReviewRequest) -> dict:
        parent_or_401(request)
        try: return {"review":facade.quality.undo_resource_review(**{key:value for key,value in payload.model_dump().items() if key != 'decision'})}
        except ValueError as error: raise HTTPException(status_code=422, detail={"code":"RESOURCE_MAPPING_REVIEW_UNDO_INVALID","message":str(error)}) from error

    @parent.get("/atomic-context-usage")
    def atomic_context_usage(request: Request) -> dict:
        parent_or_401(request)
        return {"usage":facade.atomic_companion.usage_summary(), "operator_only":True,
                "privacy":{"raw_child_id_stored":False,"raw_query_stored":False}}

    @parent.post("/mapping-reviews")
    def record_mapping_review(request: Request, payload: MappingReviewRequest) -> dict:
        parent_or_401(request)
        try:
            state = facade.quality.record_review(**payload.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"MAPPING_REVIEW_INVALID", "message":str(error)}) from error
        return {"mapping":state}

    @parent.post("/mapping-reviews/undo")
    def undo_mapping_review(request: Request, payload: MappingUndoRequest) -> dict:
        parent_or_401(request)
        try:
            state = facade.quality.undo_review(**payload.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"MAPPING_REVIEW_UNDO_INVALID", "message":str(error)}) from error
        return {"mapping":state}

    @parent.post("/textbook-pages/{page_ref}/language-candidates/extract")
    def extract_language_candidates(page_ref: str, request: Request) -> dict:
        parent_or_401(request)
        try:
            resource_id=textbook_pages.resource_id_for_ref(page_ref)
            return {"items":[language_candidates.public(item) for item in language_candidates.extract(resource_id)],"source_boundary":"document_ocr_page_only"}
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"TEXTBOOK_PAGE_INVALID", "message":str(error)}) from error

    @parent.get("/textbook-pages")
    def list_textbook_pages(request: Request) -> dict:
        parent_or_401(request)
        return {"items":textbook_pages.list_public(),"source_boundary":"local_parent_ocr_only"}

    @parent.post("/textbook-pages")
    def import_textbook_page(request: Request, payload: TextbookOcrPageRequest) -> dict:
        parent_or_401(request)
        try:
            # Reservation, page mutation and receipt share the restore/deletion
            # guard; otherwise an in-flight import can recreate erased text.
            with database.external_effect_guard(timeout=0):
                if bool(payload.capture_ref) != bool(payload.child_id):
                    raise ValueError('capture_ref and child_id must be supplied together')
                if not payload.capture_ref and payload.source_ref.strip().startswith('local_parent_textbook_capture:'):
                    raise ValueError('camera OCR source references require capture_ref, child_id, and client_action_id')
                fields=payload.model_dump(exclude={'child_id','capture_ref','client_action_id'})
                if payload.capture_ref:
                    if not payload.client_action_id: raise ValueError('captured textbook page import requires client_action_id')
                    fields['source_ref']=capture_service().page_source_ref(payload.capture_ref)
                    if capture_service().reserve_page_import(payload.child_id,payload.capture_ref,payload.client_action_id,fields):
                        return {"page":textbook_pages.public_page(textbook_pages.resource_id_for_source_ref(fields['source_ref'])),"source_boundary":"local_parent_ocr_only"}
                page=textbook_pages.import_page(TextbookOcrPageInput(**fields))
                if payload.capture_ref and payload.child_id and payload.client_action_id:
                    capture_service().finalize_page_import(payload.child_id,payload.capture_ref,payload.client_action_id,fields)
                return {"page":textbook_pages.public_page(page["resource_id"]),"source_boundary":"local_parent_ocr_only"}
        except ValueError as error:
            raise capture_error(error,"TEXTBOOK_PAGE_INVALID") from error

    @parent.post("/textbook-capture/{child_id}/leases")
    def acquire_textbook_camera(child_id: str, payload: ParentTextbookLeaseRequest, request: Request) -> dict:
        parent_or_401(request)
        try: return capture_service().acquire(child_id,**payload.model_dump())
        except ValueError as error: raise capture_device_error(error) from error
    @parent.post("/textbook-capture/leases/{lease_id}/renew")
    def renew_textbook_camera(lease_id: str, request: Request) -> dict:
        parent_or_401(request)
        try: return capture_service().renew(lease_id)
        except ValueError as error: raise capture_error(error,"TEXTBOOK_CAMERA_INVALID") from error
    @parent.post("/textbook-capture/leases/{lease_id}/release")
    def release_textbook_camera(lease_id: str, request: Request) -> dict:
        parent_or_401(request)
        try: return capture_service().release(lease_id,'parent_closed')
        except ValueError as error: raise capture_error(error,"TEXTBOOK_CAMERA_INVALID") from error
    @parent.post("/textbook-capture/{child_id}/ocr")
    def capture_textbook_ocr(child_id: str, payload: ParentTextbookOcrRequest, request: Request) -> dict:
        parent_or_401(request)
        try: return {"artifact":capture_service().recognize(child_id,**payload.model_dump())}
        except ValueError as error: raise capture_error(error,"TEXTBOOK_OCR_INVALID") from error
    @parent.post("/textbook-capture/{child_id}/file-ocr")
    def import_textbook_file_ocr(child_id: str, payload: ParentTextbookFileOcrRequest, request: Request) -> dict:
        parent_or_401(request)
        try: return {"artifact":capture_service().recognize_file(child_id,**payload.model_dump())}
        except ValueError as error: raise capture_error(error,"TEXTBOOK_OCR_INVALID") from error
    @parent.get("/textbook-capture/{child_id}/artifacts")
    def textbook_ocr_artifacts(child_id: str, request: Request) -> dict:
        parent_or_401(request); return {"items":capture_service().pending(child_id)}
    @parent.get('/textbook-capture/{child_id}/artifacts/{capture_ref}/actions/{client_action_id}')
    def textbook_capture_action_receipt(child_id: str, capture_ref: str, client_action_id: str, request: Request, response: Response) -> dict:
        parent_or_401(request)
        active_child_or_404(child_id)
        response.headers['Cache-Control'] = 'no-store'
        if not 8 <= len(client_action_id) <= 128:
            raise HTTPException(status_code=422, detail={'code':'TEXTBOOK_ACTION_INVALID'})
        try:
            result = capture_service().action_receipt(child_id, capture_ref, client_action_id)
            if result.get('action') == 'imported':
                result['page'] = textbook_pages.public_page(textbook_pages.resource_id_for_source_ref(result['request']['source_ref']))
            return result
        except ValueError as error:
            raise capture_error(error, 'TEXTBOOK_ACTION_UNAVAILABLE') from error

    @parent.get("/textbook-capture/{child_id}/artifacts/{capture_ref}/source")
    def textbook_ocr_source(child_id: str,capture_ref: str,request: Request) -> Response:
        parent_or_401(request)
        try:
            image,media_type=capture_service().source_image(child_id,capture_ref)
            return Response(content=image,media_type=media_type,headers={"Cache-Control":"no-store"})
        except ValueError as error:
            raise capture_error(error,"TEXTBOOK_OCR_SOURCE_UNAVAILABLE") from error
    @parent.put("/textbook-capture/{child_id}/artifacts/{capture_ref}/review")
    def review_textbook_ocr(child_id: str,capture_ref: str,payload: ParentTextbookReviewRequest,request: Request) -> dict:
        parent_or_401(request)
        try:return {"artifact":capture_service().review(child_id,capture_ref,payload.extracted_text,payload.client_action_id)}
        except ValueError as error:raise capture_error(error,"TEXTBOOK_OCR_REVIEW_INVALID") from error
    @parent.post("/textbook-capture/{child_id}/artifacts/{capture_ref}/undo-review")
    def undo_textbook_ocr_review(child_id: str,capture_ref: str,payload: ParentTextbookActionRequest,request: Request) -> dict:
        parent_or_401(request)
        try:return {"artifact":capture_service().undo_review(child_id,capture_ref,payload.client_action_id)}
        except ValueError as error:raise capture_error(error,"TEXTBOOK_OCR_STATE_INVALID") from error
    @parent.post("/textbook-capture/{child_id}/artifacts/{capture_ref}/skip")
    def skip_textbook_ocr(child_id: str,capture_ref: str,payload: ParentTextbookActionRequest,request: Request) -> dict:
        parent_or_401(request)
        try:return {"artifact":capture_service().skip(child_id,capture_ref,payload.client_action_id)}
        except ValueError as error:raise capture_error(error,"TEXTBOOK_OCR_STATE_INVALID") from error
    @parent.post("/textbook-capture/{child_id}/artifacts/{capture_ref}/restore")
    def restore_textbook_ocr(child_id: str,capture_ref: str,payload: ParentTextbookActionRequest,request: Request) -> dict:
        parent_or_401(request)
        try:return {"artifact":capture_service().restore(child_id,capture_ref,payload.client_action_id)}
        except ValueError as error:raise capture_error(error,"TEXTBOOK_OCR_STATE_INVALID") from error
    @parent.post("/textbook-capture/{child_id}/artifacts/{capture_ref}/retry")
    def retry_textbook_ocr(child_id: str,capture_ref: str,payload: ParentTextbookActionRequest,request: Request) -> dict:
        parent_or_401(request)
        try:return {"artifact":capture_service().retry_requested(child_id,capture_ref,payload.client_action_id)}
        except ValueError as error:raise capture_error(error,"TEXTBOOK_OCR_STATE_INVALID") from error

    @parent.get("/textbook-pages/{page_ref}/language-candidates")
    def list_language_candidates(page_ref: str, request: Request) -> dict:
        parent_or_401(request)
        try:
            return {"items":[language_candidates.public(item) for item in language_candidates.list(textbook_pages.resource_id_for_ref(page_ref))],"source_boundary":"document_ocr_page_only"}
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"TEXTBOOK_PAGE_INVALID", "message":str(error)}) from error

    @parent.post("/textbook-pages/{page_ref}/language-candidates")
    def create_language_candidate(page_ref: str, payload: LanguageCandidateCreateRequest, request: Request) -> dict:
        parent_or_401(request)
        try:
            item=language_candidates.create(textbook_pages.resource_id_for_ref(page_ref), **payload.model_dump())
            return {"item":language_candidates.public(item)}
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"LANGUAGE_CANDIDATE_INVALID", "message":str(error)}) from error

    @parent.put("/language-candidates/{candidate_ref}")
    def save_language_candidate(candidate_ref: str, payload: LanguageCandidateSaveRequest, request: Request) -> dict:
        parent_or_401(request)
        try:
            item=language_candidates.save(language_candidates.candidate_id_for_ref(candidate_ref),**payload.model_dump())
            return {"item":language_candidates.public(item)}
        except LanguageCandidateConflictError as error:
            raise HTTPException(status_code=409, detail={"code":"LANGUAGE_CANDIDATE_ACTION_CONFLICT", "message":str(error)}) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"LANGUAGE_CANDIDATE_INVALID", "message":str(error)}) from error

    @parent.get("/language-candidates/{candidate_ref}/actions/{client_action_id}")
    def language_candidate_action_status(candidate_ref: str, client_action_id: str, request: Request) -> dict:
        parent_or_401(request)
        try:
            state = language_candidates.action_status(language_candidates.candidate_id_for_ref(candidate_ref), client_action_id)
            return {"item": language_candidates.public(state.pop("item")), **state}
        except ValueError as error:
            raise HTTPException(status_code=404, detail={"code":"LANGUAGE_CANDIDATE_ACTION_NOT_FOUND", "message":str(error)}) from error

    @parent.post("/language-candidates/{candidate_ref}/withdraw")
    def withdraw_language_candidate(candidate_ref: str, payload: LanguageCandidateWithdrawRequest, request: Request) -> dict:
        parent_or_401(request)
        try:
            item=language_candidates.withdraw_confirmation(language_candidates.candidate_id_for_ref(candidate_ref), client_action_id=payload.client_action_id)
            return {"item":language_candidates.public(item)}
        except LanguageCandidateConflictError as error:
            raise HTTPException(status_code=409, detail={"code":"LANGUAGE_CANDIDATE_ACTION_CONFLICT", "message":str(error)}) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code":"LANGUAGE_CANDIDATE_WITHDRAW_INVALID", "message":str(error)}) from error

    application.include_router(child)
    application.include_router(parent)
    application.state.knowledge_graph = facade
    application.state.knowledge_graph_routes_installed = True
