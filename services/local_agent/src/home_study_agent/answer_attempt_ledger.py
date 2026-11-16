from __future__ import annotations

import json
from typing import Any

from .database import Database
from .learning_obligations import project_child_mastery_evidence


def ensure_visual_promotion_schema(connection: Any) -> None:
    """Create the immutable, source-qualified bridge from vision to a mistake case."""
    existing = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='visual_answer_evidence_promotion'"
    ).fetchone()
    if existing and "qualified_scoring_authority" not in (existing["sql"] or ""):
        connection.execute("DROP TRIGGER IF EXISTS visual_answer_evidence_promotion_no_update")
        connection.execute("DROP INDEX IF EXISTS idx_visual_answer_promotion_child")
        connection.execute(
            "ALTER TABLE visual_answer_evidence_promotion "
            "RENAME TO visual_answer_evidence_promotion_legacy_v1"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS visual_answer_evidence_promotion (
          promotion_id TEXT PRIMARY KEY,
          visual_answer_evidence_id TEXT NOT NULL UNIQUE
            REFERENCES observed_answer_evidence(visual_answer_evidence_id) ON DELETE CASCADE,
          candidate_id TEXT NOT NULL
            REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
          mistake_id TEXT NOT NULL
            REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          confirmed_answer TEXT NOT NULL,
          confirmation_source TEXT NOT NULL
            CHECK(confirmation_source IN (
              'child_spoken_fact_confirmation',
              'qualified_scoring_authority',
              'child_spoken_confirmation'
            )),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_visual_answer_promotion_child
          ON visual_answer_evidence_promotion(child_id, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS visual_answer_evidence_promotion_no_update
        BEFORE UPDATE ON visual_answer_evidence_promotion
        BEGIN SELECT RAISE(ABORT, 'immutable visual answer promotion'); END;
        """
    )
    legacy_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='visual_answer_evidence_promotion_legacy_v1'"
    ).fetchone()
    if legacy_exists:
        connection.execute(
            """INSERT INTO visual_answer_evidence_promotion(
                 promotion_id,visual_answer_evidence_id,candidate_id,mistake_id,child_id,
                 confirmed_answer,confirmation_source,created_at,client_action_id
               )
               SELECT promotion_id,visual_answer_evidence_id,candidate_id,mistake_id,child_id,
                      confirmed_answer,confirmation_source,created_at,client_action_id
               FROM visual_answer_evidence_promotion_legacy_v1"""
        )
        connection.execute("DROP TABLE visual_answer_evidence_promotion_legacy_v1")


class AnswerAttemptLedger:
    """Read-only projection of visual observations and scored answer receipts."""

    def __init__(self, database: Database):
        self.database = database

    def list_for_child(self, child_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        if not isinstance(child_id, str) or not child_id.strip():
            raise ValueError("invalid child ID")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("answer attempt ledger limit must be between 1 and 500")
        # The parent ledger reconciles stale scoring authority as part of its
        # read. That reconciliation can withdraw report evidence.
        with self.database.external_effect_guard(timeout=2), self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id=?", (child_id,)
            ).fetchone():
                raise ValueError("unknown child")
            from .paper_scoring import PaperScoringService
            PaperScoringService.reconcile_subjective_decisions(
                connection, child_id=child_id
            )
            PaperScoringService.reconcile_source_decisions(
                connection, child_id=child_id
            )
            effective, excluded = self._qualification(connection, child_id)
            rows = self._visual_rows(connection, child_id)
            rows.extend(self._diagnostic_rows(connection, child_id, effective, excluded))
            rows.extend(self._mistake_rows(connection, child_id, effective, excluded))
            connection.commit()
        rows.sort(key=lambda row: (row["occurred_at"], row["ledger_entry_id"]), reverse=True)
        return rows[:limit]

    @staticmethod
    def _qualification(connection: Any, child_id: str) -> tuple[set[str], dict[str, str]]:
        projection = project_child_mastery_evidence(connection, child_id)
        effective = {
            row["evidence_id"]
            for node in projection.values()
            for row in node.get("effective_evidence", [])
        }
        excluded = {
            row["evidence_id"]: row.get("reason") or "not_effective"
            for node in projection.values()
            for row in node.get("excluded_evidence", [])
        }
        return effective, excluded

    @staticmethod
    def _visual_rows(connection: Any, child_id: str) -> list[dict[str, Any]]:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observed_answer_evidence'"
        ).fetchone():
            return []
        promotion_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visual_answer_evidence_promotion'"
        ).fetchone()
        scoring_request_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_scoring_request'"
        ).fetchone()
        if scoring_request_exists and promotion_exists:
            promotion_select = (
                "CASE WHEN scoring.request_id IS NULL THEN promotion.mistake_id "
                "WHEN decision.correctness != 0 THEN NULL "
                "WHEN decision.decision_id=decision.root_decision_id THEN promotion.mistake_id "
                "ELSE revision_promotion.mistake_id END AS promoted_mistake_id,"
                "CASE WHEN scoring.request_id IS NULL THEN promotion.confirmation_source "
                "WHEN decision.correctness != 0 THEN NULL "
                "WHEN decision.decision_id=decision.root_decision_id THEN promotion.confirmation_source "
                "WHEN revision_promotion.mistake_id IS NOT NULL THEN 'qualified_scoring_authority' "
                "ELSE NULL END AS promotion_confirmation_source"
            )
        elif scoring_request_exists:
            promotion_select = (
                "CASE WHEN decision.correctness=0 THEN revision_promotion.mistake_id "
                "ELSE NULL END AS promoted_mistake_id,"
                "CASE WHEN decision.correctness=0 AND revision_promotion.mistake_id IS NOT NULL "
                "THEN 'qualified_scoring_authority' ELSE NULL END AS promotion_confirmation_source"
            )
        elif promotion_exists:
            promotion_select = (
                "promotion.mistake_id AS promoted_mistake_id,"
                "promotion.confirmation_source AS promotion_confirmation_source"
            )
        else:
            promotion_select = "NULL AS promoted_mistake_id,NULL AS promotion_confirmation_source"
        promotion_join = (
            "LEFT JOIN visual_answer_evidence_promotion AS promotion "
            "ON promotion.visual_answer_evidence_id=evidence.visual_answer_evidence_id "
            "AND promotion.child_id=evidence.child_id"
            if promotion_exists else ""
        )
        scoring_request_select = (
            "scoring.request_id AS scoring_request_id,"
            "CASE WHEN decision.decision_id IS NULL THEN scoring.status ELSE 'scored' END AS scoring_request_status,"
            "decision.authority_type AS scoring_authority_type,"
            "decision.correctness AS resolved_correctness"
            if scoring_request_exists
            else "NULL AS scoring_request_id,NULL AS scoring_request_status,"
                 "NULL AS scoring_authority_type,NULL AS resolved_correctness"
        )
        scoring_request_join = (
            "LEFT JOIN paper_scoring_request AS scoring "
            "ON scoring.visual_answer_evidence_id=evidence.visual_answer_evidence_id "
            "AND scoring.child_id=evidence.child_id "
            "LEFT JOIN paper_scoring_visible_decision AS decision ON decision.request_id=scoring.request_id "
            "AND decision.child_id=evidence.child_id "
            "AND decision.effective_status='qualified' "
            "LEFT JOIN paper_scoring_revision_mistake_link AS revision_promotion "
            "ON revision_promotion.revision_id=decision.decision_id "
            "AND revision_promotion.child_id=evidence.child_id"
            if scoring_request_exists else ""
        )
        rows = connection.execute(
            f"""SELECT evidence.visual_answer_evidence_id,evidence.child_id,
                      evidence.session_id,evidence.question_id,evidence.revision_id,
                      evidence.artifact_id,evidence.answer_text,evidence.grading_marks_json,
                      evidence.evidence_json,evidence.confidence,evidence.observed_at,
                      question.canonical_text,{promotion_select},{scoring_request_select}
               FROM observed_answer_evidence AS evidence
               JOIN observed_question AS question ON question.question_id=evidence.question_id
                 AND question.child_id=evidence.child_id
               {promotion_join}
               {scoring_request_join}
               WHERE evidence.child_id=?""",
            (child_id,),
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "ledger_entry_id": row["visual_answer_evidence_id"],
                "record_kind": "visual_observation",
                "child_id": row["child_id"],
                "session_id": row["session_id"],
                "question_ref": row["question_id"],
                "question_text": row["canonical_text"],
                "submitted_answer": row["answer_text"],
                "visible_grading_marks": json.loads(row["grading_marks_json"]),
                "correctness": row["resolved_correctness"],
                "scoring_status": row["scoring_request_status"] or "unverified",
                "qualification_reason": (
                    "authority_scored_but_not_mastery_evidence"
                    if row["resolved_correctness"] is not None else "visual_observation_is_not_a_score"
                ),
                "hint_level": None,
                "independent": None,
                "elapsed_seconds": None,
                "confidence": row["confidence"],
                "mastery_update_allowed": False,
                "cloud_transmission_allowed": False,
                "promoted_mistake_id": row["promoted_mistake_id"],
                "promotion_confirmation_source": row["promotion_confirmation_source"],
                "promotion_status": AnswerAttemptLedger._promotion_status(
                    row["promoted_mistake_id"], row["promotion_confirmation_source"]
                ),
                "scoring_request_id": row["scoring_request_id"],
                "scoring_request_status": row["scoring_request_status"],
                "scoring_authority_type": row["scoring_authority_type"],
                "evidence_refs": {
                    "visual_answer_evidence_id": row["visual_answer_evidence_id"],
                    "revision_id": row["revision_id"],
                    "artifact_id": row["artifact_id"],
                    "visible_blocks": json.loads(row["evidence_json"]),
                },
                "occurred_at": row["observed_at"],
            })
        return result

    @staticmethod
    def _promotion_status(mistake_id: Any, confirmation_source: Any) -> str | None:
        if not mistake_id:
            return None
        if confirmation_source == "qualified_scoring_authority":
            return "authority_confirmed_wrong"
        if confirmation_source == "child_spoken_fact_confirmation":
            return "fact_confirmed_then_scored_wrong"
        if confirmation_source == "child_spoken_confirmation":
            return "legacy_child_confirmation_then_scored_wrong"
        return "promoted_with_unknown_source"

    @staticmethod
    def _scoring(correctness: Any, evidence_id: str, effective: set[str], excluded: dict[str, str]) -> tuple[str, str | None, bool]:
        if correctness is None:
            return "unscored", "score_not_available", False
        if evidence_id in effective:
            return "qualified_effective", None, True
        return "recorded_scored_not_effective", excluded.get(evidence_id, "not_effective"), False

    @classmethod
    def _diagnostic_rows(
        cls, connection: Any, child_id: str, effective: set[str], excluded: dict[str, str]
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT attempt.*,item.prompt,evidence.hint_level,evidence.independent,
                      evidence.confidence AS evidence_confidence,evidence.knowledge_node_id
               FROM diagnostic_attempt AS attempt
               JOIN diagnostic_item AS item ON item.diagnostic_item_id=attempt.diagnostic_item_id
               JOIN learning_evidence AS evidence ON evidence.evidence_id=attempt.evidence_id
                 AND evidence.child_id=attempt.child_id
               WHERE attempt.child_id=?""",
            (child_id,),
        ).fetchall()
        result = []
        for row in rows:
            status, reason, allowed = cls._scoring(row["correctness"], row["evidence_id"], effective, excluded)
            result.append({
                "ledger_entry_id": row["attempt_id"], "record_kind": "diagnostic_attempt",
                "child_id": row["child_id"], "session_id": row["session_id"],
                "question_ref": row["diagnostic_item_id"], "question_text": row["prompt"],
                "submitted_answer": row["submitted_answer"], "visible_grading_marks": [],
                "correctness": row["correctness"], "scoring_status": status,
                "qualification_reason": reason, "hint_level": row["hint_level"],
                "independent": bool(row["independent"]), "elapsed_seconds": None,
                "confidence": row["evidence_confidence"], "mastery_update_allowed": allowed,
                "cloud_transmission_allowed": False,
                "evidence_refs": {"evidence_id": row["evidence_id"],
                                  "diagnostic_item_id": row["diagnostic_item_id"],
                                  "knowledge_node_id": row["knowledge_node_id"]},
                "occurred_at": row["created_at"],
            })
        return result

    @classmethod
    def _mistake_rows(
        cls, connection: Any, child_id: str, effective: set[str], excluded: dict[str, str]
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT attempt.*,item.prompt,evidence.confidence AS evidence_confidence,
                      evidence.knowledge_node_id,evidence.session_id
               FROM mistake_attempt AS attempt
               JOIN diagnostic_item AS item ON item.diagnostic_item_id=attempt.diagnostic_item_id
               JOIN learning_evidence AS evidence ON evidence.evidence_id=attempt.evidence_id
                 AND evidence.child_id=attempt.child_id
               WHERE attempt.child_id=?""",
            (child_id,),
        ).fetchall()
        result = []
        for row in rows:
            status, reason, allowed = cls._scoring(row["correctness"], row["evidence_id"], effective, excluded)
            result.append({
                "ledger_entry_id": row["attempt_id"], "record_kind": "mistake_attempt",
                "child_id": row["child_id"], "session_id": row["session_id"],
                "question_ref": row["diagnostic_item_id"], "question_text": row["prompt"],
                "submitted_answer": row["submitted_answer"], "visible_grading_marks": [],
                "correctness": row["correctness"], "scoring_status": status,
                "qualification_reason": reason, "hint_level": row["hint_level"],
                "independent": bool(row["independent"]), "elapsed_seconds": None,
                "confidence": row["evidence_confidence"], "mastery_update_allowed": allowed,
                "cloud_transmission_allowed": False,
                "evidence_refs": {"evidence_id": row["evidence_id"],
                                  "diagnostic_item_id": row["diagnostic_item_id"],
                                  "knowledge_node_id": row["knowledge_node_id"],
                                  "mistake_id": row["mistake_id"], "stage": row["stage"]},
                "occurred_at": row["created_at"],
            })
        return result
