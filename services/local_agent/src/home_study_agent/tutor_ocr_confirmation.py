"""Durable, local-only child confirmation for OCR-backed tutor requests.

An OCR artifact is evidence to review, not a prompt the model may trust.  This
service stores the exact, child-confirmed text locally for a short period and
binds it to one child, study session and document-OCR artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from .database import Database


CONFIRMATION_TTL = timedelta(hours=24)


def normalize_confirmation_text(value: str) -> str:
    return " ".join(value.split())


def preserve_confirmation_text(value: str) -> str:
    """Trim transport padding while retaining OCR line and layout evidence."""
    return value.strip()


class OcrTutorConfirmationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OcrTutorConfirmation:
    confirmation_id: str
    artifact_id: str
    child_id: str
    session_id: str
    confirmed_text: str
    confirmed_text_hash: str
    confirmation_kind: str
    confirmed_at: str
    expires_at: str
    client_action_id: str
    formula_review_json: str | None = None

    def public(self) -> dict:
        return {
            "confirmation_id": self.confirmation_id,
            "artifact_id": self.artifact_id,
            "confirmation_kind": self.confirmation_kind,
            "confirmed_text": self.confirmed_text,
            "confirmed_text_hash": self.confirmed_text_hash,
            "confirmed_at": self.confirmed_at,
            "expires_at": self.expires_at,
        }


class OcrTutorConfirmationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_schema(self) -> None:
        # Kept here rather than in the shared database module so this isolated
        # feature can be merged without taking ownership of its active migration.
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tutor_ocr_confirmation (
                  confirmation_id TEXT PRIMARY KEY,
                  artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id),
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id),
                  confirmed_text TEXT NOT NULL,
                  confirmed_text_hash TEXT NOT NULL,
                  confirmation_kind TEXT NOT NULL CHECK(confirmation_kind IN ('ocr_verified', 'manual_correction')),
                  confirmed_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE,
                  revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tutor_ocr_confirmation_lookup
                  ON tutor_ocr_confirmation(confirmation_id, session_id, child_id, expires_at);
                """
            )
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(tutor_ocr_confirmation)')}
            if 'formula_review_json' not in columns:
                connection.execute('ALTER TABLE tutor_ocr_confirmation ADD COLUMN formula_review_json TEXT')
            connection.commit()

    def confirm(
        self,
        session_id: str,
        artifact_id: str,
        confirmed_text: str,
        confirmation_kind: str,
        client_action_id: str,
        *, expected_formula_binding: dict | None = None,
    ) -> dict:
        text = preserve_confirmation_text(confirmed_text)
        normalized_text = normalize_confirmation_text(text)
        if len(normalized_text) < 2:
            raise OcrTutorConfirmationError("OCR_CONFIRMATION_TEXT_REQUIRED", "请先逐字核对并补全题干，再确认。")
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            existing = connection.execute(
                """SELECT confirmation_id, artifact_id, child_id, session_id, confirmed_text, confirmed_text_hash,
                          confirmation_kind, confirmed_at, expires_at, client_action_id, formula_review_json
                   FROM tutor_ocr_confirmation WHERE client_action_id = ?""",
                (client_action_id,),
            ).fetchone()
            if existing:
                existing_record = OcrTutorConfirmation(**dict(existing))
                if (
                    existing_record.session_id != session_id
                    or existing_record.artifact_id != artifact_id
                    or existing_record.confirmed_text != text
                    or existing_record.confirmation_kind != confirmation_kind
                    or (expected_formula_binding is not None and
                        json.loads(existing_record.formula_review_json or 'null') != expected_formula_binding)
                ):
                    raise OcrTutorConfirmationError(
                        "OCR_CONFIRMATION_ACTION_CONFLICT",
                        "这次题干确认已用于另一份内容，请重新核对后再提交。",
                    )
                return existing_record.public()
            artifact = connection.execute(
                """SELECT artifact_id, child_id, session_id, artifact_type, extracted_text, details_json
                   FROM capture_artifact WHERE artifact_id = ?""",
                (artifact_id,),
            ).fetchone()
            if not artifact:
                raise OcrTutorConfirmationError("OCR_ARTIFACT_NOT_FOUND", "找不到这次拍题记录，请重新拍题。")
            from .mistake_source_revision import artifact_binding
            try:
                revision_binding = artifact_binding(connection, artifact_id, artifact['child_id'], text, session_id)
            except ValueError as error:
                raise OcrTutorConfirmationError('OCR_FORMULA_REVIEW_CHANGED', '更正题干的版本已变化，请重新核对。') from error
            if artifact["session_id"] != session_id and (not revision_binding or revision_binding != expected_formula_binding):
                raise OcrTutorConfirmationError("OCR_ARTIFACT_SESSION_MISMATCH", "这次拍题不属于当前学习会话，请重新拍题。")
            session = connection.execute("SELECT child_id,status,state FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
            if not session or session["child_id"] != artifact["child_id"]:
                raise OcrTutorConfirmationError("OCR_ARTIFACT_CHILD_MISMATCH", "这次拍题不属于当前孩子，请重新拍题。")
            if expected_formula_binding is not None and (session['status'], session['state']) != ('active', 'study'):
                raise OcrTutorConfirmationError('OCR_CONFIRMATION_SESSION_INACTIVE', '学习会话已变化，请先恢复并核对原题。')
            if artifact["artifact_type"] != "document_ocr":
                raise OcrTutorConfirmationError("OCR_ARTIFACT_TYPE_DENIED", "只能核对当前高拍仪识别的题目，请重新拍题。")
            if not (artifact["extracted_text"] or "").strip():
                raise OcrTutorConfirmationError("OCR_TEXT_UNREADABLE", "没有识别到可核对的文字，请重拍。")
            verified_texts = {normalize_confirmation_text(artifact["extracted_text"])}
            try:
                details = json.loads(artifact["details_json"] or "{}")
                candidates = details.get("local_pipeline", {}).get("wrong_question_candidates", [])
                verified_texts.update(
                    normalize_confirmation_text(str(candidate.get("text", "")))
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and candidate.get("evidence") == "red_correction_mark_near_question"
                    and candidate.get("requires_child_confirmation") is True
                    and float(candidate.get("confidence", 0.0)) >= .72
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            verified_texts.discard("")
            if confirmation_kind == "ocr_verified" and normalized_text not in verified_texts:
                raise OcrTutorConfirmationError(
                    "OCR_VERIFIED_TEXT_MISMATCH",
                    "核对无误时不能改写识别文字；如有改动，请选择逐字人工修正。",
                )
            confirmed_at = now.isoformat()
            expires_at = (now + CONFIRMATION_TTL).isoformat()
            from .paper_formula_review import tutor_binding
            try:
                formula_binding = tutor_binding(connection, artifact, text, session_id)
                if expected_formula_binding is not None and formula_binding != expected_formula_binding:
                    raise ValueError('formula review changed before confirmation')
            except (ValueError, TypeError, KeyError) as error:
                raise OcrTutorConfirmationError('OCR_FORMULA_REVIEW_CHANGED', '公式题干的核对记录已变化，请重新核对。') from error
            record = OcrTutorConfirmation(
                confirmation_id=str(uuid4()), artifact_id=artifact_id, child_id=artifact["child_id"],
                session_id=session_id, confirmed_text=text,
                confirmed_text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                confirmation_kind=confirmation_kind, confirmed_at=confirmed_at, expires_at=expires_at,
                client_action_id=client_action_id,
                formula_review_json=json.dumps(formula_binding, sort_keys=True) if formula_binding else None,
            )
            connection.execute(
                """INSERT INTO tutor_ocr_confirmation(
                    confirmation_id, artifact_id, child_id, session_id, confirmed_text, confirmed_text_hash,
                    confirmation_kind, confirmed_at, expires_at, client_action_id, formula_review_json, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (record.confirmation_id, record.artifact_id, record.child_id, record.session_id,
                 record.confirmed_text, record.confirmed_text_hash, record.confirmation_kind,
                 record.confirmed_at, record.expires_at, record.client_action_id, record.formula_review_json),
            )
            connection.commit()
        return record.public()

    def resolve_for_tutor(self, session_id: str, confirmation_id: str) -> OcrTutorConfirmation:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT confirmation_id, artifact_id, child_id, session_id, confirmed_text, confirmed_text_hash,
                          confirmation_kind, confirmed_at, expires_at, client_action_id, formula_review_json
                   FROM tutor_ocr_confirmation WHERE confirmation_id = ? AND revoked_at IS NULL""",
                (confirmation_id,),
            ).fetchone()
            if not row:
                raise OcrTutorConfirmationError("OCR_CONFIRMATION_NOT_FOUND", "请先核对题干，再让我讲解。")
            confirmation = OcrTutorConfirmation(**dict(row))
            if confirmation.session_id != session_id:
                raise OcrTutorConfirmationError("OCR_CONFIRMATION_SESSION_MISMATCH", "这份题干确认不属于当前学习会话，请重新核对。")
            if datetime.fromisoformat(confirmation.expires_at) <= datetime.now(timezone.utc):
                raise OcrTutorConfirmationError("OCR_CONFIRMATION_EXPIRED", "这份题干确认已超过24小时，请重新核对题干。")
            artifact = connection.execute(
                """SELECT * FROM capture_artifact
                   WHERE artifact_id = ?""", (confirmation.artifact_id,)
            ).fetchone()
            from .mistake_source_revision import artifact_binding
            try:
                revision_binding = artifact_binding(connection, artifact['artifact_id'], confirmation.child_id,
                    confirmation.confirmed_text, session_id) if artifact else None
            except ValueError as error:
                raise OcrTutorConfirmationError('OCR_FORMULA_REVIEW_CHANGED', '更正题干的版本已变化，请重新核对。') from error
            expected_revision = json.loads(confirmation.formula_review_json) if confirmation.formula_review_json else None
            if (not artifact or artifact["child_id"] != confirmation.child_id
                    or (artifact["session_id"] != session_id and (not revision_binding or revision_binding != expected_revision))):
                raise OcrTutorConfirmationError("OCR_CONFIRMATION_ARTIFACT_INVALID", "题干确认对应的拍题记录已不可用，请重新拍题。")
            session = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
            if not session or session["child_id"] != confirmation.child_id:
                raise OcrTutorConfirmationError("OCR_CONFIRMATION_CHILD_MISMATCH", "这份题干确认不属于当前孩子，请重新核对。")
            if artifact["artifact_type"] != "document_ocr":
                raise OcrTutorConfirmationError("OCR_ARTIFACT_TYPE_DENIED", "只能使用当前高拍仪识别的题目，请重新拍题。")
            from .paper_formula_review import tutor_binding
            try:
                current = tutor_binding(connection, artifact, confirmation.confirmed_text, session_id)
                expected = json.loads(confirmation.formula_review_json) if confirmation.formula_review_json else None
                if current != expected:
                    raise ValueError('formula review version changed')
            except (ValueError, TypeError, KeyError) as error:
                raise OcrTutorConfirmationError('OCR_FORMULA_REVIEW_CHANGED', '公式题干的核对记录已变化，请重新核对。') from error
        return confirmation
