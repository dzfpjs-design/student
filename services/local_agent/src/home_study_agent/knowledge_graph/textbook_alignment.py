"""Local candidate alignment between atomic drafts and scanned textbook pages.

The matcher is deliberately conservative: it only compares identical grade and
subject scopes, retains a continuous quote from local OCR, and never promotes a
candidate into formal draft evidence without a later model or human review.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4

from ..database import Database
from ..repository import utc_now
from .schema import ensure_schema


_NORMALIZE_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")
_ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_GENERIC_TERMS = {"学习", "理解", "掌握", "能够", "独立", "计算", "判断", "说明", "识别"}
_ENGLISH_STOPWORDS = {"a", "an", "the", "to", "of", "and", "or", "in", "on"}


@dataclass(frozen=True)
class _TextbookSegment:
    resource_id: str
    segment_id: str
    grade: str
    subject: str
    semester: str
    text: str


class TextbookAtomicAlignmentService:
    """Generate reviewable textbook evidence candidates without cloud access."""

    ALGORITHM_VERSION = "textbook-lexical-v2"

    def __init__(self, database: Database):
        self.database = database

    def generate(
        self,
        *,
        job_id: str,
        textbook_corpus_id: str,
        minimum_score: float = .34,
        max_candidates_per_scope: int = 3,
    ) -> dict:
        if not 0 <= minimum_score <= 1:
            raise ValueError("minimum_score must be between 0 and 1")
        if not 1 <= max_candidates_per_scope <= 10:
            raise ValueError("max_candidates_per_scope must be between 1 and 10")

        now = utc_now()
        with self.database.connect() as connection:
            ensure_schema(connection, now)
            connection.commit()
            if not connection.execute(
                "SELECT 1 FROM kg_atomic_extraction_job WHERE job_id=?", (job_id,)
            ).fetchone():
                raise ValueError("unknown atomic extraction job")
            if not connection.execute(
                "SELECT 1 FROM kg_corpus WHERE corpus_id=?", (textbook_corpus_id,)
            ).fetchone():
                raise ValueError("unknown textbook corpus")
            drafts = self._draft_scopes(connection, job_id)
            textbook_segments = self._textbook_segments(connection, textbook_corpus_id)
            alignment_rows = []
            by_scope: dict[tuple[str, str], list[_TextbookSegment]] = defaultdict(list)
            for segment in textbook_segments:
                by_scope[(self._scope(segment.grade), self._scope(segment.subject))].append(segment)

            for draft in drafts:
                component, tags = self._query(draft)
                candidates: list[tuple[float, float, _TextbookSegment, str]] = []
                for segment in by_scope.get((self._scope(draft["source_grade"]), self._scope(draft["source_subject"])), []):
                    quote, lexical_score = self._best_quote(component, tags, segment.text)
                    if quote is None or lexical_score < minimum_score:
                        continue
                    scope_score = 1.0 if self._scope(draft["source_semester"]) == self._scope(segment.semester) else .85
                    candidates.append((lexical_score, scope_score, segment, quote))
                candidates.sort(key=lambda item: (item[0], item[1], item[2].resource_id), reverse=True)
                for lexical_score, scope_score, segment, quote in candidates[:max_candidates_per_scope]:
                    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
                    alignment_rows.append(
                        (str(uuid4()), job_id, draft["draft_id"], segment.resource_id, segment.segment_id,
                         quote, quote_hash, lexical_score, scope_score, self.ALGORITHM_VERSION, now, now)
                    )
            # Scoring thousands of local pages is read-only and can take time.
            # Acquire the SQLite writer only for the short replacement commit so
            # concurrent atomic extraction checkpoints are not starved.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """DELETE FROM kg_atomic_textbook_alignment
                   WHERE source_job_id=? AND review_status='unreviewed'
                     AND textbook_resource_id IN (
                       SELECT resource_id FROM kg_resource WHERE corpus_id=?
                     )""",
                (job_id, textbook_corpus_id),
            )
            connection.executemany(
                """INSERT INTO kg_atomic_textbook_alignment(
                   alignment_id,source_job_id,draft_id,textbook_resource_id,textbook_segment_id,
                   exact_quote,quote_sha256,lexical_score,scope_score,match_method,review_status,
                   evidence_role,algorithm_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,
                   'local_lexical','unreviewed','mention',?,?,?) ON CONFLICT(source_job_id,draft_id,textbook_resource_id,
                   textbook_segment_id,algorithm_version) DO UPDATE SET exact_quote=excluded.exact_quote,
                   quote_sha256=excluded.quote_sha256,lexical_score=excluded.lexical_score,
                   scope_score=excluded.scope_score,updated_at=excluded.updated_at""",
                alignment_rows,
            )
            connection.commit()
        return self.report(job_id=job_id, textbook_corpus_id=textbook_corpus_id, generated=len(alignment_rows))

    def report(self, *, job_id: str, textbook_corpus_id: str, generated: int | None = None) -> dict:
        with self.database.connect() as connection:
            ensure_schema(connection, utc_now())
            row = connection.execute(
                """SELECT
                   (SELECT COUNT(DISTINCT db.draft_id) FROM kg_atomic_draft_batch db
                    JOIN kg_atomic_draft_evidence de ON de.draft_id=db.draft_id WHERE db.job_id=?) eligible_drafts,
                   COUNT(DISTINCT a.draft_id) aligned_drafts,COUNT(*) candidate_alignments,
                   COUNT(DISTINCT a.textbook_resource_id) matched_textbook_pages
                   FROM kg_atomic_textbook_alignment a JOIN kg_resource r
                   ON r.resource_id=a.textbook_resource_id
                   WHERE a.source_job_id=? AND r.corpus_id=? AND a.review_status!='rejected'""",
                (job_id, job_id, textbook_corpus_id),
            ).fetchone()
            textbook_pages = connection.execute(
                "SELECT COUNT(*) FROM kg_resource WHERE corpus_id=?", (textbook_corpus_id,)
            ).fetchone()[0]
            review = {
                item["review_status"]: item["count"]
                for item in connection.execute(
                    """SELECT a.review_status,COUNT(*) count FROM kg_atomic_textbook_alignment a
                       JOIN kg_resource r ON r.resource_id=a.textbook_resource_id
                       WHERE a.source_job_id=? AND r.corpus_id=? GROUP BY a.review_status""",
                    (job_id, textbook_corpus_id),
                )
            }
        result = dict(row)
        result.update({
            "textbook_pages": textbook_pages,
            "unmatched_textbook_pages": textbook_pages - result["matched_textbook_pages"],
            "review_status": review,
            "algorithm_version": self.ALGORITHM_VERSION,
            "formal_evidence_promoted": 0,
        })
        if generated is not None:
            result["generated_this_run"] = generated
        return result

    @staticmethod
    def _draft_scopes(connection, job_id: str) -> list[dict]:
        rows = connection.execute(
            """SELECT DISTINCT d.draft_id,d.title,d.action_verb,d.knowledge_component,d.concept_tags_json,
               r.grade_scope source_grade,r.subject_scope source_subject,r.semester_scope source_semester
               FROM kg_atomic_draft_batch db JOIN kg_atomic_definition_draft d ON d.draft_id=db.draft_id
               JOIN kg_atomic_draft_evidence de ON de.draft_id=d.draft_id
               JOIN kg_resource r ON r.resource_id=de.resource_id
               WHERE db.job_id=? AND d.review_status!='rejected'
               ORDER BY d.draft_id,r.grade_scope,r.subject_scope,r.semester_scope""",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _textbook_segments(connection, corpus_id: str) -> list[_TextbookSegment]:
        rows = connection.execute(
            """SELECT DISTINCT r.resource_id,s.segment_id,r.grade_scope,r.subject_scope,r.semester_scope,s.text
               FROM kg_resource r JOIN kg_segment_occurrence o ON o.resource_id=r.resource_id
               JOIN kg_segment s ON s.segment_id=o.segment_id
               WHERE r.corpus_id=? AND r.review_status!='rejected' AND s.review_status!='rejected'
               ORDER BY r.resource_id,s.ordinal""",
            (corpus_id,),
        ).fetchall()
        return [
            _TextbookSegment(row["resource_id"], row["segment_id"], row["grade_scope"] or "",
                             row["subject_scope"] or "", row["semester_scope"] or "", row["text"])
            for row in rows
        ]

    @classmethod
    def _query(cls, draft: dict) -> tuple[str, list[str]]:
        try:
            raw_tags = json.loads(draft["concept_tags_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_tags = []
        tags = [str(tag).strip() for tag in raw_tags if len(cls._normalize(str(tag))) >= 2]
        return draft["knowledge_component"] or draft["title"] or "", tags

    @classmethod
    def _best_quote(cls, component: str, tags: list[str], text: str) -> tuple[str | None, float]:
        component_normalized = cls._normalize(component)
        component_grams = cls._ngrams(component_normalized)
        if not component_grams:
            return None, 0.0
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            lines = [text.strip()] if text.strip() else []
        windows: list[str] = []
        for index in range(len(lines)):
            for width in (1, 2, 3):
                quote = "\n".join(lines[index:index + width]).strip()
                if quote and len(quote) <= 220:
                    windows.append(quote)
        best_quote = None
        best_score = 0.0
        normalized_tags = [cls._normalize(tag) for tag in tags]
        for quote in windows:
            candidate = cls._normalize(quote)
            grams = cls._ngrams(candidate)
            if not grams:
                continue
            overlap = len(component_grams & grams)
            coverage = overlap / len(component_grams)
            precision = overlap / len(grams)
            tag_score = (sum(tag in candidate for tag in normalized_tags) / len(normalized_tags)) if normalized_tags else 0.0
            if not cls._passes_semantic_gate(
                component, component_normalized, component_grams, normalized_tags, quote, candidate, grams
            ):
                continue
            exact_bonus = .12 if component_normalized in candidate else 0.0
            score = min(1.0, .72 * coverage + .12 * math.sqrt(precision) + .16 * tag_score + exact_bonus)
            if score > best_score:
                best_quote, best_score = quote, score
        return best_quote, round(best_score, 6)

    @classmethod
    def _passes_semantic_gate(
        cls,
        raw_component: str,
        component: str,
        component_grams: set[str],
        tags: list[str],
        raw_candidate: str,
        candidate: str,
        candidate_grams: set[str],
    ) -> bool:
        overlap = len(component_grams & candidate_grams)
        contains_chinese = bool(re.search(r"[\u4e00-\u9fff]", component))
        required_coverage = .55 if contains_chinese and not _ENGLISH_TOKEN_RE.search(raw_component) else .22
        if overlap < max(2, math.ceil(len(component_grams) * required_coverage)):
            return False
        english_tokens = {
            token.lower().replace("'", "") for token in _ENGLISH_TOKEN_RE.findall(raw_component)
            if token.lower().replace("'", "") not in _ENGLISH_STOPWORDS
        }
        parenthetical_tokens: set[str] = set()
        for group in re.findall(r"[\(（]([^\)）]+)[\)）]", raw_component):
            parenthetical_tokens.update(
                token.lower().replace("'", "") for token in _ENGLISH_TOKEN_RE.findall(group)
                if token.lower().replace("'", "") not in _ENGLISH_STOPWORDS
            )
        if len(parenthetical_tokens) >= 2:
            english_tokens = parenthetical_tokens
        if english_tokens:
            candidate_tokens = {
                token.lower().replace("'", "") for token in _ENGLISH_TOKEN_RE.findall(raw_candidate)
            }
            ratio = 1.0 if len(english_tokens) <= 3 else .7
            if len(english_tokens & candidate_tokens) < math.ceil(len(english_tokens) * ratio):
                return False
        # A page mentioning only the entity is not evidence for the atom's
        # tested property. Example: "平行四边形" alone cannot support
        # "平行四边形的面积（底乘高）".
        for tag in tags:
            if len(tag) < 3 or tag not in component or tag not in candidate:
                continue
            remainder = component.replace(tag, "")
            remainder = remainder.strip("的及和与中内")
            remainder_grams = cls._ngrams(remainder)
            if remainder_grams and not (remainder_grams & candidate_grams):
                return False
        return True

    @staticmethod
    def _normalize(value: str) -> str:
        normalized = _NORMALIZE_RE.sub("", (value or "").lower())
        for term in _GENERIC_TERMS:
            normalized = normalized.replace(term, "")
        return normalized

    @staticmethod
    def _ngrams(value: str) -> set[str]:
        if len(value) < 2:
            return {value} if value else set()
        return {value[index:index + 2] for index in range(len(value) - 1)}

    @staticmethod
    def _scope(value: str | None) -> str:
        normalized = _NORMALIZE_RE.sub("", (value or "").lower())
        normalized = normalized.removeprefix("小学").removeprefix("初中")
        grade_aliases = {"七年级": "初一", "八年级": "初二", "九年级": "初三"}
        return grade_aliases.get(normalized, normalized)
