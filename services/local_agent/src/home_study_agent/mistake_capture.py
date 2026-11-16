from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
from uuid import uuid4

from .answer_attempt_ledger import ensure_visual_promotion_schema
from .database import Database
from .mistake_book import MistakeBookService, utc_now
from .paper_scoring import ensure_paper_scoring_schema


QUESTION_MARKER = re.compile(
    r"^\s*(?:(?:[（(]\s*[A-Za-z]?\s*[)）])\s*)?"
    r"(?:\d{1,3}\s*[.．、)）](?!\d)|[一二三四五六七八九十百]+\s*[、.．])\s*"
)
ARABIC_QUESTION_MARKER = re.compile(r"^\s*(\d{1,3})\s*[.．、)）](?!\d)")


class MistakeCaptureService:
    def __init__(self, database: Database, mistake_book: MistakeBookService):
        self.database = database
        self.mistake_book = mistake_book
        with self.database.connect() as connection:
            ensure_paper_scoring_schema(connection)
            connection.commit()

    def prepare(self, artifact_id: str, child_id: str, client_action_id: str) -> dict:
        with self.database.connect() as connection:
            artifact = self._owned_artifact(connection, artifact_id, child_id)
            existing = self._list(connection, artifact_id)
            if existing:
                return {"items": existing}
            groups = self._segment(artifact, connection=connection)
            now = utc_now()
            for index, group in enumerate(groups):
                candidate_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO mistake_capture_candidate(
                      candidate_id, artifact_id, child_id, question_index,
                      question_text, region_json, confidence, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        candidate_id,
                        artifact_id,
                        child_id,
                        index,
                        group["question_text"],
                        json.dumps(group["region"], ensure_ascii=False) if group["region"] else None,
                        group["confidence"],
                        now,
                        now,
                    ),
                )
                self._event(
                    connection,
                    candidate_id,
                    child_id,
                    "prepared",
                    {"question_text": group["question_text"], "region": group["region"]},
                    f"{client_action_id}:{index}",
                    now,
                )
            connection.commit()
            return {"items": self._list(connection, artifact_id)}

    def list(self, artifact_id: str, child_id: str) -> dict:
        with self.database.connect() as connection:
            self._owned_artifact(connection, artifact_id, child_id)
            return {"items": self._list(connection, artifact_id)}

    def pending_queues(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
            if not child:
                raise UnknownMistakeCaptureCandidateError("找不到孩子。")
            rows = connection.execute(
                """
                SELECT ca.artifact_id, ca.extracted_text, ca.confidence, ca.local_path,
                       ca.status, ca.details_json,
                       SUM(CASE WHEN mcc.status = 'pending' AND psr.request_id IS NULL
                                THEN 1 ELSE 0 END) AS pending_count,
                       COUNT(mcc.candidate_id) AS candidate_count
                FROM capture_artifact ca
                JOIN mistake_capture_candidate mcc ON mcc.artifact_id = ca.artifact_id
                LEFT JOIN paper_scoring_request psr ON psr.candidate_id=mcc.candidate_id
                WHERE ca.child_id = ?
                GROUP BY ca.artifact_id
                HAVING pending_count > 0
                ORDER BY ca.created_at DESC
                """,
                (child_id,),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["details"] = json.loads(item.pop("details_json") or "{}")
                items.append(item)
            return {"items": items}

    def edit(
        self,
        candidate_id: str,
        child_id: str,
        question_text: str,
        client_action_id: str,
    ) -> dict:
        text = question_text.strip()
        if len(text) < 2:
            raise InvalidMistakeCaptureCandidateError("题目文字至少需要两个字符。")
        with self.database.connect() as connection:
            candidate = self._owned_candidate(connection, candidate_id, child_id)
            if self._action_exists(connection, client_action_id):
                return self._candidate(connection, candidate_id)
            if candidate["status"] != "pending":
                raise InvalidMistakeCaptureCandidateStateError("已处理的候选题不能再修改。")
            now = utc_now()
            connection.execute(
                "UPDATE mistake_capture_candidate SET question_text = ?, updated_at = ? WHERE candidate_id = ?",
                (text, now, candidate_id),
            )
            self._event(
                connection,
                candidate_id,
                child_id,
                "edited",
                {"previous_text": candidate["question_text"], "question_text": text},
                client_action_id,
                now,
            )
            connection.commit()
            return self._candidate(connection, candidate_id)

    def skip(self, candidate_id: str, child_id: str, client_action_id: str) -> dict:
        with self.database.connect() as connection:
            candidate = self._owned_candidate(connection, candidate_id, child_id)
            if self._action_exists(connection, client_action_id):
                return self._candidate(connection, candidate_id)
            if candidate["status"] != "pending":
                raise InvalidMistakeCaptureCandidateStateError("这道候选题已经处理。")
            now = utc_now()
            connection.execute(
                "UPDATE mistake_capture_candidate SET status = 'skipped', updated_at = ? WHERE candidate_id = ?",
                (now, candidate_id),
            )
            self._event(connection, candidate_id, child_id, "skipped", {}, client_action_id, now)
            connection.commit()
            return self._candidate(connection, candidate_id)

    def capture(
        self,
        candidate_id: str,
        child_id: str,
        knowledge_node_id: str,
        subject: str,
        title: str,
        submitted_answer: str,
        client_action_id: str,
        promotion_source: str = "child_spoken_fact_confirmation",
    ) -> dict:
        answer = submitted_answer.strip()
        if not answer:
            raise InvalidMistakeCaptureCandidateError("请填写孩子当时实际写下的答案。")
        if promotion_source not in {
            "child_spoken_fact_confirmation", "qualified_scoring_authority"
        }:
            raise InvalidMistakeCaptureCandidateError("无效的视觉作答晋升来源。")
        with self.database.connect() as connection:
            candidate = self._owned_candidate(connection, candidate_id, child_id)
            existing_event = connection.execute(
                "SELECT candidate_id, payload_json FROM mistake_capture_candidate_event WHERE client_action_id = ?",
                (f"{client_action_id}:candidate",),
            ).fetchone()
            if existing_event and existing_event["candidate_id"] != candidate_id:
                raise InvalidMistakeCaptureCandidateStateError("这次保存动作已用于另一道候选题，请重试。")
            if existing_event and candidate["mistake_id"]:
                mistake_id = candidate["mistake_id"]
                self._bind_visual_promotion(
                    connection, candidate_id, child_id, mistake_id, answer,
                    f"{client_action_id}:visual-promotion", promotion_source,
                )
                connection.commit()
                return {
                    "candidate": self._candidate(connection, candidate_id),
                    "mistake": self.mistake_book.get(mistake_id, child_id),
                }
            if candidate["status"] != "pending":
                raise InvalidMistakeCaptureCandidateStateError("这道候选题已经处理。")

        mistake = self.mistake_book.capture(
            child_id=child_id,
            knowledge_node_id=knowledge_node_id,
            subject=subject,
            title=title,
            prompt_summary=candidate["question_text"],
            submitted_answer=answer,
            source_type="document_camera",
            source_ref=f"{candidate['artifact_id']}#{candidate_id}",
            confidence=float(candidate["confidence"]),
            client_action_id=f"candidate-capture:{candidate_id}:{client_action_id}",
        )
        with self.database.connect() as connection:
            current = self._owned_candidate(connection, candidate_id, child_id)
            if current["status"] == "pending":
                now = utc_now()
                connection.execute(
                    """
                    UPDATE mistake_capture_candidate
                    SET status = 'saved', mistake_id = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (mistake["mistake_id"], now, candidate_id),
                )
                self._event(
                    connection,
                    candidate_id,
                    child_id,
                    "captured",
                    {"mistake_id": mistake["mistake_id"]},
                    f"{client_action_id}:candidate",
                    now,
                )
            self._bind_visual_promotion(
                connection, candidate_id, child_id, mistake["mistake_id"], answer,
                f"{client_action_id}:visual-promotion", promotion_source,
            )
            connection.commit()
            return {"candidate": self._candidate(connection, candidate_id), "mistake": mistake}

    @staticmethod
    def _normalized_answer(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    def visual_answer_for_candidate(
        self, candidate_id: str, child_id: str, *, minimum_confidence: float = 0.75,
    ) -> dict | None:
        with self.database.connect() as connection:
            self._owned_candidate(connection, candidate_id, child_id)
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observed_answer_evidence'"
            ).fetchone():
                return None
            row = connection.execute(
                """SELECT evidence.*
                   FROM observed_answer_evidence AS evidence
                   JOIN observed_question AS question
                     ON question.question_id=evidence.question_id
                    AND question.child_id=evidence.child_id
                   WHERE question.mistake_candidate_id=? AND evidence.child_id=?
                     AND TRIM(evidence.answer_text)<>'' AND evidence.confidence>=?
                   ORDER BY evidence.observed_at DESC,evidence.rowid DESC
                   LIMIT 1""",
                (candidate_id, child_id, minimum_confidence),
            ).fetchone()
            return dict(row) if row else None

    def _bind_visual_promotion(
        self, connection, candidate_id: str, child_id: str, mistake_id: str,
        confirmed_answer: str, client_action_id: str, confirmation_source: str,
    ) -> None:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observed_answer_evidence'"
        ).fetchone():
            return
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='visual_answer_evidence_promotion'"
        ).fetchone():
            ensure_visual_promotion_schema(connection)
        evidence = connection.execute(
            """SELECT evidence.visual_answer_evidence_id,evidence.answer_text
               FROM observed_answer_evidence AS evidence
               JOIN observed_question AS question
                 ON question.question_id=evidence.question_id
                AND question.child_id=evidence.child_id
               WHERE question.mistake_candidate_id=? AND evidence.child_id=?
                 AND TRIM(evidence.answer_text)<>'' AND evidence.confidence>=.75
               ORDER BY evidence.observed_at DESC,evidence.rowid DESC
               LIMIT 1""",
            (candidate_id, child_id),
        ).fetchone()
        if not evidence or self._normalized_answer(evidence["answer_text"]) != self._normalized_answer(confirmed_answer):
            return
        existing = connection.execute(
            """SELECT candidate_id,mistake_id,child_id,confirmed_answer,confirmation_source
               FROM visual_answer_evidence_promotion
               WHERE visual_answer_evidence_id=?""",
            (evidence["visual_answer_evidence_id"],),
        ).fetchone()
        if existing:
            expected = (candidate_id, mistake_id, child_id,
                        self._normalized_answer(confirmed_answer), confirmation_source)
            actual = (existing["candidate_id"], existing["mistake_id"], existing["child_id"],
                      self._normalized_answer(existing["confirmed_answer"]),
                      existing["confirmation_source"])
            if actual != expected:
                raise InvalidMistakeCaptureCandidateStateError(
                    "视觉作答证据已经绑定到另一条确认记录，不能覆盖。"
                )
            return
        connection.execute(
            """INSERT INTO visual_answer_evidence_promotion(
                 promotion_id,visual_answer_evidence_id,candidate_id,mistake_id,child_id,
                 confirmed_answer,confirmation_source,created_at,client_action_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (str(uuid4()), evidence["visual_answer_evidence_id"], candidate_id, mistake_id,
             child_id, confirmed_answer.strip(), confirmation_source, utc_now(), client_action_id),
        )

    @staticmethod
    def _segment(artifact, *, connection=None) -> list[dict]:
        try:
            details = json.loads(artifact["details_json"] or "{}")
        except json.JSONDecodeError:
            details = {}

        def within_declared_scope(groups: list[dict], evidence_basis: str) -> list[dict]:
            if connection is None:
                return groups
            try:
                child_id = artifact["child_id"]
                session_id = artifact["session_id"]
            except (KeyError, IndexError):
                return groups
            declared_count = MistakeCaptureService._declared_question_count(
                connection, child_id, session_id,
            )
            if declared_count is not None and groups and len(groups) != declared_count:
                for group in groups:
                    region = group.get("region")
                    region = dict(region) if isinstance(region, dict) else {}
                    region["declared_scope"] = {
                        "declared_question_count": declared_count,
                        "recognized_candidate_count": len(groups),
                        "evidence_basis": evidence_basis,
                        "newness_not_required": True,
                    }
                    group["region"] = region
            return groups

        marked = MistakeCaptureService._marked_wrong_questions(details)
        # A provider result is eligible only when it exactly matches the
        # persisted receipt. When present, its visible handwriting/mark roles
        # are the strongest available selector for attempted questions; local
        # OCR question blocks remain the text-only fallback.
        cloud_groups = MistakeCaptureService._verified_cloud_answered_questions(
            connection, artifact, details,
        )
        if cloud_groups:
            return within_declared_scope(cloud_groups, "visible_answer_marks")
        cloud = details.get("cloud_vision_fallback")
        privacy = cloud.get("privacy") if isinstance(cloud, dict) else None
        if (isinstance(cloud, dict) and isinstance(privacy, dict)
                and (privacy.get("provider_response_received") is True
                     or cloud.get("status") == "needs_review")):
            raise InvalidMistakeCaptureCandidateError(
                "页面解析已经返回，但题号、题块或笔迹关联没有通过校验。我不会把整页题目冒充成已作答题，请告诉我刚做的是哪几题。"
            )
        pipeline = details.get("local_pipeline") or {}
        blocks = pipeline.get("question_blocks")
        if isinstance(blocks, list):
            complete = [block for block in blocks if isinstance(block, dict)
                        and block.get("block_kind") == "question"
                        and not block.get("needs_manual_boundary_check", True)]
            if not complete:
                raise InvalidMistakeCaptureCandidateError(
                    "这页有截断或相邻题目混在一起，我不能拼凑成错题。请把目标题和全部选项放完整，再说“重新拍题”。"
                )
            # Marks prioritize a complete block, never replace its boundaries or
            # remove unmarked questions that the child may select by number.
            marked_texts = {item["question_text"] for item in marked}
            complete.sort(key=lambda block: block["text"] not in marked_texts)
            return within_declared_scope([
                {"question_text": block["text"], "confidence": float(block.get("confidence", 0)),
                 "region": dict(zip(("x", "y", "width", "height"), block["bbox_xywh"]))
                 if block.get("bbox_xywh") else None}
                for block in complete
            ], "question_blocks")
        if marked:
            return within_declared_scope(marked, "visible_answer_marks")
        raw_lines = details.get("lines") if isinstance(details.get("lines"), list) else []
        lines = []
        for raw in raw_lines:
            if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
                continue
            lines.append({
                "text": str(raw["text"]).strip(),
                "confidence": raw.get("confidence"),
                "box": raw.get("box"),
            })
        if not lines:
            lines = [
                {"text": line.strip(), "confidence": artifact["confidence"], "box": None}
                for line in str(artifact["extracted_text"] or "").splitlines()
                if line.strip()
            ]
        if not lines:
            raise InvalidMistakeCaptureCandidateError("没有可整理的识别文字。")

        markers = [index for index, line in enumerate(lines) if QUESTION_MARKER.match(line["text"])]
        if len(markers) < 2:
            ranges = [(0, len(lines))]
        else:
            ranges = []
            for marker_index, start in enumerate(markers):
                group_start = 0 if marker_index == 0 else start
                end = markers[marker_index + 1] if marker_index + 1 < len(markers) else len(lines)
                ranges.append((group_start, end))

        groups = []
        for start, end in ranges:
            group_lines = lines[start:end]
            scores = [
                float(line["confidence"])
                for line in group_lines
                if isinstance(line.get("confidence"), (int, float))
            ]
            confidence = sum(scores) / len(scores) if scores else float(artifact["confidence"] or 0)
            groups.append({
                "question_text": "\n".join(line["text"] for line in group_lines),
                "confidence": round(max(0.0, min(confidence, 1.0)), 4),
                "region": MistakeCaptureService._region(group_lines),
            })
        return within_declared_scope(groups, "question_blocks")

    @staticmethod
    def _verified_cloud_answered_questions(connection, artifact, details: dict) -> list[dict]:
        """Return visible answered question regions from a persisted cloud receipt.

        Cloud OCR remains unverified text. It can identify review candidates, but
        it cannot mark an answer wrong, diagnose a cause, or update mastery.
        """
        if connection is None:
            return []
        cloud = details.get("cloud_vision_fallback")
        if not isinstance(cloud, dict) or cloud.get("status") != "needs_review":
            return []
        artifact_id = str(artifact["artifact_id"])
        request_id = cloud.get("request_id")
        if request_id != f"artifact:{artifact_id}":
            return []
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_vision_request'"
        ).fetchone()
        if not table:
            return []
        receipt = connection.execute(
            """SELECT result_json FROM document_vision_request
               WHERE child_id=? AND request_id=? AND status='succeeded'""",
            (artifact["child_id"], request_id),
        ).fetchone()
        try:
            if not receipt or json.loads(receipt[0]) != cloud:
                return []
        except (TypeError, ValueError):
            return []
        blocks = cloud.get("blocks")
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 12:
            return []
        visible = []
        for block in blocks:
            if not isinstance(block, dict):
                return []
            text = block.get("text")
            bbox = block.get("bbox")
            confidence = block.get("confidence")
            source = block.get("source")
            if (not isinstance(text, str) or not text.strip()
                    or source not in {"printed", "handwritten", "grading_mark", "uncertain"}
                    or type(confidence) not in (int, float) or not 0 <= confidence <= 1
                    or not isinstance(bbox, list) or len(bbox) != 4
                    or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in bbox)
                    or not (bbox[0] < bbox[2] and bbox[1] < bbox[3])):
                return []
            visible.append({**block, "text": text.strip(), "center_y": (bbox[1] + bbox[3]) / 2})
        visible.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
        question_starts = [item for item in visible
                           if item["source"] in {"printed", "uncertain"}
                           and QUESTION_MARKER.match(item["text"])]
        if not question_starts:
            return []
        revision_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_page_revision'"
        ).fetchone()
        revision = connection.execute(
            """SELECT previous_revision_id FROM document_page_revision
               WHERE artifact_id=? AND child_id=? AND session_id=?""",
            (artifact_id, artifact["child_id"], artifact["session_id"]),
        ).fetchone() if revision_table else None
        previous_revision_id = revision["previous_revision_id"] if revision else None
        groups = []
        answered_question_starts = []
        for index, start in enumerate(question_starts):
            top = start["bbox"][1]
            bottom = question_starts[index + 1]["bbox"][1] if index + 1 < len(question_starts) else 1.0
            region_blocks = [item for item in visible if top <= item["center_y"] < bottom]
            answer_evidence = [item for item in region_blocks
                               if item["source"] in {"handwritten", "grading_mark"}]
            if not answer_evidence:
                continue
            answered_question_starts.append(start)
            question_blocks = [item for item in region_blocks
                               if item["source"] in {"printed", "uncertain"}]
            if not question_blocks:
                continue
            left = min(item["bbox"][0] for item in region_blocks)
            right = max(item["bbox"][2] for item in region_blocks)
            region_top = min(item["bbox"][1] for item in region_blocks)
            region_bottom = max(item["bbox"][3] for item in region_blocks)
            groups.append({
                "question_text": "\n".join(item["text"] for item in question_blocks),
                "confidence": round(min(float(item["confidence"]) for item in region_blocks), 4),
                "region": {
                    "x": round(left, 6), "y": round(region_top, 6),
                    "width": round(right - left, 6), "height": round(region_bottom - region_top, 6),
                    "coordinate_space": "normalized_safe_page",
                    "observation_basis": "visible_snapshot",
                    "temporal_baseline_available": previous_revision_id is not None,
                    "previous_revision_id": previous_revision_id,
                },
            })
        MistakeCaptureService._require_cloud_question_alignment(
            artifact, details, answered_question_starts,
        )
        return groups

    @staticmethod
    def _declared_question_count(connection, child_id: str, session_id: str) -> int | None:
        if connection is None:
            return None
        rows = connection.execute(
            """SELECT title,range_text,special_requirements FROM homework_item
               WHERE child_id=? AND session_id=? AND status='pending'
               ORDER BY created_at DESC""",
            (child_id, session_id),
        ).fetchall()
        numerals = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
                    "七": 7, "八": 8, "九": 9, "十": 10}
        for row in rows:
            text = "；".join(str(row[key] or "") for key in ("title", "range_text", "special_requirements"))
            match = re.search(r"(?:(\d{1,2})|([一二三四五六七八九十]))\s*(?:道)?题", text)
            if not match:
                continue
            if match.group(1):
                value = int(match.group(1))
            else:
                value = numerals[match.group(2)]
            if 1 <= value <= 50:
                return value
        return None

    @staticmethod
    def _require_cloud_question_alignment(artifact, details: dict, question_starts: list[dict]) -> None:
        """Reject provider-renumbered questions when local OCR can cross-check them."""
        pipeline = details.get("local_pipeline") if isinstance(details, dict) else None
        raw_blocks = pipeline.get("question_blocks") if isinstance(pipeline, dict) else None
        local_blocks = [
            str(block.get("text", "")).strip()
            for block in (raw_blocks if isinstance(raw_blocks, list) else [])
            if isinstance(block, dict) and block.get("block_kind") == "question"
            and str(block.get("text", "")).strip()
        ]
        if len(local_blocks) < 2:
            return

        def normalized(value: str) -> str:
            value = QUESTION_MARKER.sub("", value, count=1)
            return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value).lower()[:1200]

        local = []
        for text in local_blocks:
            marker = ARABIC_QUESTION_MARKER.match(text)
            body = normalized(text)
            if marker and body:
                local.append((marker.group(1), body))
        if len(local) < 2:
            return
        for cloud in question_starts:
            marker = ARABIC_QUESTION_MARKER.match(cloud["text"])
            body = normalized(cloud["text"])
            if not marker or not body:
                raise InvalidMistakeCaptureCandidateError(
                    "我看到了作答痕迹，但题号没有可靠读出。请告诉我刚做的是哪几题，我再逐题核对。"
                )
            score, matched_number = max(
                ((SequenceMatcher(None, body, candidate).ratio(), number) for number, candidate in local),
                default=(0.0, ""),
            )
            if score >= .45 and matched_number != marker.group(1):
                raise InvalidMistakeCaptureCandidateError(
                    "我看到了作答痕迹，但云端把题号和相邻题目配错了。请告诉我刚做的是哪几题，我不会猜。"
                )

    @staticmethod
    def _marked_wrong_questions(details: dict) -> list[dict]:
        pipeline = details.get("local_pipeline")
        if not isinstance(pipeline, dict):
            return []
        raw_candidates = pipeline.get("wrong_question_candidates")
        if not isinstance(raw_candidates, list):
            return []
        groups = []
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            if raw.get("needs_manual_boundary_check") is True or raw.get("mark_kind") == "tick_like":
                continue
            text = str(raw.get("text") or "").strip()
            if len(text) < 2:
                continue
            confidence = raw.get("confidence")
            if not isinstance(confidence, (int, float)):
                confidence = raw.get("ocr_confidence")
            score = float(confidence) if isinstance(confidence, (int, float)) else 0.0
            bbox = raw.get("bbox_xywh")
            region = None
            if (
                isinstance(bbox, list)
                and len(bbox) >= 4
                and all(isinstance(value, (int, float)) for value in bbox[:4])
            ):
                region = {
                    "x": int(bbox[0]),
                    "y": int(bbox[1]),
                    "width": max(0, int(bbox[2])),
                    "height": max(0, int(bbox[3])),
                }
            groups.append({
                "question_text": text,
                "confidence": round(max(0.0, min(score, 1.0)), 4),
                "region": region,
                "mark_kind": str(raw.get("mark_kind") or "visible_mark"),
            })
        return groups

    @staticmethod
    def _region(lines: list[dict]) -> dict | None:
        points = []
        for line in lines:
            box = line.get("box")
            if not isinstance(box, list):
                continue
            for point in box:
                if isinstance(point, list) and len(point) >= 2:
                    points.append((int(point[0]), int(point[1])))
        if not points:
            return None
        left = min(point[0] for point in points)
        top = min(point[1] for point in points)
        right = max(point[0] for point in points)
        bottom = max(point[1] for point in points)
        return {"x": left, "y": top, "width": right - left, "height": bottom - top}

    @staticmethod
    def _event(connection, candidate_id: str, child_id: str, event_type: str, payload: dict, client_action_id: str, now: str) -> None:
        connection.execute(
            """
            INSERT INTO mistake_capture_candidate_event(
              event_id, candidate_id, child_id, event_type, payload_json, created_at, client_action_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), candidate_id, child_id, event_type, json.dumps(payload, ensure_ascii=False), now, client_action_id),
        )

    @staticmethod
    def _action_exists(connection, client_action_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM mistake_capture_candidate_event WHERE client_action_id = ?",
            (client_action_id,),
        ).fetchone() is not None

    @staticmethod
    def _owned_artifact(connection, artifact_id: str, child_id: str):
        row = connection.execute(
            "SELECT * FROM capture_artifact WHERE artifact_id = ? AND child_id = ?",
            (artifact_id, child_id),
        ).fetchone()
        if not row:
            raise UnknownMistakeCaptureCandidateError("找不到这份作业采集记录。")
        return row

    @staticmethod
    def _owned_candidate(connection, candidate_id: str, child_id: str):
        row = connection.execute(
            "SELECT * FROM mistake_capture_candidate WHERE candidate_id = ? AND child_id = ?",
            (candidate_id, child_id),
        ).fetchone()
        if not row:
            raise UnknownMistakeCaptureCandidateError("找不到这道候选题。")
        return row

    @staticmethod
    def _list(connection, artifact_id: str) -> list[dict]:
        rows = connection.execute(
            """SELECT candidate.*,request.request_id AS scoring_request_id,
                      CASE WHEN decision.decision_id IS NULL THEN request.status ELSE 'scored' END
                        AS scoring_request_status,
                      decision.authority_type AS scoring_authority_type,
                      decision.correctness AS scored_correctness
               FROM mistake_capture_candidate AS candidate
               LEFT JOIN paper_scoring_request AS request
                 ON request.candidate_id=candidate.candidate_id
               LEFT JOIN paper_scoring_visible_decision AS decision
                 ON decision.request_id=request.request_id
                AND decision.effective_status='qualified'
               WHERE candidate.artifact_id=? ORDER BY candidate.question_index""",
            (artifact_id,),
        ).fetchall()
        return [MistakeCaptureService._serialize(row) for row in rows]

    @staticmethod
    def _candidate(connection, candidate_id: str) -> dict:
        row = connection.execute(
            """SELECT candidate.*,request.request_id AS scoring_request_id,
                      CASE WHEN decision.decision_id IS NULL THEN request.status ELSE 'scored' END
                        AS scoring_request_status,
                      decision.authority_type AS scoring_authority_type,
                      decision.correctness AS scored_correctness
               FROM mistake_capture_candidate AS candidate
               LEFT JOIN paper_scoring_request AS request
                 ON request.candidate_id=candidate.candidate_id
               LEFT JOIN paper_scoring_visible_decision AS decision
                 ON decision.request_id=request.request_id
                AND decision.effective_status='qualified'
               WHERE candidate.candidate_id=?""",
            (candidate_id,),
        ).fetchone()
        return MistakeCaptureService._serialize(row)

    @staticmethod
    def _serialize(row) -> dict:
        item = dict(row)
        item["region"] = json.loads(item.pop("region_json")) if item.get("region_json") else None
        return item


class UnknownMistakeCaptureCandidateError(Exception):
    pass


class InvalidMistakeCaptureCandidateError(Exception):
    pass


class InvalidMistakeCaptureCandidateStateError(Exception):
    pass
