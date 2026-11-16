from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from typing import Any
from uuid import uuid4

from .database import Database


class PetVocabularyError(ValueError):
    pass


REVIEW_INTERVAL_DAYS = (1, 3, 7, 14, 30, 60)
DEFAULT_SESSION_REVIEW_LIMIT = 12
PLAN_SOURCE_PREFIX = "pet-vocabulary-due:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PetVocabularyService:
    """Evidence-bound, per-child PET vocabulary review scheduler.

    Strong HTTP callers retain prompt_session_id/card_version from present_next
    or active_prompt. read_prompt/submit_prompt/cancel_prompt return
    {status, prompt, attempt, reason}; only waiting_answer invites an answer.
    Legacy active-based methods retain their call signatures but cannot identify
    a late new action; never fall back to them from a bound HTTP request.
    Existing unbound prompt rows are not upgraded into invented presentations.
    """

    def __init__(self, database: Database):
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pet_vocabulary_card (
                  card_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  lexeme_id TEXT NOT NULL REFERENCES kg_language_lexeme(lexeme_id) ON DELETE CASCADE,
                  surface TEXT NOT NULL,
                  focus_kind TEXT NOT NULL CHECK(focus_kind IN ('word', 'phrase')),
                  senses_json TEXT NOT NULL,
                  collocations_json TEXT NOT NULL,
                  source_ref TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('new', 'learning', 'review', 'suspended')),
                  consecutive_successes INTEGER NOT NULL DEFAULT 0,
                  total_attempts INTEGER NOT NULL DEFAULT 0,
                  lapse_count INTEGER NOT NULL DEFAULT 0,
                  interval_days INTEGER NOT NULL DEFAULT 0,
                  due_at TEXT NOT NULL,
                  last_attempt_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(child_id, lexeme_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pet_vocabulary_due
                  ON pet_vocabulary_card(child_id, status, due_at);
                CREATE TABLE IF NOT EXISTS pet_vocabulary_attempt (
                  attempt_id TEXT PRIMARY KEY,
                  card_id TEXT NOT NULL REFERENCES pet_vocabulary_card(card_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT REFERENCES daily_session(session_id) ON DELETE SET NULL,
                  review_mode TEXT NOT NULL CHECK(review_mode IN ('recognition', 'recall')),
                  submitted_answer TEXT NOT NULL,
                  correct INTEGER NOT NULL CHECK(correct IN (0, 1)),
                  previous_due_at TEXT NOT NULL,
                  next_due_at TEXT NOT NULL,
                  interval_days INTEGER NOT NULL,
                  occurred_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_pet_vocabulary_attempt_child
                  ON pet_vocabulary_attempt(child_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS pet_vocabulary_prompt_session (
                  prompt_session_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  card_id TEXT NOT NULL REFERENCES pet_vocabulary_card(card_id) ON DELETE CASCADE,
                  status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'cancelled')),
                  presented_at TEXT NOT NULL,
                  completed_at TEXT,
                  card_version TEXT,
                  card_snapshot_json TEXT,
                  source_version TEXT,
                  attempt_id TEXT REFERENCES pet_vocabulary_attempt(attempt_id) ON DELETE SET NULL,
                  invalidated_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pet_vocabulary_active_prompt
                  ON pet_vocabulary_prompt_session(session_id) WHERE status='active';
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            attempt_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(pet_vocabulary_attempt)")
            }
            if "session_id" not in attempt_columns:
                connection.execute("ALTER TABLE pet_vocabulary_attempt ADD COLUMN session_id TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pet_vocabulary_attempt_session ON pet_vocabulary_attempt(session_id, occurred_at)"
            )
            prompt_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pet_vocabulary_prompt_session)")}
            for name, definition in (
                ("card_version", "TEXT"), ("card_snapshot_json", "TEXT"), ("source_version", "TEXT"),
                ("attempt_id", "TEXT REFERENCES pet_vocabulary_attempt(attempt_id) ON DELETE SET NULL"),
                ("invalidated_reason", "TEXT"), ("authorized_turn_id", "TEXT"),
            ):
                if name not in prompt_columns:
                    connection.execute(f"ALTER TABLE pet_vocabulary_prompt_session ADD COLUMN {name} {definition}")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pet_vocabulary_prompt_attempt ON pet_vocabulary_prompt_session(attempt_id)")
            connection.commit()

    @staticmethod
    def _version(value) -> str:
        canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return "pet_prompt_v1:" + sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _active_session(connection, session_id):
        session = connection.execute("""SELECT ds.*,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp ON cp.child_id=ds.child_id WHERE session_id=?""", (session_id,)).fetchone()
        if (not session or session["status"] != "active" or session["child_status"] != "active"
                or session["state"] in {"evolving", "completed"}):
            raise PetVocabularyError("vocabulary_session_inactive")
        return session

    @staticmethod
    def _owned_prompt(connection, session, prompt_session_id):
        row = connection.execute("SELECT * FROM pet_vocabulary_prompt_session WHERE prompt_session_id=?",
                                 (prompt_session_id,)).fetchone()
        if not row or (row["session_id"], row["child_id"]) != (session["session_id"], session["child_id"]):
            raise PetVocabularyError("vocabulary_prompt_ownership_conflict")
        return dict(row)

    @staticmethod
    def _source_version(connection, card):
        lexeme = connection.execute("SELECT * FROM kg_language_lexeme WHERE lexeme_id=?", (card["lexeme_id"],)).fetchone()
        if not lexeme:
            return None
        if (lexeme["surface"] != card["surface"] or lexeme["focus_kind"] != card["focus_kind"]
                or lexeme["source_ref"] != card["source_ref"]
                or _string_values(json.loads(lexeme["senses_json"] or "[]")) != json.loads(card["senses_json"])
                or json.loads(lexeme["collocations_json"] or "[]") != json.loads(card["collocations_json"] or "[]")):
            return None
        candidates = connection.execute("""SELECT candidate_id,resource_id,proposed_json,status,updated_at
            FROM kg_language_focus_candidate WHERE language=? AND normalized=? AND focus_kind=?
              AND status='confirmed' AND proposed_json!='{}' ORDER BY candidate_id""",
            (lexeme["language"], lexeme["normalized"], lexeme["focus_kind"])).fetchall()
        if not candidates:
            return None
        return PetVocabularyService._version({"lexeme": dict(lexeme), "confirmations": [dict(row) for row in candidates]})

    def _validate_prompt(self, connection, prompt):
        if prompt["status"] != "active":
            return prompt
        reason = self._prompt_invalid_reason(connection, prompt)
        if reason:
            connection.execute("""UPDATE pet_vocabulary_prompt_session SET status='cancelled',
                invalidated_reason=?,completed_at=? WHERE prompt_session_id=?""",
                (reason, _now().isoformat(), prompt["prompt_session_id"]))
            return {**prompt, "status": "cancelled", "invalidated_reason": reason}
        return prompt

    @staticmethod
    def _prompt_invalid_reason(connection, prompt):
        card = connection.execute("SELECT * FROM pet_vocabulary_card WHERE card_id=? AND child_id=?",
                                  (prompt["card_id"], prompt["child_id"])).fetchone()
        reason = None
        if not prompt["card_snapshot_json"] or not prompt["card_version"] or not prompt["source_version"]:
            reason = "legacy_prompt_unbound"
        elif not card or card["status"] == "suspended":
            reason = "card_unavailable"
        elif PetVocabularyService._version(dict(card)) != prompt["card_version"]:
            reason = "card_version_changed"
        elif PetVocabularyService._source_version(connection, card) != prompt["source_version"]:
            reason = "card_source_changed"
        return reason

    @staticmethod
    def validate_capture_reference(connection, *, session_id, child_id, binding):
        """Read-only, exact presented-card validation in the caller's transaction."""
        if not isinstance(binding, dict) or set(binding) != {'prompt_session_id', 'card_version'}:
            raise PetVocabularyError('vocabulary_capture_binding_invalid')
        session = PetVocabularyService._active_session(connection, session_id)
        if session['child_id'] != child_id:
            raise PetVocabularyError('vocabulary_prompt_ownership_conflict')
        prompt = PetVocabularyService._owned_prompt(connection, session, binding['prompt_session_id'])
        if (prompt['status'] != 'active' or prompt['invalidated_reason']
                or prompt['card_version'] != binding['card_version']
                or PetVocabularyService._prompt_invalid_reason(connection, prompt)):
            raise PetVocabularyError('vocabulary_capture_source_changed')
        return dict(binding)

    def _prompt_card(self, prompt):
        if not prompt["card_snapshot_json"]:
            return None
        card = self._public_card(json.loads(prompt["card_snapshot_json"]))
        return {**card, **{key: prompt[key] for key in ("prompt_session_id", "card_version", "child_id", "session_id")}}

    def _prompt_result(self, connection, prompt):
        status = {"active": "waiting_answer", "completed": "answered", "cancelled": "cancelled"}[prompt["status"]]
        reason = prompt["invalidated_reason"]
        attempt = None
        if reason:
            status = "invalidated"
        elif status == "answered":
            row = connection.execute("SELECT * FROM pet_vocabulary_attempt WHERE attempt_id=?", (prompt["attempt_id"],)).fetchone()
            if not row or any(row[key] != prompt[key] for key in ("card_id", "child_id", "session_id")):
                status, reason = "invalidated", "attempt_unavailable"
            else:
                attempt = self._result_from_rows(json.loads(prompt["card_snapshot_json"]), row, bool(row["correct"]))
        return {"status": status, "prompt": self._prompt_card(prompt), "attempt": attempt, "reason": reason}

    def sync_confirmed_textbook_lexemes(self, child_id: str) -> dict[str, int]:
        self.ensure_schema()
        now = _now().isoformat()
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)
            ).fetchone()
            if not child:
                raise KeyError(child_id)
            if child["grade"] != "四年级":
                raise PetVocabularyError("PET词汇复习只适用于当前四年级孩子。")
            rows = connection.execute(
                """
                SELECT DISTINCT l.lexeme_id, l.surface, l.focus_kind, l.senses_json,
                       l.collocations_json, l.source_ref
                FROM kg_language_lexeme l
                JOIN kg_language_focus_candidate c
                  ON c.language=l.language AND c.normalized=l.normalized
                 AND c.focus_kind=l.focus_kind
                WHERE l.language='english' AND l.focus_kind IN ('word','phrase')
                  AND c.status='confirmed' AND c.proposed_json != '{}'
                ORDER BY l.normalized
                """
            ).fetchall()
            inserted = 0
            skipped_without_meaning = 0
            for row in rows:
                senses = _string_values(json.loads(row["senses_json"] or "[]"))
                if not senses:
                    skipped_without_meaning += 1
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO pet_vocabulary_card(
                      card_id, child_id, lexeme_id, surface, focus_kind, senses_json,
                      collocations_json, source_ref, status, due_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
                    """,
                    (
                        str(uuid4()), child_id, row["lexeme_id"], row["surface"], row["focus_kind"],
                        json.dumps(senses, ensure_ascii=False), row["collocations_json"],
                        row["source_ref"], now, now, now,
                    ),
                )
                inserted += cursor.rowcount
            connection.commit()
            total = connection.execute(
                "SELECT COUNT(*) FROM pet_vocabulary_card WHERE child_id=?", (child_id,)
            ).fetchone()[0]
        return {
            "available_confirmed": len(rows),
            "inserted": inserted,
            "skipped_without_meaning": skipped_without_meaning,
            "deck_size": int(total),
        }

    def summary(self, child_id: str, at: datetime | None = None) -> dict[str, Any]:
        self.ensure_schema()
        moment = (at or _now()).isoformat()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS deck_size,
                       SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) AS new_count,
                       SUM(CASE WHEN status IN ('learning','review') THEN 1 ELSE 0 END) AS learning_count,
                       SUM(CASE WHEN status!='suspended' AND due_at<=? THEN 1 ELSE 0 END) AS due_count,
                       SUM(total_attempts) AS total_attempts,
                       SUM(lapse_count) AS lapse_count
                FROM pet_vocabulary_card WHERE child_id=?
                """,
                (moment, child_id),
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def next_card(self, child_id: str, at: datetime | None = None) -> dict[str, Any] | None:
        self.ensure_schema()
        moment = (at or _now()).isoformat()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pet_vocabulary_card
                WHERE child_id=? AND status!='suspended' AND due_at<=?
                ORDER BY CASE status WHEN 'learning' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                         due_at, total_attempts, card_id LIMIT 1
                """,
                (child_id, moment),
            ).fetchone()
        return self._public_card(row) if row else None

    def present_next(self, session_id: str, at: datetime | None = None) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._active_session(connection, session_id)
            active = connection.execute(
                "SELECT prompt_session_id FROM pet_vocabulary_prompt_session WHERE session_id=? AND status='active'",
                (session_id,),
            ).fetchone()
            if active:
                prompt = self._validate_prompt(connection, self._owned_prompt(connection, session, active["prompt_session_id"]))
                if prompt["status"] == "active":
                    return self._prompt_card(prompt)
            limit_row = connection.execute(
                """
                SELECT pi.source_ref
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
                WHERE dp.session_id=? AND pi.source_type='pet_daily'
                  AND pi.source_ref LIKE ?
                  AND pi.status IN ('active','paused','pending')
                ORDER BY CASE pi.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                         pi.position
                LIMIT 1
                """,
                (session_id, f"{PLAN_SOURCE_PREFIX}%"),
            ).fetchone()
            session_limit = self._session_limit(limit_row["source_ref"] if limit_row else None)
            completed_count = connection.execute(
                """
                SELECT COUNT(*) FROM pet_vocabulary_prompt_session
                WHERE session_id=? AND status='completed'
                """,
                (session_id,),
            ).fetchone()[0]
            if int(completed_count) >= session_limit:
                connection.commit()
                return None
            now = (at or _now()).isoformat()
            candidates = connection.execute("""SELECT * FROM pet_vocabulary_card
                WHERE child_id=? AND status!='suspended' AND due_at<=?
                ORDER BY CASE status WHEN 'learning' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                    due_at,total_attempts,card_id""", (session["child_id"], now)).fetchall()
            card, source_version = None, None
            for candidate in candidates:
                source_version = self._source_version(connection, candidate)
                if source_version:
                    card = dict(candidate)
                    break
            if card is None:
                connection.commit()
                return None
            prompt_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO pet_vocabulary_prompt_session(
                  prompt_session_id,session_id,child_id,card_id,status,presented_at,
                  card_version,card_snapshot_json,source_version
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (prompt_id, session_id, session["child_id"], card["card_id"], now,
                 self._version(card), json.dumps(card, ensure_ascii=True), source_version),
            )
            result = self._prompt_card(self._owned_prompt(connection, session, prompt_id))
            connection.commit()
            return result

    def active_prompt(self, session_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = self._active_session(connection, session_id)
            except PetVocabularyError:
                return None
            row = connection.execute("SELECT prompt_session_id FROM pet_vocabulary_prompt_session WHERE session_id=? AND status='active'",
                                     (session_id,)).fetchone()
            if not row:
                return None
            prompt = self._validate_prompt(connection, self._owned_prompt(connection, session, row["prompt_session_id"]))
            connection.commit()
            return self._prompt_card(prompt) if prompt["status"] == "active" else None

    def read_prompt(self, session_id: str, *, prompt_session_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._active_session(connection, session_id)
            prompt = self._validate_prompt(connection, self._owned_prompt(connection, session, prompt_session_id))
            result = self._prompt_result(connection, prompt)
            connection.commit()
            return result

    @staticmethod
    def _check_answer(answer, client_action_id):
        if not isinstance(answer, str) or not answer.strip():
            raise PetVocabularyError("词汇回答不能为空。")
        if not isinstance(client_action_id, str) or not client_action_id.strip():
            raise PetVocabularyError("vocabulary_action_required")
        return answer.strip()

    @staticmethod
    def _check_replay(attempt, child_id, session_id, card_id, answer):
        if (attempt["child_id"], attempt["session_id"], attempt["card_id"], attempt["submitted_answer"]) != (child_id, session_id, card_id, answer):
            raise PetVocabularyError("vocabulary_action_payload_conflict")

    def _submit_prompt(self, connection, session, prompt_session_id, card_version, answer, client_action_id, at, authorized_turn_id=None):
        prompt = self._owned_prompt(connection, session, prompt_session_id)
        if not card_version or prompt["card_version"] != card_version:
            raise PetVocabularyError("vocabulary_card_version_conflict")
        replay = connection.execute("SELECT * FROM pet_vocabulary_attempt WHERE client_action_id=?", (client_action_id,)).fetchone()
        if replay:
            self._check_replay(replay, session["child_id"], session["session_id"], prompt["card_id"], answer)
            if replay["attempt_id"] != prompt["attempt_id"] or prompt["status"] != "completed":
                raise PetVocabularyError("vocabulary_prompt_receipt_conflict")
            return self._prompt_result(connection, prompt)
        if prompt["status"] == "completed":
            raise PetVocabularyError("vocabulary_prompt_already_answered")
        prompt = self._validate_prompt(connection, prompt)
        if prompt["status"] != "active":
            return self._prompt_result(connection, prompt)
        from .agent_runtime import AgentRuntimeStore
        AgentRuntimeStore.validate_local_answer_write(connection, authorized_turn_id, 'pet_vocabulary.submit',
            child_id=session['child_id'], session_id=session['session_id'],
            binding={'prompt_session_id': prompt_session_id, 'card_version': card_version}, answer=answer)
        result = self.submit(session["child_id"], prompt["card_id"], answer, client_action_id, at,
                             session_id=session["session_id"], _connection=connection)
        connection.execute("""UPDATE pet_vocabulary_prompt_session SET status='completed',attempt_id=?,completed_at=?,authorized_turn_id=?
            WHERE prompt_session_id=? AND status='active'""",
            (result["attempt_id"], (at or _now()).isoformat(), authorized_turn_id, prompt_session_id))
        return self._prompt_result(connection, self._owned_prompt(connection, session, prompt_session_id))

    def submit_prompt(self, session_id: str, *, prompt_session_id: str, card_version: str,
                      answer: str, client_action_id: str, at: datetime | None = None,
                      authorized_turn_id: str | None = None) -> dict[str, Any]:
        """Strong HTTP path: never select the current card on a submission or retry."""
        answer = self._check_answer(answer, client_action_id)
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._active_session(connection, session_id)
            result = self._submit_prompt(connection, session, prompt_session_id, card_version, answer, client_action_id, at, authorized_turn_id)
            connection.commit()
            return result

    def cancel_prompt(self, session_id: str, *, prompt_session_id: str, card_version: str) -> dict[str, Any]:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._active_session(connection, session_id)
            prompt = self._owned_prompt(connection, session, prompt_session_id)
            if not card_version or prompt["card_version"] != card_version:
                raise PetVocabularyError("vocabulary_card_version_conflict")
            if prompt["status"] == "completed":
                raise PetVocabularyError("vocabulary_prompt_already_answered")
            connection.execute("UPDATE pet_vocabulary_prompt_session SET status='cancelled',completed_at=? WHERE prompt_session_id=? AND status='active'",
                               (_now().isoformat(), prompt_session_id))
            result = self._prompt_result(connection, self._owned_prompt(connection, session, prompt_session_id))
            connection.commit()
            return result

    def submit_active(
        self, session_id: str, answer: str, client_action_id: str,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        answer = self._check_answer(answer, client_action_id)
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._active_session(connection, session_id)
            replay = connection.execute("SELECT * FROM pet_vocabulary_attempt WHERE client_action_id=?", (client_action_id,)).fetchone()
            if replay:
                self._check_replay(replay, session["child_id"], session_id, replay["card_id"], answer)
                return self._attempt_result(connection, replay)
            prompt = connection.execute(
                """
                SELECT * FROM pet_vocabulary_prompt_session
                WHERE session_id=? AND status='active'
                """,
                (session_id,),
            ).fetchone()
            if not prompt:
                raise PetVocabularyError("当前没有等待回答的词汇卡。")
            result = self._submit_prompt(connection, session, prompt["prompt_session_id"], prompt["card_version"], answer, client_action_id, at)
            connection.commit()
            if result["attempt"] is None:
                raise PetVocabularyError(result["reason"] or "vocabulary_prompt_unavailable")
            return result["attempt"]

    def cancel_active(self, session_id: str) -> bool:
        self.ensure_schema()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pet_vocabulary_prompt_session SET status='cancelled', completed_at=?
                WHERE session_id=? AND status='active'
                """,
                (_now().isoformat(), session_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def submit(
        self, child_id: str, card_id: str, answer: str, client_action_id: str,
        at: datetime | None = None, *, session_id: str | None = None, _connection=None,
    ) -> dict[str, Any]:
        if _connection is None:
            self.ensure_schema()
        answer = self._check_answer(answer, client_action_id)
        moment = at or _now()
        now = moment.isoformat()
        with (self.database.connect() if _connection is None else nullcontext(_connection)) as connection:
            if _connection is None:
                connection.execute("BEGIN IMMEDIATE")
            child = connection.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
            if not child or (session_id is not None and self._active_session(connection, session_id)["child_id"] != child_id):
                raise PetVocabularyError("vocabulary_session_ownership_conflict")
            replay = connection.execute(
                "SELECT * FROM pet_vocabulary_attempt WHERE client_action_id=?", (client_action_id,)
            ).fetchone()
            if replay:
                self._check_replay(replay, child_id, session_id, card_id, answer)
                return self._attempt_result(connection, replay)
            card = connection.execute(
                "SELECT * FROM pet_vocabulary_card WHERE card_id=? AND child_id=?",
                (card_id, child_id),
            ).fetchone()
            if not card or card["status"] == "suspended":
                raise PetVocabularyError("这张词汇卡不可用。")
            mode = self._mode(card)
            correct = self._is_correct(card, mode, answer)
            previous_due_at = card["due_at"]
            attempts = int(card["total_attempts"]) + 1
            if correct:
                successes = int(card["consecutive_successes"]) + 1
                interval_days = REVIEW_INTERVAL_DAYS[min(successes - 1, len(REVIEW_INTERVAL_DAYS) - 1)]
                next_due = moment + timedelta(days=interval_days)
                status = "review"
                lapses = int(card["lapse_count"])
            else:
                successes = 0
                interval_days = 0
                next_due = moment + timedelta(minutes=10)
                status = "learning"
                lapses = int(card["lapse_count"]) + 1
            attempt_id = str(uuid4())
            connection.execute(
                """
                UPDATE pet_vocabulary_card SET status=?, consecutive_successes=?, total_attempts=?,
                  lapse_count=?, interval_days=?, due_at=?, last_attempt_at=?, updated_at=?
                WHERE card_id=?
                """,
                (status, successes, attempts, lapses, interval_days, next_due.isoformat(), now, now, card_id),
            )
            connection.execute(
                """
                INSERT INTO pet_vocabulary_attempt(
                  attempt_id, card_id, child_id, session_id, review_mode, submitted_answer, correct,
                  previous_due_at, next_due_at, interval_days, occurred_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, card_id, child_id, session_id, mode, answer, int(correct), previous_due_at,
                    next_due.isoformat(), interval_days, now, client_action_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'child', 'pet_vocabulary_reviewed', ?, ?, ?)
                """,
                (
                    str(uuid4()), child_id,
                    json.dumps({"card_id": card_id, "mode": mode, "correct": correct, "interval_days": interval_days}),
                    now,
                ),
            )
            attempt = connection.execute(
                "SELECT * FROM pet_vocabulary_attempt WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            result = self._result_from_rows(card, attempt, correct)
            if _connection is None:
                connection.commit()
            return result

    def _attempt_result(self, connection, attempt) -> dict[str, Any]:
        original = connection.execute("SELECT card_snapshot_json FROM pet_vocabulary_prompt_session WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
        if original and original["card_snapshot_json"]:
            return self._result_from_rows(json.loads(original["card_snapshot_json"]), attempt, bool(attempt["correct"]))
        card = connection.execute(
            "SELECT * FROM pet_vocabulary_card WHERE card_id=?", (attempt["card_id"],)
        ).fetchone()
        return self._result_from_rows(card, attempt, bool(attempt["correct"]))

    def _result_from_rows(self, card, attempt, correct: bool) -> dict[str, Any]:
        senses = _string_values(json.loads(card["senses_json"] or "[]"))
        return {
            "attempt_id": attempt["attempt_id"],
            "card_id": attempt["card_id"],
            "review_mode": attempt["review_mode"],
            "correct": correct,
            "expected": card["surface"] if attempt["review_mode"] == "recall" else senses,
            "next_due_at": attempt["next_due_at"],
            "interval_days": int(attempt["interval_days"]),
            "feedback": "答对了，已经安排下一次间隔复习。" if correct else "这次还不稳定，十分钟后会再用另一种方式问一次。",
        }

    def _public_card(self, row) -> dict[str, Any]:
        mode = self._mode(row)
        senses = _string_values(json.loads(row["senses_json"] or "[]"))
        prompt = (
            f"{row['surface']} 在这里可以表示什么意思？用中文或简单英文回答。"
            if mode == "recognition"
            else f"请说出这个意思对应的英文词或词组：{senses[0]}"
        )
        return {
            "card_id": row["card_id"],
            "review_mode": mode,
            "prompt": prompt,
            "focus_kind": row["focus_kind"],
            "due_at": row["due_at"],
            "source_ref": row["source_ref"],
        }

    @staticmethod
    def _mode(card) -> str:
        return "recognition" if int(card["total_attempts"]) % 2 == 0 else "recall"

    @staticmethod
    def _session_limit(source_ref: str | None) -> int:
        if source_ref and source_ref.startswith(PLAN_SOURCE_PREFIX):
            try:
                return max(1, min(DEFAULT_SESSION_REVIEW_LIMIT, int(source_ref[len(PLAN_SOURCE_PREFIX):])))
            except ValueError:
                pass
        return DEFAULT_SESSION_REVIEW_LIMIT

    @staticmethod
    def _is_correct(card, mode: str, answer: str) -> bool:
        normalized_answer = _normalize(answer)
        if mode == "recall":
            return _normalize(card["surface"]) in normalized_answer
        senses = _string_values(json.loads(card["senses_json"] or "[]"))
        for sense in senses:
            normalized_sense = _normalize(sense)
            if normalized_sense and (normalized_sense in normalized_answer or normalized_answer in normalized_sense):
                return True
            tokens = [token for token in re.findall(r"[a-z]{3,}|[\u3400-\u9fff]{2,}", sense.lower())]
            if any(_normalize(token) in normalized_answer for token in tokens):
                return True
        return False


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value.lower())


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if isinstance(item, str) and item.strip()]
    return []
