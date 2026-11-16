from __future__ import annotations

from ..curriculum_standards import CurriculumStandardService
from ..database import Database
from .service import KnowledgeGraphService


STANDARD_TERMS = {
    "starter:J3.MATH.QUADRATIC.FORMULA": ("一元二次方程", "二次函数"),
    "starter:J3.MATH.QUADRATIC.DISCRIMINANT": ("一元二次方程", "二次函数"),
}


class StandardEvidenceProjector:
    """Attach local official-standard page evidence to curated graph concepts.

    It creates no model-generated requirements; absence of an exact page leaves a
    concept unaligned instead of manufacturing a standard claim.
    """
    def __init__(self, database: Database):
        self.database = database
        self.graph = KnowledgeGraphService(database)
        self.standards = CurriculumStandardService(database)

    def run(self) -> dict:
        self.graph.ensure_ready(); linked = 0; unresolved = 0
        with self.database.connect() as connection:
            nodes = connection.execute("SELECT node_id,canonical_key,title,scope_json FROM kg_node WHERE canonical_key LIKE 'starter:%' AND status='active'").fetchall()
        for node in nodes:
            scope = __import__('json').loads(node["scope_json"])
            subject = scope.get("subject")
            terms = STANDARD_TERMS.get(node["canonical_key"], (node["title"],))
            evidence = next((items[0] for term in terms if (items := self.standards.search_pages(term, subject=subject, limit=1))), None)
            if not evidence:
                unresolved += 1; continue
            capability_id = self.graph.upsert_node(
                canonical_key=f"standard:{evidence['source_id']}:{evidence['page_number']}:{node['canonical_key']}",
                node_type="standard_capability", title=f"{node['title']}（课标证据）", description=evidence["excerpt"][:1000],
                scope=scope, source_ref=f"curriculum_standard:{evidence['source_id']}:page:{evidence['page_number']}",
                source_time=evidence["version_label"], version_label=evidence["version_label"],
                license_boundary="official_standard", confidence=.82, review_status="unreviewed",
            )
            self.graph.link(capability_id,node["node_id"],"aligned_to",rationale=f"本地课标页命中：{terms[0]}",
                            source_ref=f"curriculum_standard:{evidence['source_id']}:page:{evidence['page_number']}",
                            version_label=evidence["version_label"],confidence=.82,review_status="unreviewed")
            linked += 1
        return {"linked_capabilities": linked, "unresolved_starter_nodes": unresolved}
