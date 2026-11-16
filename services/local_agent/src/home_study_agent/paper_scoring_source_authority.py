"""Deterministic promotion of explicit local objective answer-key evidence."""
from __future__ import annotations

import json
import re
from typing import Any

from .database import Database
from .paper_scoring import PaperScoringService
from .paper_scoring_source_match import PaperScoringSourceMatchService
from .paper_scoring_supply import record_attempt
from .repository import utc_now


QUALIFIER_VERSION = "paper_source_qualifier_v3"
_ANSWER_PATTERN = re.compile(
    r"(?:参考)?答案\s*[:：]\s*("
    r"[A-D](?:\s*[,，、和]\s*[A-D]){0,3}|"
    r"[-+]?\d+(?:\.\d+)?(?:/\d+)?|"
    r"正确|错误|对|错|是|否)\b",
    re.IGNORECASE,
)


def ensure_schema(connection: Any) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_scoring_source_qualification (
          candidate_id TEXT NOT NULL
            REFERENCES paper_scoring_source_candidate(candidate_id) ON DELETE CASCADE,
          qualifier_version TEXT NOT NULL,
          obligation_id TEXT NOT NULL
            REFERENCES paper_scoring_supply_obligation(obligation_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN (
            'qualified','not_objective','source_insufficient','source_stale','calibration_pending'
          )),
          reason_code TEXT NOT NULL,
          authority_ref TEXT,
          expected_answers_sha256 TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY(candidate_id, qualifier_version)
        );
        """
    )


def _compact(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _split_answers(value: str) -> list[str]:
    compact = value.strip().upper()
    if re.fullmatch(r"[A-D](?:\s*[,，、和]\s*[A-D]){1,3}", compact):
        return ["".join(sorted(set(re.findall(r"[A-D]", compact))))]
    return [compact]


class PaperScoringSourceAuthorityService:
    """Qualifies only narrow, explicit, version-bound objective answer pages."""

    def __init__(self, database: Database):
        self.database = database
        self.scoring = PaperScoringService(database)
        self.sources = PaperScoringSourceMatchService(database)

    def ensure_schema(self) -> None:
        self.sources.ensure_schema()
        with self.database.connect() as connection:
            ensure_schema(connection)
            connection.commit()

    @staticmethod
    def _qualification(question: str, draft: dict[str, Any], source: dict[str, Any]) -> tuple[str, str, list[str]]:
        if draft.get("answer_mode") != "objective":
            return "calibration_pending", "SUBJECTIVE_OR_UNCERTAIN_REQUIRES_CALIBRATION", []
        if source.get("ocr_confidence") is None or float(source["ocr_confidence"]) < 0.88:
            return "source_insufficient", "SOURCE_OCR_CONFIDENCE_LOW", []
        text = source["text"]
        question_key = _compact(question)
        if len(question_key) < 8 or question_key not in _compact(text):
            return "source_insufficient", "EXACT_QUESTION_NOT_BOUND_TO_SOURCE", []
        matches = list(_ANSWER_PATTERN.finditer(text))
        if len(matches) != 1 or len(text) > 600:
            return "source_insufficient", "ANSWER_KEY_BLOCK_AMBIGUOUS", []
        answers = _split_answers(matches[0].group(1))
        if not answers or any(not 1 <= len(answer) <= 32 for answer in answers):
            return "source_insufficient", "EXACT_ANSWER_TOKEN_INVALID", []
        return "qualified", "EXPLICIT_VERSIONED_OBJECTIVE_ANSWER", answers

    def process_next(self) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT candidate.*,obligation.request_id,request.question_text,
                          event.evidence_json AS draft_json
                   FROM paper_scoring_source_candidate AS candidate
                   JOIN paper_scoring_supply_obligation AS obligation USING(obligation_id)
                   JOIN paper_scoring_request AS request USING(request_id)
                   JOIN paper_scoring_supply_event AS event ON event.event_id=(
                     SELECT draft.event_id FROM paper_scoring_supply_event AS draft
                     WHERE draft.obligation_id=candidate.obligation_id
                       AND draft.status='rubric_candidate'
                     ORDER BY draft.rowid DESC LIMIT 1
                   )
                   LEFT JOIN paper_scoring_source_qualification AS qualification
                     ON qualification.candidate_id=candidate.candidate_id
                    AND qualification.qualifier_version=?
                   LEFT JOIN paper_scoring_effective_decision AS decision
                     ON decision.request_id=obligation.request_id
                    AND decision.effective_status='qualified'
                   WHERE qualification.candidate_id IS NULL AND decision.request_id IS NULL
                   ORDER BY candidate.created_at,candidate.candidate_id LIMIT 1""",
                (QUALIFIER_VERSION,),
            ).fetchone()
        if not row:
            return None
        try:
            draft = json.loads(row["draft_json"])
        except (TypeError, json.JSONDecodeError):
            draft = {}
        valid = {
            item["candidate_id"]: item
            for item in self.sources.valid_candidates(row["obligation_id"])
        }
        source = valid.get(row["candidate_id"])
        if not source:
            status, reason, answers = "source_stale", "SOURCE_VERSION_CHANGED", []
        else:
            status, reason, answers = self._qualification(row["question_text"], draft, source)
        authority_ref = None
        expected_hash = None
        subjective_spec = None
        if status == 'calibration_pending' and draft.get('answer_mode') == 'subjective' and source:
            from .paper_subjective_rubric import PaperSubjectiveRubricService
            try:
                subjective_spec = PaperSubjectiveRubricService(self.database).freeze(row['child_id'], row['request_id'], source)
                reason = 'FROZEN_SOURCE_CRITERIA_REQUIRES_INDEPENDENT_CALIBRATION'
            except ValueError as error:
                if str(error).startswith('SUBJECTIVE_'):
                    reason = str(error)
                else:
                    raise
        if status == "qualified":
            authority_ref = (
                f"local-answer-source:{row['candidate_id']}:{row['source_version']}"
            )
            import hashlib
            expected_hash = hashlib.sha256(
                json.dumps(answers, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            self.scoring.register_exact_authority(
                child_id=row["child_id"], session_id=None,
                question_text=row["question_text"], expected_answers=answers,
                authority_type="published_answer_key", authority_ref=authority_ref,
                rubric_version=row["source_version"],
                client_action_id=f"paper-source-authority:{row['candidate_id']}:{QUALIFIER_VERSION}",
            )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO paper_scoring_source_qualification(
                     candidate_id,qualifier_version,obligation_id,child_id,status,reason_code,
                     authority_ref,expected_answers_sha256,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (row["candidate_id"], QUALIFIER_VERSION, row["obligation_id"], row["child_id"],
                 status, reason, authority_ref, expected_hash, utc_now()),
            )
            if status == "calibration_pending":
                record_attempt(
                    connection, request_id=row["request_id"], child_id=row["child_id"],
                    status="calibration_pending",
                    reason="local source candidate requires calibrated subjective scoring",
                    evidence={
                        "version": QUALIFIER_VERSION,
                        "source_candidate_id": row["candidate_id"],
                        "source_version": row["source_version"],
                        "contains_question_or_answer_text": False,
                        "authority_granted": False,
                        "frozen_rubric_artifact_id": subjective_spec['artifact_id'] if subjective_spec else None,
                        "frozen_rubric_sha256": subjective_spec['spec_sha256'] if subjective_spec else None,
                        "criterion_count": len(subjective_spec['criteria']) if subjective_spec else 0,
                        "original_response_version": subjective_spec['original_response']['source_version'] if subjective_spec else None,
                        "original_written_answer_available": subjective_spec['original_response']['original_written_answer_available'] if subjective_spec else False,
                        "independent_calibration_required": True,
                    },
                    client_action_id=f"paper-source-calibration:{row['candidate_id']}:{QUALIFIER_VERSION}",
                )
            connection.commit()
        decision = None
        if status == "qualified":
            decision = self.scoring.resolve(
                row["request_id"], row["child_id"],
                f"paper-source-decision:{row['candidate_id']}:{QUALIFIER_VERSION}",
            )
        return {
            "candidate_id": row["candidate_id"],
            "obligation_id": row["obligation_id"],
            "status": status,
            "reason_code": reason,
            "decision_id": decision["decision_id"] if decision else None,
            "correctness": decision["correctness"] if decision else None,
            "local_only": True,
            "cloud_transmission_allowed": False,
        }

    def process_batch(self, limit: int = 8) -> list[dict[str, Any]]:
        results = []
        for _ in range(max(1, min(int(limit), 30))):
            result = self.process_next()
            if not result:
                break
            results.append(result)
        return results
