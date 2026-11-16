"""Independent-check obligations for append-only paper score revisions."""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .repository import utc_now


def ensure_schema(connection: Any) -> None:
    if not hasattr(connection, "executescript"):
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_correct_revision_obligation (
          obligation_id TEXT PRIMARY KEY,
          revision_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL
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
        CREATE INDEX IF NOT EXISTS idx_paper_correct_revision_child
          ON paper_correct_revision_obligation(child_id,due_at);
        CREATE TRIGGER IF NOT EXISTS paper_correct_revision_obligation_no_update
        BEFORE UPDATE ON paper_correct_revision_obligation
        BEGIN SELECT RAISE(ABORT,'immutable paper revision obligation'); END;
        CREATE TABLE IF NOT EXISTS paper_correct_revision_target (
          target_binding_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_revision_obligation(obligation_id) ON DELETE CASCADE,
          diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
          target_question_identity TEXT NOT NULL,
          review_task_id TEXT NOT NULL UNIQUE REFERENCES review_task(review_task_id),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(obligation_id,target_question_identity)
        );
        CREATE TRIGGER IF NOT EXISTS paper_correct_revision_target_no_update
        BEFORE UPDATE ON paper_correct_revision_target
        BEGIN SELECT RAISE(ABORT,'immutable paper revision target'); END;
        CREATE TABLE IF NOT EXISTS paper_correct_revision_event (
          event_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_correct_revision_obligation(obligation_id) ON DELETE CASCADE,
          target_binding_id TEXT
            REFERENCES paper_correct_revision_target(target_binding_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN (
            'pending_supply','scheduled','target_invalid','withdrawn'
          )),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE TRIGGER IF NOT EXISTS paper_correct_revision_event_no_update
        BEFORE UPDATE ON paper_correct_revision_event
        BEGIN SELECT RAISE(ABORT,'immutable paper revision event'); END;
        """
    )


def _append_event(
    connection: Any, obligation: Any, status: str, reason: str,
    action_id: str, target_binding_id: str | None = None,
) -> None:
    connection.execute(
        """INSERT OR IGNORE INTO paper_correct_revision_event(
             event_id,obligation_id,target_binding_id,child_id,status,reason,
             created_at,client_action_id
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (str(uuid4()), obligation["obligation_id"], target_binding_id,
         obligation["child_id"], status, reason, utc_now(), action_id),
    )


def latest_status(connection: Any, obligation_id: str) -> str | None:
    row = connection.execute(
        """SELECT status FROM paper_correct_revision_event
           WHERE obligation_id=? ORDER BY rowid DESC LIMIT 1""",
        (obligation_id,),
    ).fetchone()
    return row["status"] if row else None


def create_obligation(
    connection: Any, *, revision_id: str, child_id: str,
    knowledge_node_id: str, client_action_id: str,
) -> dict[str, Any]:
    from .paper_correct_verification import _next_learning_day
    from .learning_obligations import question_identity

    ensure_schema(connection)
    decision = connection.execute(
        """SELECT decision.*,request.session_id,request.question_text
           FROM paper_scoring_effective_decision AS decision
           JOIN paper_scoring_request AS request USING(request_id)
           WHERE decision.decision_id=? AND decision.child_id=?
             AND decision.effective_status='qualified' AND decision.correctness=1""",
        (revision_id, child_id),
    ).fetchone()
    if not decision or decision["decision_id"] == decision["root_decision_id"]:
        raise ValueError("paper revision verification requires an active correct revision")
    existing = connection.execute(
        """SELECT * FROM paper_correct_revision_obligation
           WHERE revision_id=? OR client_action_id=?""",
        (revision_id, client_action_id),
    ).fetchone()
    if existing:
        if (existing["revision_id"], existing["child_id"], existing["knowledge_node_id"]) != (
            revision_id, child_id, knowledge_node_id
        ):
            raise ValueError("paper revision verification action payload conflict")
        return project(connection, existing)
    if not connection.execute(
        "SELECT 1 FROM knowledge_node WHERE knowledge_node_id=? AND status='active'",
        (knowledge_node_id,),
    ).fetchone():
        raise ValueError("paper revision verification requires an active knowledge node")
    evidence_id, obligation_id = str(uuid4()), str(uuid4())
    source_identity = question_identity({"prompt": decision["question_text"]})
    now = utc_now()
    connection.execute(
        """INSERT INTO learning_evidence(
             evidence_id,child_id,session_id,knowledge_node_id,evidence_type,
             correctness,hint_level,independent,confidence,evidence_summary,
             occurred_at,client_action_id
           ) VALUES(?,?,?,?, 'paper_scored_correct_source',1,5,0,?,?,?,?)""",
        (evidence_id, child_id, decision["session_id"], knowledge_node_id,
         float(decision["confidence"]), json.dumps({
             "decision_id": revision_id,
             "root_decision_id": decision["root_decision_id"],
             "authority_type": decision["authority_type"],
             "mastery_update_allowed": False,
         }, ensure_ascii=False, separators=(",", ":")), decision["created_at"],
         f"{client_action_id}:source-evidence"),
    )
    connection.execute(
        """INSERT INTO paper_correct_revision_obligation(
             obligation_id,revision_id,request_id,child_id,session_id,
             knowledge_node_id,source_evidence_id,source_question_identity,
             due_at,created_at,client_action_id
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (obligation_id, revision_id, decision["request_id"], child_id,
         decision["session_id"], knowledge_node_id, evidence_id,
         source_identity, _next_learning_day(decision["created_at"]), now, client_action_id),
    )
    obligation = connection.execute(
        "SELECT * FROM paper_correct_revision_obligation WHERE obligation_id=?",
        (obligation_id,),
    ).fetchone()
    _append_event(connection, obligation, "pending_supply", "awaiting qualified distinct item",
                  f"{client_action_id}:created")
    return project(connection, obligation)


def project(connection: Any, obligation: Any) -> dict[str, Any]:
    target = connection.execute(
        """SELECT target.*,review.status AS review_status FROM paper_correct_revision_target target
           JOIN review_task AS review USING(review_task_id)
           WHERE target.obligation_id=? ORDER BY target.rowid DESC LIMIT 1""",
        (obligation["obligation_id"],),
    ).fetchone()
    status = latest_status(connection, obligation["obligation_id"])
    return {
        "obligation_id": obligation["obligation_id"],
        "status": "withdrawn" if status == "withdrawn" else (
            target["review_status"] if target else "pending_supply"
        ),
        "review_task_id": target["review_task_id"] if target else None,
        "diagnostic_item_id": target["diagnostic_item_id"] if target else None,
        "due_at": obligation["due_at"],
        "mastery_update_allowed": False,
    }


def validate_review_source(connection: Any, review_task_id: str, child_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT review.*,target.target_binding_id,target.target_question_identity,
                  obligation.obligation_id,obligation.revision_id,
                  obligation.source_question_identity
           FROM review_task AS review
           JOIN paper_correct_revision_target AS target USING(review_task_id)
           JOIN paper_correct_revision_obligation AS obligation USING(obligation_id)
           WHERE review.review_task_id=? AND review.child_id=?""",
        (review_task_id, child_id),
    ).fetchone()
    return dict(row) if row else None


def resume_pending_supply(connection: Any, child_id: str) -> list[dict[str, Any]]:
    from .paper_correct_verification import _select_target, INTERVAL_LABEL

    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_correct_revision_obligation'"
    ).fetchone():
        return []
    rows = connection.execute(
        """SELECT * FROM paper_correct_revision_obligation
           WHERE child_id=? ORDER BY created_at""",
        (child_id,),
    ).fetchall()
    results = []
    for obligation in rows:
        receipt = connection.execute(
            """SELECT 1 FROM paper_scoring_dialogue_revision_reconciliation
               WHERE revision_id=? AND child_id=?""",
            (obligation["revision_id"], child_id),
        ).fetchone()
        if not receipt:
            continue
        effective = connection.execute(
            """SELECT 1 FROM paper_scoring_effective_decision
               WHERE decision_id=? AND child_id=? AND correctness=1
                 AND effective_status='qualified'""",
            (obligation["revision_id"], child_id),
        ).fetchone()
        if not effective:
            withdraw_for_revision(
                connection, obligation["revision_id"], child_id,
                f"paper-correct-revision-reconcile:{obligation['revision_id']}",
            )
            continue
        state = project(connection, obligation)
        if state["status"] == "withdrawn":
            continue
        if state["review_task_id"] and state["status"] == "pending":
            results.append(state)
            continue
        target = _select_target(connection, obligation, as_of=utc_now())
        if not target:
            results.append(state)
            continue
        item_id, identity = target
        review_id, target_id = str(uuid4()), str(uuid4())
        connection.execute(
            """INSERT INTO review_task(
                 review_task_id,child_id,knowledge_node_id,diagnostic_item_id,
                 source_evidence_id,interval_label,due_at,status,created_at
               ) VALUES(?,?,?,?,?,?,?,'pending',?)""",
            (review_id, child_id, obligation["knowledge_node_id"], item_id,
             obligation["source_evidence_id"], INTERVAL_LABEL, obligation["due_at"], utc_now()),
        )
        connection.execute(
            """INSERT INTO paper_correct_revision_target(
                 target_binding_id,obligation_id,diagnostic_item_id,target_question_identity,
                 review_task_id,created_at,client_action_id
               ) VALUES(?,?,?,?,?,?,?)""",
            (target_id, obligation["obligation_id"], item_id, identity, review_id,
             utc_now(), f"paper-revision-resupply:{obligation['obligation_id']}:{identity}"),
        )
        _append_event(connection, obligation, "scheduled", "qualified distinct item scheduled",
                      f"paper-revision-resupply:{obligation['obligation_id']}:{identity}:scheduled",
                      target_id)
        results.append(project(connection, obligation))
    return results


def withdraw_for_revision(
    connection: Any, revision_id: str, child_id: str, client_action_id: str,
) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_correct_revision_obligation'"
    ).fetchone():
        return False
    obligation = connection.execute(
        """SELECT * FROM paper_correct_revision_obligation
           WHERE revision_id=? AND child_id=?""",
        (revision_id, child_id),
    ).fetchone()
    if not obligation or latest_status(connection, obligation["obligation_id"]) == "withdrawn":
        return False
    from .paper_scoring import _require_reconciliation_guard_if_probing
    _require_reconciliation_guard_if_probing()
    from .learning_evidence_withdrawal import record_withdrawal
    record_withdrawal(
        connection, child_id=child_id, session_id=obligation["session_id"],
        evidence_id=obligation["source_evidence_id"], client_action_id=client_action_id,
        request={"kind": "paper_correct_revision_verification", "revision_id": revision_id},
    )
    _append_event(connection, obligation, "withdrawn", "revised scoring authority withdrawn",
                  f"{client_action_id}:event")
    from .learning_obligations import withdraw_excluded_reviews
    withdraw_excluded_reviews(connection, child_id, utc_now())
    from .learning_state import LearningStateService
    LearningStateService._replay_mastery_scope(
        connection, child_id, obligation["knowledge_node_id"], utc_now(),
        "paper_revision_withdrawn",
    )
    return True
