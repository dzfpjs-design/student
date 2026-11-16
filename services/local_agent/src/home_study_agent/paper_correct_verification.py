"""Durable independent verification for reliably correct paper answers.

A teacher check mark or qualified answer key proves what happened on the paper.
It does not prove durable mastery.  This module keeps that source fact outside
the mastery projection and schedules a different, qualified question whose
actual answer can become independent learning evidence.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
import re
from typing import Any
from uuid import uuid4

from .repository import utc_now


INTERVAL_LABEL = "paper_independent_check"


def ensure_schema(connection: Any) -> None:
    if not hasattr(connection, "executescript"):
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_correct_verification_obligation (
          obligation_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
          source_evidence_id TEXT NOT NULL UNIQUE REFERENCES learning_evidence(evidence_id),
          source_question_identity TEXT NOT NULL,
          due_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_correct_obligation_child
          ON paper_correct_verification_obligation(child_id, due_at);
        CREATE TRIGGER IF NOT EXISTS paper_correct_obligation_no_update
        BEFORE UPDATE ON paper_correct_verification_obligation
        BEGIN SELECT RAISE(ABORT, 'immutable paper correct verification obligation'); END;

        CREATE TABLE IF NOT EXISTS paper_correct_verification_target (
          target_binding_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_verification_obligation(obligation_id) ON DELETE CASCADE,
          diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
          target_question_identity TEXT NOT NULL,
          review_task_id TEXT NOT NULL UNIQUE REFERENCES review_task(review_task_id),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(obligation_id, target_question_identity)
        );
        CREATE INDEX IF NOT EXISTS idx_paper_correct_target_obligation
          ON paper_correct_verification_target(obligation_id, created_at);
        CREATE TRIGGER IF NOT EXISTS paper_correct_target_no_update
        BEFORE UPDATE ON paper_correct_verification_target
        BEGIN SELECT RAISE(ABORT, 'immutable paper correct verification target'); END;

        CREATE TABLE IF NOT EXISTS paper_correct_verification_event (
          event_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_verification_obligation(obligation_id) ON DELETE CASCADE,
          target_binding_id TEXT
            REFERENCES paper_correct_verification_target(target_binding_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN (
            'pending_supply','scheduled','target_invalid','withdrawn'
          )),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_correct_event_obligation
          ON paper_correct_verification_event(obligation_id, created_at);
        CREATE TRIGGER IF NOT EXISTS paper_correct_event_no_update
        BEFORE UPDATE ON paper_correct_verification_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper correct verification event'); END;

        CREATE TRIGGER IF NOT EXISTS paper_correct_decision_delete_withdrawal
        BEFORE DELETE ON paper_scoring_decision
        BEGIN
          INSERT OR IGNORE INTO learning_evidence_withdrawal(
            withdrawal_id,child_id,evidence_id,reason,replacement_evidence_id,
            session_id,client_action_id,request_hash,created_at
          )
          SELECT lower(hex(randomblob(16))),o.child_id,o.source_evidence_id,
                 'answer_withdrawn',NULL,o.session_id,
                 'paper-decision-delete:' || OLD.decision_id,
                 'paper-decision-delete:' || OLD.decision_id,datetime('now')
            FROM paper_correct_verification_obligation AS o
           WHERE o.decision_id=OLD.decision_id;
          UPDATE review_task SET status='cancelled'
           WHERE status='pending' AND source_evidence_id IN (
             SELECT source_evidence_id FROM paper_correct_verification_obligation
              WHERE decision_id=OLD.decision_id
           );
          UPDATE plan_item SET status='cancelled'
           WHERE source_type='due_review' AND status IN ('pending','rescheduled')
             AND started_at IS NULL AND source_ref IN (
               SELECT t.review_task_id
                 FROM paper_correct_verification_target AS t
                 JOIN paper_correct_verification_obligation AS o USING(obligation_id)
                WHERE o.decision_id=OLD.decision_id
             );
        END;

        CREATE TRIGGER IF NOT EXISTS paper_correct_request_delete_withdrawal
        BEFORE DELETE ON paper_scoring_request
        BEGIN
          INSERT OR IGNORE INTO learning_evidence_withdrawal(
            withdrawal_id,child_id,evidence_id,reason,replacement_evidence_id,
            session_id,client_action_id,request_hash,created_at
          )
          SELECT lower(hex(randomblob(16))),o.child_id,o.source_evidence_id,
                 'answer_withdrawn',NULL,o.session_id,
                 'paper-request-delete:' || OLD.request_id,
                 'paper-request-delete:' || OLD.request_id,datetime('now')
            FROM paper_correct_verification_obligation AS o
           WHERE o.request_id=OLD.request_id;
          UPDATE review_task SET status='cancelled'
           WHERE status='pending' AND source_evidence_id IN (
             SELECT source_evidence_id FROM paper_correct_verification_obligation
              WHERE request_id=OLD.request_id
           );
          UPDATE plan_item SET status='cancelled'
           WHERE source_type='due_review' AND status IN ('pending','rescheduled')
             AND started_at IS NULL AND source_ref IN (
               SELECT t.review_task_id
                 FROM paper_correct_verification_target AS t
                 JOIN paper_correct_verification_obligation AS o USING(obligation_id)
                WHERE o.request_id=OLD.request_id
             );
        END;
        """
    )


def _next_learning_day(created_at: str) -> str:
    occurred = datetime.fromisoformat(created_at)
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    nanjing = timezone(timedelta(hours=8))
    day = occurred.astimezone(nanjing).date() + timedelta(days=1)
    return datetime.combine(day, time.min, nanjing).astimezone(timezone.utc).isoformat()


def _latest_status(connection: Any, obligation_id: str) -> str | None:
    row = connection.execute(
        """SELECT status FROM paper_correct_verification_event
           WHERE obligation_id=? ORDER BY rowid DESC LIMIT 1""",
        (obligation_id,),
    ).fetchone()
    return row["status"] if row else None


def _canonical_prompt(value: str) -> str:
    """Ignore worksheet numbering when deciding whether two prompts are the same."""
    text = " ".join(value.split())
    text = re.sub(r"^\s*(?:第\s*)?\d+\s*(?:题)?[.、．:：)）]?\s*", "", text)
    return text.casefold()


def _append_event(
    connection: Any, obligation: Any, status: str, reason: str,
    client_action_id: str, *, target_binding_id: str | None = None,
) -> None:
    connection.execute(
        """INSERT OR IGNORE INTO paper_correct_verification_event(
             event_id,obligation_id,target_binding_id,child_id,status,reason,
             created_at,client_action_id
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (str(uuid4()), obligation["obligation_id"], target_binding_id,
         obligation["child_id"], status, reason, utc_now(), client_action_id),
    )


def _decision_is_qualified(connection: Any, decision_id: str) -> bool:
    row = connection.execute(
        """SELECT effective_status,correctness
             FROM paper_scoring_effective_decision
             WHERE decision_id=? OR root_decision_id=?""",
        (decision_id, decision_id),
    ).fetchone()
    return bool(row and row["effective_status"] == "qualified" and row["correctness"] == 1)


def _select_target(connection: Any, obligation: Any, *, as_of: str) -> tuple[str, str] | None:
    from .learning_obligations import _question_catalog, project_item_exposure
    from .learning_state import LearningStateService

    catalog, identities = _question_catalog(connection)
    request = connection.execute(
        "SELECT question_text FROM paper_scoring_request WHERE request_id=?",
        (obligation["request_id"],),
    ).fetchone()
    if not request:
        return None
    source_prompt = _canonical_prompt(request["question_text"])
    previous = {
        row[0] for row in connection.execute(
            """SELECT target_question_identity FROM paper_correct_verification_target
               WHERE obligation_id=?""", (obligation["obligation_id"],)
        )
    }
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_correct_revision_target'"
    ).fetchone():
        previous.update(row[0] for row in connection.execute(
            """SELECT target_question_identity FROM paper_correct_revision_target
               WHERE obligation_id=?""", (obligation["obligation_id"],)
        ))
    candidates = connection.execute(
        """SELECT diagnostic_item_id,difficulty FROM diagnostic_item
           WHERE knowledge_node_id=? AND status='active'
           ORDER BY difficulty,diagnostic_item_id""",
        (obligation["knowledge_node_id"],),
    ).fetchall()
    for row in candidates:
        item_id = row["diagnostic_item_id"]
        identity = identities.get(item_id)
        if (not identity or identity == obligation["source_question_identity"]
                or identity in previous):
            continue
        if _canonical_prompt(catalog[item_id]["prompt"]) == source_prompt:
            continue
        if (not LearningStateService._eligible_diagnostic(connection, obligation["child_id"], item_id)
                or not LearningStateService._qualified_review_item(
                    connection, obligation["child_id"], item_id
                )):
            continue
        exposure = project_item_exposure(
            connection, obligation["child_id"], item_id, as_of=as_of
        )
        if exposure["answer_repeated"] or exposure["tutor_assisted"]:
            continue
        if item_id not in catalog:
            continue
        return item_id, identity
    return None


def _scheduled_projection(connection: Any, obligation: Any) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT target.*,review.status AS review_status,review.due_at
             FROM paper_correct_verification_target target
             JOIN review_task review USING(review_task_id)
            WHERE target.obligation_id=? ORDER BY target.rowid DESC LIMIT 1""",
        (obligation["obligation_id"],),
    ).fetchone()
    if not row:
        return None
    return {
        "obligation_id": obligation["obligation_id"],
        "status": "completed" if row["review_status"] == "completed" else row["review_status"],
        "review_task_id": row["review_task_id"],
        "diagnostic_item_id": row["diagnostic_item_id"],
        "due_at": row["due_at"],
        "mastery_update_allowed": False,
    }


def _try_schedule(connection: Any, obligation: Any, *, action_prefix: str) -> dict[str, Any]:
    latest = _latest_status(connection, obligation["obligation_id"])
    scheduled = _scheduled_projection(connection, obligation)
    if latest == "withdrawn":
        return {"obligation_id": obligation["obligation_id"], "status": "withdrawn",
                "mastery_update_allowed": False}
    if scheduled and scheduled["status"] in {"pending", "completed"}:
        return scheduled
    target = _select_target(connection, obligation, as_of=utc_now())
    if not target:
        if latest not in {"pending_supply", "target_invalid"}:
            _append_event(
                connection, obligation, "pending_supply",
                "no qualified distinct independent-check item is available",
                f"{action_prefix}:pending-supply",
            )
        return {"obligation_id": obligation["obligation_id"], "status": "pending_supply",
                "due_at": obligation["due_at"], "mastery_update_allowed": False}
    item_id, identity = target
    review_id = str(uuid4())
    target_id = str(uuid4())
    connection.execute(
        """INSERT INTO review_task(
             review_task_id,child_id,knowledge_node_id,diagnostic_item_id,
             source_evidence_id,interval_label,due_at,status,created_at
           ) VALUES(?,?,?,?,?,?,?,'pending',?)""",
        (review_id, obligation["child_id"], obligation["knowledge_node_id"], item_id,
         obligation["source_evidence_id"], INTERVAL_LABEL, obligation["due_at"], utc_now()),
    )
    connection.execute(
        """INSERT INTO paper_correct_verification_target(
             target_binding_id,obligation_id,diagnostic_item_id,target_question_identity,
             review_task_id,created_at,client_action_id
           ) VALUES(?,?,?,?,?,?,?)""",
        (target_id, obligation["obligation_id"], item_id, identity, review_id,
         utc_now(), f"{action_prefix}:target:{identity}"),
    )
    _append_event(
        connection, obligation, "scheduled", "qualified distinct item scheduled",
        f"{action_prefix}:scheduled:{identity}", target_binding_id=target_id,
    )
    return {"obligation_id": obligation["obligation_id"], "status": "pending",
            "review_task_id": review_id, "diagnostic_item_id": item_id,
            "due_at": obligation["due_at"], "mastery_update_allowed": False}


def create_obligation(
    connection: Any, *, decision_id: str, child_id: str, knowledge_node_id: str,
    client_action_id: str,
) -> dict[str, Any]:
    """Create or replay one correct-paper independent-check obligation."""
    ensure_schema(connection)
    decision = connection.execute(
        """SELECT decision.*,request.request_id,request.session_id,request.question_text
             FROM paper_scoring_effective_decision decision
             JOIN paper_scoring_request request USING(request_id)
            WHERE decision.decision_id=? AND decision.child_id=?""",
        (decision_id, child_id),
    ).fetchone()
    if not decision or not _decision_is_qualified(connection, decision_id):
        raise ValueError("paper correct verification requires a qualified correct decision")
    root_decision_id = decision["root_decision_id"]
    node = connection.execute(
        """SELECT 1 FROM knowledge_node WHERE knowledge_node_id=? AND status='active'""",
        (knowledge_node_id,),
    ).fetchone()
    if not node:
        raise ValueError("paper correct verification requires an active knowledge node")
    from .learning_obligations import question_identity
    source_identity = question_identity({"prompt": decision["question_text"]})
    existing = connection.execute(
        """SELECT * FROM paper_correct_verification_obligation
           WHERE decision_id=? OR client_action_id=?""",
        (root_decision_id, client_action_id),
    ).fetchone()
    if existing:
        if (existing["decision_id"], existing["child_id"], existing["knowledge_node_id"],
                existing["source_question_identity"]) != (
                root_decision_id, child_id, knowledge_node_id, source_identity):
            raise ValueError("paper correct verification action payload conflict")
        return _try_schedule(connection, existing, action_prefix=client_action_id)

    now = utc_now()
    evidence_id = str(uuid4())
    obligation_id = str(uuid4())
    due_at = _next_learning_day(decision["created_at"])
    connection.execute(
        """INSERT INTO learning_evidence(
             evidence_id,child_id,session_id,knowledge_node_id,evidence_type,
             correctness,hint_level,independent,confidence,evidence_summary,
             occurred_at,client_action_id
           ) VALUES(?,?,?,?, 'paper_scored_correct_source',1,5,0,?,?,?,?)""",
        (evidence_id, child_id, decision["session_id"], knowledge_node_id,
         float(decision["confidence"]), json.dumps({
             "decision_id": decision_id,
             "authority_type": decision["authority_type"],
             "mastery_update_allowed": False,
         }, ensure_ascii=False, separators=(",", ":")), decision["created_at"],
         f"{client_action_id}:source-evidence"),
    )
    connection.execute(
        """INSERT INTO paper_correct_verification_obligation(
             obligation_id,decision_id,request_id,child_id,session_id,
             knowledge_node_id,source_evidence_id,source_question_identity,
             due_at,created_at,client_action_id
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (obligation_id, root_decision_id, decision["request_id"], child_id,
         decision["session_id"], knowledge_node_id, evidence_id, source_identity,
         due_at, now, client_action_id),
    )
    obligation = connection.execute(
        "SELECT * FROM paper_correct_verification_obligation WHERE obligation_id=?",
        (obligation_id,),
    ).fetchone()
    _append_event(
        connection, obligation, "pending_supply", "awaiting qualified distinct item",
        f"{client_action_id}:created",
    )
    return _try_schedule(connection, obligation, action_prefix=client_action_id)


def resume_pending_supply(connection: Any, child_id: str) -> list[dict[str, Any]]:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_correct_verification_obligation'"
    ).fetchone():
        return []
    rows = connection.execute(
        """SELECT obligation.* FROM paper_correct_verification_obligation obligation
           JOIN paper_scoring_decision decision USING(decision_id)
          WHERE obligation.child_id=? ORDER BY obligation.created_at""",
        (child_id,),
    ).fetchall()
    results = []
    for obligation in rows:
        if not _decision_is_qualified(connection, obligation["decision_id"]):
            withdraw_for_decision(
                connection, obligation["decision_id"], child_id,
                f"paper-correct-reconcile:{obligation['decision_id']}",
            )
            continue
        scheduled = _scheduled_projection(connection, obligation)
        if scheduled and scheduled["status"] == "pending":
            try:
                validate_review_source(
                    connection, scheduled["review_task_id"], child_id,
                    require_due=False,
                )
                results.append(scheduled)
                continue
            except ValueError:
                connection.execute(
                    "UPDATE review_task SET status='cancelled' WHERE review_task_id=? AND status='pending'",
                    (scheduled["review_task_id"],),
                )
                _append_event(
                    connection, obligation, "target_invalid",
                    "scheduled independent-check item is no longer qualified",
                    f"paper-correct-target-invalid:{scheduled['review_task_id']}",
                )
        results.append(_try_schedule(
            connection, obligation,
            action_prefix=f"paper-correct-resupply:{obligation['obligation_id']}",
        ))
    from .paper_correct_revision_verification import resume_pending_supply as resume_revisions
    results.extend(resume_revisions(connection, child_id))
    return results


def validate_review_source(
    connection: Any, review_task_id: str, child_id: str, *, require_due: bool = True,
) -> dict[str, Any]:
    row = connection.execute(
        """SELECT review.*,target.target_binding_id,target.target_question_identity,
                  obligation.obligation_id,obligation.decision_id,
                  obligation.source_question_identity
             FROM review_task review
             JOIN paper_correct_verification_target target USING(review_task_id)
             JOIN paper_correct_verification_obligation obligation USING(obligation_id)
            WHERE review.review_task_id=? AND review.child_id=?""",
        (review_task_id, child_id),
    ).fetchone()
    if not row:
        from .paper_correct_revision_verification import validate_review_source as revision_review
        row = revision_review(connection, review_task_id, child_id)
        decision_id = row["revision_id"] if row else None
    else:
        decision_id = row["decision_id"]
    if (not row or row["interval_label"] != INTERVAL_LABEL
            or row["status"] not in {"pending", "completed"}
            or not _decision_is_qualified(connection, decision_id)):
        raise ValueError("paper independent-check source is unavailable")
    if require_due:
        due = datetime.fromisoformat(row["due_at"])
        if due.tzinfo is None or due > datetime.fromisoformat(utc_now()):
            raise ValueError("paper independent-check is not due")
    withdrawn = connection.execute(
        "SELECT 1 FROM learning_evidence_withdrawal WHERE evidence_id=?",
        (row["source_evidence_id"],),
    ).fetchone()
    if withdrawn:
        raise ValueError("paper independent-check source was withdrawn")
    from .learning_obligations import _question_catalog
    from .learning_state import LearningStateService
    _, identities = _question_catalog(connection)
    identity = identities.get(row["diagnostic_item_id"])
    if (not identity or identity != row["target_question_identity"]
            or identity == row["source_question_identity"]
            or not LearningStateService._eligible_diagnostic(
                connection, child_id, row["diagnostic_item_id"]
            )
            or not LearningStateService._qualified_review_item(
                connection, child_id, row["diagnostic_item_id"]
            )):
        raise ValueError("paper independent-check target is no longer qualified")
    return dict(row)


def withdraw_for_decision(
    connection: Any, decision_id: str, child_id: str, client_action_id: str,
) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_correct_verification_obligation'"
    ).fetchone():
        return False
    obligation = connection.execute(
        """SELECT * FROM paper_correct_verification_obligation
           WHERE decision_id=? AND child_id=?""",
        (decision_id, child_id),
    ).fetchone()
    if not obligation or _latest_status(connection, obligation["obligation_id"]) == "withdrawn":
        return False
    from .paper_scoring import _require_reconciliation_guard_if_probing
    _require_reconciliation_guard_if_probing()
    from .learning_evidence_withdrawal import record_withdrawal
    record_withdrawal(
        connection, child_id=child_id, session_id=obligation["session_id"],
        evidence_id=obligation["source_evidence_id"], client_action_id=client_action_id,
        request={"kind": "paper_correct_verification", "decision_id": decision_id},
    )
    _append_event(
        connection, obligation, "withdrawn", "paper scoring authority withdrawn",
        f"{client_action_id}:event",
    )
    from .learning_obligations import withdraw_excluded_reviews
    withdraw_excluded_reviews(connection, child_id, utc_now())
    return True
