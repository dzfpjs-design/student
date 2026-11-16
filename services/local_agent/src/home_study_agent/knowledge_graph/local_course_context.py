"""Evidence excerpts for local teaching, separate from cloud and mastery inputs."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from .atomic_companion import AtomicGraphCompanionService, compatible_grade_bands
from .context_schema import ensure_schema


class LocalCourseContextService:
    def __init__(self, database):
        self.database = database
        with database.connect() as db:
            ensure_schema(db)
            db.commit()

    def context_for_child(self, *, child_id: str, query: str, subject: str | None = None, limit: int = 3) -> dict:
        query = query.strip()[:500]
        with self.database.connect() as db:
            child = db.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
            if not child: raise ValueError("active child profile required")
            grade = child["grade"]
            grades = list(dict.fromkeys([grade, *compatible_grade_bands(grade)]))
            aliases = {"七年级": "初一", "八年级": "初二", "九年级": "初三"}
            if grade in aliases: grades.append(aliases[grade])
            english = re.findall(r"[a-zA-Z]{2,}", query.lower())
            chinese = re.sub(r"[a-zA-Z]+", " ", query)
            fragments = list(dict.fromkeys(
                [word for word in english if word not in {"what", "is", "it", "the", "an", "and", "of", "to"}]
                + AtomicGraphCompanionService._query_fragments(chinese)
            ))[:18]
            terms = [part for part in fragments if len(part) >= 3][:10]
            filters = "r.grade_scope IN (" + ",".join("?" for _ in grades) + ") AND r.review_status!='rejected' AND s.review_status!='rejected'"
            params = list(grades)
            if subject and subject != "其他":
                filters += " AND r.subject_scope=?"
                params.append(subject)
            common = """SELECT s.segment_id,s.text,s.start_char,s.end_char,s.confidence,
                r.resource_id,r.title,r.grade_scope,r.subject_scope,r.source_ref,r.review_status,
                p.page_number,p.ocr_confidence,r.metadata_json FROM kg_segment s
                JOIN kg_resource r ON r.resource_id=s.resource_id
                JOIN kg_corpus c ON c.corpus_id=r.corpus_id
                LEFT JOIN kg_document_page p ON p.resource_id=r.resource_id """
            # Only pre-delivered curriculum is eligible. Captured family worksheets
            # and arbitrary user corpora never join this background teaching path.
            filters += " AND c.corpus_id IN ('seyk-local-v1','bili-local-v1','nanjing-textbooks-v1')"
            rows = []
            if terms:
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = db.execute(common + " JOIN kg_segment_fts f ON f.segment_id=s.segment_id WHERE kg_segment_fts MATCH ? AND " + filters + " ORDER BY bm25(kg_segment_fts) LIMIT 60", [match, *params]).fetchall()
            if not rows and fragments:
                title_parts = fragments[:5]
                title_filter = " OR ".join("r.title LIKE ? ESCAPE '\\'" for _ in title_parts)
                escaped = ["%" + part.replace("%", "\\%").replace("_", "\\_") + "%" for part in title_parts]
                rows = db.execute(common + " WHERE (" + title_filter + ") AND " + filters + " ORDER BY s.ordinal LIMIT 60", [*escaped, *params]).fetchall()
                if not rows:
                    # FTS5 trigram cannot index two-character Chinese terms such
                    # as oxygen. Scan only this child's subject/grade, with a cap.
                    text_filter = " OR ".join("s.text LIKE ? ESCAPE '\\'" for _ in title_parts)
                    rows = db.execute(common + " WHERE (" + text_filter + ") AND " + filters + " LIMIT 60", [*escaped, *params]).fetchall()
            selected, seen = [], set()
            for row in rows:
                if row["resource_id"] in seen: continue
                metadata = json.loads(row["metadata_json"])
                if metadata.get("manual_review_required") or metadata.get("formula_review_required"): continue
                # Ingest confidence is a conservative mapping prior, not an ASR
                # quality score. Only measured OCR quality gates page excerpts.
                if row["ocr_confidence"] is not None and row["ocr_confidence"] < .8: continue
                text = row["text"]
                positions = [text.lower().find(term.lower()) for term in fragments if term.lower() in text.lower()]
                if not positions: continue
                start = max(0, min(positions) - 70)
                quote = text[start:start + 360]
                selected.append({"segment_id": row["segment_id"], "resource_id": row["resource_id"],
                                 "title": row["title"], "subject": row["subject_scope"], "grade": row["grade_scope"],
                                 "page_number": row["page_number"], "source_ref": row["source_ref"], "excerpt": quote,
                                 "excerpt_start_char": row["start_char"] + start, "review_status": row["review_status"],
                                 "usage": "source_comparison_not_verified_curriculum_alignment"})
                seen.add(row["resource_id"])
                if len(selected) >= max(1, min(limit, 5)): break
            event_id = str(uuid4())
            db.execute("INSERT INTO kg_local_course_context_event VALUES(?,?,?,?,?)", (
                event_id, child_id, hashlib.sha256(query.encode()).hexdigest(),
                json.dumps([item["segment_id"] for item in selected]), datetime.now(timezone.utc).isoformat()))
            db.commit()
        return {"status": "matched" if selected else "no_match", "audit_event_id": event_id, "evidence": selected,
                "cloud_transmission_allowed": False, "mastery_update_allowed": False,
                "diagnostic_prompt": "我也找到了本机课程中的相关片段。你今天学到的是这一部分吗？" if selected else None}
