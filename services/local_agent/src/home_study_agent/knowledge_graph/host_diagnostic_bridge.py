"""Bind KG mastery evidence to the host's already-scored diagnostic attempts.

The public KG API deliberately never accepts a client-reported correctness value.
This adapter reads the durable host attempt after `LearningStateService` has scored
and committed it, then projects only approved KG exercise relationships.
"""
from __future__ import annotations

from ..database import Database
from .application_service import KnowledgeGraphApplicationService


class HostDiagnosticKnowledgeGraphBridge:
    def __init__(self, database: Database):
        self.database = database
        self.facade = KnowledgeGraphApplicationService(database)

    def record_attempt(self, attempt_id: str, *, mistake_id: str | None = None) -> dict:
        with self.database.connect() as connection:
            attempt = connection.execute("""SELECT da.attempt_id,da.child_id,da.diagnostic_item_id,da.correctness,
                di.assessment_mode,di.knowledge_node_id,le.independent,le.confidence
                FROM diagnostic_attempt da JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
                JOIN learning_evidence le ON le.evidence_id=da.evidence_id
                WHERE da.attempt_id=?""", (attempt_id,)).fetchone()
            if not attempt:
                raise ValueError("host diagnostic attempt was not found")
            if attempt['assessment_mode'] != 'objective':
                return self._manual("this assessment mode is reference-only and is not calibrated mastery evidence")
            qualified = self.facade.graph.host_attempt_evidence(connection, attempt_id)
            if qualified is None:
                return self._manual("host receipt is excluded by the current reliable projection or approved KG binding")
            attempt = qualified
            exercise = connection.execute("SELECT node_id,review_status,status FROM kg_node WHERE canonical_key=?",
                (f"diagnostic:{attempt['diagnostic_item_id']}",)).fetchone()
            if not exercise or exercise['review_status'] != 'approved' or exercise['status'] != 'active':
                return self._manual("approved exercise projection is unavailable")
            relations = connection.execute("""SELECT source_node_id FROM kg_edge WHERE target_node_id=?
                AND relation_type='assessed_by' AND review_status='approved'""", (exercise['node_id'],)).fetchall()
        if len(relations) != 1:
            return self._manual("approved exercise must have exactly one approved assessed-by knowledge point")
        relation = relations[0]
        mistake_recorded = False
        if mistake_id:
            with self.database.connect() as connection:
                mistake = connection.execute("""SELECT mistake_id FROM mistake_case WHERE mistake_id=? AND child_id=?
                    AND diagnostic_item_id=?""", (mistake_id, attempt['child_id'], attempt['diagnostic_item_id'])).fetchone()
            if not mistake:
                return self._manual("host mistake is not attributable to this scored diagnostic attempt")
        try:
            # This validates child, grade and current approved material evidence.
            if mistake_id:
                # One failed observation is one mastery update.  The host
                # mistake case is the origin for that sole evidence row, while
                # the approved exercise relation above remains the validation
                # that makes this diagnostic assess the selected atom.
                self.facade._require_node_for_child(attempt['child_id'], relation['source_node_id'])
                state = self.facade.mastery.graph.record_evidence(
                    child_id=attempt['child_id'], node_id=relation['source_node_id'], correctness=float(attempt['correctness']),
                    independent=bool(attempt['independent']), origin_type='mistake', origin_id=mistake_id,
                    idempotency_key=f"host-diagnostic:{attempt['attempt_id']}", note='host_scored_diagnostic_mistake',
                    confidence=float(attempt['confidence']),
                )
                mistake_recorded = True
            else:
                state = self.facade.record_exercise_result(
                    child_id=attempt['child_id'], node_id=relation['source_node_id'], exercise_id=exercise['node_id'],
                    correctness=float(attempt['correctness']), independent=bool(attempt['independent']), confidence=float(attempt['confidence']),
                    action_id=f"host-diagnostic:{attempt['attempt_id']}", note="host_scored_diagnostic_attempt",
                )
        except ValueError as error:
            return self._manual(str(error))
        return {"status": "recorded", "mastery": state, "node_id": relation['source_node_id'],
                "exercise_id": exercise['node_id'], "mistake_recorded": mistake_recorded,
                "result_source": "host_scored_diagnostic_attempt"}

    @staticmethod
    def _manual(reason: str) -> dict:
        return {"status": "manual_confirmation_required", "reason": reason,
                "result_source": "host_attempt_not_approved_for_kg_mastery"}
