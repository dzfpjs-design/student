from __future__ import annotations

import json
from uuid import uuid4

from ..knowledge_graph.service import KnowledgeGraphService
from ..repository import utc_now


class ReviewPlanService:
    """Persisted, evidence-grounded local review plans with no cloud dependency."""
    algorithm_version = "weakness_prerequisite_review_v1"

    def __init__(self, graph: KnowledgeGraphService):
        self.graph = graph

    def generate(self, *, child_id: str, max_items: int = 5, idempotency_key: str) -> dict:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        max_items = max(1, min(int(max_items), 12))
        self.graph.ensure_ready(); now = utc_now()
        with self.graph.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.graph._require_active_child(connection, child_id)
            projection = self.graph.host_mastery_projection(connection, child_id)
            existing = connection.execute("SELECT * FROM kg_review_plan WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                if existing['child_id'] != child_id:
                    raise ValueError('review plan action belongs to another child')
                plan = self._validated_plan(connection, existing, projection)
                connection.commit()
                if plan is None:
                    raise ValueError('host review plan evidence changed; regenerate with a new action id')
                return plan
            states = connection.execute("""SELECT s.node_id,s.mastery_probability,s.evidence_count,s.weakness_signal,
                n.title,n.source_ref,n.review_status FROM kg_mastery_state s JOIN kg_node n ON n.node_id=s.node_id
                WHERE s.child_id=? AND n.node_type='knowledge_point' AND n.status='active' AND n.review_status='approved' ORDER BY s.weakness_signal DESC,s.mastery_probability ASC,n.title""", (child_id,)).fetchall()
            # Product/curriculum structure can make a point diagnosable, but a
            # child plan may only recommend concrete material after a current
            # resource mapping has been approved.
            states = [row for row in states if connection.execute(
                "SELECT 1 FROM kg_resource_node WHERE node_id=? AND review_status='approved' LIMIT 1", (row['node_id'],)
            ).fetchone()]
            if projection['fingerprint'] is not None:
                states = [{**dict(row), **projection['states'][row['node_id']]} for row in states]
                states = [row for row in states if row['evidence_count'] > 0]
                states.sort(key=lambda row: (-row['weakness_signal'], row['mastery_probability'], row['title']))
            evidence_count = projection['evidence_count']
            selected = [dict(row) for row in states[:max_items]]
            connection.commit()
            items = [self._item(child_id, row, index + 1) for index, row in enumerate(selected)]
            plan = {"child_id":child_id,"algorithm_version":self.algorithm_version,"generated_at":now,
                    "source_evidence_count":evidence_count,"items":items,
                    "truthfulness":{"only_observed_mastery_is_planned":True,
                                    "mapping_status":"approved_parent_or_product_evidence_only",
                                    "unreviewed_candidates_hidden":True}}
            if projection['fingerprint'] is not None:
                plan['host_evidence_fingerprint'] = projection['fingerprint']
            connection.execute("BEGIN IMMEDIATE")
            current = self.graph.host_mastery_projection(connection, child_id)
            if current['fingerprint'] != projection['fingerprint']:
                raise ValueError('host evidence changed during planning; retry generation')
            existing = connection.execute('SELECT * FROM kg_review_plan WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if existing:
                if existing['child_id'] != child_id:
                    raise ValueError('review plan action belongs to another child')
                cached = self._validated_plan(connection, existing, current)
                connection.commit()
                if cached is None:
                    raise ValueError('host review plan evidence changed; regenerate with a new action id')
                return cached
            # A fresh plan supersedes only previous plans; it does not erase their evidence.
            connection.execute("UPDATE kg_review_plan SET status='superseded',updated_at=? WHERE child_id=? AND status='active'", (now,child_id))
            connection.execute("""INSERT INTO kg_review_plan(review_plan_id,child_id,status,algorithm_version,item_count,source_evidence_count,plan_json,idempotency_key,generated_at,updated_at)
                VALUES(?,?,'active',?,?,?,?,?,?,?)""", (str(uuid4()),child_id,self.algorithm_version,len(items),evidence_count,
                json.dumps(plan,ensure_ascii=False),idempotency_key,now,now))
            connection.commit()
        return plan

    def latest(self, child_id: str) -> dict | None:
        self.graph.ensure_ready()
        with self.graph.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.graph._require_active_child(connection, child_id)
            row = connection.execute("SELECT * FROM kg_review_plan WHERE child_id=? AND status='active' ORDER BY generated_at DESC LIMIT 1", (child_id,)).fetchone()
            result = self._validated_plan(connection, row, self.graph.host_mastery_projection(connection, child_id)) if row else None
            connection.commit()
            return result

    @staticmethod
    def _validated_plan(connection, row, projection):
        plan = json.loads(row['plan_json'])
        fingerprint = projection['fingerprint']
        if fingerprint is not None or 'host_evidence_fingerprint' in plan:
            if row['status'] != 'active' or plan.get('host_evidence_fingerprint') != fingerprint:
                connection.execute("UPDATE kg_review_plan SET status='superseded',updated_at=? WHERE review_plan_id=? AND status='active'",
                                   (utc_now(), row['review_plan_id']))
                return None
        return plan

    def _item(self, child_id: str, state: dict, sequence: int) -> dict:
        node_id = state["node_id"]
        prerequisites = self.graph.prerequisites(node_id, max_depth=2)
        prerequisite_states = [self.graph.mastery(child_id, item["node_id"]) for item in prerequisites]
        weak_prerequisites = sorted(
            (item for item in prerequisite_states if item["weakness_signal"] > 0),
            key=lambda item: (-item["weakness_signal"], item["mastery_probability"]),
        )[:2]
        materials = self.graph.explanation_materials(node_id, limit=2, approved_only=True)
        exercises = self.graph.similar_exercises(node_id, limit=2, approved_only=True)
        mastery, weakness = float(state["mastery_probability"]), float(state["weakness_signal"])
        action = "relearn_with_material" if mastery < .55 or weakness >= .2 else "retest_with_variant"
        return {"sequence":sequence,"knowledge_node_id":node_id,"title":state["title"],
                "mastery_probability":mastery,"weakness_signal":weakness,"evidence_count":state["evidence_count"],
                "recommended_action":action,"weak_prerequisites":weak_prerequisites,
                "materials":[self._material_ref(item) for item in materials],
                "retest_items":[self._exercise_ref(item) for item in exercises]}

    @staticmethod
    def _material_ref(item: dict) -> dict:
        return {key:item.get(key) for key in ("segment_id","resource_id","title","transcript_relative_path","video_relative_path",
            "source_ref","mapping_source_ref","license_boundary","confidence","review_status","media_status")}

    @staticmethod
    def _exercise_ref(item: dict) -> dict:
        return {key:item.get(key) for key in ("node_id","title","source_ref","review_status","source_knowledge_node_id","basis")}
