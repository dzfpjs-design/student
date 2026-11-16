from __future__ import annotations

import json
from pathlib import Path

from ..database import Database
from ..mastery.evidence import MasteryEvidenceService
from ..mastery.review_planning import ReviewPlanService
from .atomic_companion import AtomicGraphCompanionService
from .local_course_context import LocalCourseContextService
from .quality import GraphQualityService
from .service import KnowledgeGraphChildNotFoundError, KnowledgeGraphService


class KnowledgeGraphApplicationService:
    """Stable integration facade for UI/API adapters; never exposes absolute media paths."""
    def __init__(self, database: Database, atomic_graph_db_path: Path | str | None = None):
        self.graph = KnowledgeGraphService(database)
        self.mastery = MasteryEvidenceService(self.graph)
        self.review_plans = ReviewPlanService(self.graph)
        self.quality = GraphQualityService(database)
        self.atomic_companion = AtomicGraphCompanionService(database.path, build_db=atomic_graph_db_path)
        self.local_courses = LocalCourseContextService(database)

    def learning_path(self, child_id: str, node_id: str) -> dict:
        self._require_node_for_child(child_id, node_id)
        diagnosis = self.graph.diagnose_mistake(child_id, node_id, approved_only=True)
        state = self.graph.mastery(child_id, node_id)
        materials = [self._material(item) for item in diagnosis.pop("explanation_materials")]
        # A curriculum/product alignment may make a point diagnosable, but it is
        # not a lesson.  Never tell a child to "explain" when no approved local
        # explanation segment exists.
        if state["mastery_probability"] >= .55 and diagnosis["retest_items"]:
            next_action = "retest"
        elif materials:
            next_action = "explain"
        else:
            next_action = "manual_material_required"
        return {"child_id":child_id,"node_id":node_id,"mastery":state,"diagnosis":diagnosis,
                "materials":materials,"next_action":next_action,
                "material_availability": "available" if materials else "material_unavailable",
                "truthfulness":{"mapping_status":"approved_parent_or_product_evidence_only",
                                  "unreviewed_candidates_hidden":True,
                                  "media_is_local_only":True}}

    def record_diagnostic_result(self, *, child_id: str, node_id: str, correctness: float,
                                 independent: bool, action_id: str, note: str = "") -> dict:
        self._require_node_for_child(child_id, node_id)
        return self.mastery.apply_diagnostic(child_id,node_id,correctness,independent,action_id,note)

    def record_mistake(self, *, child_id: str, node_id: str, mistake_id: str, note: str = "") -> dict:
        self._require_node_for_child(child_id, node_id)
        return self.mastery.apply_mistake(child_id,node_id,mistake_id,note)

    def search_materials(self, query: str, *, subject: str | None = None, grade: str | None = None,
                         limit: int = 8, approved_only: bool = False) -> list[dict]:
        """UI/API-safe hybrid search result with local provenance, never absolute roots."""
        return [self._search_material(item) for item in self.graph.search_explanations_with_policy(
            query, subject=subject, grade=grade, limit=limit, approved_only=approved_only)]

    def search_materials_for_child(self, child_id: str, query: str, *, subject: str | None = None,
                                   limit: int = 8) -> list[dict]:
        """Student searches are bound to profile grade and approved evidence only."""
        grade = self._active_child_grade(child_id)
        return self.search_materials(query, subject=subject, grade=grade, limit=limit, approved_only=True)

    def similar_exercises(self, node_id: str, *, limit: int = 3) -> list[dict]:
        """Graph-grounded variants only; each result contains its similarity basis."""
        self._require_diagnosable_node(node_id)
        return self.graph.similar_exercises(node_id, limit=limit, approved_only=True)

    def similar_exercises_for_child(self, child_id: str, node_id: str, *, limit: int = 3) -> list[dict]:
        self._require_node_for_child(child_id, node_id)
        return self.graph.similar_exercises(node_id, limit=limit, approved_only=True)

    def record_exercise_result(self, *, child_id: str, node_id: str, exercise_id: str, correctness: float,
                               independent: bool, action_id: str, note: str = "", confidence: float = 1.0) -> dict:
        self._require_node_for_child(child_id, node_id)
        with self.graph.database.connect() as connection:
            approved = connection.execute("""SELECT 1 FROM kg_edge e JOIN kg_node exercise ON exercise.node_id=e.target_node_id
                WHERE e.source_node_id=? AND e.target_node_id=? AND e.relation_type='assessed_by'
                  AND e.review_status='approved' AND exercise.node_type='exercise'
                  AND exercise.review_status='approved' AND exercise.status='active'""", (node_id, exercise_id)).fetchone()
        if not approved:
            raise ValueError('exercise is not an approved retest for this knowledge point')
        return self.mastery.apply_exercise_result(child_id,node_id,exercise_id,correctness,independent,action_id,note,confidence=confidence)

    def review_plan(self, *, child_id: str, action_id: str, max_items: int = 5) -> dict:
        """Generate or idempotently resume a persisted local review plan."""
        return self.review_plans.generate(child_id=child_id, max_items=max_items, idempotency_key=action_id)

    def latest_review_plan(self, child_id: str) -> dict | None:
        return self.review_plans.latest(child_id)

    def atomic_context(self, *, child_id: str, query: str, subject: str | None = None,
                       limit: int = 6) -> dict:
        """Give the agent bounded graph context without treating drafts as mastery facts."""
        self._require_active_child(child_id)
        return self.atomic_companion.context_for_child(
            child_id=child_id, query=query, subject=subject, limit=limit
        )

    def operator_overview(self) -> dict:
        return {"quality":self.quality.report(),"review_queue":self.quality.review_queue(),
                "atomic_companion_usage":self.atomic_companion.usage_summary()}

    def _require_active_child(self, child_id: str) -> None:
        with self.graph.database.connect() as connection:
            self.graph._require_active_child(connection, child_id)

    def _active_child_grade(self, child_id: str) -> str:
        with self.graph.database.connect() as connection:
            row = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
        if not row:
            raise KnowledgeGraphChildNotFoundError(child_id)
        return str(row['grade'])

    def _require_diagnosable_node(self, node_id: str) -> None:
        with self.graph.database.connect() as connection:
            row = connection.execute("SELECT 1 FROM kg_node WHERE node_id=? AND status='active'", (node_id,)).fetchone()
        if not row:
            raise ValueError('knowledge point is unavailable because its source evidence changed')

    def _require_node_for_child(self, child_id: str, node_id: str) -> None:
        grade = self._active_child_grade(child_id)
        with self.graph.database.connect() as connection:
            row = connection.execute("SELECT scope_json,review_status,status FROM kg_node WHERE node_id=? AND node_type='knowledge_point'", (node_id,)).fetchone()
        if not row or row['status'] != 'active' or row['review_status'] != 'approved':
            raise ValueError('knowledge point is not approved for child learning')
        scope = json.loads(row['scope_json'] or '{}')
        scope_grade = str(scope.get('grade') or '')
        if scope_grade and scope_grade != grade and not scope_grade.startswith(f'{grade}-'):
            raise ValueError('knowledge point is outside this child grade')
        # A curriculum relationship describes the graph, not material a child may
        # learn from.  In particular, an approved prerequisite/mastery/evidence
        # edge must never keep an otherwise withdrawn textbook projection usable.
        # The only non-resource exception is a deliberately named, product-owned
        # curriculum evidence edge.  Keep that allowlist narrow and stable so new
        # relation types cannot silently become child-facing evidence.
        with self.graph.database.connect() as connection:
            current_evidence = connection.execute("""SELECT 1 WHERE EXISTS(
                SELECT 1 FROM kg_resource_node WHERE node_id=? AND review_status='approved'
            ) OR EXISTS(
                SELECT 1 FROM kg_edge
                WHERE (source_node_id=? OR target_node_id=?)
                  AND review_status='approved'
                  AND relation_type='aligned_to'
                  AND (source_ref LIKE 'curriculum_standard:%' OR source_ref LIKE 'product_evidence:%')
            )""", (node_id,node_id,node_id)).fetchone()
        if not current_evidence:
            raise ValueError('knowledge point has no current approved learning evidence')

    @staticmethod
    def _material(item: dict) -> dict:
        # A child-facing learning path needs a small local cue, not an export of
        # an approved transcript. Reuse the bounded privacy redaction used by
        # the parent evidence queue; actual playback/opening remains local and
        # separately subject to the resource's usage boundary.
        result = {key:item.get(key) for key in ("segment_id","resource_id","title","start_char","end_char",
            "transcript_relative_path","video_relative_path","source_ref","mapping_source_ref","license_boundary","confidence","review_status","media_status")}
        result["preview"] = GraphQualityService._redacted_preview(str(item.get("text") or ""))
        return result

    @staticmethod
    def _search_material(item: dict) -> dict:
        return {key:item.get(key) for key in ("segment_id","resource_id","title","text","start_char","end_char",
            "subject_scope","grade_scope","transcript_relative_path","video_relative_path","source_ref","license_boundary",
            "source_occurrences","approvals","score","media_status")}
