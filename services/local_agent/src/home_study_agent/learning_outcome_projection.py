"""Read-only Tutor evidence for child models; never infer a node from a topic.

Integration: include ``LearningOutcomeProjection(database).for_child(child_id)``
in the child model payload *before* calculating its projection fingerprint.
``for_child(..., connection=connection)`` shares the caller's read transaction.
The four dimensions are evidence counts, not calibrated mastery probabilities.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .database import Database
from .learning_obligations import cancel_unstarted_plans


PROJECTION_VERSION = "tutor_outcome_projection_v2"


class LearningOutcomeProjection:
    def __init__(self, database: Database):
        self.database = database

    def for_session(self, child_id: str, session_id: str, *, connection=None) -> list[dict[str, Any]]:
        """Use the same provenance/revocation gates for a daily local summary."""
        if connection is None:
            with self.database.connect() as opened:
                opened.execute("BEGIN")
                return self.for_session(child_id, session_id, connection=opened)
        child = connection.execute("""SELECT cp.grade FROM child_profile cp JOIN daily_session ds USING(child_id)
            WHERE cp.child_id=? AND ds.session_id=?""", (child_id, session_id)).fetchone()
        if not child:
            raise ValueError("learning outcome session ownership")
        rows = connection.execute("""SELECT DISTINCT o.* FROM agent_outcome o
            JOIN json_each(o.evidence_refs_json) ref ON json_extract(ref.value,'$.type')='child_response'
            JOIN agent_step s ON s.step_id=json_extract(ref.value,'$.agent_step_id')
            JOIN agent_plan p ON p.plan_id=s.plan_id AND p.objective_id=o.objective_id
            WHERE o.child_id=? AND p.child_id=o.child_id AND p.session_id=?
            ORDER BY o.created_at,o.outcome_id""", (child_id, session_id)).fetchall()
        gates = self._current_gates(connection, {row["objective_id"] for row in rows})
        return [self._item(connection, dict(row), child["grade"], gates=gates) for row in rows]

    def review_outcome(self, child_id: str, outcome_id: str, *, decision: str,
                       reason: str, client_action_id: str) -> dict[str, Any]:
        """Local parent adapter must authorize this command; history is append-only."""
        if decision not in {"revoke", "restore"} or not reason.strip() or not client_action_id.strip():
            raise ValueError("review requires revoke/restore, a reason and an idempotency key")
        if len(reason) > 800 or len(client_action_id) > 200:
            raise ValueError("review input too long")
        request = {"child_id": child_id, "outcome_id": outcome_id, "decision": decision, "reason": reason.strip()}
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM agent_outcome WHERE outcome_id=? AND child_id=?",
                                      (outcome_id, child_id)).fetchone():
                raise ValueError("outcome is not owned by this child")
            connection.execute("""CREATE TABLE IF NOT EXISTS learning_outcome_review_event (
                event_id TEXT PRIMARY KEY, outcome_id TEXT NOT NULL REFERENCES agent_outcome(outcome_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                decision TEXT NOT NULL CHECK(decision IN ('revoke','restore')), reason TEXT NOT NULL,
                client_action_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            )""")
            old = connection.execute("SELECT * FROM learning_outcome_review_event WHERE client_action_id=?",
                                     (client_action_id,)).fetchone()
            if old:
                if any(old[key] != value for key, value in request.items()):
                    raise ValueError("review idempotency key conflicts with an earlier request")
                return dict(old)
            latest = connection.execute("SELECT decision FROM learning_outcome_review_event WHERE outcome_id=? ORDER BY rowid DESC LIMIT 1",
                                        (outcome_id,)).fetchone()
            if decision == "restore" and (not latest or latest["decision"] != "revoke"):
                raise ValueError("only a revoked outcome can be restored")
            event = {**request, "event_id": str(uuid4()), "client_action_id": client_action_id,
                     "created_at": datetime.now(timezone.utc).isoformat()}
            connection.execute("""INSERT INTO learning_outcome_review_event
                (event_id,outcome_id,child_id,decision,reason,client_action_id,created_at)
                VALUES (:event_id,:outcome_id,:child_id,:decision,:reason,:client_action_id,:created_at)""", event)
            self._reconcile_reviewed_completion(connection, event)
            from .learning_outcome_coordinator import LearningOutcomeCoordinator
            LearningOutcomeCoordinator(self.database).reconcile_child(child_id, connection=connection)
            connection.commit()
            return event

    def available_outcome(self, connection, child_id: str, outcome_id: str, objective_id: str) -> bool:
        """Legacy source-readability API used by daily closure to propose checks.

        This is not certification. Mastery consumers must use qualified_outcome.
        """
        return self.source_available_outcome(connection, child_id, outcome_id, objective_id)

    def qualified_outcome(self, connection, child_id: str, outcome_id: str, objective_id: str) -> bool:
        """Current certification, without preserving a downgraded historical claim."""
        row = connection.execute("""SELECT o.*,cp.grade FROM agent_outcome o
            JOIN child_profile cp USING(child_id) WHERE o.child_id=? AND o.outcome_id=? AND o.objective_id=?""",
            (child_id, outcome_id, objective_id)).fetchone()
        if not row:
            return False
        item = self._item(connection, dict(row), row["grade"])
        rank = {"exposed": 0, "understood_now": 1, "independent": 2, "transferred": 3, "retained": 4}
        return (item["evidence_status"] == "available"
                and rank.get(item["outcome_level"], -1) >= rank.get(row["outcome_level"], 5))

    def source_available_outcome(self, connection, child_id: str, outcome_id: str, objective_id: str) -> bool:
        """Provenance only. The evaluator uses this layer, never the gated reader."""
        row = connection.execute("""SELECT o.*,cp.grade FROM agent_outcome o
            JOIN child_profile cp USING(child_id) WHERE o.child_id=? AND o.outcome_id=? AND o.objective_id=?""",
            (child_id, outcome_id, objective_id)).fetchone()
        return bool(row and self._source_item(connection, dict(row), row["grade"])["evidence_status"] == "available")

    def commitment_available(self, connection, commitment) -> bool:
        try:
            payload = json.loads(commitment["payload_json"])
        except (ValueError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("origin") == "retained_outcome_recheck":
            # The parent's review is the authority to recheck, not the revoked score.
            review = connection.execute("""SELECT e.outcome_id FROM learning_outcome_review_event e
                JOIN agent_outcome o ON o.outcome_id=e.outcome_id AND o.child_id=e.child_id
                JOIN agent_objective a ON a.objective_id=o.objective_id AND a.child_id=o.child_id
                WHERE e.event_id=? AND e.child_id=? AND o.objective_id=? AND e.decision='revoke'
                  AND o.outcome_level='retained' AND a.objective_type='learning'
                  AND (SELECT latest.decision FROM learning_outcome_review_event latest
                       WHERE latest.outcome_id=e.outcome_id AND latest.child_id=e.child_id
                       ORDER BY latest.rowid DESC LIMIT 1)='revoke'""",
                (payload.get("review_event_id"), commitment["child_id"], commitment["objective_id"])).fetchone()
            # A new passing answer must not invalidate its in-flight task receipt
            # between evaluator commit and task completion.
            gates = self._current_gates(connection, {commitment["objective_id"]}) if review else {}
            return bool(review and any(item["outcome_level"] in {"transferred", "retained"}
                                       and item["eligible"] for item in gates.values()))
        source = payload.get("source_outcome_id")
        if source:
            if commitment["commitment_type"] == "follow_up":
                # A readable candidate may justify another check, never mastery.
                return self.source_available_outcome(connection, commitment["child_id"], source, commitment["objective_id"])
            if not self.qualified_outcome(connection, commitment["child_id"], source, commitment["objective_id"]):
                return False
            gate = self._current_gates(connection, {commitment["objective_id"]}).get(source)
            return bool(gate and gate["outcome_level"] in {"transferred", "retained"})
        if payload.get("origin") == "daily_learning_closure":
            # Explanation-only followups have a step, not an evaluated outcome.
            step = connection.execute("""SELECT s.capability_name FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE s.step_id=? AND p.child_id=? AND p.objective_id=? AND s.status IN ('completed','waiting_child')""",
                (payload.get("source_step_id"), commitment["child_id"], commitment["objective_id"])).fetchone()
            return bool(step and step[0] != "learning.evaluate_response")
        # Older retention receipts predate source_outcome_id. Bind only the
        # transferred outcome that existed when the receipt was created.
        source = connection.execute("""SELECT outcome_id FROM agent_outcome WHERE child_id=? AND objective_id=?
            AND outcome_level='transferred' AND julianday(created_at)<=julianday(?)
            ORDER BY julianday(created_at) DESC,rowid DESC LIMIT 1""",
            (commitment["child_id"], commitment["objective_id"], commitment["created_at"])).fetchone()
        return bool(source and self.qualified_outcome(connection, commitment["child_id"], source[0], commitment["objective_id"]))

    def _has_retained_basis(self, connection, child_id: str, objective_id: str) -> bool:
        owned = connection.execute("""SELECT 1 FROM agent_objective
            WHERE child_id=? AND objective_id=? AND objective_type='learning'""",
            (child_id, objective_id)).fetchone()
        if not owned:
            return False
        return any(item["eligible"] and item["outcome_level"] == "retained"
                   for item in self._current_gates(connection, {objective_id}).values())

    def _reconcile_reviewed_completion(self, connection, event: dict[str, Any]) -> None:
        objective = connection.execute("""SELECT a.* FROM agent_objective a
            JOIN agent_outcome o ON o.objective_id=a.objective_id AND o.child_id=a.child_id
            WHERE o.outcome_id=? AND a.child_id=? AND a.objective_type='learning'
              AND o.outcome_level='retained'""", (event["outcome_id"], event["child_id"])).fetchone()
        if not objective:
            return
        child_id, objective_id, now = event["child_id"], objective["objective_id"], event["created_at"]
        has_basis = self._has_retained_basis(connection, child_id, objective_id)
        if has_basis:
            # Restore only an idle recheck we created; do not interrupt another workflow.
            if event["decision"] == "restore" and (objective["status"], objective["phase"]) == ("paused", "retention_due"):
                pending = connection.execute("""SELECT commitment_id,payload_json FROM agent_commitment
                    WHERE child_id=? AND objective_id=? AND status='pending'
                      AND json_extract(payload_json,'$.origin')='retained_outcome_recheck'
                    ORDER BY created_at DESC LIMIT 1""", (child_id, objective_id)).fetchone()
                if pending:
                    connection.execute("""UPDATE agent_objective SET status='completed',phase='completed',
                        completed_at=?,updated_at=? WHERE objective_id=?""",
                        (json.loads(pending["payload_json"])["previous_completed_at"], now, objective_id))
                    connection.execute("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=?",
                                       (pending["commitment_id"],))
                    cancel_unstarted_plans(connection, child_id, "agent_retention", pending["commitment_id"], now)
            return
        if event["decision"] != "revoke" or objective["status"] != "completed":
            return
        question = connection.execute("""SELECT s.arguments_json FROM agent_outcome o
            JOIN json_each(o.evidence_refs_json) ref ON json_extract(ref.value,'$.type')='child_response'
            JOIN agent_step s ON s.step_id=json_extract(ref.value,'$.agent_step_id')
            JOIN agent_plan p ON p.plan_id=s.plan_id AND p.objective_id=o.objective_id AND p.child_id=o.child_id
            JOIN daily_session ds ON ds.session_id=p.session_id AND ds.child_id=o.child_id
            WHERE o.outcome_id=? AND o.child_id=? AND s.capability_name='learning.evaluate_response'""",
            (event["outcome_id"], child_id)).fetchone()
        arguments = json.loads(question[0]) if question else {}
        payload = {
            "origin": "retained_outcome_recheck", "review_event_id": event["event_id"],
            "revoked_outcome_id": event["outcome_id"], "previous_completed_at": objective["completed_at"],
            "phase": "retention_check", "summary": objective["summary"],
            "subject": str(arguments.get("subject") or "复习"),
            "prompt": str(arguments.get("question_asked") or "").strip() or f"请独立说明并举例：{objective['summary']}",
        }
        # Keep completed attempts/commitments intact. Outstanding old reviews are
        # superseded by this one; the transaction serializes repeated revocations.
        outstanding = connection.execute("""SELECT commitment_id FROM agent_commitment
            WHERE child_id=? AND objective_id=? AND commitment_type IN ('review','follow_up')
              AND status IN ('pending','due')""", (child_id, objective_id)).fetchall()
        for row in outstanding:
            connection.execute("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=?", (row[0],))
            cancel_unstarted_plans(connection, child_id, "agent_retention", row[0], now)
        connection.execute("""UPDATE agent_objective SET status='paused',phase='retention_due',
            completed_at=NULL,updated_at=? WHERE objective_id=?""", (now, objective_id))
        connection.execute("""INSERT INTO agent_commitment
            (commitment_id,objective_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
            VALUES (?,?,?,'review',?,'pending',?,?,?)""",
            (str(uuid4()), objective_id, child_id, now, json.dumps(payload, ensure_ascii=False), now,
             f"retained-recheck:{event['event_id']}"))

    def reconcile_commitments(self, connection, child_id: str, now: str) -> None:
        rows = connection.execute("""SELECT ac.* FROM agent_commitment ac JOIN agent_objective ao USING(objective_id)
            WHERE ac.child_id=? AND ao.child_id=ac.child_id AND ao.objective_type='learning'
            AND ac.status IN ('pending','due','cancelled') AND (ac.commitment_type='review' OR
                (ac.commitment_type='follow_up' AND CASE WHEN json_valid(ac.payload_json)
                    THEN json_extract(ac.payload_json,'$.origin') END='daily_learning_closure'))""", (child_id,)).fetchall()
        for row in rows:
            if not self.commitment_available(connection, row):
                connection.execute("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=? AND status IN ('pending','due')", (row["commitment_id"],))
                cancel_unstarted_plans(connection, child_id, "agent_retention", row["commitment_id"], now)

    def for_child(self, child_id: str, *, limit: int = 10, connection=None) -> dict[str, Any]:
        if connection is None:
            with self.database.connect() as opened:
                opened.execute("BEGIN")
                return self.for_child(child_id, limit=limit, connection=opened)
        child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'",
                                   (child_id,)).fetchone()
        if not child:
            raise ValueError("unknown active child")
        rows = connection.execute("""SELECT o.*, a.status AS objective_status
            FROM agent_outcome o JOIN agent_objective a ON a.objective_id=o.objective_id
            WHERE o.child_id=? AND a.child_id=? ORDER BY o.created_at,o.outcome_id""",
            (child_id, child_id)).fetchall()
        gates = self._current_gates(connection, {row["objective_id"] for row in rows})
        items = [self._item(connection, dict(row), child["grade"], gates=gates) for row in rows]
        nodes: dict[str, Any] = {}
        levels: dict[str, int] = {}
        for item in items:
            if item["evidence_status"] != "available":
                continue
            level = item["outcome_level"]
            levels[level] = levels.get(level, 0) + 1
            if item["mapping_status"] != "mapped":
                continue
            node_id = item["mapping"]["node_id"]
            node = nodes.setdefault(node_id, {
                **item["mapping"], "evidence_count": 0,
                "dimensions": {"understanding": 0, "independence": 0, "transfer": 0, "retention": 0},
                "mastery_probability": None, "memory_stability_days": None,
                "calibration_status": "not_calibrated", "outcome_ids": [],
            })
            node["evidence_count"] += 1
            node["outcome_ids"].append(item["outcome_id"])
            # The current gate already verifies the applicable scoring authority.
            for dimension, eligible in {
                "understanding": {"understood_now", "independent", "transferred", "retained"},
                "independence": {"independent", "transferred", "retained"},
                "transfer": {"transferred"}, "retention": {"retained"},
            }.items():
                node["dimensions"][dimension] += int(level in eligible)
        fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {
            "version": PROJECTION_VERSION, "child_id": child_id, "fingerprint": fingerprint,
            "outcome_count": len(items), "available_count": sum(i["evidence_status"] == "available" for i in items),
            "mapped_count": sum(i["mapping_status"] == "mapped" and i["evidence_status"] == "available" for i in items),
            "unmapped_count": sum(i["mapping_status"] == "unmapped" and i["evidence_status"] == "available" for i in items),
            "revoked_count": sum(i["evidence_status"] in {"revoked", "source_revoked"} for i in items),
            "source_unavailable_count": sum(i["evidence_status"] == "source_unavailable" for i in items),
            "candidate_count": sum(i["evidence_status"] == "candidate" for i in items),
            "level_counts": levels, "knowledge_evidence": list(nodes.values()),
            "recent_outcomes": list(reversed(items))[:max(0, min(limit, 50))],
            "dimension_kind": "model_rubric_evidence_counts_not_calibrated_scores",
            "boundary": "model_rubric_evidence_not_verified_mastery",
        }

    def _current_gates(self, connection, objective_ids) -> dict:
        from .learning_outcome_evaluator import LearningOutcomeEvaluator
        evaluator = LearningOutcomeEvaluator(self.database, None, None, None, initialize=False)
        return {item["outcome_id"]: item for objective_id in sorted(objective_ids)
                for item in evaluator.current_mastery_gate(objective_id, connection=connection)["outcomes"]}

    def _item(self, connection, row: dict[str, Any], grade: str, *, gates=None) -> dict[str, Any]:
        item = self._source_item(connection, row, grade)
        gates = self._current_gates(connection, {row["objective_id"]}) if gates is None else gates
        gate = gates[row["outcome_id"]]
        item.update(recorded_outcome_level=row["outcome_level"], outcome_level=gate["outcome_level"],
                    mastery_gate=gate, certification_status="qualified" if gate["eligible"] else "candidate")
        if item["evidence_status"] == "available" and not gate["eligible"]:
            item["evidence_status"] = "candidate"
        return item

    def _source_item(self, connection, row: dict[str, Any], grade: str) -> dict[str, Any]:
        refs = json.loads(row["evidence_refs_json"])
        step_ids = [r.get("agent_step_id") for r in refs if isinstance(r, dict) and r.get("type") == "child_response"]
        step = None
        if len(step_ids) == 1:
            step = connection.execute("""SELECT s.arguments_json,p.session_id,s.step_id
                FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id
                JOIN daily_session ds ON ds.session_id=p.session_id
                WHERE s.step_id=? AND p.objective_id=? AND p.child_id=? AND ds.child_id=?
                  AND s.capability_name='learning.evaluate_response'""",
                (step_ids[0], row["objective_id"], row["child_id"], row["child_id"])).fetchone()
        try:
            arguments = json.loads(step["arguments_json"]) if step else None
        except (ValueError, TypeError):
            arguments = None
        arguments_current = isinstance(arguments, dict)
        if not arguments_current:
            arguments = {}
        mapping = self._mapping(connection, arguments, row["child_id"], grade)
        call_ids = [r.get("model_call_id") for r in refs if isinstance(r, dict) and r.get("type") == "model_rubric"]
        model_source = None
        if step and len(call_ids) == 1:
            model_source = connection.execute("""SELECT status,verification_status,safety_status FROM model_call
                WHERE call_id=? AND child_id=? AND session_id=? AND purpose='learning_outcome_evaluation'""",
                (call_ids[0], row["child_id"], step["session_id"])).fetchone()
        available = bool(arguments_current and step and model_source and model_source["status"] == "completed"
                         and model_source["verification_status"] in {"verified", "partially_verified"}
                         and model_source["safety_status"] == "passed"
                         and str(arguments.get("response") or "").strip())
        local_ids = [r.get("assessment_id") for r in refs if isinstance(r, dict) and r.get("type") == "local_rubric"]
        if arguments_current and step and len(local_ids) == 1 and not call_ids:
            from .learning_rubric import RubricEvidenceService
            available = RubricEvidenceService(self.database).local_source_available(
                connection, row["child_id"], step["step_id"], local_ids[0])
        evidence_status = "available" if available else "source_unavailable"
        if model_source and (model_source["verification_status"] == "rejected" or model_source["safety_status"] == "rejected"):
            evidence_status = "source_revoked"
        review = None
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_outcome_review_event'").fetchone():
            latest = connection.execute("""SELECT event_id,decision,reason,created_at FROM learning_outcome_review_event
                WHERE outcome_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1""", (row["outcome_id"], row["child_id"])).fetchone()
            review = dict(latest) if latest else None
            if review and review["decision"] == "revoke":
                evidence_status = "revoked"
        return {
            "outcome_id": row["outcome_id"], "objective_id": row["objective_id"],
            "outcome_level": row["outcome_level"], "confidence": row["confidence"],
            "created_at": row["created_at"], "summary": row["summary"],
            "evidence_refs": refs, "evidence_status": evidence_status, "review": review,
            "mapping_status": "mapped" if mapping else "unmapped",
            "mapping": mapping,
            "unmapped_reason": None if mapping else "no_unique_current_approved_question_mapping",
            "source_fingerprint": hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        }

    @staticmethod
    def _mapping(connection, arguments: dict[str, Any], child_id: str, grade: str) -> dict[str, Any] | None:
        question = str(arguments.get("question_asked") or "").strip()
        if not question:
            return None
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_node'").fetchone():
            return None
        # Exact question identity + one reviewed assessed_by edge. A planner's
        # self-reported knowledge_node_id, topic or nearest text is not evidence.
        rows = connection.execute("""SELECT di.diagnostic_item_id,di.knowledge_node_id,
                n.node_id,n.title,n.scope_json,n.version_label,e.edge_id
            FROM diagnostic_item di
            JOIN knowledge_node host ON host.knowledge_node_id=di.knowledge_node_id AND host.status='active'
            JOIN kg_node exercise ON exercise.canonical_key='diagnostic:' || di.diagnostic_item_id
            JOIN kg_edge e ON e.target_node_id=exercise.node_id AND e.relation_type='assessed_by'
            JOIN kg_node n ON n.node_id=e.source_node_id
            WHERE trim(di.prompt)=? AND di.status='active' AND host.subject=?
              AND exercise.node_type='exercise' AND exercise.status='active' AND exercise.review_status='approved'
              AND n.node_type='knowledge_point' AND n.status='active' AND n.review_status='approved'
              AND e.review_status='approved'""", (question, str(arguments.get("subject") or ""))).fetchall()
        if len(rows) != 1:
            return None
        node = rows[0]
        scope = json.loads(node["scope_json"] or "{}")
        scope_grade = str(scope.get("grade") or "")
        if scope.get("child_id") not in {None, child_id}:
            return None
        if scope_grade and scope_grade != grade and not scope_grade.startswith(f"{grade}-"):
            return None
        evidence = connection.execute("""SELECT 1 WHERE EXISTS (
            SELECT 1 FROM kg_resource_node rn JOIN kg_resource r ON r.resource_id=rn.resource_id
            JOIN kg_corpus c ON c.corpus_id=r.corpus_id
            WHERE rn.node_id=? AND rn.review_status='approved' AND r.review_status='approved'
              AND c.review_status='approved'
        ) OR EXISTS (
            SELECT 1 FROM kg_edge WHERE (source_node_id=? OR target_node_id=?)
              AND relation_type='aligned_to' AND review_status='approved'
              AND (source_ref LIKE 'curriculum_standard:%' OR source_ref LIKE 'product_evidence:%')
        )""", (node["node_id"], node["node_id"], node["node_id"])).fetchone()
        if not evidence:
            return None
        return {key: node[key] for key in (
            "node_id", "knowledge_node_id", "title", "version_label", "edge_id", "diagnostic_item_id"
        )}
