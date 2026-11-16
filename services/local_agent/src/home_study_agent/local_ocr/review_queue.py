"""Durable, local-only human review queue for uncertain OCR regions.

This store deliberately contains no page pixels, media path, or cloud-facing
payload.  A browser may render a transient crop while it still owns a capture,
but after reload the reviewer sees the region coordinates and the durable text
decision rather than a silently retained household image.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from ..database import Database
from .review_lifecycle import migrate_review_store
from .review_schema import ensure_schema


REVIEW_KINDS = ("text", "handwriting", "formula", "formula_question", "marking")
REVIEW_STATUSES = ("pending", "confirmed", "skipped")
MAX_LOW_CONFIDENCE_TEXT_REGIONS = 12


class InvalidOcrReviewValueError(ValueError):
    pass


class OcrReviewActionConflictError(ValueError):
    pass


class OcrReviewQueue:
    def __init__(self, path: Database | Path, legacy_path: Path | None = None):
        self.database = path if isinstance(path, Database) else None
        self.path = path.path if self.database else Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if self.database is not None and legacy_path is not None:
            migrate_review_store(self.database, legacy_path)

    def prepare(self, artifact: dict, child_id: str, session_id: str | None, action_id: str) -> dict:
        artifact_id = str(artifact["artifact_id"])
        details = artifact.get("details") or {}
        pipeline = details.get("local_pipeline") or {}
        review = pipeline.get("manual_review") or {}
        if not review.get("required"):
            return {"items": []}
        lines = details.get("lines") if isinstance(details.get("lines"), list) else []
        regions = [line for line in lines if isinstance(line, dict) and str(line.get("text", "")).strip()]
        # No fabricated handwriting/formula/mark bounding boxes: use an OCR line
        # only as a review location, with an explicit unsupported-model reason.
        if not regions:
            regions = [{"text": str(artifact.get("extracted_text") or ""), "confidence": artifact.get("confidence", 0), "box": None}]
        requested: list[tuple[str, list[dict]]] = []
        text_regions = _low_confidence_text_regions(regions, review)
        if text_regions:
            requested.append(("text", text_regions))
        if _needs_field_review(review.get("handwriting")):
            requested.append(("handwriting", regions))
        formula_regions = _formula_review_regions(pipeline.get("formula_routing"))
        if formula_regions:
            requested.append(("formula", formula_regions))
        if _needs_mark_review(review):
            requested.append(("marking", regions))
        if not requested:
            return {"items": []}
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT 1 FROM ocr_review_item WHERE artifact_id = ? LIMIT 1", (artifact_id,)).fetchone()
            from ..paper_formula_review import prepare_questions
            full_questions = prepare_questions(db, artifact_id, child_id, session_id) if self.database else []
            if existing:
                # Preserve earlier fragment decisions and full-question reviews.
                full_questions = [question for question in full_questions if not db.execute('''SELECT 1 FROM ocr_review_item
                    WHERE artifact_id=? AND child_id=? AND review_kind='formula_question' AND proposed_text=? AND region_json=?''',
                    (artifact_id, child_id, question['text'], json.dumps(question['box']))).fetchone()]
                requested = []
            if not existing or full_questions:
                if full_questions:
                    covered = {tuple(pipeline['formula_routing']['regions'][i]['bbox_xywh'])
                               for question in full_questions for i in question['source_binding']['formula_indices']}
                    requested = [(kind, [region for region in entries if tuple(region['box']) not in covered]
                                  if kind == 'formula' else entries) for kind, entries in requested]
                    requested.append(('formula_question', full_questions))
                now = _now()
                full_bindings = {}
                for kind, kind_regions in requested:
                    for index, region in enumerate(kind_regions):
                        review_id = str(uuid4())
                        if kind == 'formula_question':
                            full_bindings[review_id] = region['source_binding']
                        confidence = _confidence(region.get("confidence", artifact.get("confidence", 0)))
                        db.execute(
                            """INSERT INTO ocr_review_item VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)""",
                            (review_id, artifact_id, child_id, session_id, kind, index,
                             json.dumps(region.get("box")), str(region.get("text") or ""), confidence, now, now),
                        )
                db.execute("INSERT INTO ocr_review_event(event_id, artifact_id, review_id, event_type, client_action_id, payload_json, created_at) VALUES (?, ?, NULL, 'prepared', ?, ?, ?)",
                           (str(uuid4()), artifact_id, action_id, json.dumps({"raw_media_retained": False,
                            'full_formula_questions': full_bindings}), now))
                db.commit()
            return {"items": self._list(db, artifact_id, child_id)}

    def list(self, artifact_id: str, child_id: str) -> dict:
        with self._connect() as db:
            return {"items": self._list(db, artifact_id, child_id)}

    def pending(self, child_id: str) -> dict:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM ocr_review_item WHERE child_id = ? AND status = 'pending' ORDER BY created_at, region_index", (child_id,)).fetchall()
            return {"items": [self._item(row) for row in rows]}

    def recent(self, child_id: str, limit: int = 10) -> dict:
        """Recent human actions survive restart so a parent can undo one safely."""
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM ocr_review_item
                WHERE child_id = ? AND status IN ('confirmed', 'skipped')
                ORDER BY updated_at DESC, review_id DESC LIMIT ?""", (child_id, limit)).fetchall()
            return {"items": [self._item(row) for row in rows]}

    def update(self, review_id: str, child_id: str, corrected_text: str, action_id: str) -> dict:
        text = corrected_text.strip()
        if not text:
            raise InvalidOcrReviewValueError("修正文字不能为空；如该区域不是文本，请选择跳过。")
        if len(text) > 4000:
            raise InvalidOcrReviewValueError("修正内容不能超过 4000 个字符。")
        return self._transition(review_id, child_id, "confirmed", text, "corrected", action_id)

    def skip(self, review_id: str, child_id: str, action_id: str) -> dict:
        return self._transition(review_id, child_id, "skipped", None, "skipped", action_id)

    def undo(self, review_id: str, child_id: str, action_id: str) -> dict:
        return self._transition(review_id, child_id, "pending", None, "undone", action_id, allow_any=True)

    def batch(self, review_ids: list[str], child_id: str, decision: str, action_id: str) -> dict:
        unique_ids = list(dict.fromkeys(str(value).strip() for value in review_ids if str(value).strip()))
        if not 1 <= len(unique_ids) <= 50:
            raise InvalidOcrReviewValueError("每批请选择 1 至 50 条校对项。")
        if decision not in {"confirm_proposed", "skip"}:
            raise InvalidOcrReviewValueError("批量校对决定无效。")
        request_payload = json.dumps(
            {"review_ids": unique_ids, "child_id": child_id, "decision": decision},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        with self._connect() as db:
            replay = db.execute(
                "SELECT request_json,response_json FROM ocr_review_batch WHERE client_action_id=?",
                (action_id,),
            ).fetchone()
            if replay:
                if replay["request_json"] != request_payload:
                    raise OcrReviewActionConflictError("同一批量操作标识的内容不一致；请重新执行。")
                return json.loads(replay["response_json"])
            placeholders = ",".join("?" for _ in unique_ids)
            rows = db.execute(
                f"SELECT * FROM ocr_review_item WHERE review_id IN ({placeholders}) AND child_id=?",
                (*unique_ids, child_id),
            ).fetchall()
            by_id = {row["review_id"]: row for row in rows}
            missing = [review_id for review_id in unique_ids if review_id not in by_id]
            if missing:
                raise KeyError("Unknown OCR review item")
            already_processed = [review_id for review_id in unique_ids if by_id[review_id]["status"] != "pending"]
            if already_processed:
                raise OcrReviewActionConflictError("选中项中包含已处理记录；请刷新后重试。")
            if decision == 'confirm_proposed' and any(row['review_kind'] == 'formula_question' for row in rows):
                raise InvalidOcrReviewValueError('公式题干需要逐题核对并保存，不能批量确认。')
            now = _now()
            processed: list[dict] = []
            event_type = "batch_confirmed" if decision == "confirm_proposed" else "batch_skipped"
            status = "confirmed" if decision == "confirm_proposed" else "skipped"
            for review_id in unique_ids:
                row = by_id[review_id]
                corrected_text = row["proposed_text"] if decision == "confirm_proposed" else None
                db.execute(
                    "UPDATE ocr_review_item SET status=?,corrected_text=?,updated_at=? WHERE review_id=?",
                    (status, corrected_text, now, review_id),
                )
                db.execute(
                    """INSERT INTO ocr_review_event(
                         event_id,artifact_id,review_id,event_type,client_action_id,payload_json,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()), row["artifact_id"], review_id, event_type,
                        f"{action_id}:{review_id}",
                        json.dumps({"corrected_text": corrected_text}, ensure_ascii=False, separators=(",", ":")),
                        now,
                    ),
                )
                processed.append(self._item(db.execute(
                    "SELECT * FROM ocr_review_item WHERE review_id=?", (review_id,)
                ).fetchone()))
            remaining = db.execute(
                "SELECT COUNT(*) FROM ocr_review_item WHERE child_id=? AND status='pending'", (child_id,)
            ).fetchone()[0]
            result = {
                "items": processed,
                "processed_count": len(processed),
                "remaining_count": int(remaining),
                "decision": decision,
            }
            db.execute(
                "INSERT INTO ocr_review_batch(client_action_id,child_id,request_json,response_json,created_at) VALUES(?,?,?,?,?)",
                (action_id, child_id, request_payload, json.dumps(result, ensure_ascii=False), now),
            )
            db.commit()
            return result

    def truth_summary(self, child_id: str) -> dict:
        """Local evaluation truth derived only from explicitly confirmed edits."""
        with self._connect() as db:
            rows = db.execute("""SELECT review_kind, COUNT(*) AS reviewed,
                SUM(CASE WHEN proposed_text = corrected_text THEN 1 ELSE 0 END) AS exact_matches
                FROM ocr_review_item WHERE child_id = ? AND status = 'confirmed' GROUP BY review_kind""", (child_id,)).fetchall()
        return {"local_only": True, "items": [{"review_kind": row["review_kind"], "reviewed": row["reviewed"], "exact_matches": row["exact_matches"] or 0} for row in rows],
                "boundary": "Only explicit local corrections are evaluation truth; skipped items are excluded."}

    def _transition(self, review_id: str, child_id: str, status: str, text: str | None, event: str, action_id: str, allow_any: bool = False) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM ocr_review_item WHERE review_id = ? AND child_id = ?", (review_id, child_id)).fetchone()
            if not row:
                raise KeyError("Unknown OCR review item")
            payload = json.dumps({"corrected_text": text}, ensure_ascii=False, separators=(",", ":"))
            duplicate = db.execute(
                "SELECT event_type, payload_json FROM ocr_review_event WHERE review_id = ? AND client_action_id = ? LIMIT 1",
                (review_id, action_id),
            ).fetchone()
            if duplicate:
                if duplicate["event_type"] != event or duplicate["payload_json"] != payload:
                    raise OcrReviewActionConflictError("同一操作标识的内容不一致；请重新执行这次校对。")
                return self._item(row)
            if not allow_any and row["status"] != "pending":
                raise OcrReviewActionConflictError("该区域已经处理；可选择撤销后再修改。")
            now = _now()
            db.execute("UPDATE ocr_review_item SET status = ?, corrected_text = ?, updated_at = ? WHERE review_id = ?", (status, text, now, review_id))
            db.execute("INSERT INTO ocr_review_event(event_id, artifact_id, review_id, event_type, client_action_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid4()), row["artifact_id"], review_id, event, action_id, payload, now))
            db.commit()
            return self._item(db.execute("SELECT * FROM ocr_review_item WHERE review_id = ?", (review_id,)).fetchone())

    def _list(self, db: sqlite3.Connection, artifact_id: str, child_id: str) -> list[dict]:
        return [self._item(row) for row in db.execute("SELECT * FROM ocr_review_item WHERE artifact_id = ? AND child_id = ? ORDER BY review_kind, region_index", (artifact_id, child_id))]

    @staticmethod
    def _item(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["region"] = json.loads(item.pop("region_json"))
        item["raw_media_retained"] = False
        return item

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            ensure_schema(db)
            db.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.database is not None:
            with self.database.connect() as db:
                yield db
            return
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def _now() -> str: return datetime.now(timezone.utc).isoformat()
def _confidence(value: object) -> float:
    try: return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError): return 0.0


def _low_confidence_text_regions(regions: list[dict], review: dict) -> list[dict]:
    """Queue only bounded uncertain printed-text evidence.

    A missing handwriting model is a capability boundary, not evidence that
    printed text is handwriting.  Poor pages must not create unbounded parent
    work, so the lowest-confidence regions are capped.
    """
    reasons = {str(item) for item in review.get("reasons", []) if isinstance(item, str)}
    if "low_ocr_confidence" not in reasons:
        return []
    uncertain = [region for region in regions if _confidence(region.get("confidence")) < .78]
    return sorted(uncertain, key=lambda region: _confidence(region.get("confidence")))[:MAX_LOW_CONFIDENCE_TEXT_REGIONS]


def _needs_field_review(value: object) -> bool:
    """Require explicit handwriting evidence; do not infer it from low confidence."""
    value = str(value or "").strip().lower()
    if not value or value in {"ready", "supported", "not_detected"}:
        return False
    if "not inferred" in value or "未推断" in value:
        return False
    return any(token in value for token in ("handwriting", "手写", "unsupported"))


def _needs_mark_review(review: dict) -> bool:
    value = str(review.get("marking") or "").strip().lower()
    reasons = {str(item).strip().lower() for item in review.get("reasons", []) if isinstance(item, str)}
    explicit_reasons = {"marking_detected", "red_ink_detected", "annotation_detected", "correction_mark_detected"}
    return (
        any(token in value for token in ("marking", "red ink", "annotation", "批改"))
        or bool(reasons & explicit_reasons)
        or any("批改" in reason for reason in reasons)
    )


def _formula_review_regions(value: object) -> list[dict]:
    """Use formula detector regions only; never fabricate a formula review row."""
    if not isinstance(value, dict):
        return []
    source = value.get("regions")
    if not isinstance(source, list):
        return []
    regions = []
    for item in source:
        if not isinstance(item, dict) or item.get("status") != "manual_edit_required" or item.get("review_eligible") is False:
            continue
        box = item.get("bbox_xywh")
        if not isinstance(box, list) or len(box) != 4:
            continue
        regions.append({
            "text": str(item.get("candidate_latex") or ""),
            "confidence": item.get("source_text_confidence", 0),
            "box": box,
        })
    return regions
