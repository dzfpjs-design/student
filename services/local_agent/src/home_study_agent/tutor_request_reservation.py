"""Transactional, durable reservation for tutor model calls.

The browser may lose an HTTP response after the local service has started a
model call.  A unique reservation prevents a retry from spending a second
model call before ``tutor_turn`` has been written.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json

from .database import Database


PENDING_STALE_AFTER = timedelta(minutes=2)


class TutorReservationConflictError(Exception):
    pass


class TutorReservationPendingError(Exception):
    pass


class TutorReservationUnknownOutcomeError(Exception):
    """The provider may have answered, but no local tutor turn was saved."""
    pass


@dataclass(frozen=True)
class TutorRequestIdentity:
    action_id: str
    session_id: str
    child_id: str
    subject: str
    question: str
    hint_level: int
    parent_turn_id: str | None
    ocr_confirmation_id: str | None
    request_hash: str


@dataclass(frozen=True)
class TutorReservation:
    action_id: str
    status: str
    attempts: int
    turn_id: str | None


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def client_request_hash(session_id: str, subject: str, client_question: str, hint_level: int, parent_turn_id: str | None, ocr_confirmation_id: str | None,
                        *, prompt_id: str | None = None, item_version: str | None = None, check_response_id: str | None = None) -> str:
    payload = {
        "session_id": session_id, "subject": subject.strip(), "question": normalize_text(client_question),
        "hint_level": hint_level, "parent_turn_id": parent_turn_id, "ocr_confirmation_id": ocr_confirmation_id,
    }
    if prompt_id is not None or item_version is not None:
        payload['question_reference'] = {'prompt_id': prompt_id, 'item_version': item_version}
    if check_response_id is not None:
        payload['check_response_id']=check_response_id
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class TutorRequestReservationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tutor_request_reservation (
                  action_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
                  subject TEXT NOT NULL,
                  normalized_question TEXT NOT NULL,
                  hint_level INTEGER NOT NULL,
                  parent_turn_id TEXT,
                  ocr_confirmation_id TEXT,
                  request_hash TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed', 'interrupted')),
                  tutor_turn_id TEXT REFERENCES tutor_turn(turn_id),
                  attempts INTEGER NOT NULL DEFAULT 1,
                  last_error_code TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tutor_request_reservation_session
                  ON tutor_request_reservation(session_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS tutor_request_attempt (
                  action_id TEXT NOT NULL REFERENCES tutor_request_reservation(action_id),
                  attempt INTEGER NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed', 'interrupted')),
                  error_code TEXT,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  PRIMARY KEY(action_id, attempt)
                );
                """
            )
            connection.commit()

    def lookup_completed_or_pending(self, action_id: str, request_hash: str, confirm_unknown_outcome_retry: bool = False) -> TutorReservation | None:
        """Use before OCR resolution so an expired confirmation cannot hide a completed turn."""
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM tutor_request_reservation WHERE action_id = ?", (action_id,)).fetchone()
            if not row:
                return None
            if row["request_hash"] != request_hash:
                raise TutorReservationConflictError()
            if row["status"] == "completed":
                return TutorReservation(action_id, "completed", row["attempts"], row["tutor_turn_id"])
            if row["status"] == "failed" and row["last_error_code"] == "FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED":
                from .model_gateway import ModelResponseError
                raise ModelResponseError(row["last_error_code"], fallback_eligible=False)
            if row["status"] == "pending" and not self._is_stale(row["updated_at"]):
                raise TutorReservationPendingError()
            if row["status"] == "interrupted" and row["last_error_code"] == "INTERRUPTED_UNKNOWN_OUTCOME":
                if not confirm_unknown_outcome_retry:
                    raise TutorReservationUnknownOutcomeError()
                return None
            if row["status"] == "pending":
                # A stale reservation has crossed an unknown boundary.  This
                # includes the legacy NULL marker: an older process may have
                # been inside ``gateway.complete`` when it died, so treating
                # it as a harmless timeout can silently bill a second call.
                now = datetime.now(timezone.utc).isoformat()
                connection.execute("BEGIN IMMEDIATE")
                latest = connection.execute("SELECT * FROM tutor_request_reservation WHERE action_id = ?", (action_id,)).fetchone()
                if latest and latest["status"] == "pending" and self._is_stale(latest["updated_at"]):
                    connection.execute(
                        """UPDATE tutor_request_reservation
                           SET status = 'interrupted', last_error_code = 'INTERRUPTED_UNKNOWN_OUTCOME', updated_at = ?
                           WHERE action_id = ?""", (now, action_id),
                    )
                    connection.execute(
                        """UPDATE tutor_request_attempt
                           SET status = 'interrupted', error_code = 'INTERRUPTED_UNKNOWN_OUTCOME', finished_at = ?
                           WHERE action_id = ? AND attempt = ? AND status = 'pending'""",
                        (now, action_id, latest["attempts"]),
                    )
                    connection.commit()
                    if not confirm_unknown_outcome_retry:
                        raise TutorReservationUnknownOutcomeError()
                    return None
                connection.commit()
                # A racing writer made the reservation fresh or durable; let
                # the next request read the new state instead of guessing.
                raise TutorReservationPendingError()
            # Failed, interrupted, and stale requests may be retried.  Do not
            # mutate here: ``claim`` makes the retry atomic after OCR evidence
            # has been resolved.
            return None

    def claim(self, identity: TutorRequestIdentity, confirm_unknown_outcome_retry: bool = False, *, pre_claim=None) -> TutorReservation:
        """Atomically reserve one model invocation or return its completed turn.

        The immediate transaction is deliberately held only around SQLite work,
        never around the model request.  A second concurrent caller sees the
        fresh ``pending`` row and is told to poll/retry instead of calling the
        model.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM tutor_request_reservation WHERE action_id = ?", (identity.action_id,)).fetchone()
            if existing:
                if not self._matches(existing, identity):
                    connection.rollback()
                    raise TutorReservationConflictError()
                if existing["status"] == "completed":
                    connection.commit()
                    return TutorReservation(identity.action_id, "completed", existing["attempts"], existing["tutor_turn_id"])
                if existing["status"] == "failed" and existing["last_error_code"] == "FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED":
                    connection.rollback()
                    from .model_gateway import ModelResponseError
                    raise ModelResponseError(existing["last_error_code"], fallback_eligible=False)
                if existing["status"] == "pending" and not self._is_stale(existing["updated_at"]):
                    connection.commit()
                    raise TutorReservationPendingError()
                if existing["status"] == "pending":
                    # ``lookup_completed_or_pending`` normally performs this
                    # transition first.  Keep the claim path safe as well for
                    # concurrent callers that observe the stale row here.
                    connection.execute(
                        """UPDATE tutor_request_reservation
                           SET status = 'interrupted', last_error_code = 'INTERRUPTED_UNKNOWN_OUTCOME', updated_at = ?
                           WHERE action_id = ? AND status = 'pending'""",
                        (now, identity.action_id),
                    )
                    connection.execute(
                        """UPDATE tutor_request_attempt
                           SET status = 'interrupted', error_code = 'INTERRUPTED_UNKNOWN_OUTCOME', finished_at = ?
                           WHERE action_id = ? AND attempt = ? AND status = 'pending'""",
                        (now, identity.action_id, existing["attempts"]),
                    )
                    existing = connection.execute("SELECT * FROM tutor_request_reservation WHERE action_id = ?", (identity.action_id,)).fetchone()
                if existing["status"] == "interrupted" and not confirm_unknown_outcome_retry:
                    connection.commit()
                    raise TutorReservationUnknownOutcomeError()
                if pre_claim is not None:
                    pre_claim(connection)
                previous_error = (
                    "UNKNOWN_OUTCOME_USER_CONFIRMED_RETRY"
                    if existing["status"] == "interrupted"
                    else f"RETRY_AFTER_{existing['status'].upper()}"
                )
                connection.execute(
                    """UPDATE tutor_request_attempt
                       SET status = CASE WHEN status = 'pending' THEN 'interrupted' ELSE status END,
                           error_code = COALESCE(error_code, ?), finished_at = COALESCE(finished_at, ?)
                       WHERE action_id = ? AND attempt = ?""",
                    (previous_error, now, identity.action_id, existing["attempts"]),
                )
                connection.execute(
                    """UPDATE tutor_request_reservation
                       SET status = 'pending', attempts = attempts + 1, last_error_code = ?, updated_at = ?
                       WHERE action_id = ?""",
                    (previous_error, now, identity.action_id),
                )
                connection.execute(
                    """INSERT INTO tutor_request_attempt(action_id, attempt, status, error_code, started_at, finished_at)
                       VALUES (?, ?, 'pending', NULL, ?, NULL)""",
                    (identity.action_id, existing["attempts"] + 1, now),
                )
                connection.commit()
                return TutorReservation(identity.action_id, "claimed", existing["attempts"] + 1, None)
            if pre_claim is not None:
                pre_claim(connection)
            connection.execute(
                """INSERT INTO tutor_request_reservation(
                  action_id, session_id, child_id, subject, normalized_question, hint_level, parent_turn_id,
                  ocr_confirmation_id, request_hash, status, tutor_turn_id, attempts, last_error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, 1, NULL, ?, ?)""",
                (identity.action_id, identity.session_id, identity.child_id, identity.subject, identity.question,
                 identity.hint_level, identity.parent_turn_id, identity.ocr_confirmation_id, identity.request_hash, now, now),
            )
            connection.execute(
                """INSERT INTO tutor_request_attempt(action_id, attempt, status, error_code, started_at, finished_at)
                   VALUES (?, 1, 'pending', NULL, ?, NULL)""",
                (identity.action_id, now),
            )
            connection.commit()
        return TutorReservation(identity.action_id, "claimed", 1, None)

    @staticmethod
    def _is_stale(updated_at: str) -> bool:
        try:
            timestamp = datetime.fromisoformat(updated_at)
            if timestamp.tzinfo is None:
                return True
        except (TypeError, ValueError):
            return True
        return datetime.now(timezone.utc) - timestamp > PENDING_STALE_AFTER

    @staticmethod
    def _matches(row, identity: TutorRequestIdentity) -> bool:
        return (
            row["request_hash"] == identity.request_hash
            and row["session_id"] == identity.session_id
            and row["child_id"] == identity.child_id
            and row["subject"] == identity.subject
            and row["normalized_question"] == identity.question
            and row["hint_level"] == identity.hint_level
            and row["parent_turn_id"] == identity.parent_turn_id
            and row["ocr_confirmation_id"] == identity.ocr_confirmation_id
        )

    def complete(self, action_id: str, turn_id: str, *, expected_attempt: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE tutor_request_reservation SET status = 'completed', tutor_turn_id = ?, last_error_code = NULL, updated_at = ?
                   WHERE action_id = ? AND status IN ('pending', 'interrupted')
                     AND (? IS NULL OR attempts = ?)""",
                (turn_id, now, action_id, expected_attempt, expected_attempt),
            )
            connection.execute(
                """UPDATE tutor_request_attempt
                   SET status = 'completed', error_code = NULL, finished_at = ?
                   WHERE action_id = ? AND attempt = (
                     SELECT attempts FROM tutor_request_reservation WHERE action_id = ?
                   ) AND status IN ('pending', 'interrupted') AND (? IS NULL OR attempt = ?)""",
                (now, action_id, action_id, expected_attempt, expected_attempt),
            )
            connection.commit()

    def mark_gateway_submission_started(self, action_id: str, *, expected_attempt: int | None = None) -> None:
        """Durably cross the provider-submission boundary before awaiting it.

        Once this marker is written, a stale request can no longer be retried
        automatically: the provider might be working even if this process is
        gone or the original HTTP request still appears hung.
        """
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE tutor_request_reservation
                   SET last_error_code = 'GATEWAY_SUBMISSION_STARTED', updated_at = ?
                   WHERE action_id = ? AND status = 'pending' AND (? IS NULL OR attempts = ?)""",
                (datetime.now(timezone.utc).isoformat(), action_id, expected_attempt, expected_attempt),
            )
            connection.commit()

    def mark_gateway_result_received(self, action_id: str, *, expected_attempt: int | None = None) -> bool:
        """Persist the narrow uncertainty boundary before local result storage.

        If the process dies after the model returns but before a tutor turn is
        saved, a later retry is shown as an explicitly uncertain outcome rather
        than silently claiming exactly-once delivery.
        """
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE tutor_request_reservation
                   SET status = 'pending', last_error_code = 'MODEL_RESULT_RECEIVED_UNPERSISTED', updated_at = ?
                   WHERE action_id = ? AND (? IS NULL OR attempts = ?)
                     AND (status = 'pending' OR (status = 'interrupted'
                       AND last_error_code = 'INTERRUPTED_UNKNOWN_OUTCOME' AND ? IS NOT NULL))""",
                (datetime.now(timezone.utc).isoformat(), action_id, expected_attempt, expected_attempt, expected_attempt),
            ).rowcount
            if changed:
                connection.execute("""UPDATE tutor_request_attempt SET status='pending',error_code=NULL,finished_at=NULL
                    WHERE action_id=? AND attempt=(SELECT attempts FROM tutor_request_reservation WHERE action_id=?)
                    AND status IN ('pending','interrupted')""", (action_id, action_id))
            connection.commit()
            return bool(changed)

    def interrupt_unknown_outcome(self, action_id: str, *, expected_attempt: int | None = None) -> None:
        """Record a post-submission failure without implying the provider did not run."""
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE tutor_request_reservation
                   SET status = 'interrupted', last_error_code = 'INTERRUPTED_UNKNOWN_OUTCOME', updated_at = ?
                   WHERE action_id = ? AND status = 'pending' AND (? IS NULL OR attempts = ?)""",
                (now, action_id, expected_attempt, expected_attempt),
            )
            connection.execute(
                """UPDATE tutor_request_attempt
                   SET status = 'interrupted', error_code = 'INTERRUPTED_UNKNOWN_OUTCOME', finished_at = ?
                   WHERE action_id = ? AND attempt = (
                     SELECT attempts FROM tutor_request_reservation WHERE action_id = ?
                   ) AND status = 'pending' AND (? IS NULL OR attempt = ?)""",
                (now, action_id, action_id, expected_attempt, expected_attempt),
            )
            connection.commit()

    def complete_from_saved_turn(self, turn: dict, request_hash: str) -> None:
        """Repair a pending reservation after a post-save process crash."""
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tutor_request_reservation WHERE action_id = ?", (turn["client_action_id"],)
            ).fetchone()
            if existing and existing["request_hash"] != request_hash:
                connection.rollback()
                raise TutorReservationConflictError()
            if existing:
                connection.execute(
                    """UPDATE tutor_request_reservation
                       SET status = 'completed', tutor_turn_id = ?, last_error_code = NULL, updated_at = ?
                       WHERE action_id = ?""",
                    (turn["turn_id"], now, turn["client_action_id"]),
                )
                attempt = existing["attempts"]
            else:
                connection.execute(
                    """INSERT INTO tutor_request_reservation(
                      action_id, session_id, child_id, subject, normalized_question, hint_level, parent_turn_id,
                      ocr_confirmation_id, request_hash, status, tutor_turn_id, attempts, last_error_code, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, 1, NULL, ?, ?)""",
                    (turn["client_action_id"], turn["session_id"], turn["child_id"], turn["subject"],
                     normalize_text(turn["question"]), turn["hint_level"], turn["parent_turn_id"],
                     turn["ocr_confirmation_id"], request_hash, turn["turn_id"], now, now),
                )
                attempt = 1
            connection.execute(
                """INSERT INTO tutor_request_attempt(action_id, attempt, status, error_code, started_at, finished_at)
                   VALUES (?, ?, 'completed', NULL, ?, ?)
                   ON CONFLICT(action_id, attempt) DO UPDATE SET status = 'completed', error_code = NULL, finished_at = excluded.finished_at""",
                (turn["client_action_id"], attempt, now, now),
            )
            connection.commit()

    def fail(self, action_id: str, error_code: str, *, expected_attempt: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE tutor_request_reservation SET status = 'failed', last_error_code = ?, updated_at = ?
                   WHERE action_id = ? AND status = 'pending' AND (? IS NULL OR attempts = ?)""",
                (error_code[:120], now, action_id, expected_attempt, expected_attempt),
            )
            connection.execute(
                """UPDATE tutor_request_attempt
                   SET status = 'failed', error_code = ?, finished_at = ?
                   WHERE action_id = ? AND attempt = (
                     SELECT attempts FROM tutor_request_reservation WHERE action_id = ?
                   ) AND status = 'pending' AND (? IS NULL OR attempt = ?)""",
                (error_code[:120], now, action_id, action_id, expected_attempt, expected_attempt),
            )
            connection.commit()
