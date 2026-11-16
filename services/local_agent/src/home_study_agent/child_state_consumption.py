from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .child_model import ChildModelService
from .database import Database


CONTRACT_VERSION = "child_state_consumption_v1"
CONSUMER_TYPES = frozenset({
    "daily_plan",
    "tutor",
    "proactive_prompt",
    "daily_report",
    "period_report",
})
CAPTURE_PHASES = frozenset({"decision_input", "result_state", "legacy_recovery"})


class ChildStateConsumptionError(ValueError):
    pass


class ChildStateConsumptionService:
    """Freeze which durable child-state snapshot a product decision consumed.

    This receipt is intentionally separate from the consumer tables. Existing
    plans and reports keep their stable schemas while every new decision gains
    an immutable, locally auditable state binding.
    """

    def __init__(self, database: Database):
        self.database = database
        self.child_model = ChildModelService(database)

    def ensure_schema(self) -> None:
        self.child_model.ensure_schema()
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS child_state_consumption (
                  consumption_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  consumer_type TEXT NOT NULL,
                  source_type TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  snapshot_id TEXT NOT NULL,
                  model_version TEXT NOT NULL,
                  evidence_fingerprint TEXT NOT NULL,
                  capture_phase TEXT NOT NULL CHECK(capture_phase IN (
                    'decision_input','result_state','legacy_recovery'
                  )),
                  purpose TEXT NOT NULL,
                  strategy_version TEXT NOT NULL,
                  strategy_experiment_id TEXT,
                  contract_version TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(consumer_type, source_type, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_child_state_consumption_child_time
                  ON child_state_consumption(child_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_child_state_consumption_snapshot
                  ON child_state_consumption(snapshot_id);
                CREATE TRIGGER IF NOT EXISTS trg_child_state_consumption_no_update
                BEFORE UPDATE ON child_state_consumption BEGIN
                  SELECT RAISE(ABORT, 'child_state_consumption is immutable');
                END;
                """
            )
            connection.commit()

    def capture(self, child_id: str) -> dict[str, Any]:
        """Materialize the exact local state before a consequential decision."""
        self.ensure_schema()
        return self.child_model.snapshot(child_id)

    def persist_snapshot(
        self,
        *,
        child_id: str,
        consumer_type: str,
        source_type: str,
        source_id: str,
        snapshot: dict[str, Any],
        capture_phase: str,
        purpose: str,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        self._validate_contract(consumer_type, source_type, source_id, capture_phase, purpose)
        owns_connection = connection is None
        if owns_connection:
            self.ensure_schema()
            connection_context = self.database.connect()
            connection = connection_context.__enter__()
            connection.execute("BEGIN IMMEDIATE")
        try:
            prior = connection.execute(
                """SELECT * FROM child_state_consumption
                   WHERE consumer_type=? AND source_type=? AND source_id=?""",
                (consumer_type, source_type, source_id),
            ).fetchone()
            if prior:
                if prior["child_id"] != child_id:
                    raise ChildStateConsumptionError("CHILD_STATE_CONSUMPTION_SCOPE_MISMATCH")
                result = self._serialize(prior)
                if owns_connection:
                    connection.commit()
                return result

            snapshot_id = str(snapshot.get("snapshot_id") or "")
            fingerprint = str(snapshot.get("evidence_fingerprint") or "")
            model_version = str(snapshot.get("model_version") or "")
            persisted = connection.execute(
                """SELECT child_id,model_version,evidence_fingerprint,payload_json,created_at
                   FROM child_model_snapshot WHERE snapshot_id=?""",
                (snapshot_id,),
            ).fetchone()
            if (
                not persisted
                or persisted["child_id"] != child_id
                or persisted["model_version"] != model_version
                or persisted["evidence_fingerprint"] != fingerprint
            ):
                raise ChildStateConsumptionError("CHILD_STATE_SNAPSHOT_BINDING_INVALID")
            payload = json.loads(persisted["payload_json"] or "{}")
            strategy = payload.get("active_strategy") if isinstance(payload, dict) else None
            strategy = strategy if isinstance(strategy, dict) else {}
            consumption_id = str(uuid4())
            connection.execute(
                """INSERT INTO child_state_consumption(
                     consumption_id,child_id,consumer_type,source_type,source_id,
                     snapshot_id,model_version,evidence_fingerprint,capture_phase,
                     purpose,strategy_version,strategy_experiment_id,contract_version,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    consumption_id,
                    child_id,
                    consumer_type,
                    source_type,
                    source_id,
                    snapshot_id,
                    model_version,
                    fingerprint,
                    capture_phase,
                    purpose,
                    str(strategy.get("strategy_version") or "no_active_strategy"),
                    strategy.get("experiment_id"),
                    CONTRACT_VERSION,
                    persisted["created_at"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM child_state_consumption WHERE consumption_id=?",
                (consumption_id,),
            ).fetchone()
            if owns_connection:
                connection.commit()
            return self._serialize(row)
        except Exception:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
                connection_context.__exit__(None, None, None)

    def bind_current(
        self,
        *,
        child_id: str,
        consumer_type: str,
        source_type: str,
        source_id: str,
        capture_phase: str,
        purpose: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        prior = self.get(consumer_type, source_type, source_id)
        if prior:
            if prior["child_id"] != child_id:
                raise ChildStateConsumptionError("CHILD_STATE_CONSUMPTION_SCOPE_MISMATCH")
            return prior
        snapshot = self.capture(child_id)
        return self.persist_snapshot(
            child_id=child_id,
            consumer_type=consumer_type,
            source_type=source_type,
            source_id=source_id,
            snapshot=snapshot,
            capture_phase=capture_phase,
            purpose=purpose,
        )

    def get(self, consumer_type: str, source_type: str, source_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM child_state_consumption
                   WHERE consumer_type=? AND source_type=? AND source_id=?""",
                (consumer_type, source_type, source_id),
            ).fetchone()
        return self._serialize(row) if row else None

    @staticmethod
    def _validate_contract(
        consumer_type: str, source_type: str, source_id: str,
        capture_phase: str, purpose: str,
    ) -> None:
        if consumer_type not in CONSUMER_TYPES:
            raise ChildStateConsumptionError("CHILD_STATE_CONSUMER_UNSUPPORTED")
        if capture_phase not in CAPTURE_PHASES:
            raise ChildStateConsumptionError("CHILD_STATE_CAPTURE_PHASE_INVALID")
        if not source_type.strip() or not source_id.strip() or not purpose.strip():
            raise ChildStateConsumptionError("CHILD_STATE_CONSUMPTION_IDENTITY_REQUIRED")

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        value = dict(row)
        value.update({
            "authority": "local_child_state_consumption",
            "local_only": True,
            "cloud_transmission_allowed": False,
            "causal_claim_allowed": False,
        })
        return value
