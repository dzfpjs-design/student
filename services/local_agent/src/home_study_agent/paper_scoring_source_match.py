"""Local-only retrieval of source evidence for unresolved paper scoring.

The matcher never treats a search hit as an answer key. It stores immutable,
version-bound candidates for a later extraction and calibration stage.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from .database import Database
from .paper_scoring_supply import ensure_schema as ensure_supply_schema, record_attempt
from .repository import utc_now


SOURCE_MATCH_VERSION = "paper_scoring_source_match_v2"


def ensure_schema(connection: Any) -> None:
    ensure_supply_schema(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_scoring_source_lookup (
          obligation_id TEXT PRIMARY KEY
            REFERENCES paper_scoring_supply_obligation(obligation_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK(status IN ('pending','processing','candidate','waiting')),
          owner_token TEXT,
          catalog_fingerprint TEXT,
          candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
          error_code TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_scoring_source_lookup_status
          ON paper_scoring_source_lookup(status, updated_at);

        CREATE TABLE IF NOT EXISTS paper_scoring_source_candidate (
          candidate_id TEXT PRIMARY KEY,
          obligation_id TEXT NOT NULL
            REFERENCES paper_scoring_supply_obligation(obligation_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          segment_id TEXT NOT NULL,
          resource_id TEXT NOT NULL,
          document_kind TEXT NOT NULL,
          source_ref_sha256 TEXT NOT NULL,
          source_version TEXT NOT NULL,
          segment_text_sha256 TEXT NOT NULL,
          match_basis_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE,
          UNIQUE(obligation_id, segment_id, source_version)
        );
        CREATE INDEX IF NOT EXISTS idx_paper_scoring_source_candidate_obligation
          ON paper_scoring_source_candidate(obligation_id, created_at);
        CREATE TRIGGER IF NOT EXISTS paper_scoring_source_candidate_no_update
        BEFORE UPDATE ON paper_scoring_source_candidate
        BEGIN SELECT RAISE(ABORT, 'immutable paper scoring source candidate'); END;
        CREATE TABLE IF NOT EXISTS paper_scoring_source_catalog_revision (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          revision INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO paper_scoring_source_catalog_revision(singleton,revision)
          VALUES(1,0);
        """
    )
    for table in ("kg_resource", "kg_segment", "kg_document_page"):
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        for event in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS paper_scoring_catalog_{table}_{event.lower()}
                    AFTER {event} ON {table}
                    BEGIN
                      UPDATE paper_scoring_source_catalog_revision
                      SET revision=revision+1 WHERE singleton=1;
                    END"""
            )


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_version(row: Any) -> str:
    return _digest({
        "resource_id": row["resource_id"],
        "transcript_sha256": row["transcript_sha256"],
        "version_label": row["version_label"],
        "segment_text_sha256": row["text_sha256"],
        "page_updated_at": row["page_updated_at"],
        "ocr_confidence": row["ocr_confidence"],
    })


def _source_version_matches(row: Any, recorded_version: str) -> bool:
    if _source_version(row) == recorded_version:
        return True
    legacy = _digest({
        "resource_id": row["resource_id"],
        "transcript_sha256": row["transcript_sha256"],
        "version_label": row["version_label"],
        "segment_text_sha256": row["text_sha256"],
    })
    return (recorded_version == legacy
            and row["page_updated_at"] <= row["candidate_created_at"])


def _catalog_fingerprint(connection: Any) -> str:
    revision = connection.execute(
        "SELECT revision FROM paper_scoring_source_catalog_revision WHERE singleton=1"
    ).fetchone()[0]
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_segment'"
    ).fetchone():
        return _digest({"version": SOURCE_MATCH_VERSION, "revision": revision,
                        "resources": 0, "segments": 0})
    row = connection.execute(
        """SELECT COUNT(DISTINCT resource.resource_id) AS resources,
                  COUNT(segment.segment_id) AS segments,
                  MAX(resource.updated_at) AS latest
           FROM kg_segment AS segment
           JOIN kg_resource AS resource USING(resource_id)
           JOIN kg_document_page AS page USING(resource_id)
           WHERE page.document_kind IN (
             'textbook','school_official_practice','purchased_practice_book'
           )"""
    ).fetchone()
    return _digest({
        "version": SOURCE_MATCH_VERSION,
        "revision": revision,
        "resources": int(row["resources"] or 0),
        "segments": int(row["segments"] or 0),
        "latest": row["latest"],
    })


def candidate_is_current(connection: Any, candidate_id: str, source_version: str) -> bool:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_scoring_source_candidate'"
    ).fetchone():
        return False
    row = connection.execute(
        """SELECT candidate.source_version,candidate.segment_text_sha256,
                  candidate.document_kind,page.document_kind AS current_document_kind,
                  page.ocr_confidence,page.updated_at AS page_updated_at,
                  candidate.created_at AS candidate_created_at,
                  segment.text,segment.text_sha256,resource.transcript_sha256,
                  resource.version_label,candidate.resource_id
           FROM paper_scoring_source_candidate AS candidate
           JOIN kg_segment AS segment ON segment.segment_id=candidate.segment_id
             AND segment.resource_id=candidate.resource_id
           JOIN kg_resource AS resource ON resource.resource_id=candidate.resource_id
           JOIN kg_document_page AS page ON page.resource_id=candidate.resource_id
           WHERE candidate.candidate_id=?""",
        (candidate_id,),
    ).fetchone()
    if not row or row["source_version"] != source_version:
        return False
    return (_source_version_matches(row, source_version)
            and row["document_kind"] == row["current_document_kind"]
            and row["current_document_kind"] in (
                "textbook", "school_official_practice", "purchased_practice_book")
            and row["ocr_confidence"] is not None
            and float(row["ocr_confidence"]) >= .88
            and row["text_sha256"] == row["segment_text_sha256"]
            and hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
            == row["segment_text_sha256"])


def _latest_rubric_draft(connection: Any, obligation_id: str) -> tuple[Any, dict[str, Any]] | None:
    row = connection.execute(
        """SELECT * FROM paper_scoring_supply_event
           WHERE obligation_id=? AND status='rubric_candidate'
           ORDER BY rowid DESC LIMIT 1""",
        (obligation_id,),
    ).fetchone()
    if not row:
        return None
    try:
        evidence = json.loads(row["evidence_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if evidence.get("version") != "paper_scoring_authority_draft_v1":
        return None
    return row, evidence


def _search_probes(question: str, draft: dict[str, Any]) -> list[str]:
    probes: list[str] = []
    exact = question.strip()
    if 8 <= len(exact) <= 3000:
        probes.append(exact)
    for term in draft.get("source_search_terms", []):
        if isinstance(term, str):
            normalized = " ".join(term.split()).strip()
            if 3 <= len(normalized) <= 80:
                probes.append(normalized)
    for token in re.findall(r"[\u3400-\u9fff]{4,16}|[A-Za-z0-9][A-Za-z0-9._/-]{2,30}", question):
        probes.append(token)
    return list(dict.fromkeys(probes))[:12]


class PaperScoringSourceMatchService:
    def __init__(self, database: Database):
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            ensure_schema(connection)
            connection.commit()

    def recover(self) -> int:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                """UPDATE paper_scoring_source_lookup
                   SET status='pending',owner_token=NULL,error_code='APP_RESTART',updated_at=?
                   WHERE status='processing'""",
                (utc_now(),),
            ).rowcount
            connection.commit()
        return count

    def _seed_and_claim(self) -> tuple[Any, dict[str, Any], str, str] | None:
        owner_token = str(uuid4())
        # This worker can retract stale scored evidence while reconciling.
        with self.database.external_effect_guard(timeout=10), self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_schema(connection)
            from .paper_scoring import PaperScoringService
            children = connection.execute(
                """SELECT DISTINCT child_id FROM paper_scoring_effective_decision
                   WHERE effective_status='qualified'
                     AND (authority_ref LIKE 'local-answer-source:%'
                          OR authority_ref LIKE 'calibrated-printed-candidate:%')"""
            ).fetchall()
            for child in children:
                PaperScoringService.reconcile_source_decisions(
                    connection, child_id=child["child_id"],
                )
            catalog = _catalog_fingerprint(connection)
            rows = connection.execute(
                """SELECT obligation.*,request.question_text,child.grade
                   FROM paper_scoring_supply_obligation AS obligation
                   JOIN paper_scoring_request AS request USING(request_id)
                   JOIN child_profile AS child ON child.child_id=obligation.child_id
                   LEFT JOIN paper_scoring_effective_decision AS decision
                     ON decision.request_id=obligation.request_id
                    AND decision.effective_status='qualified'
                   WHERE decision.request_id IS NULL"""
            ).fetchall()
            for obligation in rows:
                draft = _latest_rubric_draft(connection, obligation["obligation_id"])
                if not draft:
                    continue
                connection.execute(
                    """INSERT OR IGNORE INTO paper_scoring_source_lookup(
                         obligation_id,child_id,status,candidate_count,updated_at
                       ) VALUES(?,?,'pending',0,?)""",
                    (obligation["obligation_id"], obligation["child_id"], utc_now()),
                )
                connection.execute(
                    """UPDATE paper_scoring_source_lookup
                       SET status='pending',owner_token=NULL,error_code=NULL,updated_at=?
                       WHERE obligation_id=? AND status IN ('candidate','waiting')
                         AND COALESCE(catalog_fingerprint,'')<>?""",
                    (utc_now(), obligation["obligation_id"], catalog),
                )
            row = connection.execute(
                """SELECT lookup.*,obligation.request_id,request.question_text,
                          child.grade
                   FROM paper_scoring_source_lookup AS lookup
                   JOIN paper_scoring_supply_obligation AS obligation USING(obligation_id)
                   JOIN paper_scoring_request AS request USING(request_id)
                   JOIN child_profile AS child ON child.child_id=lookup.child_id
                   WHERE lookup.status='pending'
                     AND NOT EXISTS(
                       SELECT 1 FROM paper_scoring_effective_decision AS decision
                       WHERE decision.request_id=obligation.request_id
                         AND decision.effective_status='qualified'
                     )
                   ORDER BY lookup.updated_at,lookup.obligation_id LIMIT 1"""
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            draft_row = _latest_rubric_draft(connection, row["obligation_id"])
            if not draft_row:
                connection.rollback()
                return None
            changed = connection.execute(
                """UPDATE paper_scoring_source_lookup
                   SET status='processing',owner_token=?,error_code=NULL,updated_at=?
                   WHERE obligation_id=? AND status='pending'""",
                (owner_token, utc_now(), row["obligation_id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            connection.commit()
        return row, draft_row[1], owner_token, catalog

    @staticmethod
    def _find(connection: Any, row: Any, draft: dict[str, Any]) -> list[dict[str, Any]]:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_segment_fts'"
        ).fetchone():
            return []
        subject = draft["subject"]
        results: dict[tuple[str, str], dict[str, Any]] = {}
        for probe in _search_probes(row["question_text"], draft):
            query = '"' + probe.replace('"', '""') + '"'
            try:
                matches = connection.execute(
                    """SELECT segment.segment_id,segment.resource_id,segment.text,
                              segment.text_sha256,segment.source_ref,
                              resource.transcript_sha256,resource.version_label,
                              resource.grade_scope,resource.subject_scope,page.document_kind,
                              page.updated_at AS page_updated_at,page.ocr_confidence
                       FROM kg_segment_fts
                       JOIN kg_segment AS segment USING(segment_id)
                       JOIN kg_resource AS resource USING(resource_id)
                       JOIN kg_document_page AS page USING(resource_id)
                       WHERE kg_segment_fts MATCH ?
                         AND page.document_kind IN (
                           'textbook','school_official_practice','purchased_practice_book'
                         )
                         AND page.ocr_confidence>=.88
                         AND (resource.grade_scope IS NULL OR resource.grade_scope='' OR resource.grade_scope=?)
                         AND (resource.subject_scope IS NULL OR resource.subject_scope='' OR resource.subject_scope=?)
                       LIMIT 20""",
                    (query, row["grade"], subject),
                ).fetchall()
            except Exception:
                matches = []
            for match in matches:
                key = (match["segment_id"], match["resource_id"])
                current = results.setdefault(key, {"row": match, "probes": []})
                current["probes"].append(probe)
        ordered = sorted(
            results.values(), key=lambda item: (
                0 if item["row"]["text"].count(row["question_text"].strip()) == 1 else 1,
                -len(set(item["probes"])), item["row"]["segment_id"],
            )
        )
        candidates = []
        for item in ordered[:8]:
            match = item["row"]
            source_version = _source_version(match)
            candidates.append({
                "segment_id": match["segment_id"],
                "resource_id": match["resource_id"],
                "document_kind": match["document_kind"],
                "source_ref_sha256": hashlib.sha256(
                    match["source_ref"].encode("utf-8")
                ).hexdigest(),
                "source_version": source_version,
                "segment_text_sha256": match["text_sha256"],
                "matched_probe_hashes": sorted(_digest(probe) for probe in set(item["probes"])),
                "matched_probe_count": len(set(item["probes"])),
            })
        return candidates

    def process_next(self) -> dict[str, Any] | None:
        claimed = self._seed_and_claim()
        if not claimed:
            return None
        row, draft, owner_token, catalog = claimed
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT status,owner_token FROM paper_scoring_source_lookup WHERE obligation_id=?",
                (row["obligation_id"],),
            ).fetchone()
            if not owner or owner["status"] != "processing" or owner["owner_token"] != owner_token:
                connection.rollback()
                return None
            candidates = self._find(connection, row, draft)
            candidate_ids = []
            for candidate in candidates:
                candidate_id = str(uuid4())
                action_id = (
                    f"paper-source:{row['obligation_id']}:"
                    f"{candidate['segment_id']}:{candidate['source_version']}"
                )
                basis = {
                    "version": SOURCE_MATCH_VERSION,
                    "matched_probe_hashes": candidate["matched_probe_hashes"],
                    "matched_probe_count": candidate["matched_probe_count"],
                    "contains_child_text": False,
                    "authority_granted": False,
                }
                connection.execute(
                    """INSERT OR IGNORE INTO paper_scoring_source_candidate(
                         candidate_id,obligation_id,child_id,segment_id,resource_id,
                         document_kind,source_ref_sha256,source_version,segment_text_sha256,
                         match_basis_json,created_at,client_action_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, row["obligation_id"], row["child_id"],
                     candidate["segment_id"], candidate["resource_id"],
                     candidate["document_kind"], candidate["source_ref_sha256"],
                     candidate["source_version"], candidate["segment_text_sha256"],
                     json.dumps(basis, ensure_ascii=False, sort_keys=True), utc_now(), action_id),
                )
                stored = connection.execute(
                    "SELECT candidate_id FROM paper_scoring_source_candidate WHERE client_action_id=?",
                    (action_id,),
                ).fetchone()
                candidate_ids.append(stored["candidate_id"])
            status = "candidate" if candidate_ids else "waiting"
            record_attempt(
                connection, request_id=row["request_id"], child_id=row["child_id"],
                status="source_candidate" if candidate_ids else "blocked_source",
                reason=("version-bound local source candidates found" if candidate_ids
                        else "no matching local textbook or workbook source yet"),
                evidence={
                    "version": SOURCE_MATCH_VERSION,
                    "candidate_ids": candidate_ids,
                    "candidate_count": len(candidate_ids),
                    "catalog_fingerprint": catalog,
                    "contains_question_or_answer_text": False,
                    "authority_granted": False,
                },
                client_action_id=f"paper-source-result:{row['obligation_id']}:{catalog}",
            )
            connection.execute(
                """UPDATE paper_scoring_source_lookup
                   SET status=?,owner_token=NULL,catalog_fingerprint=?,candidate_count=?,
                       error_code=NULL,updated_at=? WHERE obligation_id=?""",
                (status, catalog, len(candidate_ids), utc_now(), row["obligation_id"]),
            )
            connection.commit()
        return self.lookup(row["obligation_id"])

    def process_batch(self, limit: int = 4) -> list[dict[str, Any]]:
        results = []
        for _ in range(max(1, min(int(limit), 20))):
            result = self.process_next()
            if not result:
                break
            results.append(result)
        return results

    def valid_candidates(self, obligation_id: str, *, connection=None) -> list[dict[str, Any]]:
        from contextlib import nullcontext
        with nullcontext(connection) if connection is not None else self.database.connect() as connection:
            rows = connection.execute(
                """SELECT candidate.*,segment.text,resource.transcript_sha256,
                          resource.version_label,resource.title,resource.review_status,
                          page.ocr_confidence,page.updated_at AS page_updated_at,
                          candidate.created_at AS candidate_created_at,
                          segment.text_sha256
                   FROM paper_scoring_source_candidate AS candidate
                   JOIN kg_segment AS segment ON segment.segment_id=candidate.segment_id
                   JOIN kg_resource AS resource ON resource.resource_id=candidate.resource_id
                   JOIN kg_document_page AS page ON page.resource_id=candidate.resource_id
                   WHERE candidate.obligation_id=?
                   ORDER BY candidate.created_at,candidate.candidate_id""",
                (obligation_id,),
            ).fetchall()
        valid = []
        for row in rows:
            if (row["ocr_confidence"] is not None
                    and float(row["ocr_confidence"]) >= .88
                    and _source_version_matches(row, row["source_version"])
                    and hashlib.sha256(
                row["text"].encode("utf-8")
            ).hexdigest() == row["segment_text_sha256"]):
                valid.append(dict(row))
        return valid

    def lookup(self, obligation_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT obligation_id,child_id,status,catalog_fingerprint,candidate_count,
                          error_code,updated_at FROM paper_scoring_source_lookup
                   WHERE obligation_id=?""",
                (obligation_id,),
            ).fetchone()
        if not row:
            raise ValueError("paper scoring source lookup not found")
        return {**dict(row), "local_only": True, "cloud_transmission_allowed": False}
