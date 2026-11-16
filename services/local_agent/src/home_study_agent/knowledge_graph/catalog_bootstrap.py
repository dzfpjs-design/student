from __future__ import annotations

import json

from ..database import Database
from ..learning_state import LearningStateService
from ..repository import utc_now
from .service import KnowledgeGraphService
from .official_catalog import OfficialCurriculumCatalogProjector


NODE_TYPE = {
    "textbook_chapter": "unit", "common_error": "misconception", "assessment_task": "exercise",
}
EDGE_RULE = {
    "prerequisite_of": ("prerequisite_of", False), "misconception_of": ("misconception_of", False),
    "assessed_by": ("assessed_by", False), "part_of": ("contains", True),
    "taught_in": ("contains", True), "remediated_by": ("explained_by", False),
    "transfers_to": ("related_to", False),
}
# Conservative aliases. A match is an evidence candidate, never an approved content assertion.
ALIASES = {
    # "一元二次方程" alone also appears in function-definition lessons; it is
    # insufficient evidence for the formula method.
    "J3.MATH.QUADRATIC.FORMULA": ("求根公式", "公式法求根"),
    "J3.MATH.QUADRATIC.DISCRIMINANT": ("判别式",),
    "PET.READING.MAIN_IDEA": ("main idea", "主旨"),
    "PET.WRITING.EMAIL": ("email", "邮件"),
    "PET.LISTENING.DETAIL": ("listening", "听力"),
    "PET.SPEAKING.PICTURE": ("picture", "看图"),
    "OLY.P4.PATTERN": ("规律", "数列"), "OLY.P4.COUNTING": ("计数", "枚举"),
    "OLY.P4.AREA": ("面积", "补形"), "OLY.P4.NUMBER_SENSE": ("估算", "数感"),
    "OLY.P4.ARITHMETIC_STRUCTURE": ("分配律", "结合律"), "OLY.P4.NUMBER_THEORY": ("整除", "因数"),
    "OLY.P4.LOGIC": ("逻辑", "推理"), "OLY.P4.MODELING": ("数量关系", "建模"),
}


class CatalogGraphBootstrapper:
    """Project the existing curated starter catalogue into the independent local KG."""
    def __init__(self, database: Database):
        self.database, self.graph = database, KnowledgeGraphService(database)

    def run(self, *, rebuild_local_candidates: bool = True) -> dict:
        """Synchronize curated catalogue structure into the KG.

        Startup needs the product-owned nodes and host diagnostic relationships,
        but must not rescan a family's large local corpus.  Full ingestion or an
        explicit maintenance job may opt into rebuilding candidate mappings.
        """
        LearningStateService(self.database).seed_catalog()
        with self.database.connect() as connection:
            nodes = connection.execute("SELECT * FROM knowledge_node WHERE status='active'").fetchall()
            edges = connection.execute("SELECT * FROM knowledge_edge WHERE status='active'").fetchall()
            items = connection.execute("SELECT * FROM diagnostic_item WHERE status='active'").fetchall()
        with self.graph.catalog_batch() as graph:
            old_to_new, linked, exercises = self._project_nodes(graph, nodes, edges, items)
        mapped = self._map_local_segments(old_to_new) if rebuild_local_candidates else {
            "upserted": 0, "unreviewed_automatic_candidates_rebuilt": 0, "skipped": "startup_does_not_rescan_local_corpus",
        }
        official_catalog = OfficialCurriculumCatalogProjector(self.database).run(
            rebuild_local_candidates=rebuild_local_candidates
        )
        return {"nodes": len(old_to_new), "edges": linked, "exercise_nodes": exercises,
                "candidate_segment_mappings": mapped, "official_catalog": official_catalog}

    def _project_nodes(self, graph, nodes, edges, items):
        old_to_new: dict[str, str] = {}; scope_by_old: dict[str, dict] = {}
        for node in nodes:
            scope = {"track": node["track"], "subject": node["subject"], "grade": node["grade_band"]}
            scope_by_old[node["knowledge_node_id"]] = scope
            old_to_new[node["knowledge_node_id"]] = graph.upsert_node(
                canonical_key=f"starter:{node['code']}", node_type=NODE_TYPE.get(node["node_type"], "knowledge_point"),
                title=node["title"], description=node["description"],
                scope=scope,
                source_ref=node["source_ref"], version_label=node["knowledge_version"],
                license_boundary="project_curated_local", confidence=.8, review_status="approved",
            )
        linked = 0
        for edge in edges:
            rule = EDGE_RULE.get(edge["relation_type"])
            if not rule or edge["source_node_id"] not in old_to_new or edge["target_node_id"] not in old_to_new:
                continue
            relation, reverse = rule
            source, target = old_to_new[edge["source_node_id"]], old_to_new[edge["target_node_id"]]
            if reverse: source, target = target, source
            graph.link(source, target, relation, rationale=edge["rationale"], source_ref=edge["source_ref"],
                            version_label=edge["knowledge_version"], confidence=.8, review_status="approved")
            linked += 1
        exercises = 0
        for item in items:
            target = old_to_new.get(item["knowledge_node_id"])
            if not target: continue
            exercise = graph.upsert_node(
                canonical_key=f"diagnostic:{item['diagnostic_item_id']}", node_type="exercise", title=item["prompt"][:120],
                description=json.dumps({"diagnostic_item_id":item["diagnostic_item_id"],"answer_type":item["answer_type"],"difficulty":item["difficulty"]},ensure_ascii=False),
                scope=scope_by_old[item["knowledge_node_id"]], source_ref=item["source_ref"], version_label="starter_v1",
                license_boundary="project_curated_local", confidence=.9, review_status="approved",
            )
            graph.link(target, exercise, "assessed_by", rationale="已有本地诊断题可作为变式复测入口", source_ref=item["source_ref"],
                            version_label="starter_v1",confidence=.9,review_status="approved")
            exercises += 1
        return old_to_new, linked, exercises

    def _map_local_segments(self, old_to_new: dict[str, str]) -> int:
        now = utc_now(); mapped = 0
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Rebuild only still-unreviewed automatic candidates under the current
            # policy. A human-approved/rejected exceptional mapping is never erased.
            removed = connection.execute("""DELETE FROM kg_resource_node
                WHERE review_status='unreviewed' AND source_ref IN ('local_keyword_mapper_v1','local_keyword_mapper_v2','local_keyword_mapper_v3')""").rowcount
            for row in connection.execute("SELECT knowledge_node_id, code, title, track, subject, grade_band FROM knowledge_node WHERE status='active'"):
                node_id = old_to_new.get(row["knowledge_node_id"])
                terms = (row["title"], *ALIASES.get(row["code"], ()))
                for term in {item for item in terms if len(item) >= 2}:
                    grade_sql, grade_params = self._grade_eligibility(row["track"], row["grade_band"])
                    matches = connection.execute(f"""SELECT s.resource_id,s.segment_id FROM kg_segment s
                        JOIN kg_resource r ON r.resource_id=s.resource_id
                        WHERE instr(s.text, ?) > 0 AND (COALESCE(r.subject_scope,'')='' OR r.subject_scope=?)
                        AND {grade_sql}""", (term,row["subject"],*grade_params)).fetchall()
                    for match in matches:
                        connection.execute("""INSERT INTO kg_resource_node(resource_id,segment_id,node_id,relation_type,confidence,review_status,source_ref,created_at)
                        VALUES(?,?,?,'covers',.55,'unreviewed','local_keyword_mapper_v3',?)
                        ON CONFLICT(resource_id,segment_id,node_id,relation_type) DO UPDATE SET
                        confidence=excluded.confidence,source_ref=excluded.source_ref,created_at=excluded.created_at""",(match["resource_id"],match["segment_id"],node_id,now))
                        mapped += connection.execute("SELECT changes()").fetchone()[0]
            connection.commit()
        return {"upserted":mapped,"unreviewed_automatic_candidates_rebuilt":max(0,removed)}

    @staticmethod
    def _grade_eligibility(track: str, grade_band: str) -> tuple[str, tuple[str, ...]]:
        if track in {"competition_math", "pet"}:
            return "(COALESCE(r.grade_scope,'')='' OR r.grade_scope IN ('四年级','五年级','六年级'))", ()
        if track == "school" and grade_band in {"初三", "四年级"}:
            return "(COALESCE(r.grade_scope,'')='' OR r.grade_scope=?)", (grade_band,)
        return "1=1", ()
