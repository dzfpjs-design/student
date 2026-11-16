from __future__ import annotations

import json
import re
from typing import Any


PRESENTATION_VERSION = "tutor_blackboard_v1"
MAX_SEGMENTS = 8
MAX_BLOCKS_PER_SEGMENT = 6
ALLOWED_KINDS = frozenset({"heading", "text", "formula", "steps", "table", "relation", "question"})
ALLOWED_TONES = frozenset({"chalk", "yellow", "cyan", "coral", "green"})


class InvalidTutorPresentationError(ValueError):
    pass


def tutor_blackboard_prompt_contract() -> str:
    return (
        "只输出一个JSON对象，不要输出Markdown代码块或JSON以外的文字。"
        "结构为{\"blackboard\":{\"title\":\"本轮板书标题\",\"segments\":[...]}}。"
        "segments为1到8段，每段包含narration和blocks；narration是这一段要朗读的自然口语，"
        "所有narration按顺序拼接后就是完整讲解。blocks为同步出现的板书块，kind只能是"
        "heading、text、formula、steps、table、relation、question。"
        "heading/text/question使用text；formula使用latex和可选text；steps使用items字符串数组；"
        "table使用columns字符串数组和rows二维字符串数组；relation使用nodes字符串数组。"
        "每个块可选tone，值为chalk、yellow、cyan、coral或green。"
        "板书只写关键词、条件、公式、推导骨架和检查问题，不复制整段口语。"
        "公式latex不包含美元符号、HTML或自定义宏。关系图只表达节点的先后或因果顺序。"
    )


def parse_tutor_blackboard_response(content: str, subject: str, hint_level: int) -> tuple[str, dict[str, Any]]:
    raw = _strip_json_fence(content)
    try:
        payload, json_repaired = _decode_json_object(raw)
    except json.JSONDecodeError:
        answer = content.strip()
        return answer, build_degraded_presentation(answer, subject, hint_level, "invalid_json")
    try:
        presentation = _validate_presentation(payload, subject, hint_level)
        if json_repaired:
            presentation["source"] = "model_structured_repaired"
            presentation["repair_warnings"] = ["json_wrapper_removed"]
        answer = "".join(segment["narration"] for segment in presentation["segments"]).strip()
        if not answer:
            raise InvalidTutorPresentationError("empty narration")
        return answer, presentation
    except (InvalidTutorPresentationError, TypeError, ValueError):
        repaired = _repair_presentation(payload, subject, hint_level)
        if repaired is not None:
            if json_repaired:
                repaired.setdefault("repair_warnings", []).insert(0, "json_wrapper_removed")
            answer = "".join(segment["narration"] for segment in repaired["segments"]).strip()
            return answer, repaired
        answer = _extract_narration(payload) or content.strip()
        return answer, build_degraded_presentation(answer, subject, hint_level, "invalid_schema")


def _decode_json_object(raw: str) -> tuple[Any, bool]:
    try:
        return json.loads(raw), False
    except json.JSONDecodeError as original_error:
        start = raw.find("{")
        if start < 0:
            raise original_error
        try:
            payload, end = json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError:
            raise original_error
        trailing = raw[start + end:].strip()
        if trailing and not trailing.startswith("```"):
            raise original_error
        return payload, True


def _repair_presentation(payload: Any, subject: str, hint_level: int) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    board = payload.get("blackboard")
    if not isinstance(board, dict):
        board = payload if isinstance(payload.get("segments"), list) else None
    if not isinstance(board, dict) or not isinstance(board.get("segments"), list):
        return None
    warnings: list[str] = []
    title = _clean_text(board.get("title"), 60)
    if not title:
        title = f"第{hint_level}级讲解"
        warnings.append("title_defaulted")
    segments: list[dict[str, Any]] = []
    for raw_segment in board["segments"][:MAX_SEGMENTS]:
        if not isinstance(raw_segment, dict):
            warnings.append("segment_dropped")
            continue
        narration = _clean_text(
            raw_segment.get("narration") or raw_segment.get("speech") or raw_segment.get("text"),
            280,
        )
        if not narration:
            warnings.append("segment_without_narration_dropped")
            continue
        raw_blocks = raw_segment.get("blocks")
        if not isinstance(raw_blocks, list):
            raw_blocks = []
            warnings.append("segment_blocks_defaulted")
        blocks: list[dict[str, Any]] = []
        for raw_block in raw_blocks[:MAX_BLOCKS_PER_SEGMENT]:
            try:
                blocks.append(_validate_block(raw_block))
            except (InvalidTutorPresentationError, TypeError, ValueError):
                repaired_block = _repair_block(raw_block)
                if repaired_block is not None:
                    blocks.append(repaired_block)
                    warnings.append("block_normalized")
                else:
                    warnings.append("block_dropped")
        if not blocks:
            blocks = [{"kind": "text", "tone": "chalk", "text": narration[:180]}]
            warnings.append("narration_block_created")
        segments.append({
            "segment_id": f"segment-{len(segments) + 1}",
            "narration": narration,
            "duration_hint_ms": _duration_hint(narration),
            "blocks": blocks,
        })
    if not segments:
        return None
    return {
        "version": PRESENTATION_VERSION,
        "source": "model_structured_repaired",
        "repair_warnings": list(dict.fromkeys(warnings))[:8],
        "sync_mode": "segment_estimate",
        "subject": _clean_text(subject, 40),
        "title": title,
        "segments": segments,
    }


def _repair_block(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind in {"equation", "math", "math_formula"} or (not kind and raw.get("latex")):
        latex = _clean_latex(raw.get("latex") or raw.get("formula"))
        if latex:
            result = {"kind": "formula", "tone": _safe_tone(raw.get("tone")), "latex": latex}
            label = _clean_text(raw.get("text") or raw.get("label"), 100)
            if label:
                result["text"] = label
            return result
    text = _clean_text(raw.get("text") or raw.get("content") or raw.get("label"), 180)
    if text:
        normalized_kind = kind if kind in {"heading", "text", "question"} else "text"
        return {"kind": normalized_kind, "tone": _safe_tone(raw.get("tone")), "text": text}
    items = raw.get("items") or raw.get("steps")
    if isinstance(items, list):
        cleaned = [_clean_text(item, 120) for item in items[:8]]
        cleaned = [item for item in cleaned if item]
        if cleaned:
            return {"kind": "steps", "tone": _safe_tone(raw.get("tone")), "items": cleaned}
    nodes = raw.get("nodes")
    if isinstance(nodes, list):
        cleaned = [_clean_text(item, 70) for item in nodes[:7]]
        cleaned = [item for item in cleaned if item]
        if cleaned:
            return {"kind": "relation", "tone": _safe_tone(raw.get("tone")), "nodes": cleaned}
    return None


def _safe_tone(value: Any) -> str:
    return value if value in ALLOWED_TONES else "chalk"


def _extract_narration(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    board = payload.get("blackboard") if isinstance(payload.get("blackboard"), dict) else payload
    segments = board.get("segments") if isinstance(board, dict) else None
    if not isinstance(segments, list):
        return ""
    return "".join(
        _clean_text(item.get("narration") or item.get("speech") or item.get("text"), 280)
        for item in segments[:MAX_SEGMENTS]
        if isinstance(item, dict)
    ).strip()


def build_degraded_presentation(
    answer: str,
    subject: str,
    hint_level: int,
    degraded_reason: str = "local_fallback",
) -> dict[str, Any]:
    clean = re.sub(r"\s+", " ", answer).strip()
    sentences = [part.strip() for part in re.split(r"(?<=[。？！；])", clean) if part.strip()]
    if not sentences and clean:
        sentences = [clean]
    segments: list[dict[str, Any]] = []
    for index, sentence in enumerate(sentences[:MAX_SEGMENTS]):
        kind = "question" if sentence.endswith(("？", "?")) or "检查题" in sentence else "text"
        text = sentence
        if kind == "text" and len(text) > 72:
            text = f"{text[:70].rstrip()}…"
        segments.append(
            {
                "segment_id": f"segment-{index + 1}",
                "narration": sentence,
                "duration_hint_ms": _duration_hint(sentence),
                "blocks": [{"kind": kind, "text": text, "tone": "yellow" if kind == "question" else "chalk"}],
            }
        )
    if not segments:
        segments = [{
            "segment_id": "segment-1",
            "narration": "我们先把题目条件看清楚。",
            "duration_hint_ms": 2200,
            "blocks": [{"kind": "text", "text": "先读清条件和问题", "tone": "chalk"}],
        }]
    return {
        "version": PRESENTATION_VERSION,
        "source": "local_degraded",
        "degraded_reason": degraded_reason,
        "sync_mode": "segment_estimate",
        "subject": _clean_text(subject, 40),
        "title": f"第{hint_level}级讲解",
        "segments": segments,
    }


def presentation_visible_text(presentation: dict[str, Any]) -> str:
    parts: list[str] = []
    for segment in presentation.get("segments", []):
        for block in segment.get("blocks", []):
            parts.extend(_block_text_values(block))
    return " ".join(part for part in parts if part)


def _validate_presentation(payload: Any, subject: str, hint_level: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("blackboard"), dict):
        raise InvalidTutorPresentationError("blackboard object required")
    board = payload["blackboard"]
    title = _clean_text(board.get("title"), 60)
    raw_segments = board.get("segments")
    if not title or not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= MAX_SEGMENTS:
        raise InvalidTutorPresentationError("invalid title or segments")
    segments: list[dict[str, Any]] = []
    for index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise InvalidTutorPresentationError("invalid segment")
        narration = _clean_text(raw_segment.get("narration"), 280)
        raw_blocks = raw_segment.get("blocks")
        if not narration or not isinstance(raw_blocks, list) or not 1 <= len(raw_blocks) <= MAX_BLOCKS_PER_SEGMENT:
            raise InvalidTutorPresentationError("segment narration and blocks required")
        blocks = [_validate_block(item) for item in raw_blocks]
        segments.append({
            "segment_id": f"segment-{index + 1}",
            "narration": narration,
            "duration_hint_ms": _duration_hint(narration),
            "blocks": blocks,
        })
    return {
        "version": PRESENTATION_VERSION,
        "source": "model_structured",
        "sync_mode": "segment_estimate",
        "subject": _clean_text(subject, 40),
        "title": title,
        "segments": segments,
    }


def _validate_block(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in ALLOWED_KINDS:
        raise InvalidTutorPresentationError("unknown block kind")
    kind = raw["kind"]
    tone = raw.get("tone", "chalk")
    if tone not in ALLOWED_TONES:
        tone = "chalk"
    result: dict[str, Any] = {"kind": kind, "tone": tone}
    if kind in {"heading", "text", "question"}:
        text = _clean_text(raw.get("text"), 180)
        if not text:
            raise InvalidTutorPresentationError("text block is empty")
        result["text"] = text
    elif kind == "formula":
        latex = _clean_latex(raw.get("latex"))
        if not latex:
            raise InvalidTutorPresentationError("formula is empty")
        result["latex"] = latex
        label = _clean_text(raw.get("text"), 100)
        if label:
            result["text"] = label
    elif kind == "steps":
        result["items"] = _string_list(raw.get("items"), 8, 120)
    elif kind == "table":
        columns = _string_list(raw.get("columns"), 5, 40)
        rows_raw = raw.get("rows")
        if not isinstance(rows_raw, list) or not 1 <= len(rows_raw) <= 8:
            raise InvalidTutorPresentationError("table rows required")
        rows = [_string_list(row, len(columns), 60, exact=True) for row in rows_raw]
        result.update({"columns": columns, "rows": rows})
    elif kind == "relation":
        result["nodes"] = _string_list(raw.get("nodes"), 7, 70)
    return result


def _string_list(value: Any, maximum: int, item_limit: int, exact: bool = False) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise InvalidTutorPresentationError("invalid list")
    items = [_clean_text(item, item_limit) for item in value]
    if any(not item for item in items) or (exact and len(items) != maximum):
        raise InvalidTutorPresentationError("invalid list item")
    return items


def _block_text_values(block: dict[str, Any]) -> list[str]:
    values = [str(block.get("text") or ""), str(block.get("latex") or "")]
    values.extend(str(item) for item in block.get("items", []))
    values.extend(str(item) for item in block.get("columns", []))
    for row in block.get("rows", []):
        values.extend(str(item) for item in row)
    values.extend(str(item) for item in block.get("nodes", []))
    return values


def _duration_hint(text: str) -> int:
    punctuation = sum(text.count(mark) for mark in "，。？！；：")
    return max(1400, min(16000, len(text) * 235 + punctuation * 180))


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _clean_latex(value: Any) -> str:
    text = _clean_text(value, 240)
    if not text or any(token in text.lower() for token in ("<", ">", "\\html", "\\href", "\\includegraphics", "\\def", "\\newcommand")):
        return ""
    return text.replace("$", "")


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()
