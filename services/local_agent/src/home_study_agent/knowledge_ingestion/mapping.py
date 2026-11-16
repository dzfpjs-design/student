from __future__ import annotations

from ..knowledge_graph.service import KnowledgeGraphService


class TermKnowledgeMapper:
    """Transparent local candidate mapper; it never presents an unreviewed match as fact."""
    def __init__(self, graph: KnowledgeGraphService, terms: dict[str, str]):
        self.graph, self.terms = graph, terms  # term -> node id

    def map_segment(self, resource_id: str, segment_id: str, text: str, source_ref: str) -> list[dict]:
        matches=[]
        for term,node_id in self.terms.items():
            if term in text:
                confidence=min(.85,.45 + .05 * text.count(term))
                self.graph.map_segment(resource_id,segment_id,node_id,"covers",confidence=confidence,source_ref=source_ref)
                matches.append({"term":term,"node_id":node_id,"confidence":confidence,"review_status":"unreviewed"})
        return matches
