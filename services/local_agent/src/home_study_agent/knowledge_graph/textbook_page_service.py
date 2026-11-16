"""Durable local entry point for one textbook OCR page.

The caller supplies text already produced on this computer (for example by the
document camera's local OCR).  This service deliberately has no video/transcript
lookup, network client, or model call: a vocabulary list can only start from the
page that the parent selected.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from ..database import Database
from ..repository import utc_now
from ..retrieval.hybrid import HashingVectorizer, index_segment
from .schema import ensure_schema


@dataclass(frozen=True)
class TextbookOcrPageInput:
    title: str
    ocr_text: str
    material_kind: str = "textbook"
    grade_scope: str = ""
    subject_scope: str = ""
    semester_scope: str = ""
    textbook_version: str = ""
    volume: str = ""
    unit: str = ""
    lesson: str = ""
    page_number: int = 1
    source_ref: str = "local_ocr_capture"
    ocr_confidence: float | None = None


class TextbookOcrPageService:
    """Imports one local OCR page and lists only OCR-page evidence for parents."""

    corpus_id = "family-textbook-ocr-pages-v1"
    version = "family-textbook-ocr-page-v1"
    _document_kind_by_material = {
        "textbook": "textbook",
        "workbook": "purchased_practice_book",
    }

    def __init__(self, database: Database, vectorizer: HashingVectorizer | None = None):
        self.database = database
        self.vectorizer = vectorizer or HashingVectorizer()

    def import_page(self, page: TextbookOcrPageInput) -> dict:
        self._validate(page)
        now = utc_now()
        # A page is identified by the parent-visible teaching location and local
        # capture reference; re-importing it updates OCR text rather than cloning it.
        capture_source = page.source_ref.strip()
        # A camera artifact is a stable source record.  It may be submitted once
        # before a crash and again after a parent corrects the page metadata, so
        # identity must not clone the page merely because its labels changed.
        identity_fields = ({"capture_source": capture_source}
                           if capture_source.startswith("local_parent_textbook_capture:") else {
                               "title": page.title.strip(), "kind": page.material_kind, "grade": page.grade_scope.strip(), "subject": page.subject_scope.strip(),
                               "semester": page.semester_scope.strip(), "version": page.textbook_version.strip(), "volume": page.volume.strip(),
                               "unit": page.unit.strip(), "lesson": page.lesson.strip(), "page": page.page_number, "source": capture_source,
                           })
        identity = json.dumps(identity_fields, ensure_ascii=False, sort_keys=True)
        external_id = "ocr-page-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        resource_id = "kg-res-" + hashlib.sha256((self.corpus_id + ":" + external_id).encode("utf-8")).hexdigest()[:24]
        text = page.ocr_text.strip()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        metadata = {"material_kind": page.material_kind, "textbook_version": page.textbook_version.strip(), "volume": page.volume.strip(),
                    "unit": page.unit.strip(), "lesson": page.lesson.strip(), "page_number": page.page_number,
                    "ocr_origin": "local_parent_textbook_page"}
        with self.database.connect() as con:
            ensure_schema(con, now); con.commit(); con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT transcript_sha256 FROM kg_resource WHERE corpus_id=? AND external_id=?", (self.corpus_id, external_id)).fetchone()
            con.execute("""INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at)
                VALUES(?,?,?,'local://family-textbook-ocr-pages',NULL,NULL,?,'user_local','unreviewed',?,?,?)
                ON CONFLICT(corpus_id) DO UPDATE SET updated_at=excluded.updated_at""",
                (self.corpus_id, "家庭教材 OCR 页", "local_scanned_document", "local_parent_ocr", self.version, now, now))
            con.execute("""INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,grade_scope,subject_scope,semester_scope,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,video_fingerprint,metadata_json,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at)
                VALUES(?,?,?,?,?,?,?, ?,NULL,?,NULL,NULL,?,?,'user_local',.80,'unreviewed',?,?,?)
                ON CONFLICT(corpus_id,external_id) DO UPDATE SET title=excluded.title,grade_scope=excluded.grade_scope,subject_scope=excluded.subject_scope,semester_scope=excluded.semester_scope,transcript_sha256=excluded.transcript_sha256,metadata_json=excluded.metadata_json,source_ref=excluded.source_ref,updated_at=excluded.updated_at""",
                (resource_id,self.corpus_id,external_id,page.title.strip(),page.grade_scope.strip() or None,page.subject_scope.strip() or None,page.semester_scope.strip() or None,
                 external_id + ".txt",digest,json.dumps(metadata,ensure_ascii=False),page.source_ref.strip(),self.version,now,now))
            con.execute("""INSERT INTO kg_document_page(page_id,resource_id,document_kind,page_number,image_relative_path,image_sha256,image_fingerprint,ocr_engine,ocr_confidence,source_ref,license_boundary,version_label,created_at,updated_at)
                VALUES(?,?,?,?,NULL,NULL,NULL,'local_parent_ocr',? ,?,'user_local',?,?,?)
                ON CONFLICT(resource_id) DO UPDATE SET document_kind=excluded.document_kind,page_number=excluded.page_number,ocr_confidence=excluded.ocr_confidence,source_ref=excluded.source_ref,updated_at=excluded.updated_at""",
                (str(uuid4()),resource_id,self._document_kind_by_material[page.material_kind],page.page_number,page.ocr_confidence,page.source_ref.strip(),self.version,now,now))
            changed = not existing or existing["transcript_sha256"] != digest
            if changed:
                # This source-owned segment is not deduplicated across pages: its
                # evidence offsets and later parent editing must remain page-local.
                old_ids = [row[0] for row in con.execute("SELECT segment_id FROM kg_segment WHERE resource_id=?", (resource_id,))]
                for segment_id in old_ids:
                    con.execute("DELETE FROM kg_segment_fts WHERE segment_id=?", (segment_id,))
                con.execute("DELETE FROM kg_segment WHERE resource_id=?", (resource_id,))
                segment_id = str(uuid4())
                con.execute("INSERT INTO kg_segment(segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,source_ref,confidence,review_status,created_at) VALUES(?,?,?,?,?,?,?,?,.80,'unreviewed',?)",
                            (segment_id,resource_id,0,0,len(text),text,digest,page.source_ref.strip(),now))
                con.execute("INSERT INTO kg_segment_fts(segment_id,text,title,subject_scope,grade_scope) VALUES(?,?,?,?,?)",
                            (segment_id,text,page.title.strip(),page.subject_scope.strip(),page.grade_scope.strip()))
                index_segment(con, segment_id, text, self.vectorizer)
                con.execute("INSERT INTO kg_segment_occurrence(resource_id,segment_id,ordinal,start_char,end_char) VALUES(?,?,?,?,?)",
                            (resource_id,segment_id,0,0,len(text)))
            con.commit()
        result = self.get(resource_id)
        # A crash immediately after the page transaction commits is recoverable:
        # reconciliation runs on *every* import, even when the OCR hash is now
        # unchanged on retry.  It is idempotent, so the common no-change path is
        # safe as well as the interrupted-update path.
        from .language_candidate_service import TextbookFocusCandidateService
        result['candidate_reconciliation'] = TextbookFocusCandidateService(self.database).reconcile_page(resource_id, text)
        result["created_or_updated"] = bool(changed)
        return result

    def list(self) -> list[dict]:
        with self.database.connect() as con:
            rows = con.execute("""SELECT r.resource_id,r.external_id,r.title,r.grade_scope,r.subject_scope,r.semester_scope,r.metadata_json,
                d.page_number,d.ocr_confidence,d.source_ref,r.updated_at
                FROM kg_document_page d JOIN kg_resource r ON r.resource_id=d.resource_id
                WHERE d.document_kind IN ('textbook','purchased_practice_book') ORDER BY r.updated_at DESC,r.title,d.page_number""").fetchall()
        return [self._public(row) for row in rows]

    def list_public(self) -> list[dict]:
        return [self._without_internal(page) for page in self.list()]

    def get(self, resource_id: str) -> dict:
        with self.database.connect() as con:
            row = con.execute("""SELECT r.resource_id,r.external_id,r.title,r.grade_scope,r.subject_scope,r.semester_scope,r.metadata_json,
                d.page_number,d.ocr_confidence,d.source_ref,r.updated_at
                FROM kg_document_page d JOIN kg_resource r ON r.resource_id=d.resource_id
                WHERE d.document_kind IN ('textbook','purchased_practice_book') AND r.resource_id=?""", (resource_id,)).fetchone()
        if not row: raise ValueError("textbook OCR page not found")
        return self._public(row)

    def public_page(self, resource_id: str) -> dict:
        return self._without_internal(self.get(resource_id))

    def resource_id_for_ref(self, page_ref: str) -> str:
        with self.database.connect() as con:
            row = con.execute("""SELECT r.resource_id FROM kg_resource r JOIN kg_document_page d ON d.resource_id=r.resource_id
                WHERE r.corpus_id=? AND r.external_id=? AND d.document_kind IN ('textbook','purchased_practice_book')""", (self.corpus_id, page_ref)).fetchone()
        if not row: raise ValueError("textbook OCR page not found")
        return str(row["resource_id"])

    def resource_id_for_source_ref(self, source_ref: str) -> str:
        with self.database.connect() as con:
            row = con.execute("""SELECT resource_id FROM kg_resource
                WHERE corpus_id=? AND source_ref=? ORDER BY updated_at DESC LIMIT 1""", (self.corpus_id, source_ref)).fetchone()
        if not row: raise ValueError("textbook OCR page not found")
        return str(row["resource_id"])

    @staticmethod
    def _public(row: object) -> dict:
        value = dict(row)  # sqlite.Row
        metadata = json.loads(value.pop("metadata_json") or "{}")
        value["page_ref"] = value.pop("external_id")
        return {**value, "material_kind": metadata.get("material_kind", "textbook"), "textbook_version": metadata.get("textbook_version", ""), "volume": metadata.get("volume", ""),
                "unit": metadata.get("unit", ""), "lesson": metadata.get("lesson", "")}

    @staticmethod
    def _without_internal(page: dict) -> dict:
        return {key: value for key, value in page.items() if key != "resource_id"}

    @staticmethod
    def _validate(page: TextbookOcrPageInput) -> None:
        if page.material_kind not in {"textbook", "workbook"}: raise ValueError("material_kind is invalid")
        if not page.title.strip() or len(page.title) > 160: raise ValueError("title is required")
        if len(page.ocr_text.strip()) < 1 or len(page.ocr_text) > 200_000: raise ValueError("OCR text must contain 1 to 200000 characters")
        if not 1 <= page.page_number <= 20_000: raise ValueError("page_number is invalid")
        if not page.source_ref.strip() or len(page.source_ref) > 300: raise ValueError("source_ref is required")
        if page.ocr_confidence is not None and not 0 <= page.ocr_confidence <= 1: raise ValueError("ocr_confidence is invalid")
