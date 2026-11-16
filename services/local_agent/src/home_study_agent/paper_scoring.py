"""Durable scoring authority for child-confirmed paper answer readbacks.

The child confirms only the question and the answer that were read.  A score
must come from a teacher mark, a published answer key, or a rubric that was
stored when the agent generated the exercise.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .database import Database
from .repository import utc_now


class _ReconciliationNeedsEffectGuard(Exception):
    """Retry a stale-authority reconciliation under the external-effect guard."""


_probe_reconciliation = ContextVar('paper_score_probe_reconciliation', default=False)


@contextmanager
def _reconciliation_probe(enabled):
    token = _probe_reconciliation.set(enabled)
    try:
        yield
    finally:
        _probe_reconciliation.reset(token)


def _require_reconciliation_guard_if_probing():
    if _probe_reconciliation.get():
        raise _ReconciliationNeedsEffectGuard()


def _run_reconciliation_withdrawal_fence(database, operation):
    """Rollback an optimistic transaction before retrying a real withdrawal."""
    try:
        with _reconciliation_probe(True):
            return operation()
    except _ReconciliationNeedsEffectGuard:
        with database.external_effect_guard(timeout=2), _reconciliation_probe(False):
            return operation()


def ensure_paper_scoring_schema(connection: Any) -> None:
    # Transaction-scoped connection wrappers intentionally expose only execute.
    # Their owning Database has already initialized this schema before nesting.
    if not hasattr(connection, "executescript"):
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_scoring_request (
          request_id TEXT PRIMARY KEY,
          -- Optional provenance only. Candidate/session ownership provides the
          -- cascade boundary, so voice-only installs need not initialize the
          -- document-monitoring schema merely to present a learning item.
          visual_answer_evidence_id TEXT,
          candidate_id TEXT NOT NULL UNIQUE
            REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          question_text TEXT NOT NULL,
          answer_text TEXT NOT NULL,
          answer_source TEXT NOT NULL
            CHECK(answer_source IN ('visual_unverified','child_spoken')),
          question_sha256 TEXT NOT NULL,
          answer_sha256 TEXT NOT NULL,
          confirmation_source TEXT NOT NULL
            CHECK(confirmation_source='child_spoken_readback'),
          fact_source TEXT CHECK(fact_source IN (
            'child_spoken_readback','qualified_visual_observation','system_presented_attempt'
          )),
          fact_confidence REAL CHECK(fact_confidence IS NULL OR
            (fact_confidence >= 0 AND fact_confidence <= 1)),
          status TEXT NOT NULL CHECK(status='pending_authority'),
          correctness REAL CHECK(correctness IS NULL),
          mastery_update_allowed INTEGER NOT NULL DEFAULT 0
            CHECK(mastery_update_allowed=0),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_scoring_request_child
          ON paper_scoring_request(child_id, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS paper_scoring_request_no_update
        BEFORE UPDATE ON paper_scoring_request
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring request'); END;

        CREATE TABLE IF NOT EXISTS paper_answer_authority (
          authority_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT REFERENCES daily_session(session_id) ON DELETE CASCADE,
          question_text TEXT NOT NULL,
          question_sha256 TEXT NOT NULL,
          authority_type TEXT NOT NULL CHECK(authority_type IN (
            'published_answer_key','agent_generated_rubric'
          )),
          authority_ref TEXT NOT NULL,
          expected_answers_json TEXT NOT NULL,
          rubric_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status='qualified'),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(child_id, question_sha256, authority_type, authority_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_paper_answer_authority_lookup
          ON paper_answer_authority(child_id, question_sha256, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS paper_answer_authority_no_update
        BEFORE UPDATE ON paper_answer_authority
        BEGIN SELECT RAISE(ABORT, 'immutable paper answer authority'); END;

        CREATE TABLE IF NOT EXISTS paper_scoring_decision (
          decision_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          authority_type TEXT NOT NULL CHECK(authority_type IN (
            'teacher_mark','published_answer_key','agent_generated_rubric'
          )),
          authority_ref TEXT NOT NULL,
          authority_snapshot_json TEXT NOT NULL,
          correctness REAL NOT NULL CHECK(correctness IN (0,1)),
          confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          status TEXT NOT NULL CHECK(status='qualified'),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_scoring_decision_child
          ON paper_scoring_decision(child_id, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS paper_scoring_decision_no_update
        BEFORE UPDATE ON paper_scoring_decision
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring decision'); END;

        CREATE TABLE IF NOT EXISTS paper_subjective_score_binding (
          binding_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          decision_id TEXT NOT NULL UNIQUE
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          assessment_id TEXT NOT NULL,
          source_evidence_id TEXT NOT NULL,
          assessment_fingerprint TEXT NOT NULL,
          authority_snapshot_sha256 TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_subjective_binding_child
          ON paper_subjective_score_binding(child_id, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS paper_subjective_score_binding_no_update
        BEFORE UPDATE ON paper_subjective_score_binding
        BEGIN SELECT RAISE(ABORT, 'immutable paper subjective score binding'); END;

        CREATE TABLE IF NOT EXISTS paper_scoring_decision_status_event (
          event_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('qualified','withdrawn')),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_scoring_status_decision
          ON paper_scoring_decision_status_event(decision_id, created_at);
        CREATE TRIGGER IF NOT EXISTS paper_scoring_decision_status_no_update
        BEFORE UPDATE ON paper_scoring_decision_status_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring status event'); END;

        CREATE TABLE IF NOT EXISTS paper_scoring_decision_revision (
          revision_id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL
            REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          revision_sequence INTEGER NOT NULL CHECK(revision_sequence >= 1),
          previous_revision_id TEXT
            REFERENCES paper_scoring_decision_revision(revision_id),
          authority_type TEXT NOT NULL CHECK(authority_type IN (
            'teacher_mark','published_answer_key','agent_generated_rubric'
          )),
          authority_ref TEXT NOT NULL,
          authority_snapshot_json TEXT NOT NULL,
          correctness REAL NOT NULL CHECK(correctness IN (0,1)),
          confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(decision_id,revision_sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_paper_scoring_revision_request
          ON paper_scoring_decision_revision(request_id,created_at);
        CREATE TRIGGER IF NOT EXISTS paper_scoring_revision_no_update
        BEFORE UPDATE ON paper_scoring_decision_revision
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring revision'); END;
        CREATE TABLE IF NOT EXISTS paper_scoring_revision_status_event (
          event_id TEXT PRIMARY KEY,
          revision_id TEXT NOT NULL
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('qualified','withdrawn')),
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE TRIGGER IF NOT EXISTS paper_scoring_revision_status_no_update
        BEFORE UPDATE ON paper_scoring_revision_status_event
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring revision status'); END;
        CREATE TABLE IF NOT EXISTS paper_scoring_dialogue_revision_reconciliation (
          revision_id TEXT PRIMARY KEY
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          request_id TEXT NOT NULL
            REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
          outcome TEXT NOT NULL CHECK(outcome IN (
            'mistake_created','correct_verification_scheduled','incorrect_unmapped'
          )),
          mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS paper_scoring_revision_mistake_link (
          revision_id TEXT PRIMARY KEY
            REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
          mistake_id TEXT NOT NULL UNIQUE REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE TRIGGER IF NOT EXISTS paper_scoring_revision_mistake_no_update
        BEFORE UPDATE ON paper_scoring_revision_mistake_link
        BEGIN SELECT RAISE(ABORT,'immutable paper revision mistake link'); END;
        CREATE VIEW IF NOT EXISTS paper_scoring_effective_decision AS
        SELECT root.request_id,root.child_id,root.decision_id AS root_decision_id,
               COALESCE(revision.revision_id,root.decision_id) AS decision_id,
               COALESCE(revision.authority_type,root.authority_type) AS authority_type,
               COALESCE(revision.authority_ref,root.authority_ref) AS authority_ref,
               COALESCE(revision.authority_snapshot_json,root.authority_snapshot_json)
                 AS authority_snapshot_json,
               COALESCE(revision.correctness,root.correctness) AS correctness,
               COALESCE(revision.confidence,root.confidence) AS confidence,
               COALESCE(revision.created_at,root.created_at) AS created_at,
               CASE WHEN revision.revision_id IS NOT NULL THEN
                 COALESCE((SELECT event.status FROM paper_scoring_revision_status_event AS event
                           WHERE event.revision_id=revision.revision_id
                           ORDER BY event.rowid DESC LIMIT 1),'qualified')
               ELSE COALESCE((SELECT event.status FROM paper_scoring_decision_status_event AS event
                              WHERE event.decision_id=root.decision_id
                              ORDER BY event.rowid DESC LIMIT 1),'qualified') END AS effective_status
        FROM paper_scoring_decision AS root
        LEFT JOIN paper_scoring_decision_revision AS revision
          ON revision.revision_id=(
            SELECT latest.revision_id FROM paper_scoring_decision_revision AS latest
            WHERE latest.decision_id=root.decision_id
            ORDER BY latest.revision_sequence DESC LIMIT 1
          );
        CREATE VIEW IF NOT EXISTS paper_scoring_visible_decision AS
        SELECT effective.* FROM paper_scoring_effective_decision AS effective
        WHERE effective.decision_id=effective.root_decision_id
           OR NOT EXISTS(
             SELECT 1 FROM paper_scoring_request AS request
             JOIN mistake_dialogue AS dialogue
               ON dialogue.candidate_id=request.candidate_id
              AND dialogue.child_id=request.child_id
             WHERE request.request_id=effective.request_id
           )
           OR EXISTS(
             SELECT 1 FROM paper_scoring_dialogue_revision_reconciliation AS receipt
             WHERE receipt.revision_id=effective.decision_id
               AND receipt.child_id=effective.child_id
           );
        """
    )
    from .paper_correct_verification import ensure_schema as ensure_correct_verification_schema
    ensure_correct_verification_schema(connection)
    from .paper_correct_revision_verification import ensure_schema as ensure_revision_verification_schema
    ensure_revision_verification_schema(connection)
    from .paper_scoring_supply import ensure_schema as ensure_supply_schema
    ensure_supply_schema(connection)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(paper_scoring_request)")}
    if "fact_source" not in columns:
        connection.execute(
            """ALTER TABLE paper_scoring_request ADD COLUMN fact_source TEXT
               CHECK(fact_source IN ('child_spoken_readback','qualified_visual_observation',
                                     'system_presented_attempt'))"""
        )
    if "fact_confidence" not in columns:
        connection.execute(
            """ALTER TABLE paper_scoring_request ADD COLUMN fact_confidence REAL
               CHECK(fact_confidence IS NULL OR
                     (fact_confidence >= 0 AND fact_confidence <= 1))"""
        )


class PaperScoringService:
    """Freeze readback facts and resolve them only through qualified authority."""

    def __init__(self, database: Database):
        self.database = database
        with self.database.connect() as connection:
            ensure_paper_scoring_schema(connection)
            connection.commit()

    @staticmethod
    def _normalized(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        compact = re.sub(r"[\s,，、;；和]+", "", value)
        if re.fullmatch(r"[a-d]+", compact):
            return "".join(sorted(set(compact)))
        return compact

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def request(
        self, *, candidate_id: str, child_id: str, session_id: str,
        question_text: str, answer_text: str, answer_source: str,
        visual_answer_evidence_id: str | None, client_action_id: str,
        fact_source: str = "child_spoken_readback", fact_confidence: float = 1.0,
        _connection=None,
    ) -> dict[str, Any]:
        question = " ".join(question_text.split())
        answer = answer_text.strip()
        if len(question) < 2 or not answer:
            raise ValueError("paper scoring requires confirmed question and answer facts")
        if answer_source not in {"visual_unverified", "child_spoken"}:
            raise ValueError("invalid paper answer source")
        if fact_source not in {
            "child_spoken_readback", "qualified_visual_observation", "system_presented_attempt"
        }:
            raise ValueError("invalid paper scoring fact source")
        if not 0 <= float(fact_confidence) <= 1:
            raise ValueError("invalid paper scoring fact confidence")
        with (nullcontext(_connection) if _connection is not None else self.database.connect()) as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """SELECT candidate.*,artifact.session_id AS artifact_session_id
                   FROM mistake_capture_candidate AS candidate
                   JOIN capture_artifact AS artifact ON artifact.artifact_id=candidate.artifact_id
                    AND artifact.child_id=candidate.child_id
                   WHERE candidate.candidate_id=? AND candidate.child_id=?""",
                (candidate_id, child_id),
            ).fetchone()
            if not candidate or candidate["artifact_session_id"] != session_id:
                raise ValueError("paper scoring candidate ownership mismatch")
            existing = connection.execute(
                """SELECT request.*,decision.decision_id,decision.authority_type,
                          decision.authority_ref,decision.correctness AS resolved_correctness,
                          decision.confidence AS decision_confidence
                   FROM paper_scoring_request AS request
                   LEFT JOIN paper_scoring_visible_decision AS decision
                     ON decision.request_id=request.request_id
                    AND decision.effective_status='qualified'
                   WHERE request.candidate_id=? OR request.client_action_id=?""",
                (candidate_id, client_action_id),
            ).fetchone()
            if existing:
                expected = (
                    candidate_id, child_id, session_id, question, answer, answer_source,
                    visual_answer_evidence_id, fact_source, float(fact_confidence),
                )
                actual = tuple(existing[key] for key in (
                    "candidate_id", "child_id", "session_id", "question_text",
                    "answer_text", "answer_source", "visual_answer_evidence_id",
                )) + (
                    existing["fact_source"] or existing["confirmation_source"],
                    float(existing["fact_confidence"] if existing["fact_confidence"] is not None else 1.0),
                )
                if actual != expected:
                    raise ValueError("paper scoring request payload conflict")
                from .paper_scoring_supply import ensure_for_request
                ensure_for_request(
                    connection, existing,
                    action_prefix=f"paper-scoring-supply:{existing['request_id']}",
                )
                if _connection is None:
                    connection.commit()
                return self._serialize(existing)
            if visual_answer_evidence_id:
                evidence = connection.execute(
                    """SELECT evidence.answer_text,evidence.confidence
                       FROM observed_answer_evidence AS evidence
                       JOIN observed_question AS question
                         ON question.question_id=evidence.question_id
                        AND question.child_id=evidence.child_id
                       WHERE evidence.visual_answer_evidence_id=?
                         AND evidence.child_id=? AND question.mistake_candidate_id=?""",
                    (visual_answer_evidence_id, child_id, candidate_id),
                ).fetchone()
                if (not evidence or evidence["confidence"] < 0.75
                        or self._normalized(evidence["answer_text"]) != self._normalized(answer)):
                    raise ValueError("visual answer evidence is not qualified for readback")
            request_id = str(uuid4())
            now = utc_now()
            connection.execute(
                """INSERT INTO paper_scoring_request(
                     request_id,visual_answer_evidence_id,candidate_id,child_id,session_id,
                     question_text,answer_text,answer_source,question_sha256,answer_sha256,
                     confirmation_source,fact_source,fact_confidence,status,correctness,mastery_update_allowed,
                     created_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,'child_spoken_readback',?,?,
                            'pending_authority',NULL,0,?,?)""",
                (request_id, visual_answer_evidence_id, candidate_id, child_id, session_id,
                 question, answer, answer_source, self._hash(question), self._hash(answer),
                 fact_source, float(fact_confidence), now, client_action_id),
            )
            row = connection.execute(
                "SELECT * FROM paper_scoring_request WHERE request_id=?", (request_id,)
            ).fetchone()
            from .paper_scoring_supply import ensure_for_request
            ensure_for_request(
                connection, row,
                action_prefix=f"paper-scoring-supply:{request_id}",
            )
            if _connection is None:
                connection.commit()
            return self._serialize(row)

    def register_exact_authority(
        self, *, child_id: str, session_id: str | None, question_text: str,
        expected_answers: list[str], authority_type: str, authority_ref: str,
        rubric_version: str, client_action_id: str,
    ) -> dict[str, Any]:
        """Register a versioned objective answer source before it can score work."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = self._register_exact_authority(
                connection, child_id=child_id, session_id=session_id,
                question_text=question_text, expected_answers=expected_answers,
                authority_type=authority_type, authority_ref=authority_ref,
                rubric_version=rubric_version, client_action_id=client_action_id,
            )
            connection.commit()
            return result

    @classmethod
    def register_presented_item_authority(
        cls, connection: Any, *, child_id: str, session_id: str,
        item: dict[str, Any], item_version: str, prompt_id: str,
    ) -> dict[str, Any] | None:
        """Freeze an answer key when the Runtime actually presents an objective item.

        Merely existing in the catalog is not enough: the child/session binding and
        the exact presented item version are part of the authority provenance.
        """
        if item.get("assessment_mode") != "objective" or item.get("status") != "active":
            return None
        try:
            rubric = json.loads(item["expected_answer_json"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        answers = rubric.get("exact_answers") if isinstance(rubric, dict) else None
        if (rubric.get("local_rule") != "local_exact_v1" or not isinstance(answers, list)
                or rubric.get("keyword_groups") or rubric.get("task_points")):
            return None
        service = cls.__new__(cls)
        return service._register_exact_authority(
            connection, child_id=child_id, session_id=session_id,
            question_text=item.get("prompt") or "", expected_answers=answers,
            authority_type="agent_generated_rubric",
            authority_ref=f"diagnostic_prompt:{prompt_id}:{item['diagnostic_item_id']}",
            rubric_version=item_version,
            client_action_id=f"paper-authority:diagnostic-prompt:{prompt_id}",
        )

    def _register_exact_authority(
        self, connection: Any, *, child_id: str, session_id: str | None,
        question_text: str, expected_answers: list[str], authority_type: str,
        authority_ref: str, rubric_version: str, client_action_id: str,
    ) -> dict[str, Any]:
        if authority_type not in {"published_answer_key", "agent_generated_rubric"}:
            raise ValueError("paper answer authority must be a published key or agent rubric")
        question = " ".join(question_text.split())
        answers = [value.strip() for value in expected_answers if isinstance(value, str) and value.strip()]
        if len(question) < 2 or not answers or len(set(map(self._normalized, answers))) != len(answers):
            raise ValueError("qualified exact authority requires a question and unique answers")
        if not authority_ref.strip() or not rubric_version.strip():
            raise ValueError("qualified exact authority requires a source and version")
        payload = json.dumps(answers, ensure_ascii=False, separators=(",", ":"))
        if not connection.execute(
            "SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)
        ).fetchone():
            raise ValueError("unknown child for paper answer authority")
        if session_id and not connection.execute(
            "SELECT 1 FROM daily_session WHERE session_id=? AND child_id=?", (session_id, child_id)
        ).fetchone():
            raise ValueError("paper answer authority session ownership mismatch")
        existing = connection.execute(
            "SELECT * FROM paper_answer_authority WHERE client_action_id=?", (client_action_id,)
        ).fetchone()
        expected = (child_id, session_id, question, authority_type, authority_ref.strip(), payload,
                    rubric_version.strip())
        if existing:
            actual = tuple(existing[key] for key in (
                "child_id", "session_id", "question_text", "authority_type", "authority_ref",
                "expected_answers_json", "rubric_version",
            ))
            if actual != expected:
                raise ValueError("paper answer authority action payload conflict")
            return dict(existing)
        authority_id = str(uuid4())
        connection.execute(
            """INSERT INTO paper_answer_authority(
                 authority_id,child_id,session_id,question_text,question_sha256,
                 authority_type,authority_ref,expected_answers_json,rubric_version,
                 status,created_at,client_action_id
               ) VALUES(?,?,?,?,?,?,?,?,?,'qualified',?,?)""",
            (authority_id, child_id, session_id, question, self._hash(question), authority_type,
             authority_ref.strip(), payload, rubric_version.strip(), utc_now(), client_action_id),
        )
        row = connection.execute(
            "SELECT * FROM paper_answer_authority WHERE authority_id=?", (authority_id,)
        ).fetchone()
        return dict(row)

    def resolve(self, request_id: str, child_id: str, client_action_id: str) -> dict[str, Any] | None:
        """Resolve an objective paper attempt without accepting a child's correctness claim."""
        try:
            return self._resolve(request_id, child_id, client_action_id, guarded=False)
        except _ReconciliationNeedsEffectGuard:
            with self.database.external_effect_guard(timeout=2):
                return self._resolve(request_id, child_id, client_action_id, guarded=True)

    def _resolve(self, request_id: str, child_id: str, client_action_id: str, *, guarded: bool,
                 _connection=None) -> dict[str, Any] | None:
        with (nullcontext(_connection) if _connection is not None else self.database.connect()) as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT * FROM paper_scoring_request WHERE request_id=? AND child_id=?",
                (request_id, child_id),
            ).fetchone()
            if not request:
                raise ValueError("unknown paper scoring request")
            existing = connection.execute(
                "SELECT * FROM paper_scoring_decision WHERE request_id=? OR client_action_id=?",
                (request_id, client_action_id),
            ).fetchone()
            if existing:
                if existing["request_id"] != request_id or existing["child_id"] != child_id:
                    raise ValueError("paper scoring decision action payload conflict")
                with _reconciliation_probe(not guarded):
                    self.reconcile_subjective_decisions(connection, child_id=child_id, request_id=request_id)
                    self.reconcile_source_decisions(connection, child_id=child_id, request_id=request_id)
                effective = connection.execute(
                    "SELECT * FROM paper_scoring_effective_decision WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if effective and effective["effective_status"] == "qualified":
                    from .paper_scoring_supply import mark_qualified
                    mark_qualified(
                        connection, request, effective,
                        action_prefix=f"paper-scoring-supply:{request_id}",
                    )
                    if _connection is None:
                        connection.commit()
                    return self._decision(effective)
                resolved = (self._teacher_mark(connection, request)
                            or self._registered_exact_authority(connection, request)
                            or self._calibrated_subjective_authority(connection, request))
                if not resolved:
                    from .paper_scoring_supply import reopen_pending
                    reopen_pending(
                        connection, request,
                        reason="previous scoring authority was withdrawn",
                        action_prefix=f"paper-scoring-supply:{request_id}:{effective['decision_id']}",
                    )
                    if _connection is None:
                        connection.commit()
                    return None
                if self.database.runtime_mode != 'trial':
                    from .paper_scoring_supply import record_attempt
                    record_attempt(
                        connection, request_id=request_id, child_id=child_id,
                        status='blocked_source', reason='FORMAL_SCORE_REVISION_RELEASE_GATE',
                        evidence={'revision_release_required': True, 'authority_granted': False},
                        client_action_id=f'formal-score-revision-gate:{request_id}:{effective["decision_id"]}',
                    )
                    if _connection is None:
                        connection.commit()
                    return None
                snapshot = json.dumps(resolved["snapshot"], ensure_ascii=False,
                                      separators=(",", ":"))
                if (client_action_id.startswith('printed-auto-scoring:')
                        and resolved['authority_ref'].startswith('calibrated-printed-candidate:')):
                    # The accepted generation, not a worker's stale selection,
                    # owns automatic revision idempotency inside this transaction.
                    client_action_id = 'printed-auto-scoring:' + self._hash(json.dumps(
                        [request_id, effective['decision_id'], resolved['snapshot']],
                        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                    ))
                revision_id = str(uuid4())
                previous = connection.execute(
                    """SELECT revision_id,revision_sequence
                       FROM paper_scoring_decision_revision WHERE decision_id=?
                       ORDER BY revision_sequence DESC LIMIT 1""",
                    (existing["decision_id"],),
                ).fetchone()
                sequence = int(previous["revision_sequence"]) + 1 if previous else 1
                connection.execute(
                    """INSERT INTO paper_scoring_decision_revision(
                         revision_id,decision_id,request_id,child_id,
                         revision_sequence,previous_revision_id,authority_type,
                         authority_ref,authority_snapshot_json,correctness,confidence,
                         created_at,client_action_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (revision_id, existing["decision_id"], request_id, child_id,
                     sequence, previous["revision_id"] if previous else None,
                     resolved["authority_type"], resolved["authority_ref"], snapshot,
                     resolved["correctness"], resolved["confidence"], utc_now(),
                     client_action_id),
                )
                connection.execute(
                    """INSERT INTO paper_scoring_revision_status_event(
                         event_id,revision_id,child_id,status,reason,created_at,client_action_id
                       ) VALUES(?,?,?,'qualified','qualified revised scoring authority',?,?)""",
                    (str(uuid4()), revision_id, child_id, utc_now(),
                     f"{client_action_id}:qualified"),
                )
                revised = connection.execute(
                    "SELECT * FROM paper_scoring_effective_decision WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                from .paper_scoring_supply import mark_qualified
                mark_qualified(
                    connection, request, revised,
                    action_prefix=f"paper-scoring-supply:{request_id}",
                )
                if _connection is None:
                    connection.commit()
                return self._decision(revised)

            resolved = self._teacher_mark(connection, request)
            if not resolved:
                resolved = self._registered_exact_authority(connection, request)
            if not resolved:
                resolved = self._calibrated_subjective_authority(connection, request)
            if not resolved:
                from .paper_scoring_supply import ensure_for_request
                ensure_for_request(
                    connection, request,
                    action_prefix=f"paper-scoring-supply:{request_id}",
                )
                if _connection is None:
                    connection.commit()
                return None
            decision_id = str(uuid4())
            connection.execute(
                """INSERT INTO paper_scoring_decision(
                     decision_id,request_id,child_id,authority_type,authority_ref,
                     authority_snapshot_json,correctness,confidence,status,created_at,
                     client_action_id
                   ) VALUES(?,?,?,?,?,?,?,?, 'qualified',?,?)""",
                (decision_id, request_id, child_id, resolved["authority_type"],
                 resolved["authority_ref"], json.dumps(resolved["snapshot"], ensure_ascii=False,
                 separators=(",", ":")), resolved["correctness"], resolved["confidence"],
                 utc_now(), client_action_id),
            )
            row = connection.execute(
                "SELECT * FROM paper_scoring_decision WHERE decision_id=?", (decision_id,)
            ).fetchone()
            connection.execute(
                """INSERT INTO paper_scoring_decision_status_event(
                     event_id,decision_id,child_id,status,reason,created_at,client_action_id
                   ) VALUES(?,?,?,'qualified','qualified scoring authority',?,?)""",
                (str(uuid4()), decision_id, child_id, utc_now(),
                 f"{client_action_id}:qualified"),
            )
            subjective = resolved.get("subjective_binding")
            if subjective:
                snapshot_json = json.dumps(
                    resolved["snapshot"], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    """INSERT INTO paper_subjective_score_binding(
                         binding_id,request_id,decision_id,child_id,assessment_id,
                         source_evidence_id,assessment_fingerprint,
                         authority_snapshot_sha256,created_at,client_action_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), request_id, decision_id, child_id,
                     subjective["assessment_id"], subjective["source_evidence_id"],
                     subjective["assessment_fingerprint"], self._hash(snapshot_json),
                     utc_now(), f"{client_action_id}:subjective-binding"),
                )
            from .paper_scoring_supply import mark_qualified
            mark_qualified(
                connection, request, row,
                action_prefix=f"paper-scoring-supply:{request_id}",
            )
            if _connection is None:
                connection.commit()
            return self._decision(row)

    def schedule_correct_verification(
        self, *, decision_id: str, child_id: str, knowledge_node_id: str,
        client_action_id: str,
    ) -> dict[str, Any]:
        """Require a different independently answered item before mastery can change."""
        from .paper_correct_verification import create_obligation
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = connection.execute(
                "SELECT 1 FROM paper_scoring_decision_revision WHERE revision_id=? AND child_id=?",
                (decision_id, child_id),
            ).fetchone()
            if revision:
                from .paper_correct_revision_verification import create_obligation as create_revision
                result = create_revision(
                    connection, revision_id=decision_id, child_id=child_id,
                    knowledge_node_id=knowledge_node_id, client_action_id=client_action_id,
                )
            else:
                result = create_obligation(
                    connection, decision_id=decision_id, child_id=child_id,
                    knowledge_node_id=knowledge_node_id, client_action_id=client_action_id,
                )
            connection.commit()
            return result

    def _calibrated_subjective_authority(
        self, connection: Any, request: Any
    ) -> dict[str, Any] | None:
        """Resolve only an exact, current, calibrated original-response score.

        Similar wording is deliberately insufficient.  The assessment must bind
        the same child, session, complete question and complete answer.  A later
        human correction or source change invalidates this frozen paper decision.
        """
        from .paper_printed_scoring import authority_for_request
        printed = authority_for_request(connection, request)
        if printed is not None:
            return printed
        return self._original_subjective_authority(connection, request)

    def _original_subjective_authority(
        self, connection: Any, request: Any
    ) -> dict[str, Any] | None:
        """Recheck the original assessment protocol, not a replacement authority."""
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rubric_assessment'"
        ).fetchone():
            return None
        from .writing_score import current_written_score

        rows = connection.execute(
            """SELECT assessment_id FROM rubric_assessment
               WHERE child_id=?
                 AND json_extract(CASE WHEN json_valid(payload_json)
                       THEN payload_json ELSE '{}' END,'$.session_id')=?
                 AND json_extract(CASE WHEN json_valid(payload_json)
                       THEN payload_json ELSE '{}' END,'$.question')=?
                 AND json_extract(CASE WHEN json_valid(payload_json)
                       THEN payload_json ELSE '{}' END,'$.response')=?
               ORDER BY assessment_id""",
            (request["child_id"], request["session_id"], request["question_text"],
             request["answer_text"]),
        ).fetchall()
        accepted = []
        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                score = current_written_score(
                    connection, request["child_id"], row["assessment_id"], now=now
                )
            except (ValueError, KeyError, TypeError):
                continue
            current = score["current"]
            decision = score["decision"]
            receipt = score["receipt"]
            if (decision.get("status") != "calibrated_model"
                    or current.get("child_id") != request["child_id"]
                    or current.get("session_id") != request["session_id"]
                    or current.get("question") != request["question_text"]
                    or current.get("response") != request["answer_text"]
                    or receipt.get("assessment_id") != current.get("assessment_id")
                    or receipt.get("score_status") != "calibrated_model"):
                continue
            proof = receipt.get("calibration_receipt") or {}
            if (proof.get("score_qualified") is not True
                    or proof.get("source_evidence_id") != current.get("source_evidence_id")):
                continue
            dimensions = proof.get("dimensions")
            snapshot = {
                "authority_subtype": "calibrated_subjective_rubric",
                "assessment_id": current["assessment_id"],
                "assessment_fingerprint": current["fingerprint"],
                "source_evidence_id": current["source_evidence_id"],
                "original_binding_version": score["original_binding_version"],
                "score_authority_id": decision["authority_id"],
                "calibration_certificate_event_id": proof.get("certificate_event_id"),
                "dimensions_sha256": self._hash(json.dumps(
                    dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )),
                "passed": decision["passed"],
            }
            accepted.append((score, snapshot))
        if len(accepted) != 1:
            return None
        score, snapshot = accepted[0]
        current = score["current"]
        receipt = score["receipt"]
        return {
            "authority_type": "agent_generated_rubric",
            "authority_ref": f"calibrated-assessment:{current['assessment_id']}",
            "correctness": float(bool(score["decision"]["passed"])),
            "confidence": 1.0,
            "snapshot": snapshot,
            "subjective_binding": {
                "assessment_id": current["assessment_id"],
                "source_evidence_id": receipt["calibration_receipt"]["source_evidence_id"],
                "assessment_fingerprint": current["fingerprint"],
            },
        }

    @staticmethod
    def _decision_withdrawn(connection: Any, decision_id: str) -> bool:
        row = connection.execute(
            """SELECT status FROM paper_scoring_decision_status_event
               WHERE decision_id=? ORDER BY rowid DESC LIMIT 1""", (decision_id,)
        ).fetchone()
        return bool(row and row["status"] == "withdrawn")

    @classmethod
    def reconcile_subjective_decisions(
        cls, connection: Any, *, child_id: str, assessment_id: str | None = None,
        request_id: str | None = None,
    ) -> list[str]:
        """Withdraw stale subjective decisions and their derived obligations."""
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_subjective_score_binding'"
        ).fetchone():
            return []
        query = """SELECT binding.*,decision.authority_snapshot_json,request.question_text,
                          request.answer_text,request.session_id,request.candidate_id
                   FROM paper_subjective_score_binding AS binding
                   JOIN paper_scoring_decision AS decision USING(decision_id)
                   JOIN paper_scoring_request AS request USING(request_id)
                   WHERE binding.child_id=?"""
        parameters: list[Any] = [child_id]
        if assessment_id:
            query += " AND binding.assessment_id=?"
            parameters.append(assessment_id)
        if request_id:
            query += " AND binding.request_id=?"
            parameters.append(request_id)
        rows = connection.execute(query, tuple(parameters)).fetchall()
        withdrawn = []
        service = cls.__new__(cls)
        now = utc_now()
        for row in rows:
            if cls._decision_withdrawn(connection, row["decision_id"]):
                continue
            request = {
                "child_id": child_id, "session_id": row["session_id"],
                "question_text": row["question_text"], "answer_text": row["answer_text"],
            }
            current = service._original_subjective_authority(connection, request)
            expected_hash = service._hash(json.dumps(
                current["snapshot"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )) if current else None
            if (not current or current["subjective_binding"]["assessment_id"] != row["assessment_id"]
                    or current["subjective_binding"]["source_evidence_id"] != row["source_evidence_id"]
                    or current["subjective_binding"]["assessment_fingerprint"] != row["assessment_fingerprint"]
                    or expected_hash != row["authority_snapshot_sha256"]):
                event_action = f"paper-subjective-withdraw:{row['decision_id']}"
                cls._withdraw_invalid_decision(
                    connection, row["decision_id"], row["candidate_id"], child_id,
                    event_action, "subjective scoring authority changed", now,
                )
                withdrawn.append(row["decision_id"])
        return withdrawn

    @staticmethod
    def _local_source_current(connection: Any, authority_ref: str) -> bool:
        prefix = "local-answer-source:"
        if not authority_ref.startswith(prefix):
            return True
        candidate_id, separator, source_version = authority_ref[len(prefix):].partition(":")
        if not candidate_id or not separator or not source_version:
            return False
        from .paper_scoring_source_match import candidate_is_current
        return candidate_is_current(connection, candidate_id, source_version)

    @classmethod
    def _withdraw_invalid_decision(
        cls, connection: Any, decision_id: str, candidate_id: str, child_id: str,
        event_action: str, reason: str, now: str,
    ) -> None:
        # This must run before any SQL write: some downstream schema helpers
        # commit implicitly, so attempting withdrawal then rolling back is unsafe.
        if _probe_reconciliation.get():
            raise _ReconciliationNeedsEffectGuard()
        revision = connection.execute(
            "SELECT decision_id FROM paper_scoring_decision_revision WHERE revision_id=? AND child_id=?",
            (decision_id, child_id),
        ).fetchone()
        if revision:
            connection.execute(
                """INSERT OR IGNORE INTO paper_scoring_revision_status_event(
                     event_id,revision_id,child_id,status,reason,created_at,client_action_id
                   ) VALUES(?,?,?,'withdrawn',?,?,?)""",
                (str(uuid4()), decision_id, child_id, reason, now, event_action),
            )
        else:
            connection.execute(
                """INSERT OR IGNORE INTO paper_scoring_decision_status_event(
                     event_id,decision_id,child_id,status,reason,created_at,client_action_id
                   ) VALUES(?,?,?,'withdrawn',?,?,?)""",
                (str(uuid4()), decision_id, child_id, reason, now, event_action),
            )
        promotion = connection.execute(
            """SELECT mistake_id FROM paper_scoring_revision_mistake_link
               WHERE revision_id=? AND child_id=?""",
            (decision_id, child_id),
        ).fetchone() if revision else None
        if not promotion and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visual_answer_evidence_promotion'"
        ).fetchone():
            promotion = connection.execute(
                """SELECT promotion.mistake_id FROM visual_answer_evidence_promotion AS promotion
                   WHERE promotion.candidate_id=? AND promotion.child_id=?""",
                (candidate_id, child_id),
            ).fetchone()
        if not promotion:
            promotion = connection.execute(
                """SELECT mistake_id FROM mistake_capture_candidate
                   WHERE candidate_id=? AND child_id=? AND mistake_id IS NOT NULL""",
                (candidate_id, child_id),
            ).fetchone()
        if promotion:
            from .mistake_book import MistakeBookService
            MistakeBookService.exclude_invalid_scoring_authority(
                connection, promotion["mistake_id"], child_id,
                f"{event_action}:mistake", now,
            )
        from .paper_correct_verification import withdraw_for_decision
        if revision:
            from .paper_correct_revision_verification import withdraw_for_revision
            withdraw_for_revision(
                connection, decision_id, child_id,
                f"{event_action}:correct-verification",
            )
        else:
            withdraw_for_decision(
                connection, decision_id, child_id,
                f"{event_action}:correct-verification",
            )
        request = connection.execute(
            """SELECT request.* FROM paper_scoring_request AS request
               JOIN paper_scoring_effective_decision AS decision USING(request_id)
               WHERE decision.decision_id=? AND decision.child_id=?""",
            (decision_id, child_id),
        ).fetchone()
        if request:
            from .paper_scoring_supply import reopen_pending
            reopen_pending(
                connection, request, reason=reason,
                action_prefix=f"{event_action}:supply",
            )

    @classmethod
    def reconcile_source_decisions(
        cls, connection: Any, *, child_id: str, request_id: str | None = None,
    ) -> list[str]:
        from .paper_printed_scoring import reconcile
        printed_withdrawn = reconcile(connection, child_id, request_id)
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_scoring_source_candidate'"
        ).fetchone():
            return printed_withdrawn
        query = """SELECT decision.decision_id,decision.authority_ref,
                          request.candidate_id
                   FROM paper_scoring_effective_decision AS decision
                   JOIN paper_scoring_request AS request USING(request_id)
                   WHERE decision.child_id=?
                     AND decision.effective_status='qualified'
                     AND decision.authority_ref LIKE 'local-answer-source:%'"""
        params: list[Any] = [child_id]
        if request_id:
            query += " AND decision.request_id=?"
            params.append(request_id)
        withdrawn = list(printed_withdrawn)
        now = utc_now()
        for row in connection.execute(query, params).fetchall():
            if cls._local_source_current(connection, row["authority_ref"]):
                continue
            cls._withdraw_invalid_decision(
                connection, row["decision_id"], row["candidate_id"], child_id,
                f"paper-source-withdraw:{row['decision_id']}",
                "local answer source changed", now,
            )
            withdrawn.append(row["decision_id"])
        return withdrawn

    def _teacher_mark(self, connection: Any, request: Any) -> dict[str, Any] | None:
        evidence_id = request["visual_answer_evidence_id"]
        row = None
        marks: set[str] = set()
        authority_ref = evidence_id
        confidence = 0.0
        minimum_confidence = .75
        if evidence_id:
            row = connection.execute(
                """SELECT grading_marks_json,confidence FROM observed_answer_evidence
                   WHERE visual_answer_evidence_id=? AND child_id=?""",
                (evidence_id, request["child_id"]),
            ).fetchone()
            if row:
                try:
                    marks = {
                        re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", str(mark)).casefold())
                        for mark in json.loads(row["grading_marks_json"])
                    }
                    confidence = float(row["confidence"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    marks = set()
        if not marks:
            candidate = connection.execute(
                """SELECT candidate.question_text,candidate.confidence,artifact.artifact_id,
                          artifact.details_json
                   FROM mistake_capture_candidate AS candidate
                   JOIN capture_artifact AS artifact ON artifact.artifact_id=candidate.artifact_id
                    AND artifact.child_id=candidate.child_id
                   WHERE candidate.candidate_id=? AND candidate.child_id=?""",
                (request["candidate_id"], request["child_id"]),
            ).fetchone()
            if not candidate:
                return None
            try:
                details = json.loads(candidate["details_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            candidates = (details.get("local_pipeline") or {}).get("wrong_question_candidates") or []
            matching = [item for item in candidates if isinstance(item, dict)
                        and self._normalized(str(item.get("text") or "")) == self._normalized(request["question_text"])
                        and not item.get("needs_manual_boundary_check")
                        and not item.get("question_binding_ambiguous")
                        and item.get("evidence") == "red_correction_mark_near_question"]
            if not matching:
                return None
            marks = {
                re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", str(item.get("mark_kind") or "")).casefold())
                for item in matching
            }
            confidence = min(float(candidate["confidence"]),
                             max(float(item.get("confidence") or 0) for item in matching))
            # The local red-ink geometry classifier is deliberately conservative;
            # a clean, question-bound cross is qualified from 0.65 upward.
            minimum_confidence = .65
            authority_ref = f"{candidate['artifact_id']}#{request['candidate_id']}"
        if confidence < minimum_confidence:
            return None
        wrong = bool(marks & {"cross", "crosslike", "wrong", "error", "x", "叉", "错", "红叉"})
        right = bool(marks & {"check", "checklike", "tick", "correct", "right", "√", "对", "红勾"})
        if wrong == right:
            return None
        return {
            "authority_type": "teacher_mark",
            "authority_ref": authority_ref,
            "correctness": 0.0 if wrong else 1.0,
            "confidence": confidence,
            "snapshot": {
                "grading_marks": sorted(marks),
                "visual_answer_evidence_id": evidence_id,
                "candidate_id": request["candidate_id"],
            },
        }

    def _registered_exact_authority(self, connection: Any, request: Any) -> dict[str, Any] | None:
        rows = connection.execute(
            """SELECT * FROM paper_answer_authority
               WHERE child_id=? AND question_sha256=? AND status='qualified'
                 AND (session_id IS NULL OR session_id=?)
               ORDER BY created_at DESC,authority_id DESC""",
            (request["child_id"], request["question_sha256"], request["session_id"]),
        ).fetchall()
        scored = []
        answer = self._normalized(request["answer_text"])
        for row in rows:
            if not self._local_source_current(connection, row["authority_ref"]):
                continue
            try:
                references = json.loads(row["expected_answers_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(references, list) or not references:
                continue
            correctness = float(answer in {self._normalized(str(value)) for value in references})
            scored.append((correctness, row, references))
        if not scored or len({item[0] for item in scored}) != 1:
            return None
        correctness, row, references = scored[0]
        return {
            "authority_type": row["authority_type"],
            "authority_ref": row["authority_ref"],
            "correctness": correctness,
            "confidence": 1.0,
            "snapshot": {
                "authority_id": row["authority_id"], "rubric_version": row["rubric_version"],
                "expected_answers_sha256": self._hash(json.dumps(references, ensure_ascii=False,
                    separators=(",", ":"))),
            },
        }

    def for_candidate(self, candidate_id: str, child_id: str) -> dict[str, Any] | None:
        try:
            return self._for_candidate(candidate_id, child_id, guarded=False)
        except _ReconciliationNeedsEffectGuard:
            with self.database.external_effect_guard(timeout=2):
                return self._for_candidate(candidate_id, child_id, guarded=True)

    def _for_candidate(self, candidate_id: str, child_id: str, *, guarded: bool) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with _reconciliation_probe(not guarded):
                self.reconcile_subjective_decisions(connection, child_id=child_id)
                self.reconcile_source_decisions(connection, child_id=child_id)
            row = connection.execute(
                """SELECT request.*,decision.decision_id,decision.authority_type,
                          decision.authority_ref,decision.correctness AS resolved_correctness,
                          decision.confidence AS decision_confidence
                   FROM paper_scoring_request AS request
                   LEFT JOIN paper_scoring_visible_decision AS decision
                     ON decision.request_id=request.request_id
                    AND decision.effective_status='qualified'
                   WHERE request.candidate_id=? AND request.child_id=?""",
                (candidate_id, child_id),
            ).fetchone()
            result = self._serialize(row) if row else None
            awaiting_reconciliation = False
            if result and not result.get("decision_id"):
                internal = connection.execute(
                    """SELECT decision_id,root_decision_id FROM paper_scoring_effective_decision
                       WHERE request_id=? AND effective_status='qualified'""",
                    (result["request_id"],),
                ).fetchone()
                awaiting_reconciliation = bool(
                    internal and internal["decision_id"] != internal["root_decision_id"]
                )
                if awaiting_reconciliation:
                    result["effective_status"] = "pending_reconciliation"
            if result and connection.execute(
                "SELECT 1 FROM paper_scoring_supply_obligation WHERE request_id=?",
                (result["request_id"],),
            ).fetchone():
                from .paper_scoring_supply import project
                result["authority_supply"] = project(
                    connection, request_id=result["request_id"]
                )
                if awaiting_reconciliation:
                    result["authority_supply"]["status"] = "pending_reconciliation"
                    result["authority_supply"]["reason"] = (
                        "revised scoring authority is awaiting durable learning reconciliation"
                    )
            connection.commit()
            return result

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["fact_source"] = result.get("fact_source") or result.get("confirmation_source")
        result["fact_confidence"] = (
            result.get("fact_confidence") if result.get("fact_confidence") is not None else 1.0
        )
        result["mastery_update_allowed"] = False
        result["cloud_transmission_allowed"] = False
        if result.get("decision_id"):
            result["effective_status"] = "scored"
        else:
            result["effective_status"] = result["status"]
        return result

    @staticmethod
    def _decision(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["authority_snapshot"] = json.loads(result.pop("authority_snapshot_json"))
        result["mastery_update_allowed"] = False
        result["cloud_transmission_allowed"] = False
        return result
