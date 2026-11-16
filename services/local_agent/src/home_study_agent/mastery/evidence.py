from __future__ import annotations

from ..knowledge_graph.service import KnowledgeGraphService


class MasteryEvidenceService:
    """Narrow adapter for existing diagnosis/mistake services to call the KG safely."""
    def __init__(self, graph: KnowledgeGraphService):
        self.graph = graph

    def apply_diagnostic(self, child_id: str, node_id: str, correctness: float, independent: bool,
                         action_id: str, note: str = "") -> dict:
        return self.graph.record_evidence(child_id=child_id, node_id=node_id, correctness=correctness,
                                          independent=independent, origin_type="diagnostic",
                                          idempotency_key=f"diagnostic:{action_id}", note=note)

    def apply_mistake(self, child_id: str, node_id: str, mistake_id: str, note: str = "") -> dict:
        return self.graph.record_evidence(child_id=child_id, node_id=node_id, correctness=0.0,
                                          independent=False, origin_type="mistake", origin_id=mistake_id,
                                          idempotency_key=f"mistake:{mistake_id}", note=note, confidence=.9)

    def apply_exercise_result(self, child_id: str, node_id: str, exercise_id: str, correctness: float,
                              independent: bool, action_id: str, note: str = "", confidence: float = 1.0) -> dict:
        return self.graph.record_evidence(child_id=child_id, node_id=node_id, correctness=correctness,
                                          independent=independent, origin_type="exercise", origin_id=exercise_id,
                                          idempotency_key=f"exercise:{action_id}", note=note, confidence=confidence)
