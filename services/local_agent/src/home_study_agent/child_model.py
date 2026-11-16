from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .database import Database
from .family_clock import family_today
from .learning_outcome_projection import LearningOutcomeProjection


MODEL_VERSION = "evidence_child_model_v6"

CORRECTION_OPTIONS: dict[str, set[Any] | tuple[int, int]] = {
    "explanation_pacing": {"standard_layered", "short_step_then_restate"},
    "task_segment_minutes": (10, 45),
    "interaction_tone": {"gentle", "brief", "direct"},
    "mistake_priority": {"due_or_relapsed_first", "schoolwork_first"},
}


class UnknownChildModelError(KeyError):
    pass


class UnknownChildModelCorrectionError(KeyError):
    pass


class ChildModelCorrectionConflictError(ValueError):
    pass


class ChildModelCorrectionStateError(ValueError):
    pass


def _evidence_time(value: str | None) -> datetime | None:
    """Parse a stored evidence timestamp without substituting the read clock."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChildModelService:
    """Builds a local, evidence-only child state shared by all product surfaces."""

    def __init__(self, database: Database):
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS child_model_snapshot (
                  snapshot_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  model_version TEXT NOT NULL,
                  evidence_fingerprint TEXT NOT NULL,
                  source_watermark_json TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  is_current INTEGER NOT NULL CHECK(is_current IN (0, 1)),
                  created_at TEXT NOT NULL,
                  UNIQUE(child_id, model_version, evidence_fingerprint)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_child_model_current
                ON child_model_snapshot(child_id) WHERE is_current = 1;

                CREATE TABLE IF NOT EXISTS child_model_correction (
                  correction_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  target_key TEXT NOT NULL CHECK(target_key IN (
                    'explanation_pacing', 'task_segment_minutes',
                    'interaction_tone', 'mistake_priority'
                  )),
                  corrected_value_json TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active', 'revoked', 'expired')),
                  expires_at TEXT,
                  created_at TEXT NOT NULL,
                  revoked_at TEXT,
                  revoked_reason TEXT,
                  client_action_id TEXT NOT NULL UNIQUE,
                  request_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_child_model_correction_child_status
                ON child_model_correction(child_id, status, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_child_model_active_correction
                ON child_model_correction(child_id, target_key) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS child_model_correction_event (
                  event_id TEXT PRIMARY KEY,
                  correction_id TEXT NOT NULL REFERENCES child_model_correction(correction_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  event_type TEXT NOT NULL CHECK(event_type IN ('created', 'revoked', 'expired', 'superseded')),
                  reason TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE,
                  occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_child_model_correction_event_child
                ON child_model_correction_event(child_id, occurred_at DESC);
                """
            )
            connection.commit()

    def correct(
        self,
        child_id: str,
        target_key: str,
        value: Any,
        reason: str,
        expires_at: str | None,
        client_action_id: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        normalized_value = self._validate_correction(target_key, value)
        now = datetime.now(timezone.utc)
        normalized_expiry = self._normalize_expiry(expires_at, now)
        request_hash = self._request_hash({
            "operation": "create", "child_id": child_id, "target_key": target_key,
            "value": normalized_value, "reason": reason.strip(), "expires_at": normalized_expiry,
        })
        with self.database.connect() as connection:
            existing_event = connection.execute(
                "SELECT * FROM child_model_correction_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing_event:
                if existing_event["request_hash"] != request_hash or existing_event["event_type"] != "created":
                    raise ChildModelCorrectionConflictError("This correction action was already used for different content.")
                row = connection.execute(
                    "SELECT * FROM child_model_correction WHERE correction_id = ?",
                    (existing_event["correction_id"],),
                ).fetchone()
                return self._serialize_correction(row, idempotent=True)
            self._require_child(connection, child_id)
            connection.execute("BEGIN IMMEDIATE")
            self._expire_corrections(connection, child_id, now)
            previous = connection.execute(
                "SELECT * FROM child_model_correction WHERE child_id = ? AND target_key = ? AND status = 'active'",
                (child_id, target_key),
            ).fetchone()
            if previous:
                supersede_reason = "Replaced by a newer parent correction."
                connection.execute(
                    "UPDATE child_model_correction SET status = 'revoked', revoked_at = ?, revoked_reason = ? WHERE correction_id = ?",
                    (now.isoformat(), supersede_reason, previous["correction_id"]),
                )
                self._insert_correction_event(
                    connection, previous["correction_id"], child_id, "superseded",
                    supersede_reason, self._request_hash({"replacement_action_id": client_action_id}),
                    f"system:supersede:{client_action_id}", now,
                )
            correction_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO child_model_correction(
                  correction_id, child_id, target_key, corrected_value_json, reason,
                  status, expires_at, created_at, client_action_id, request_hash
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    correction_id, child_id, target_key,
                    json.dumps(normalized_value, ensure_ascii=False), reason.strip(),
                    normalized_expiry, now.isoformat(), client_action_id, request_hash,
                ),
            )
            self._insert_correction_event(
                connection, correction_id, child_id, "created", reason.strip(),
                request_hash, client_action_id, now,
            )
            self._audit_correction(
                connection, child_id, "child_model_correction_created", correction_id,
                {"target_key": target_key, "expires_at": normalized_expiry}, now,
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM child_model_correction WHERE correction_id = ?", (correction_id,)
            ).fetchone()
        return self._serialize_correction(row, idempotent=False)

    def revoke(
        self, correction_id: str, reason: str, client_action_id: str
    ) -> dict[str, Any]:
        self.ensure_schema()
        request_hash = self._request_hash({
            "operation": "revoke", "correction_id": correction_id, "reason": reason.strip(),
        })
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            existing_event = connection.execute(
                "SELECT * FROM child_model_correction_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing_event:
                if existing_event["request_hash"] != request_hash or existing_event["event_type"] != "revoked":
                    raise ChildModelCorrectionConflictError("This revoke action was already used for a different correction.")
                row = connection.execute(
                    "SELECT * FROM child_model_correction WHERE correction_id = ?",
                    (existing_event["correction_id"],),
                ).fetchone()
                return self._serialize_correction(row, idempotent=True)
            row = connection.execute(
                "SELECT * FROM child_model_correction WHERE correction_id = ?", (correction_id,)
            ).fetchone()
            if not row:
                raise UnknownChildModelCorrectionError(correction_id)
            if row["status"] != "active":
                raise ChildModelCorrectionStateError("Only an active parent correction can be revoked.")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE child_model_correction SET status = 'revoked', revoked_at = ?, revoked_reason = ? WHERE correction_id = ?",
                (now.isoformat(), reason.strip(), correction_id),
            )
            self._insert_correction_event(
                connection, correction_id, row["child_id"], "revoked", reason.strip(),
                request_hash, client_action_id, now,
            )
            self._audit_correction(
                connection, row["child_id"], "child_model_correction_revoked", correction_id,
                {"target_key": row["target_key"], "reason": reason.strip()}, now,
            )
            connection.commit()
            saved = connection.execute(
                "SELECT * FROM child_model_correction WHERE correction_id = ?", (correction_id,)
            ).fetchone()
        return self._serialize_correction(saved, idempotent=False)

    def corrections(self, child_id: str, include_inactive: bool = True) -> list[dict[str, Any]]:
        self.ensure_schema()
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            if self._expire_corrections(connection, child_id, now):
                connection.commit()
            where = "child_id = ?" if include_inactive else "child_id = ? AND status = 'active'"
            rows = connection.execute(
                f"SELECT * FROM child_model_correction WHERE {where} ORDER BY created_at DESC",
                (child_id,),
            ).fetchall()
        return [self._serialize_correction(row) for row in rows]

    def snapshot(self, child_id: str) -> dict[str, Any]:
        self.ensure_schema()
        from .rubric_calibration_transaction import calibrated_write
        with calibrated_write(self.database, child_id) as connection:
            now = _utc_now()
            child = connection.execute(
                "SELECT child_id, nickname, grade, curriculum_region FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
            if not child:
                raise UnknownChildModelError(child_id)
            self._expire_corrections(connection, child_id, now)
            from .goal_program import GoalProgramService
            GoalProgramService(self.database).refresh_existing_cycles(connection, child_id, as_of=now)
            payload = self._build(connection, dict(child), now)
            watermark = self._watermark(connection, child_id)
            watermark["companion_outcome_projection"] = payload["companion_balance"]["outcome_evidence_fingerprint"]
            watermark["projection"] = self._projection_fingerprint(payload)
            evidence_fingerprint = hashlib.sha256(
                json.dumps(watermark, ensure_ascii=True, sort_keys=True).encode("utf-8")
            ).hexdigest()
            existing = connection.execute(
                """
                SELECT * FROM child_model_snapshot
                WHERE child_id = ? AND model_version = ? AND evidence_fingerprint = ?
                """,
                (child_id, MODEL_VERSION, evidence_fingerprint),
            ).fetchone()
            if existing:
                if not existing["is_current"]:
                    connection.execute(
                        "UPDATE child_model_snapshot SET is_current = 0 WHERE child_id = ? AND is_current = 1",
                        (child_id,),
                    )
                    connection.execute(
                        "UPDATE child_model_snapshot SET is_current = 1 WHERE snapshot_id = ?",
                        (existing["snapshot_id"],),
                    )
                    connection.commit()
                    existing = connection.execute(
                        "SELECT * FROM child_model_snapshot WHERE snapshot_id = ?",
                        (existing["snapshot_id"],),
                    ).fetchone()
                connection.commit()
                return self._serialize(existing)
            snapshot_id = str(uuid4())
            connection.execute(
                "UPDATE child_model_snapshot SET is_current = 0 WHERE child_id = ? AND is_current = 1",
                (child_id,),
            )
            connection.execute(
                """
                INSERT INTO child_model_snapshot(
                  snapshot_id, child_id, model_version, evidence_fingerprint,
                  source_watermark_json, payload_json, is_current, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    snapshot_id, child_id, MODEL_VERSION, evidence_fingerprint,
                    json.dumps(watermark, ensure_ascii=False, sort_keys=True),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True), now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'system', 'child_model_snapshot_created', ?, ?, ?)
                """,
                (
                    str(uuid4()), child_id,
                    json.dumps({
                        "snapshot_id": snapshot_id,
                        "model_version": MODEL_VERSION,
                        "evidence_fingerprint": evidence_fingerprint,
                    }, ensure_ascii=False, sort_keys=True),
                    now.isoformat(),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM child_model_snapshot WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        return self._serialize(row)

    def compact_for_agent(self, child_id: str) -> dict[str, Any]:
        # Agent context must stay read-only. It is assembled while learning
        # writes may still be in flight; attempting to materialize a snapshot
        # here would compete for SQLite's single writer lock and add seconds of
        # voice latency. Parent reads and session completion persist snapshots.
        now = _utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            child = connection.execute(
                "SELECT child_id, nickname, grade, curriculum_region FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
            if not child:
                raise UnknownChildModelError(child_id)
            payload = self._build(connection, dict(child), now)
            watermark = self._watermark(connection, child_id)
            watermark["companion_outcome_projection"] = payload["companion_balance"]["outcome_evidence_fingerprint"]
            watermark["projection"] = self._projection_fingerprint(payload)
            fingerprint = hashlib.sha256(
                json.dumps(watermark, ensure_ascii=True, sort_keys=True).encode("utf-8")
            ).hexdigest()
            persisted = (
                connection.execute(
                    """
                    SELECT snapshot_id FROM child_model_snapshot
                    WHERE child_id = ? AND model_version = ? AND evidence_fingerprint = ?
                      AND is_current = 1
                    """,
                    (child_id, MODEL_VERSION, fingerprint),
                ).fetchone()
                if self._table_exists(connection, "child_model_snapshot") else None
            )
        return self._compact_payload(
            payload, persisted["snapshot_id"] if persisted else None, bool(persisted), fingerprint
        )

    def persisted_compact_for_agent(self, child_id: str) -> dict[str, Any]:
        """Materialize the exact snapshot consumed by a consequential runtime decision."""
        snapshot = self.snapshot(child_id)
        return self._compact_payload(
            snapshot["payload"], snapshot["snapshot_id"], True,
            snapshot["evidence_fingerprint"],
        )

    def tutor_controls(
        self, child_id: str, hint_level: int, strategy_context: dict | None,
        teaching_preferences: dict[str, str], *, session_id: str | None = None,
        plan_item_id: str | None = None, subject: str | None = None,
        knowledge_node_id: str | None = None, task_type: str | None = None,
        question: str | None = None, prompt_id: str | None = None, item_version: str | None = None,
        parent_turn_id: str | None = None, ocr_confirmation_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve locally; only prompt_extension may cross the model boundary."""
        from .strategy_evolution import CANDIDATES
        from .teaching_context import build_teaching_context

        snapshot = self.snapshot(child_id)
        target = None
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            if question is not None or any(value is not None for value in (prompt_id, item_version, parent_turn_id, ocr_confirmation_id)):
                from .teaching_question_target import resolve_question_target
                target = resolve_question_target(connection, session_id, subject=subject, question=question,
                    prompt_id=prompt_id, item_version=item_version, parent_turn_id=parent_turn_id,
                    ocr_confirmation_id=ocr_confirmation_id)
                if target["child_id"] not in (None, child_id):
                    target.update(status="invalid", reason_code="child_scope_mismatch", child_id=child_id,
                        binding=None, knowledge_node_id=None, task_type=None, fingerprint=None, source_fingerprint=None,
                        confidence="unknown", expires_at=None, question_content_cloud_blocked=True,
                        question_content_cloud_reason="question_reference_invalid")
                # Question mode never falls back to task or caller-supplied node guesses.
                plan_item_id = None
                knowledge_node_id = target["knowledge_node_id"] if target["status"] == "resolved" else None
                task_type = target["task_type"] if target["status"] == "resolved" else None
            context = build_teaching_context(connection, child_id, session_id=session_id,
                plan_item_id=plan_item_id, subject=subject, knowledge_node_id=knowledge_node_id,
                task_type=task_type)
        corrections = {c["target_key"]: c for c in context["corrections"]}
        feedback_pacing = context["learning"]["status"] in {"recheck_needed", "supported_only"}
        pacing = "short_step_then_restate" if feedback_pacing else "standard_layered"
        pacing_source = "reliable_feedback" if context["learning"]["status"] != "unknown" else "default"
        if context["recent"].get("energy_level") == "low":
            pacing, pacing_source = "short_step_then_restate", "recent_context"
        if teaching_preferences.get("pace") == "slow":
            pacing, pacing_source = "short_step_then_restate", "child_preference"
        candidate = CANDIDATES.get((strategy_context or {}).get("strategy_version"))
        strategy_applied = bool(
            candidate and hint_level in {1, 2, 3}
            and strategy_context.get("mode") == "active"
            and strategy_context.get("prompt_extension") == candidate[1]
        )
        if strategy_applied:
            pacing, pacing_source = "bounded_experiment", "active_strategy"
        if "explanation_pacing" in corrections:
            pacing = corrections["explanation_pacing"]["corrected_value"]
            pacing_source = "parent_correction"
            strategy_applied = False
        pacing_prompts = {
            "standard_layered": "按标准分层组织本轮讲解，只呈现当前提示级别允许的一层内容。",
            "short_step_then_restate": "使用短步骤节奏，每轮最多三个短句，随后请孩子复述当前概念或已知条件并等待回应。",
        }
        tone_prompts = {
            "gentle": "使用温和、耐心的语气，不催促。",
            "brief": "使用简洁友善的语气，省去冗长铺垫。",
            "direct": "使用清楚直接但尊重的语气，不命令、不施压。",
        }
        tone = corrections["interaction_tone"]["corrected_value"] if "interaction_tone" in corrections else "gentle"
        tone = tone if tone in tone_prompts else "gentle"
        instructions = [candidate[1] if strategy_applied else pacing_prompts.get(pacing, pacing_prompts["standard_layered"]), tone_prompts[tone]]
        style_prompts = {
            "stepwise": "孩子已选择分步表达；按当前提示边界逐段表达。",
            "visual": "孩子已选择图示表达；仅在当前提示边界内使用简明图示。",
            "example": "孩子已选择例子表达；仅在本轮提示级别允许示例时使用，不额外透露解法。",
        }
        style = teaching_preferences.get("explanation_style")
        if style in style_prompts:
            instructions.append(style_prompts[style])
        instructions.append("以上控制只改变表达方式，安全规则和本轮提示级别始终优先，不增加提示级别、不多给解法或答案。没有可靠数据不推断孩子能力或性格；讲解不等于掌握，仍需独立检查。")
        # Decisions are local receipts, not extra model instructions or ability labels.
        def decision(key, value, source):
            basis = context["learning"] if source == "reliable_feedback" else context["recent"] if source == "recent_context" else {}
            correction = corrections.get(key) if source == "parent_correction" else None
            if correction:
                basis = {"evidence_refs": [correction["correction_id"]], "observed_at": correction["created_at"],
                         "expires_at": correction["expires_at"], "confidence": correction["confidence"]}
            if source == "active_strategy":
                basis = {"evidence_refs": [strategy_context["experiment_id"]] if strategy_context.get("experiment_id") else [],
                         "confidence": "bounded_experiment_not_verified_context_benefit"}
            receipt = {"value": value, "source": source, "context_version": context["source_version"],
                    "evidence_refs": basis.get("evidence_refs", []), "observed_at": basis.get("observed_at"),
                    "expires_at": basis.get("expires_at"), "confidence": basis.get("confidence", "not_ability_evidence"),
                    "validity": "recompute_each_turn", "mastery_claim_allowed": False}
            receipt["version"] = "teaching_decision_v1:" + hashlib.sha256(json.dumps(
                receipt, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
            return receipt
        tone_source = "parent_correction" if "interaction_tone" in corrections else "default"
        return {
            "prompt_extension": "\n本轮固定教学控制：" + "".join(instructions),
            "record_strategy": bool(strategy_context) and "explanation_pacing" not in corrections and (
                strategy_applied or strategy_context.get("mode") == "shadow"
            ),
            "audit": {
                "cloud_transmission_allowed": False,
                "control_version": "tutor_local_controls_v2",
                "snapshot_id": snapshot["snapshot_id"],
                "model_version": snapshot["model_version"],
                "evidence_fingerprint": snapshot["evidence_fingerprint"],
                "hint_level": hint_level,
                "teaching_context": context,
                "question_target": target,
                "decisions": {"explanation_pacing": decision("explanation_pacing", pacing, pacing_source),
                              "interaction_tone": decision("interaction_tone", tone, tone_source),
                              "explanation_style": decision("explanation_style", style if style in style_prompts else None,
                                  "child_preference" if style in style_prompts else "default")},
                "controls": {"explanation_pacing": pacing, "interaction_tone": tone,
                             "explanation_style": style if style in style_prompts else None},
                "sources": {"explanation_pacing": pacing_source,
                            "interaction_tone": "parent_correction" if "interaction_tone" in corrections else "default"},
                "correction_ids": [corrections[k]["correction_id"] for k in ("explanation_pacing", "interaction_tone") if k in corrections],
                "strategy": {**{k: (strategy_context or {}).get(k) for k in ("experiment_id", "mode", "strategy_version")},
                             "applied": strategy_applied,
                             "suppressed_by_parent_pacing": bool(strategy_context) and "explanation_pacing" in corrections},
                "precedence": ["safety_and_hint_level", "parent_correction", "active_strategy", "child_preference", "recent_context", "reliable_feedback", "default"],
            },
        }

    @staticmethod
    def _projection_fingerprint(payload: dict[str, Any]) -> str:
        # Counts and MAX(timestamp) miss in-place corrections and time-based
        # changes such as reviews becoming due or rolling windows expiring.
        projected = {key: value for key, value in payload.items() if key != "as_of"}
        return hashlib.sha256(
            json.dumps(projected, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _compact_payload(
        payload: dict[str, Any], snapshot_id: str | None, snapshot_persisted: bool,
        evidence_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        priorities = []
        for node in payload["learning"]["reliable_feedback"]["nodes"]:
            priorities.append({"cloud_transmission_allowed": False, **{key: node[key] for key in (
                "knowledge_node_id", "subject", "title", "status", "next_action", "reason", "evidence_refs")}})
        return {
            "snapshot_id": snapshot_id,
            "snapshot_persisted": snapshot_persisted,
            "model_version": MODEL_VERSION,
            "evidence_fingerprint": evidence_fingerprint,
            "state_domains": payload["state_domains"],
            "open_hypotheses": payload["open_hypotheses"],
            "active_strategy": payload["active_strategy"],
            "plan_context": payload["plan_context"],
            "provenance": payload["provenance"],
            "evidence_level": payload["evidence_level"],
            "learning_priorities": priorities[:4],
            "learning_feedback": payload["learning"]["reliable_feedback"],
            "mistake_status": payload["mistakes"]["status_counts"],
            "execution": payload["execution"],
            "goal_progress": payload["goals"],
            "specialized_progress": payload["specialized_progress"],
            "specialized_stage_evidence": payload["specialized_stage_evidence"],
            "companion_balance": payload["companion_balance"],
            "tutor_evidence": {
                key: payload.get("tutor_outcomes", {}).get(key)
                for key in ("fingerprint", "outcome_count", "available_count", "mapped_count", "unmapped_count", "level_counts", "boundary")
            },
            "support_strategy": payload["support_strategy"],
            "behavior_summary": payload["behavior"],
            "boundary": "derived_local_facts_only_no_personality_or_medical_labels",
        }

    def history(self, child_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM child_model_snapshot WHERE child_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (child_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def _build(self, connection: sqlite3.Connection, child: dict[str, Any], now: datetime) -> dict[str, Any]:
        from .learning_feedback import build_learning_feedback
        learning_feedback = build_learning_feedback(connection, child["child_id"], now)
        subject_rows = connection.execute(
            """
            SELECT kn.subject, COUNT(*) AS node_count,
                   SUM(CASE WHEN ms.mastery_probability < .55 THEN 1 ELSE 0 END) AS weak_nodes,
                   SUM(CASE WHEN ms.mastery_probability >= .8 AND ms.status_confidence >= .7 THEN 1 ELSE 0 END) AS stable_nodes,
                   SUM(CASE WHEN ms.next_review_at IS NOT NULL AND ms.next_review_at <= ? THEN 1 ELSE 0 END) AS due_reviews,
                   AVG(ms.mastery_probability) AS average_mastery,
                   AVG(ms.hint_dependency) AS average_hint_dependency,
                   AVG(ms.status_confidence) AS average_confidence
            FROM mastery_state ms JOIN knowledge_node kn ON kn.knowledge_node_id = ms.knowledge_node_id
            WHERE ms.child_id = ? GROUP BY kn.subject ORDER BY weak_nodes DESC, kn.subject
            """,
            (now.isoformat(), child["child_id"]),
        ).fetchall()
        subjects = [{
            "subject": row["subject"],
            "node_count": int(row["node_count"] or 0),
            "weak_nodes": int(row["weak_nodes"] or 0),
            "stable_nodes": int(row["stable_nodes"] or 0),
            "due_reviews": int(row["due_reviews"] or 0),
            "average_mastery": round(float(row["average_mastery"] or 0), 3),
            "average_hint_dependency": round(float(row["average_hint_dependency"] or 0), 3),
            "average_confidence": round(float(row["average_confidence"] or 0), 3),
        } for row in subject_rows]
        mistake_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM mistake_case WHERE child_id = ? GROUP BY status",
            (child["child_id"],),
        ).fetchall()
        mistake_counts = {row["status"]: int(row["count"]) for row in mistake_rows}
        clusters = [dict(row) for row in connection.execute(
            """
            SELECT cluster_id, knowledge_node_id, subject, title, diagnosis_status,
                   occurrence_count, relapse_count, updated_at
            FROM mistake_cluster WHERE child_id = ?
              AND EXISTS (SELECT 1 FROM mistake_case c WHERE c.cluster_id = mistake_cluster.cluster_id AND c.status != 'excluded')
            ORDER BY relapse_count DESC, occurrence_count DESC, updated_at DESC LIMIT 5
            """,
            (child["child_id"],),
        ).fetchall()]
        for cluster in clusters:
            if cluster["diagnosis_status"] == "confirmed_gap":
                # Legacy diagnoses were promoted by confidence alone. Repeated
                # errors support a pattern, not confirmation of its cause.
                cluster["diagnosis_status"] = "emerging_pattern" if cluster["occurrence_count"] >= 2 else "suspected"
        since = (now - timedelta(days=14)).isoformat()
        execution = connection.execute(
            """
            SELECT COUNT(DISTINCT ds.session_id) AS sessions,
                   SUM(CASE WHEN pi.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN pi.status IN ('rescheduled', 'skipped') THEN 1 ELSE 0 END) AS deferred,
                   COUNT(pi.plan_item_id) AS planned,
                   SUM(COALESCE(pi.actual_minutes, 0)) AS actual_minutes
            FROM daily_session ds
            LEFT JOIN daily_plan dp ON dp.session_id = ds.session_id
            LEFT JOIN plan_item pi ON pi.plan_id = dp.plan_id AND pi.source_type != 'planned_break'
            WHERE ds.child_id = ? AND ds.started_at >= ?
            """,
            (child["child_id"], since),
        ).fetchone()
        planned = int(execution["planned"] or 0)
        completed = int(execution["completed"] or 0)
        execution_payload = {
            "window_days": 14,
            "sessions": int(execution["sessions"] or 0),
            "planned_tasks": planned,
            "completed_tasks": completed,
            "deferred_tasks": int(execution["deferred"] or 0),
            "actual_minutes": int(execution["actual_minutes"] or 0),
            "completion_rate": round(completed / planned, 3) if planned else None,
        }
        goals = [{"goal_kind": "legacy_weekly", **dict(row)} for row in connection.execute(
            """
            SELECT track, target_minutes, target_sessions, status, week_start, week_end
            FROM weekly_goal WHERE child_id = ? AND week_end >= ?
            ORDER BY week_start DESC, track
            """,
            (child["child_id"], family_today(now).isoformat()),
        ).fetchall()]
        if self._table_exists(connection, "goal_cycle"):
            goals.extend({
                "goal_kind": "program_cycle",
                **{key: value for key, value in dict(row).items() if key != "focus_json"},
                "focus": {**json.loads(row["focus_json"]), "cloud_transmission_allowed": False},
            } for row in connection.execute(
                """
                SELECT gp.track, gp.title, gp.target_statement, gc.target_minutes,
                       gc.target_sessions, gc.completed_minutes, gc.completed_sessions,
                       gc.status, gc.week_start, gc.week_end, gc.focus_json
                FROM goal_cycle gc JOIN goal_program gp ON gp.program_id = gc.program_id
                WHERE gc.child_id = ? AND gc.week_end >= ? AND gp.status = 'active'
                ORDER BY gp.priority DESC
                """,
                (child["child_id"], family_today(now).isoformat()),
            ).fetchall())
        behavior = self._behavior(connection, child["child_id"], now)
        feedback = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN outcome = 'understood' THEN 1 ELSE 0 END) AS understood,
                   SUM(CASE WHEN outcome = 'still_stuck' THEN 1 ELSE 0 END) AS still_stuck
            FROM tutor_feedback WHERE child_id = ? AND created_at >= ?
            """,
            (child["child_id"], since),
        ).fetchone()
        evidence_count = sum(node['attempt_count'] > 0 for node in learning_feedback["nodes"])
        session_count = execution_payload["sessions"]
        evidence_level = "established" if evidence_count >= 12 and session_count >= 5 else "emerging" if evidence_count >= 3 or session_count >= 2 else "cold_start"
        accepted_attempts = sum(item["attempt_count"] for item in learning_feedback["nodes"])
        average_hint = (sum(item["supported_attempt_count"] for item in learning_feedback["nodes"])
                        / accepted_attempts if accepted_attempts else 0)
        still_stuck = int(feedback["still_stuck"] or 0)
        strategy_evolution = self._strategy_evolution(connection, child["child_id"])
        parent_feedback = self._parent_feedback(connection, child["child_id"], since)
        specialized_progress = self._specialized_progress(connection, child["child_id"])
        stage_evidence = self._specialized_stage_evidence(connection, child, now)
        companion_balance = self._companion_balance(connection, child["child_id"], since)
        support_strategy = {
            "explanation_pacing": (
                "short_step_then_restate"
                if average_hint >= .45 or still_stuck >= 2 or strategy_evolution.get("status") == "active"
                else "standard_layered"
            ),
            "task_segment_minutes": self._task_segment(connection, child["child_id"]),
            "interaction_tone": "gentle",
            "mistake_priority": "due_or_relapsed_first",
            "require_independent_check": True,
            "pacing_basis": "qualified_support_receipts_and_explicit_feedback",
            "strategy_evolution": strategy_evolution,
            "do_not_infer": ["personality", "motivation", "diagnosis", "ability_from_missing_data"],
            "reason": "根据提示依赖、仍卡住反馈和家庭规则选择；不改变安全、隐私或家庭边界。",
        }
        active_corrections = self._active_corrections(connection, child["child_id"], now)
        for correction in active_corrections:
            support_strategy[correction["target_key"]] = correction["corrected_value"]
        support_strategy["parent_correction_count"] = len(active_corrections)
        support_strategy["correction_sources"] = [
            {key: correction[key] for key in ("correction_id", "target_key", "reason", "expires_at")}
            for correction in active_corrections
        ]
        if active_corrections:
            support_strategy["reason"] += " 已叠加家长明确设定且可随时撤销的支持策略。"
        payload = {
            "child": child,
            "as_of": now.isoformat(),
            "evidence_level": evidence_level,
            "learning": {"subjects": subjects, "evidenced_node_count": evidence_count,
                         "qualified_original_count": sum(node.get('qualified_original_count', 0)
                             for node in learning_feedback['nodes']),
                         "subject_aggregate_basis": "legacy_estimates_not_verified_mastery",
                         "reliable_feedback": learning_feedback},
            "tutor_outcomes": LearningOutcomeProjection(self.database).for_child(child["child_id"], connection=connection),
            "mistakes": {"status_counts": mistake_counts, "top_clusters": clusters},
            "execution": execution_payload,
            "behavior": behavior,
            "goals": goals,
            "specialized_progress": specialized_progress,
            "specialized_stage_evidence": stage_evidence,
            "companion_balance": companion_balance,
            "parent_feedback": parent_feedback,
            "tutor_feedback": {
                "total": int(feedback["total"] or 0),
                "understood": int(feedback["understood"] or 0),
                "still_stuck": still_stuck,
            },
            "support_strategy": support_strategy,
            "parent_corrections": active_corrections,
            "limitations": [
                "快照只汇总本机可追溯证据；没有记录不等于不会或没有学习。",
                "行为计数不用于人格、情绪、医学或能力判断。",
                "支持策略是可回滚建议，不自动改变家庭规则。",
                "旧学科掌握分仅为历史估计；新的任务优先级采用经过来源核验的作答反馈。",
            ],
        }
        latest_session = connection.execute(
            """SELECT session.session_id,session.status,session.state,
                      planning.energy_level,session.started_at,session.last_checkpoint_at
               FROM daily_session AS session
               LEFT JOIN session_planning_context AS planning USING(session_id)
               WHERE session.child_id=?
               ORDER BY session.started_at DESC,session.session_id DESC LIMIT 1""",
            (child["child_id"],),
        ).fetchone()
        latest_session_time = _evidence_time(
            latest_session["last_checkpoint_at"] if latest_session else None
        )
        daily_state = {
            "status": "observed" if latest_session else "unknown",
            "session_id": latest_session["session_id"] if latest_session else None,
            "session_state": latest_session["state"] if latest_session else None,
            "energy_level": latest_session["energy_level"] if latest_session else None,
            "observed_at": latest_session["last_checkpoint_at"] if latest_session else None,
            "expires_at": (
                (latest_session_time + timedelta(hours=12)).isoformat()
                if latest_session_time else None
            ),
            "confidence": "self_report_or_session_fact" if latest_session else "unknown",
            "verification_action": (
                "开始下一次学习前用一句话核对今天的精力和时间约束。"
                if latest_session else "开始学习时采集当日约束，不沿用昨天状态。"
            ),
            "evidence_refs": [latest_session["session_id"]] if latest_session else [],
        }
        open_hypotheses = [{
            "hypothesis_id": f"mistake-cluster:{cluster['cluster_id']}",
            "scope": {"knowledge_node_id": cluster["knowledge_node_id"],
                      "subject": cluster["subject"]},
            "claim": f"{cluster['title']}可能存在尚未核实的理解或过程缺口。",
            "evidence_for": [cluster["cluster_id"]],
            "evidence_against": [],
            "confidence": "emerging" if cluster["occurrence_count"] >= 2 else "low",
            "valid_until": (
                (_evidence_time(cluster.get("updated_at")) + timedelta(days=14)).isoformat()
                if _evidence_time(cluster.get("updated_at")) else None
            ),
            "verification_actions": ["使用不同题面、无答案提示的短检查核实。"],
            "status": "open",
        } for cluster in clusters if cluster["diagnosis_status"] in {"suspected", "emerging_pattern"}]
        open_hypotheses.extend(self._analysis_hypotheses(connection, child["child_id"], now))
        pending_plan_items = connection.execute(
            """SELECT COUNT(*) AS count FROM plan_item AS item
               JOIN daily_plan AS plan USING(plan_id)
               WHERE plan.child_id=? AND item.status IN ('pending','active','paused')
                 AND item.source_type!='planned_break'""",
            (child["child_id"],),
        ).fetchone()["count"]
        payload.update({
            "schema_version": "child_state_snapshot_v6",
            "state_domains": {
                "knowledge": {
                    "subjects": subjects,
                    "reliable_feedback": learning_feedback,
                    "mistakes": {"status_counts": mistake_counts, "top_clusters": clusters},
                },
                "learning_process": {
                    "tutor_outcomes": payload["tutor_outcomes"],
                    "support_dependency_basis": "qualified_attempts_and_feedback_only",
                },
                "habits": behavior,
                "daily_state": daily_state,
                "interaction": {
                    "support_strategy": support_strategy,
                    "verified_corrections": active_corrections,
                },
            },
            "plan_context": {
                "goals": goals,
                "pending_plan_items": int(pending_plan_items or 0),
                "execution": execution_payload,
                "unfinished_obligations_are_not_completion": True,
            },
            "open_hypotheses": open_hypotheses,
            "active_strategy": {
                **strategy_evolution,
                "authority": "bounded_local_strategy_experiment",
                "mastery_claim_allowed": False,
            },
            "provenance": {
                "snapshot_schema": "child_state_snapshot_v6",
                "model_version": MODEL_VERSION,
                "learning_projection": "qualified_learning_feedback",
                "behavior_projection": "local_observation_candidates",
                "strategy_projection": "bounded_experiment_current_state",
                "cloud_claims_are_authoritative": False,
            },
        })
        return payload

    def _analysis_hypotheses(
        self, connection: sqlite3.Connection, child_id: str, now: datetime,
    ) -> list[dict[str, Any]]:
        if not self._table_exists(connection, "analysis_candidate"):
            return []
        rows = connection.execute(
            """SELECT candidate_id,claims_json,confidence,valid_until,
                      verification_actions_json,expected_outcomes_json,
                      stop_conditions_json,rollback_action,model_provider,model_name,
                      prompt_version,response_version,source_snapshot_id,evidence_fingerprint
               FROM analysis_candidate AS candidate
               WHERE child_id=? AND valid_until>?
                 AND (SELECT event_type FROM analysis_candidate_event
                      WHERE candidate_id=candidate.candidate_id
                      ORDER BY event_seq DESC LIMIT 1)='validated'
               ORDER BY created_at DESC,candidate_id""",
            (child_id, now.isoformat()),
        ).fetchall()
        hypotheses: list[dict[str, Any]] = []
        for row in rows:
            for index, claim in enumerate(json.loads(row["claims_json"])):
                hypotheses.append({
                    "hypothesis_id": f"analysis-candidate:{row['candidate_id']}:{index}",
                    "source": "validated_cloud_analysis_candidate",
                    "claim": claim["claim"],
                    "evidence_for": claim["evidence_refs"],
                    "evidence_against": claim["counterevidence_refs"],
                    "confidence": "candidate",
                    "confidence_score": float(row["confidence"]),
                    "valid_until": row["valid_until"],
                    "verification_actions": json.loads(row["verification_actions_json"]),
                    "expected_outcomes": json.loads(row["expected_outcomes_json"]),
                    "stop_conditions": json.loads(row["stop_conditions_json"]),
                    "rollback_action": row["rollback_action"],
                    "source_snapshot_id": row["source_snapshot_id"],
                    "source_evidence_fingerprint": row["evidence_fingerprint"],
                    "analysis_version": {
                        "provider": row["model_provider"], "model": row["model_name"],
                        "prompt": row["prompt_version"], "response": row["response_version"],
                    },
                    "mastery_claim_allowed": False,
                    "habit_claim_allowed": False,
                    "plan_completion_claim_allowed": False,
                    "status": "open",
                })
        return hypotheses

    def _behavior(self, connection: sqlite3.Connection, child_id: str, now: datetime) -> dict[str, Any]:
        if not self._table_exists(connection, "daily_feature"):
            return {"window_days": 28, "observed_days": 0, "signals": {}, "intervention_feedback": {}}
        today = family_today(now)
        since = (today - timedelta(days=27)).isoformat()
        features = connection.execute(
            """
            SELECT COUNT(*) AS observed_days, SUM(writing_count) AS writing,
                   SUM(reading_count) AS reading, SUM(stall_count) AS stall,
                   SUM(away_count) AS away, SUM(missing_observation_count) AS missing
            FROM daily_feature WHERE child_id = ? AND day BETWEEN ? AND ?
            """,
            (child_id, since, today.isoformat()),
        ).fetchone()
        feedback = {"accepted": 0, "declined": 0, "unknown": 0}
        if self._table_exists(connection, "intervention"):
            rows = connection.execute(
                "SELECT feedback, COUNT(*) AS count FROM intervention WHERE child_id = ? GROUP BY feedback",
                (child_id,),
            ).fetchall()
            for row in rows:
                key = row["feedback"] if row["feedback"] in feedback else "unknown"
                feedback[key] += int(row["count"])
        return {
            "window_days": 28,
            "observed_days": int(features["observed_days"] or 0),
            "signals": {
                "writing": int(features["writing"] or 0),
                "reading": int(features["reading"] or 0),
                "stall_candidates": int(features["stall"] or 0),
                "away_candidates": int(features["away"] or 0),
                "missing_observation": int(features["missing"] or 0),
            },
            "intervention_feedback": feedback,
            "uncertainty": "仅为低频候选计数；未结合孩子确认前不作为行为事实。",
            "habit_experiments": self._habit_experiments(connection, child_id),
        }

    def _habit_experiments(self, connection: sqlite3.Connection, child_id: str) -> dict[str, Any]:
        if not self._table_exists(connection, "habit_experiment"):
            return {"open": None, "completed_count": 0, "improving_count": 0}
        rows = connection.execute(
            """SELECT experiment_id,title,target_state,status,started_at,closed_at,outcome_json
               FROM habit_experiment WHERE child_id=? ORDER BY proposed_at DESC""",
            (child_id,),
        ).fetchall()
        open_row = next((row for row in rows if row["status"] in {"proposed", "active"}), None)
        formation = None
        if open_row and self._table_exists(connection, "habit_formation_state"):
            formation_row = connection.execute(
                """SELECT stage,cue_level,revision,transition_reason,policy_version,updated_at
                   FROM habit_formation_state
                   WHERE experiment_id=? AND child_id=?""",
                (open_row["experiment_id"], child_id),
            ).fetchone()
            if formation_row:
                formation = dict(formation_row)
                formation["habit_success"] = False
        improving = 0
        repeated = 0
        bounded_success = 0
        from .behavior_model.goal_confirmation import assessment
        for row in rows:
            if not row['started_at']:
                continue
            current = assessment(connection, child_id, row['experiment_id'], datetime.now(timezone.utc))
            improving += int(current['direction'] == 'improving')
            repeated += int(current['status'] in {'repeated_observed_behavior', 'bounded_confirmed_behavior'})
            bounded_success += int(current['bounded_goal_success'])
        return {
            "open": (
                {**{key: open_row[key] for key in ("experiment_id", "title", "target_state", "status", "started_at")},
                 "formation": formation}
                if open_row else None
            ),
            "completed_count": sum(row["status"] == "completed" for row in rows),
            "improving_count": improving,
            "repeated_observed_behavior_count": repeated,
            "bounded_goal_success_count": bounded_success,
            "habit_success_count": 0,
        }

    def _strategy_evolution(self, connection: sqlite3.Connection, child_id: str) -> dict[str, Any]:
        if not self._table_exists(connection, "strategy_experiment"):
            return {"status": "baseline", "candidate_version": None, "feedback_count": 0}
        row = connection.execute(
            """SELECT experiment_id,candidate_version,status,replay_metrics_json,
                      shadow_metrics_json,active_metrics_json,rollback_reason,updated_at
               FROM strategy_experiment WHERE child_id=? ORDER BY updated_at DESC LIMIT 1""",
            (child_id,),
        ).fetchone()
        if not row:
            return {"status": "baseline", "candidate_version": None, "feedback_count": 0}
        metrics_raw = (
            row["active_metrics_json"] if row["status"] == "active"
            else row["shadow_metrics_json"] if row["status"] == "shadow"
            else row["replay_metrics_json"]
        )
        try:
            metrics = json.loads(metrics_raw or "{}")
        except (TypeError, json.JSONDecodeError):
            metrics = {}
        return {
            "experiment_id": row["experiment_id"],
            "status": row["status"],
            "candidate_version": row["candidate_version"],
            "feedback_count": int(metrics.get("feedback_count") or metrics.get("tutor_feedback_count") or 0),
            "understood_rate": metrics.get("understood_rate", metrics.get("tutor_understood_rate")),
            "rollback_reason": row["rollback_reason"],
            "updated_at": row["updated_at"],
        }

    def _parent_feedback(self, connection: sqlite3.Connection, child_id: str, since: str) -> dict[str, Any]:
        if not self._table_exists(connection, "parent_report_feedback"):
            return {"window_days": 14, "feedback_count": 0, "executed_count": 0, "average_effectiveness": None, "high_stress_count": 0}
        row = connection.execute(
            """SELECT COUNT(*) AS feedback_count,SUM(executed) AS executed_count,
                      AVG(CASE WHEN executed=1 THEN effectiveness END) AS average_effectiveness,
                      SUM(CASE WHEN stress_rating>=4 THEN 1 ELSE 0 END) AS high_stress_count
               FROM parent_report_feedback WHERE child_id=? AND updated_at>=?""",
            (child_id, since),
        ).fetchone()
        return {
            "window_days": 14,
            "feedback_count": int(row["feedback_count"] or 0),
            "executed_count": int(row["executed_count"] or 0),
            "average_effectiveness": round(float(row["average_effectiveness"]), 2) if row["average_effectiveness"] is not None else None,
            "high_stress_count": int(row["high_stress_count"] or 0),
        }

    @staticmethod
    def _specialized_stage_evidence(connection, child, now):
        from .specialized_stages import VERSION, project_specialized_stages

        tracks = ('pet', 'competition_math') if child['grade'] == '四年级' else ('junior_exam',)
        projected = []
        node_fields = ('knowledge_node_id', 'current_stage', 'qualified_stage', 'status',
                       'gates', 'next_action', 'blockers', 'retention_due_at',
                       'counterevidence_refs', 'evidence_fingerprint', 'qualification_scope',
                       'mastery_claim_allowed', 'certification_claim_allowed')
        for track in tracks:
            current = project_specialized_stages(connection, child['child_id'], track, as_of=now.isoformat())
            projected.append({
                'track': track, 'current_stage': current['current_stage'],
                'qualified_stage': current['qualified_stage'],
                'evidence_fingerprint': current['evidence_fingerprint'],
                'domains': [{
                    'domain_id': domain['domain_id'], 'label': domain['label'],
                    'subject': domain['subject'],
                    'nodes': [{key: node[key] for key in node_fields} for node in domain['nodes']],
                } for domain in current['domains']],
            })
        # The ordinary evidence projection deliberately rejects reference rubric
        # scores. Preserve reviewed stages separately, never convert them to BKT.
        return {'version': VERSION, 'tracks': projected, 'local_only': True,
                'cloud_transmission_allowed': False, 'mastery_claim_allowed': False,
                'certification_claim_allowed': False,
                'basis': 'current_qualified_stages_not_reference_scores'}

    def _specialized_progress(self, connection: sqlite3.Connection, child_id: str) -> list[dict[str, Any]]:
        if not self._table_exists(connection, "specialized_assessment_event"):
            return []
        rows = connection.execute(
            """SELECT sae.track,sae.subject,sae.assessment_type,sae.score,sae.max_score,
                      sae.domain_results_json,sae.occurred_at,sae.source_ref
               FROM specialized_assessment_event sae
               WHERE sae.child_id=? AND sae.occurred_at=(
                 SELECT MAX(latest.occurred_at) FROM specialized_assessment_event latest
                 WHERE latest.child_id=sae.child_id AND latest.track=sae.track
               ) ORDER BY sae.track""",
            (child_id,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                domains = json.loads(row["domain_results_json"])
            except (TypeError, json.JSONDecodeError):
                domains = {}
            guided = row["assessment_type"].startswith("guided_")
            if guided:
                from .specialized_program import SpecializedProgramService
                try:
                    current = SpecializedProgramService(self.database).guided_status(
                        row["source_ref"], _reconcile=False, _connection=connection)
                    domains = (current["domain_results"] if current["child_id"] == child_id
                               and current["track"] == row["track"] else {})
                except (KeyError, ValueError):
                    domains = {}
            scores = [float(value) for value in domains.values() if isinstance(value, (int, float))]
            result.append({
                "track": row["track"], "subject": row["subject"],
                "assessment_type": row["assessment_type"], "score": None if guided else row["score"],
                "max_score": None if guided else row["max_score"], "domain_count": len(scores),
                "evidence_status": ("qualified_observations" if scores else "needs_verification") if guided else "parent_reported_reference",
                "weak_domain_count": sum(value < .55 for value in scores),
                "occurred_at": row["occurred_at"],
            })
        return result

    def _companion_balance(self, connection: sqlite3.Connection, child_id: str, since: str) -> dict[str, Any]:
        from .companion_activity_outcome import project_companion_activity_evidence
        from .native_launch_evidence import unstarted_launches

        evidence = project_companion_activity_evidence(connection, child_id, since)
        unstarted, absence_fingerprint = unstarted_launches(connection, child_id, since)
        recorded_sources = set()
        result = {
            "version": "companion_balance_v4",
            "window_days": 14,
            "activity_sessions": 0,
            "video_sessions": 0,
            "game_sessions": 0,
            "not_dispatched_launches": sum(len(values) for values in unstarted.values()),
            "not_dispatched_evidence_fingerprint": absence_fingerprint,
            "activity_minutes": 0,
            "video_minutes": 0,
            "game_minutes": 0,
            "child_ended_sessions": 0,
            "bounded_end_sessions": 0,
            "self_report_sessions": evidence["self_report_count"],
            "parent_observed_sessions": evidence["parent_observed_count"],
            "task_resume_receipt_sessions": evidence["task_resume_receipt_count"],
            "verified_child_return_sessions": evidence["verified_child_return_count"],
            "habit_success_sessions": evidence["habit_success_count"],
            # Compatibility name now denotes verified child return, never timer restoration.
            "return_to_learning_sessions": evidence["verified_child_return_count"],
            "outcome_evidence_fingerprint": evidence["fingerprint"],
            "outcome_evidence": evidence["activities"],
            "boundary_status": "no_evidence",
            "usage_boundary_status": "no_records",
            "cloud_transmission_allowed": False,
            "guidance": "尚无可核验的陪伴结果记录；不据此判断活动效果或返回学习。",
        }
        if self._table_exists(connection, "companion_activity"):
            row = connection.execute(
                """SELECT COUNT(*) AS sessions,json_group_array(activity_id) AS source_ids,
                          COALESCE(SUM(MAX(0, MIN(duration_minutes * 60,
                            CAST((julianday(COALESCE(ended_at, updated_at)) - julianday(started_at)) * 86400 AS INTEGER)))), 0) AS seconds,
                          SUM(CASE WHEN end_reason='child_requested' THEN 1 ELSE 0 END) AS child_ended,
                          SUM(CASE WHEN end_reason IN ('time_limit','turn_limit') THEN 1 ELSE 0 END) AS bounded
                   FROM companion_activity WHERE child_id=? AND julianday(started_at)>=julianday(?)""",
                (child_id, since),
            ).fetchone()
            result["activity_sessions"] = int(row["sessions"] or 0)
            recorded_sources.update(("companion", value) for value in json.loads(row["source_ids"]))
            result["activity_minutes"] = round(int(row["seconds"] or 0) / 60, 1)
            result["child_ended_sessions"] += int(row["child_ended"] or 0)
            result["bounded_end_sessions"] += int(row["bounded"] or 0)
        for table, prefix, kind, key in (
            ("controlled_content_session", "video", "controlled_content", "content_session_id"),
            ("controlled_game_session", "game", "controlled_game", "game_session_id"),
        ):
            if not self._table_exists(connection, table):
                continue
            row = connection.execute(
                f"""SELECT COUNT(*) AS sessions,json_group_array({key}) AS source_ids,COALESCE(SUM(actual_seconds),0) AS seconds,
                           SUM(CASE WHEN end_reason='child_requested' THEN 1 ELSE 0 END) AS child_ended,
                           SUM(CASE WHEN end_reason='time_limit' THEN 1 ELSE 0 END) AS bounded
                    FROM {table} WHERE child_id=? AND julianday(started_at)>=julianday(?)
                        AND {key} NOT IN (SELECT value FROM json_each(?))""",
                (child_id, since, json.dumps(sorted(unstarted[kind]))),
            ).fetchone()
            result[f"{prefix}_sessions"] = int(row["sessions"] or 0)
            recorded_sources.update((kind, value) for value in json.loads(row["source_ids"]))
            result[f"{prefix}_minutes"] = round(int(row["seconds"] or 0) / 60, 1)
            result["child_ended_sessions"] += int(row["child_ended"] or 0)
            result["bounded_end_sessions"] += int(row["bounded"] or 0)
        total_minutes = round(
            float(result["activity_minutes"]) + float(result["video_minutes"]) + float(result["game_minutes"]), 1
        )
        total_sessions = int(result["activity_sessions"]) + int(result["video_sessions"]) + int(result["game_sessions"])
        result["total_minutes"] = total_minutes
        result["total_sessions"] = total_sessions
        journal_sources = {(row["source_kind"], row["source_id"]) for row in evidence["activities"]}
        result["journal_sessions"] = len(recorded_sources & journal_sources)
        result["legacy_unverified_sessions"] = len(recorded_sources - journal_sources)
        result["average_daily_minutes"] = round(total_minutes / 14, 1)
        if total_sessions:
            result["usage_boundary_status"] = "within_recorded_limits"
            result["boundary_status"] = "return_evidence_missing"
            result["guidance"] = "已有活动记录，但尚无孩子实际返回学习的核验；自述、自然结束和恢复计时不等同返回。"
            if result["task_resume_receipt_sessions"]:
                result["guidance"] = "已有原任务恢复回执，但尚不能核验孩子实际回到学习；任务后来完成不抹去该历史回执。"
            if result["verified_child_return_sessions"]:
                result["boundary_status"] = "return_observed" if result["verified_child_return_sessions"] == total_sessions else "return_evidence_partial"
                result["guidance"] = "部分活动已有孩子返回的核验；单次返回不认证习惯改善或学习完成。"
        if float(result["average_daily_minutes"]) > 30 or int(result["game_sessions"]) > 14:
            result["usage_boundary_status"] = "review_needed"
            result["boundary_status"] = "review_needed"
            result["guidance"] = "近两周陪伴或游戏使用偏高，家长应缩短预算并优先安排离屏、共同和可自然结束的活动。"
        return result

    def _watermark(self, connection: sqlite3.Connection, child_id: str) -> dict[str, Any]:
        tables = (
            ("learning_evidence", "occurred_at"), ("mastery_state", "updated_at"),
            ("mistake_case", "updated_at"),
            ("weekly_goal", "updated_at"), ("tutor_feedback", "created_at"),
            ("goal_program", "updated_at"), ("goal_cycle", "updated_at"),
            ("daily_feature", "day"), ("intervention", "feedback_at"),
            ("habit_experiment", "COALESCE(closed_at, started_at, proposed_at)"),
            ("habit_formation_state", "updated_at"),
            ("strategy_experiment", "updated_at"),
            ("parent_report_feedback", "updated_at"),
            ("specialized_assessment_event", "occurred_at"),
            ("companion_activity", "updated_at"),
            ("controlled_content_session", "updated_at"),
            ("controlled_game_session", "updated_at"),
            ("companion_activity_outcome", "created_at"),
            ("companion_activity_outcome_event", "created_at"),
            ("companion_activity_voice_binding", "created_at"),
            ("child_model_correction", "COALESCE(revoked_at, created_at)"),
        )
        result: dict[str, Any] = {}
        for table, timestamp in tables:
            if not self._table_exists(connection, table):
                result[table] = {"count": 0, "latest": None}
                continue
            row = connection.execute(
                f"SELECT COUNT(*) AS count, MAX({timestamp}) AS latest FROM {table} WHERE child_id = ?",
                (child_id,),
            ).fetchone()
            result[table] = {"count": int(row["count"] or 0), "latest": row["latest"]}
        sessions = connection.execute(
            "SELECT COUNT(*) AS count, MAX(started_at) AS latest FROM daily_session WHERE child_id = ?",
            (child_id,),
        ).fetchone()
        result["daily_session"] = {"count": int(sessions["count"] or 0), "latest": sessions["latest"]}
        plan_events = connection.execute(
            """
            SELECT COUNT(*) AS count, MAX(pie.occurred_at) AS latest
            FROM plan_item_event pie
            JOIN plan_item pi ON pi.plan_item_id = pie.plan_item_id
            JOIN daily_plan dp ON dp.plan_id = pi.plan_id
            WHERE dp.child_id = ?
            """,
            (child_id,),
        ).fetchone()
        result["plan_item_event"] = {"count": int(plan_events["count"] or 0), "latest": plan_events["latest"]}
        return result

    def _active_corrections(
        self, connection: sqlite3.Connection, child_id: str, now: datetime
    ) -> list[dict[str, Any]]:
        if not self._table_exists(connection, "child_model_correction"):
            return []
        rows = connection.execute(
            """
            SELECT * FROM child_model_correction
            WHERE child_id = ? AND status = 'active'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at
            """,
            (child_id, now.isoformat()),
        ).fetchall()
        return [self._serialize_correction(row) for row in rows]

    def _expire_corrections(
        self, connection: sqlite3.Connection, child_id: str, now: datetime
    ) -> int:
        rows = connection.execute(
            """
            SELECT * FROM child_model_correction
            WHERE child_id = ? AND status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (child_id, now.isoformat()),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE child_model_correction SET status = 'expired', revoked_at = ?, revoked_reason = 'Reached configured expiry.' WHERE correction_id = ?",
                (now.isoformat(), row["correction_id"]),
            )
            event_hash = self._request_hash({"operation": "expire", "correction_id": row["correction_id"]})
            self._insert_correction_event(
                connection, row["correction_id"], child_id, "expired",
                "Reached configured expiry.", event_hash,
                f"system:expire:{row['correction_id']}", now,
            )
            self._audit_correction(
                connection, child_id, "child_model_correction_expired", row["correction_id"],
                {"target_key": row["target_key"]}, now,
            )
        return len(rows)

    @staticmethod
    def _validate_correction(target_key: str, value: Any) -> Any:
        rule = CORRECTION_OPTIONS.get(target_key)
        if rule is None:
            raise ValueError("Unsupported child model correction target.")
        if isinstance(rule, tuple):
            if isinstance(value, bool) or not isinstance(value, int) or not rule[0] <= value <= rule[1]:
                raise ValueError(f"{target_key} must be an integer from {rule[0]} to {rule[1]}.")
            return value
        if value not in rule:
            raise ValueError(f"Unsupported value for {target_key}.")
        return value

    @staticmethod
    def _normalize_expiry(value: str | None, now: datetime) -> str | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("expires_at must be an ISO-8601 timestamp.") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= now:
            raise ValueError("expires_at must be in the future.")
        if parsed > now + timedelta(days=365):
            raise ValueError("A parent correction cannot last longer than 365 days.")
        return parsed.isoformat()

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _insert_correction_event(
        connection: sqlite3.Connection,
        correction_id: str,
        child_id: str,
        event_type: str,
        reason: str,
        request_hash: str,
        client_action_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO child_model_correction_event(
              event_id, correction_id, child_id, event_type, reason,
              request_hash, client_action_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()), correction_id, child_id, event_type, reason,
                request_hash, client_action_id, now.isoformat(),
            ),
        )

    @staticmethod
    def _audit_correction(
        connection: sqlite3.Connection,
        child_id: str,
        action: str,
        correction_id: str,
        summary: dict[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
            VALUES (?, 'parent', ?, ?, ?, ?)
            """,
            (
                str(uuid4()), action, child_id,
                json.dumps({"correction_id": correction_id, **summary}, ensure_ascii=False, sort_keys=True),
                now.isoformat(),
            ),
        )

    @staticmethod
    def _require_child(connection: sqlite3.Connection, child_id: str) -> None:
        if not connection.execute(
            "SELECT 1 FROM child_profile WHERE child_id = ? AND status = 'active'", (child_id,)
        ).fetchone():
            raise UnknownChildModelError(child_id)

    @staticmethod
    def _serialize_correction(row: sqlite3.Row, idempotent: bool | None = None) -> dict[str, Any]:
        result = dict(row)
        result["corrected_value"] = json.loads(result.pop("corrected_value_json"))
        result.pop("request_hash", None)
        if idempotent is not None:
            result["idempotent"] = idempotent
        return result

    @staticmethod
    def _task_segment(connection: sqlite3.Connection, child_id: str) -> int:
        row = connection.execute(
            "SELECT task_segment_minutes FROM family_rule WHERE child_id = ?", (child_id,)
        ).fetchone()
        return int(row["task_segment_minutes"] if row else 20)

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_current"] = bool(result["is_current"])
        result["source_watermark"] = json.loads(result.pop("source_watermark_json"))
        result["payload"] = json.loads(result.pop("payload_json"))
        return result
