"""Local adapters for reliable answers and conservative objective reconciliation.

The runtime authorizes and persists the returned evaluation plan. This service
does not invoke models, create turns, resume children or initialize any tables.
"""
from datetime import timedelta
import hashlib
import json

from .learning_outcome_evaluator import LearningOutcomeEvaluator
from .learning_outcome_projection import LearningOutcomeProjection
from .learning_rubric import evaluation_source_boundary


class LearningOutcomeCoordinator:
    def __init__(self, database):
        self.database = database
        self.evaluator = LearningOutcomeEvaluator(database, None, None, None, initialize=False)

    def diagnostic_evaluation_input(self, child_id: str, objective_id: str, evidence_id: str) -> dict:
        """Local-only receipt inspection. A ready receipt is not a cloud dispatch."""
        result = self._diagnostic_evaluation_input(child_id, objective_id, evidence_id)
        with self.database.connect() as c:
            c.execute("BEGIN")
            boundary = evaluation_source_boundary(c, child_id, evidence_id=evidence_id, objective_id=objective_id)
            receipt = self.evaluator._receipts(c, child_id, self._now()).get(evidence_id)
        return {**result, "local_only": True, "cloud_transmission_allowed": False,
                "client_action_id": f"diagnostic-objective:{objective_id}:{evidence_id}",
                "evaluation_route": "local_criterion_review_required",
                "execution_method": "prepare_local_evaluation",
                "criteria_linkage": "local_reviewed_exact_criteria_and_response_quotes_required",
                "criterion_mapping_status": "requires_local_review", "automatic_criterion_mapping": False,
                "source_boundary": boundary, "local_receipt": ({"score_status": "verified_local",
                    "score_authority": "learning_obligations_current_effective_evidence",
                    **{key: receipt.get(key) for key in (
                    "evidence_id", "correctness", "independent", "hint_level", "question_identity", "occurred_at")}}
                    if receipt else None)}

    def _diagnostic_evaluation_input(self, child_id: str, objective_id: str, evidence_id: str) -> dict:
        """Call after submit_diagnostic commits, before persisting an evaluation step."""
        with self.database.connect() as c:
            c.execute("BEGIN")
            objective = c.execute("SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND objective_type='learning'",
                                  (objective_id, child_id)).fetchone()
            if not objective:
                raise ValueError("LEARNING_OBJECTIVE_NOT_OWNED")
            row = c.execute("""SELECT le.evidence_id,le.occurred_at,le.session_id,da.submitted_answer,
                    di.prompt,kn.subject,ds.status AS session_status
                FROM learning_evidence le JOIN diagnostic_attempt da ON da.evidence_id=le.evidence_id
                JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
                JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
                JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                WHERE le.evidence_id=? AND le.child_id=? AND da.child_id=le.child_id
                  AND da.session_id=le.session_id""", (evidence_id, child_id)).fetchone()
            if not row:
                raise ValueError("LEARNING_DIAGNOSTIC_EVIDENCE_NOT_OWNED")
            now = self._now()
            receipt = self.evaluator._receipts(c, child_id, now).get(evidence_id)
            if (not receipt or receipt["correctness"] != 1 or not receipt["independent"] or receipt["hint_level"]):
                return {"status": "candidate", "evidence_id": evidence_id,
                        "reason": "diagnostic_evidence_not_current_independent_correct"}
            if objective["status"] != "active" or row["session_status"] != "active":
                return {"status": "blocked", "reason": "objective_or_session_not_active"}
            try:
                if self.evaluator._time(row["occurred_at"]) < self.evaluator._time(objective["created_at"]):
                    return {"status": "candidate", "reason": "evidence_predates_objective"}
            except (ValueError, TypeError):
                return {"status": "candidate", "reason": "legacy_time_unknown"}
            current = self.evaluator.current_mastery_gate(objective_id, connection=c)
            linked = next((item for item in current["outcomes"] if evidence_id in
                           {item.get("evidence_id"), item.get("source_evidence_id")}), None)
            if linked:
                return {"status": "already_linked", "evidence_id": evidence_id,
                        "outcome_id": linked["outcome_id"]}
            phase = {"independent": "transfer_check", "transferred": "retention_check"}.get(current["outcome_level"], "check")
            criteria = json.loads(objective["success_criteria_json"])
            return {"status": "ready", "session_id": row["session_id"], "objective_id": objective_id,
                    "client_action_id": f"diagnostic-objective:{objective_id}:{evidence_id}",
                    "objective": {"mode": "continue", "type": "learning", "summary": objective["summary"],
                                  "success_criteria": criteria, "phase": phase},
                    "capability": "learning.evaluate_response",
                    "arguments": {"evidence_id": evidence_id, "response": row["submitted_answer"],
                                  "question_asked": row["prompt"], "subject": row["subject"]},
                    "criteria_linkage": "model_rubric_exact_criteria_and_response_quotes_required",
                    "evidence_kind": "existing_locally_verified_diagnostic_receipt"}

    @staticmethod
    def _now():
        # Share the evaluator clock, including deterministic isolated test clocks.
        from .learning_outcome_evaluator import datetime, timezone
        return datetime.now(timezone.utc)

    def _recommendation(self, c, row) -> dict:
        gate = self.evaluator.current_mastery_gate(row["objective_id"], connection=c)
        level = gate["outcome_level"]
        phase = {"independent": "transfer_check", "transferred": "retention_check", "retained": "completed"}.get(level, "check")
        basis = [item for item in gate["outcomes"] if item["outcome_level"] == level and item.get("eligible")]
        due = None
        if basis and level in {"independent", "transferred"}:
            due = (max(self.evaluator._time(item["occurred_at"]) for item in basis)
                   + timedelta(hours=24 if level == "independent" else 72)).isoformat()
        covered = {criterion for item in gate["outcomes"] if item.get("eligible") for criterion in item["covered_criteria"]}
        fingerprint = hashlib.sha256(json.dumps(gate, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return {"objective_id": row["objective_id"], "recorded_status": row["status"],
                "local_only": True, "cloud_transmission_allowed": False,
                "recorded_phase": row["phase"], "outcome_level": level,
                "requires_recheck": level != "retained", "next_phase": phase,
                "not_before": due, "gate_fingerprint": fingerprint,
                "missing_criteria": [value for value in json.loads(row["success_criteria_json"]) if value not in covered],
                "exclude_question_identities": sorted({identity for item in gate["outcomes"]
                    for identity in [item.get("question_identity"), *item.get("question_identities", [])] if identity}),
                "mastery_gate": gate}

    def inspect_child(self, child_id: str, *, connection=None) -> list[dict]:
        """Durable recheck inputs, also available after a process restart."""
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN")
                return self.inspect_child(child_id, connection=c)
        rows = connection.execute("""SELECT * FROM agent_objective WHERE child_id=? AND objective_type='learning'
            AND status NOT IN ('cancelled','failed') ORDER BY created_at,objective_id""", (child_id,)).fetchall()
        return [self._recommendation(connection, row) for row in rows]

    def reconcile_child(self, child_id: str, *, connection=None) -> list[dict]:
        """Explicit local write hook after evidence review and before scheduling.

        No automatic activation or new commitments. The caller schedules checks
        from these durable recommendations using its existing authorization path.
        """
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                result = self.reconcile_child(child_id, connection=c)
                c.commit()
                return result
        items = self.inspect_child(child_id, connection=connection)
        now = self._now().isoformat()
        for item in items:
            incompatible = (item["recorded_status"] == "completed" and item["outcome_level"] != "retained"
                or item["recorded_status"] == "paused" and item["recorded_phase"] == "retention_due"
                and item["outcome_level"] not in {"transferred", "retained"})
            if incompatible:
                phase = {"independent": "transfer_check", "transferred": "retention_due"}.get(item["outcome_level"], "remediate")
                connection.execute("""UPDATE agent_objective SET status='paused',phase=?,completed_at=NULL,updated_at=?
                    WHERE objective_id=? AND child_id=? AND status=? AND phase=?""",
                    (phase, now, item["objective_id"], child_id, item["recorded_status"], item["recorded_phase"]))
        LearningOutcomeProjection(self.database).reconcile_commitments(connection, child_id, now)
        return self.inspect_child(child_id, connection=connection)
