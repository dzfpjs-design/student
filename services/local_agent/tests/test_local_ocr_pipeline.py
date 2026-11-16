from __future__ import annotations

from pathlib import Path
import json
import multiprocessing
import os
import socket
import sys
import time
from threading import Thread
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from home_study_agent.local_ocr import LocalOcrService
from home_study_agent.local_ocr.formula_recognition import FormulaPrediction
from home_study_agent.local_ocr.service import (
    OCR_PIPELINE_VERSION, RapidOcrEngine,
    _layout_formula_candidate_regions, _layout_formula_diagnostics,
    _marked_question_candidates, _page_orientation_evidence,
    _page_orientation_rank, _segment_questions,
)
from home_study_agent.vision_pipeline import FrameStabilityGate, capture_eligibility


class FakeEngine:
    name = "fake-local"
    capability = {"printed_zh", "digits"}

    def health(self) -> dict:
        return {"ready": True, "route": "cpu"}

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        return [
            {"text": "1. 计算125×32", "confidence": .95, "box": [[0, 0], [100, 0], [100, 20], [0, 20]], "kind": "printed"},
            {"text": "2. 写出过程", "confidence": .90, "box": [[0, 30], [100, 30], [100, 50], [0, 50]], "kind": "printed"},
        ]


class OfflineEngine:
    name = "offline"
    capability = set()

    def health(self) -> dict:
        return {"ready": False, "reason": "model_missing"}

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        raise AssertionError("must not be called")


class BrokenGpuEngine(FakeEngine):
    name = "broken-gpu"

    def health(self) -> dict:
        return {"ready": True, "routes": ["gpu", "cpu"]}

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        raise RuntimeError("gpu_worker_failed")


class OrientationAwareEngine(FakeEngine):
    name = "orientation-aware-test"

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        upright = float(image[35:115, 35:115].mean()) < 210
        confidence = .96 if upright else .34
        return [
            {"text": "1. Choose the right answer", "confidence": confidence, "box": [[80, 80], [500, 80], [500, 120], [80, 120]], "kind": "printed"},
            {"text": "2. This is the marked wrong question", "confidence": confidence, "box": [[80, 210], [560, 210], [560, 260], [80, 260]], "kind": "printed"},
        ]

    def recognize_page_orientation(self, image: np.ndarray, route: str) -> list[dict]:
        return self.recognize(image, route)


class MirrorAwareEngine(FakeEngine):
    name = "mirror-aware-test"

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        y, x = max(12, height // 4), max(12, width // 4)
        ink = gray < 90
        corners = (
            int(ink[:y, :x].sum()), int(ink[:y, -x:].sum()),
            int(ink[-y:, -x:].sum()), int(ink[-y:, :x].sum()),
        )
        upright = corners[0] > corners[1] * 1.25 > corners[2] * 1.25 > corners[3] * 1.25
        confidence = .97 if upright else .28
        return [
            {"text": "1. Mirror corrected question", "confidence": confidence,
             "box": [[80, 140], [520, 140], [520, 175], [80, 175]], "kind": "printed"},
            {"text": "2. Second complete question", "confidence": confidence,
             "box": [[80, 230], [520, 230], [520, 265], [80, 265]], "kind": "printed"},
        ]

    def recognize_page_orientation(self, image: np.ndarray, route: str) -> list[dict]:
        return self.recognize(image, route)


def orientation_lines(characters: int, confidence: float, *, horizontal: bool) -> list[dict]:
    text = "数" * characters
    width, height = (max(30, characters * 8), 24) if horizontal else (24, max(30, characters * 8))
    return [{
        "text": text,
        "confidence": confidence,
        "box": [[0, 0], [width, 0], [width, height], [0, height]],
        "kind": "printed",
    }]


def orientation_candidate(degrees: int, characters: int, confidence: float, *, horizontal: bool, engine: object):
    lines = orientation_lines(characters, confidence, horizontal=horizontal)
    score = confidence + min(characters, 800) / 20_000 + 1 / 4_000
    blank = np.zeros((100, 100, 3), dtype=np.uint8)
    return (score, degrees, engine, "cpu", lines, [], blank, blank)


class FakeFormulaProvider:
    name = "fake-formula-cpu"

    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def health(self) -> dict:
        return {"ready": True, "cpu_only": True, "implicit_network_download": False}

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        self.payloads.append(image_bytes)
        return FormulaPrediction(latex="125\\times32", latency_seconds=.012, provider=self.name)


class UnavailableFormulaProvider:
    name = "missing-formula-cpu"

    def health(self) -> dict:
        return {"ready": False, "reason": "model_manifest_missing", "cpu_only": True, "implicit_network_download": False}

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        raise AssertionError("unavailable provider must not run")


class SpawnFixtureFormulaProvider:
    name = "fixture-formula-worker"

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def health(self) -> dict:
        return {"ready": True, "cpu_only": True, "implicit_network_download": False}

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        raise AssertionError("parent process must never run this provider")

    def worker_spec(self) -> dict:
        return {"kind": "local_factory", "factory_key": "test_support", "kwargs": {"mode": self.mode}}


def image_bytes(width: int = 400, height: int = 520, blur: bool = False) -> bytes:
    image = np.full((height, width, 3), 220, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (width - 20, height - 20), (30, 30, 30), 3)
    cv2.putText(image, "homework 1", (45, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    if blur:
        image = cv2.GaussianBlur(image, (31, 31), 0)
    ok, payload = cv2.imencode(".png", image)
    assert ok
    return payload.tobytes()


def upside_down_marked_page_bytes() -> bytes:
    upright = np.full((420, 640, 3), 245, dtype=np.uint8)
    cv2.rectangle(upright, (55, 55), (95, 95), (0, 0, 0), -1)
    cv2.putText(upright, "1. Choose the right answer", (80, 110), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
    cv2.putText(upright, "2. This is the wrong question", (80, 245), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
    cv2.line(upright, (500, 195), (575, 275), (0, 0, 230), 8)
    cv2.line(upright, (575, 195), (500, 275), (0, 0, 230), 8)
    upside_down = cv2.rotate(upright, cv2.ROTATE_180)
    ok, payload = cv2.imencode(".png", upside_down)
    assert ok
    return payload.tobytes()


def horizontally_mirrored_page_bytes() -> bytes:
    upright = np.full((420, 640, 3), 245, dtype=np.uint8)
    for x1, y1, size in ((30, 30, 70), (560, 30, 48), (575, 345, 32), (30, 370, 14)):
        cv2.rectangle(upright, (x1, y1), (x1 + size, y1 + size), (0, 0, 0), -1)
    mirrored = cv2.flip(upright, 1)
    ok, payload = cv2.imencode(".png", mirrored)
    assert ok
    return payload.tobytes()


def warm_light_marked_page_bytes() -> bytes:
    upright = np.full((420, 640, 3), (185, 215, 238), dtype=np.uint8)
    cv2.rectangle(upright, (55, 55), (95, 95), (0, 0, 0), -1)
    cv2.putText(upright, "1. Choose the right answer", (80, 110), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
    cv2.putText(upright, "2. This is the wrong question", (80, 245), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
    # A warm-lit dark-red stroke lands around HSV hue 15-20, outside the
    # narrow pure-red range used by the first implementation.
    cv2.line(upright, (500, 195), (575, 275), (45, 70, 92), 8)
    cv2.line(upright, (575, 195), (500, 275), (45, 70, 92), 8)
    ok, payload = cv2.imencode(".png", upright)
    assert ok
    return payload.tobytes()


def test_local_ocr_queue_result_cache_restart_and_manual_limits(tmp_path: Path) -> None:
    path = tmp_path / "ocr.db"
    service = LocalOcrService(path, [FakeEngine()])
    payload = image_bytes()
    job = service.enqueue(payload, "request-1")
    done = service.run(job["job_id"], payload)
    assert done["status"] == "completed"
    assert len(done["result"]["question_blocks"]) == 2
    assert done["result"]["privacy"]["raw_image_uploaded"] is False
    assert "not inferred" in done["result"]["manual_review"]["handwriting"]
    cached = LocalOcrService(path, [FakeEngine()]).enqueue(payload, "request-2")
    assert cached["status"] == "completed"
    assert cached["result"]["engine"] == "fake-local"


def test_local_ocr_rotates_upside_down_pages_and_links_red_mark_to_question(tmp_path: Path) -> None:
    payload = upside_down_marked_page_bytes()
    service = LocalOcrService(tmp_path / "orientation.db", [OrientationAwareEngine()])

    result = service.run(service.enqueue(payload, "upside-down-marked")['job_id'], payload)["result"]

    assert result["quality"]["transforms"]["orientation_degrees"] == 180
    assert result["quality"]["transforms"]["orientation_auto_corrected"] is True
    assert result["wrong_question_candidates"]
    assert result["wrong_question_candidates"][0]["text"].startswith("2.")
    assert result["wrong_question_candidates"][0]["evidence"] == "red_correction_mark_near_question"
    assert result["wrong_question_candidates"][0]["correctness"] == "unverified"
    assert result["wrong_question_candidates"][0]["confidence"] < .72
    assert result["wrong_question_candidates"][0]["auto_accept_allowed"] is False
    assert result["proven_wrong_questions"] == []
    assert result["manual_review"]["required"] is True


def test_local_ocr_detects_driver_mirror_and_preserves_frame_geometry(tmp_path: Path) -> None:
    payload = horizontally_mirrored_page_bytes()
    service = LocalOcrService(tmp_path / "mirror.db", [MirrorAwareEngine()])

    result = service.run(service.enqueue(payload, "mirrored-page")['job_id'], payload)["result"]

    assert result["quality"]["transforms"]["orientation_degrees"] == 0
    assert result["quality"]["transforms"]["mirror_horizontal"] is True
    assert result["quality"]["transforms"]["mirror_auto_corrected"] is True
    assert result["quality"]["orientation_evidence"]["ambiguous"] is False
    assert result["text_geometry"]["mirror_horizontal"] is True
    assert len(result["question_blocks"]) == 2


def test_orientation_ranking_does_not_prefer_tiny_high_confidence_fragment_over_full_page() -> None:
    engine = OrientationAwareEngine()
    candidates = [
        orientation_candidate(0, 452, .59, horizontal=False, engine=engine),
        orientation_candidate(180, 27, .68, horizontal=False, engine=engine),
        orientation_candidate(90, 27, .70, horizontal=True, engine=engine),
        orientation_candidate(270, 536, .59, horizontal=True, engine=engine),
    ]

    best = max(candidates, key=_page_orientation_rank)
    evidence = _page_orientation_evidence(candidates, best)

    assert best[1] == 270
    assert evidence["ambiguous"] is False
    assert evidence["selected_text_characters"] == 536


def test_orientation_evidence_blocks_auto_rotation_when_opposite_direction_is_equally_credible() -> None:
    engine = OrientationAwareEngine()
    candidates = [
        orientation_candidate(0, 480, .78, horizontal=True, engine=engine),
        orientation_candidate(180, 450, .77, horizontal=True, engine=engine),
        orientation_candidate(90, 60, .62, horizontal=True, engine=engine),
        orientation_candidate(270, 55, .61, horizontal=True, engine=engine),
    ]

    best = max(candidates, key=_page_orientation_rank)
    evidence = _page_orientation_evidence(candidates, best)

    assert best[1] == 0
    assert evidence["ambiguous"] is True
    assert "orientation_direction_unverified" in evidence["review_reasons"]


def test_local_ocr_links_warm_light_red_ink_without_marking_every_nearby_question(tmp_path: Path) -> None:
    payload = warm_light_marked_page_bytes()
    service = LocalOcrService(tmp_path / "warm-mark.db", [OrientationAwareEngine()])

    result = service.run(service.enqueue(payload, "warm-light-marked")['job_id'], payload)["result"]

    assert [item["text"] for item in result["wrong_question_candidates"]] == [
        "2. This is the marked wrong question"
    ]


def test_question_segmentation_handles_answer_marks_and_chinese_section_headers() -> None:
    def line(text: str, top: int) -> dict:
        return {"text": text, "confidence": .95, "box": [[20, top], [500, top], [500, top + 20], [20, top + 20]]}

    blocks = _segment_questions([
        line("5. What colour is it?", 0),
        line("九、从II栏中选出对应答语。", 30),
        line("( B) 1. What do you like?", 60),
        line("A. I am sorry.", 90),
        line("(C)2. Would you like a banana?", 120),
        line("B. No, thank you.", 150),
        line("() 3. Where is my pencil case?", 180),
        line("C. It is under the book.", 210),
    ], 640)

    assert [block["text"].splitlines()[0] for block in blocks] == [
        "5. What colour is it?",
        "九、从II栏中选出对应答语。",
    ]
    assert blocks[1]["block_kind"] == "matching_exercise"
    assert blocks[1]["line_count"] == 7
    assert blocks[1]["needs_manual_boundary_check"] is True
    assert "C. It is under the book." in blocks[1]["text"]


def test_double_page_split_keeps_wide_left_page_options_with_their_question() -> None:
    def line(text: str, left: int, right: int, top: int) -> dict:
        return {"text": text, "confidence": .95, "box": [[left, top], [right, top], [right, top + 20], [left, top + 20]]}

    blocks = _segment_questions([
        line("4. 选出划线部分读音不同的一项", 210, 580, 100),
        line("A. grape", 230, 320, 130),
        line("B. make", 385, 470, 132),
        line("C. like", 510, 590, 134),
        line("5. Choose the answer", 220, 500, 170),
        line("十三、阅读理解", 700, 900, 90),
        line("The panda is black and white.", 690, 930, 125),
    ], 954)

    target = next(block for block in blocks if block["text"].startswith("4."))
    assert target["text"].splitlines() == [
        "4. 选出划线部分读音不同的一项", "A. grape", "B. make", "C. like"
    ]


def ocr_line(text: str, left: int, right: int, top: int, slope: float = 0.0) -> dict:
    return {"text": text, "confidence": .98, "box": [
        [left, round(top + slope * left)], [right, round(top + slope * right)],
        [right, round(top + 24 + slope * right)], [left, round(top + 24 + slope * left)],
    ]}


def test_skewed_spread_keeps_options_and_response_on_their_own_page_and_question() -> None:
    lines = [
        ocr_line("1. Is that your rubber?", 40, 340, 100, .12),
        ocr_line("Yes, ___.", 600, 760, 100, .12),
        ocr_line("A. it is", 40, 190, 134, .12),
        ocr_line("B. it's", 330, 460, 134, .12),
        ocr_line("C. this is", 600, 760, 134, .12),
        ocr_line("2. Choose the answer.", 40, 700, 174, .12),
        ocr_line("A. am late", 40, 190, 209, .12),
        ocr_line("B. is late", 330, 460, 209, .12),
        ocr_line("C. be late", 600, 760, 209, .12),
        ocr_line("1. Other page question.", 900, 1170, 65, -.08),
        ocr_line("A. Other page choice", 900, 1180, 100, -.08),
    ]
    blocks = _segment_questions(list(reversed(lines)), 1200, 800)
    assert len(blocks) == 3
    assert blocks[0]["text"].splitlines() == [line["text"] for line in lines[:5]]
    assert blocks[1]["text"].splitlines() == [line["text"] for line in lines[5:9]]
    assert [block["page_index"] for block in blocks] == [0, 0, 1]


def test_internal_matching_columns_do_not_become_neighboring_pages() -> None:
    lines = [ocr_line("九、从II栏中选出对应答语。", 40, 760, 60)]
    for index in range(1, 4):
        lines += [ocr_line(f"{index}. What do you like?", 40, 330, 100 + index * 40),
                  ocr_line(f"{chr(64 + index)}. A shared response.", 520, 760, 100 + index * 40)]
    lines += [ocr_line("1. Other page.", 850, 1180, 90), ocr_line("A. Other option", 850, 1180, 130)]
    blocks = _segment_questions(lines, 1200, 800)
    assert len(blocks) == 2
    assert blocks[0]["block_kind"] == "matching_exercise"
    assert blocks[0]["line_count"] == 7
    assert "Other" not in blocks[0]["text"]
    assert blocks[1]["page_index"] == 1


def test_no_midpoint_fallback_bisects_a_single_wide_question() -> None:
    lines = [ocr_line("1. Choose the answer.", 40, 1000, 60),
             ocr_line("A. one", 40, 180, 100), ocr_line("B. two", 450, 590, 100),
             ocr_line("C. three", 850, 1000, 100)]
    blocks = _segment_questions(lines, 1200, 600)
    assert len(blocks) == 1
    assert blocks[0]["line_count"] == 4


def test_reverse_tilt_does_not_sort_a_wide_next_question_above_previous_options() -> None:
    lines = [ocr_line("1. Choose the answer.", 40, 340, 150, -.12),
             ocr_line("A. one", 40, 190, 184, -.12),
             ocr_line("B. two", 330, 460, 184, -.12),
             ocr_line("C. three", 600, 760, 184, -.12),
             ocr_line("2. Next question.", 40, 700, 224, -.12)]
    blocks = _segment_questions(lines, 1200, 800)
    assert blocks[0]["text"].splitlines() == [line["text"] for line in lines[:4]]
    assert blocks[1]["text"] == lines[4]["text"]


def test_diagonal_gutter_separates_pages_with_overlapping_horizontal_envelopes() -> None:
    def tilted(text: str, left: int, right: int, top: int) -> dict:
        line = ocr_line(text, left, right, top)
        line["box"] = [[round(x - .2 * y), round(y + .2 * x)] for x, y in line["box"]]
        return line

    lines = [
        tilted("八、选择方框中的答语。", 80, 440, 70),
        tilted("1. Is it a ruler? No.", 80, 300, 180),
        tilted("Mike: (5)", 510, 660, 180),
        tilted("Here you are.", 510, 740, 210),
        tilted("The other page continues.", 510, 880, 650),
    ]
    blocks = _segment_questions(lines, 1000, 900)
    assert {block["page_index"] for block in blocks} == {0, 1}
    target = next(block for block in blocks if "ruler" in block["text"])
    assert target["text"] == "1. Is it a ruler? No."
    assert target["page_index"] == 0
    assert target["needs_manual_boundary_check"] is True
    assert target["page_boundary"]["x_per_y"] == pytest.approx(-.2, abs=.01)
    assert all("Mike" not in block["text"] for block in blocks if block["page_index"] == 0)


def test_unresolved_multi_lane_layout_never_produces_a_trusted_question() -> None:
    blocks = _segment_questions([
        ocr_line("1. Is it a ruler? No.", 50, 280, 100),
        ocr_line("2. Other question.", 50, 280, 150),
        ocr_line("Mike: (5)", 710, 950, 100),
        ocr_line("Here you are.", 710, 950, 150),
        ocr_line("OCR merged text spanning both pages", 50, 950, 200),
    ], 1000, 700)
    assert blocks
    assert all(block["needs_manual_boundary_check"] for block in blocks)
    assert all("page_layout_unresolved" in block["boundary_review_reasons"] for block in blocks)


def test_lost_option_letter_does_not_drop_shared_section_context() -> None:
    blocks = _segment_questions([
        ocr_line("八、选择方框中的答语。", 50, 570, 50),
        ocr_line("B. How come?", 50, 220, 90),
        ocr_line("B. Guess again", 50, 220, 130),
        ocr_line("1. Is it a ruler? No.", 50, 400, 170),
    ], 640, 480)
    target = blocks[-1]
    assert "方框" in target["context_text"]
    assert "B. Guess again" in target["context_text"]
    assert target["needs_manual_boundary_check"] is True


def test_high_confidence_isolated_question_near_clipped_edge_requires_review() -> None:
    block = _segment_questions([ocr_line("1. Is it a ruler? No.", 4, 232, 151)], 1920, 1080)[0]
    assert block["confidence"] == .98
    assert "text_clipped_at_frame_edge" in block["boundary_review_reasons"]
    assert block["needs_manual_boundary_check"] is True


def test_low_confidence_frame_cannot_promote_one_high_confidence_block(tmp_path: Path) -> None:
    class MixedConfidenceEngine(FakeEngine):
        def recognize(self, image: np.ndarray, route: str) -> list[dict]:
            lines = [ocr_line("1. Choose the answer.", 50, 300, 80),
                     ocr_line("2. Garbled question.", 50, 300, 130)]
            lines[1]["confidence"] = .5
            return lines

    service = LocalOcrService(tmp_path / "low-frame.db", [MixedConfidenceEngine()])
    payload = image_bytes()
    result = service.run(service.enqueue(payload)["job_id"], payload)["result"]
    assert result["confidence"] < .78
    assert all(block["needs_manual_boundary_check"] for block in result["question_blocks"])
    assert "low_frame_ocr_confidence" in result["question_blocks"][0]["boundary_review_reasons"]
    assert result["wrong_question_candidates"] == []
    assert result["manual_review"]["auto_accept_allowed"] is False


@pytest.mark.parametrize('clipped', [False, True])
def test_unnumbered_manuscript_is_readable_but_not_an_automatic_question(tmp_path, clipped):
    from home_study_agent.mistake_capture import MistakeCaptureService, InvalidMistakeCaptureCandidateError

    class ManuscriptEngine(FakeEngine):
        def recognize_page_orientation(self, image, route):
            self.calls = getattr(self, 'calls', 0) + 1
            lines = [ocr_line('Dear Alex, I went to the park last Sunday.', 0 if clipped else 60, 320, 100),
                     ocr_line('Would you like to visit the park with me?', 60, 320, 160)]
            # This fixture isolates clipping after a decisive global direction;
            # legacy/tied engine responses have their own review-gate tests.
            if self.calls > 1:
                for line in lines:
                    line['confidence'] = .3
            return lines

    service = LocalOcrService(tmp_path / 'manuscript.db', [ManuscriptEngine()])
    payload = image_bytes()
    result = service.run(service.enqueue(payload)['job_id'], payload)['result']
    assert result['confidence'] == .98
    assert result['quality']['passed'] is (not clipped)
    assert result['manual_review']['required'] is True
    assert 'missing_question_start' in result['manual_review']['reasons']
    assert all(block['needs_manual_boundary_check'] for block in result['question_blocks'])
    assert result['wrong_question_candidates'] == []
    assert result['manual_review']['auto_accept_allowed'] is False
    with pytest.raises(InvalidMistakeCaptureCandidateError):
        MistakeCaptureService._segment({'details_json': json.dumps({'local_pipeline': result})})


def test_repeated_option_bank_is_orphaned_instead_of_appended_to_previous_question() -> None:
    blocks = _segment_questions([
        ocr_line("5. Choose the answer.", 40, 550, 60),
        ocr_line("A. First choice", 40, 250, 100),
        ocr_line("B. Second choice", 40, 250, 140),
        ocr_line("A. Missing next question", 40, 250, 180),
        ocr_line("B. Orphan choice", 40, 250, 220),
    ], 640, 480)
    assert len(blocks) == 2
    assert blocks[0]["line_count"] == 3
    assert blocks[1]["block_kind"] == "fragment"
    assert blocks[1]["needs_manual_boundary_check"] is True


@pytest.mark.parametrize("text,top,reason", [
    ("the classroom.", 80, "missing_question_start"),
    ("2.", 80, "malformed_question_start"),
    ("() 5., What colour is it?", 80, "malformed_question_start"),
    ("2. Choose the right answer.", 0, "text_clipped_at_frame_edge"),
])
def test_fragments_and_malformed_questions_require_review_even_with_clear_cross(text: str, top: int, reason: str) -> None:
    blocks = _segment_questions([ocr_line(text, 60, 400, top)], 640, 480)
    frame = np.full((480, 640, 3), 245, dtype=np.uint8)
    mark_y = max(5, top - 10)
    cv2.line(frame, (390, mark_y), (430, mark_y + 45), (0, 0, 230), 4)
    cv2.line(frame, (430, mark_y), (390, mark_y + 45), (0, 0, 230), 4)
    candidates = _marked_question_candidates(frame, blocks)
    assert candidates
    assert reason in candidates[0]["boundary_review_reasons"]
    assert candidates[0]["status"] == "recapture_or_review_required"
    assert candidates[0]["confidence"] < .72
    assert candidates[0]["auto_accept_allowed"] is False


@pytest.mark.parametrize("box", [None, [], [[1]], [[float("nan"), 0]], "invalid"])
def test_invalid_line_geometry_is_reviewable_not_a_trusted_boundary(box) -> None:
    blocks = _segment_questions([{"text": "1. Choose the answer.", "confidence": .99, "box": box}], 640, 480)
    assert "missing_line_geometry" in blocks[0]["boundary_review_reasons"]
    assert blocks[0]["auto_accept_allowed"] is False


@pytest.mark.parametrize("kind", ["tick", "underline", "circle", "cross"])
def test_red_mark_shape_is_not_a_correctness_judgment(tmp_path: Path, kind: str) -> None:
    frame = np.full((420, 640, 3), 245, dtype=np.uint8)
    cv2.rectangle(frame, (55, 55), (95, 95), (0, 0, 0), -1)
    ink = (0, 0, 230)
    if kind == "tick":
        cv2.polylines(frame, [np.array([[470, 230], [490, 253], [562, 188]])], False, ink, 5)
    elif kind == "underline":
        cv2.line(frame, (470, 265), (570, 265), ink, 5)
    elif kind == "circle":
        cv2.circle(frame, (525, 230), 30, ink, 5)
    else:
        cv2.line(frame, (500, 200), (560, 260), ink, 5)
        cv2.line(frame, (560, 200), (500, 260), ink, 5)
    payload = cv2.imencode(".png", frame)[1].tobytes()
    service = LocalOcrService(tmp_path / "marks.db", [OrientationAwareEngine()])
    result = service.run(service.enqueue(payload)["job_id"], payload)["result"]
    assert result["correction_mark_candidates"]
    assert result["manual_review"]["correction_mark_candidates"] == result["correction_mark_candidates"]
    assert result["proven_wrong_questions"] == []
    assert result["manual_review"]["required"] is True
    assert result["manual_review"]["auto_accept_allowed"] is False
    if kind == "cross":
        assert len(result["wrong_question_candidates"]) == 1
        assert result["wrong_question_candidates"][0]["mark_kind"] == "cross_like"
    else:
        assert result["wrong_question_candidates"] == []


def test_correction_stroke_cannot_jump_page_gutter() -> None:
    blocks = _segment_questions([
        ocr_line("1. Left page question.", 80, 290, 100),
        ocr_line("A. Left option", 80, 290, 130),
        ocr_line("1. Right page question.", 330, 560, 100),
        ocr_line("A. Right option", 330, 560, 130),
    ], 640, 480)
    frame = np.full((480, 640, 3), 245, dtype=np.uint8)
    cv2.line(frame, (300, 100), (323, 130), (0, 0, 230), 3)
    assert _marked_question_candidates(frame, blocks) == []


@pytest.mark.parametrize("legacy_version", [None, 2])
def test_new_requests_do_not_reuse_pre_fix_segmentation_cache(tmp_path: Path, legacy_version) -> None:
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()])
    payload = image_bytes()
    first = service.run(service.enqueue(payload, "before")["job_id"], payload)
    legacy = dict(first["result"])
    legacy.pop("pipeline_version")
    if legacy_version is not None:
        legacy["pipeline_version"] = legacy_version
    legacy["wrong_question_candidates"] = [{"text": "the classroom.", "confidence": .99}]
    with service._connect() as db:
        db.execute("UPDATE ocr_cache SET result_json = ?", (json.dumps(legacy),))
        db.commit()
    restarted = LocalOcrService(service.path, [FakeEngine()])
    queued = restarted.enqueue(payload, "after")
    assert queued["status"] == "queued"
    fresh = restarted.run(queued["job_id"], payload)["result"]
    assert fresh["pipeline_version"] == OCR_PIPELINE_VERSION
    assert fresh["wrong_question_candidates"] == []
    assert restarted.enqueue(payload, "current-cache")["result"] == fresh


@pytest.mark.skipif(not os.environ.get("LOCAL_OCR_DOCCAM_FRAME"), reason="opt-in real local document-camera frame")
def test_actual_upside_down_english_spread_locally(tmp_path: Path, monkeypatch) -> None:
    network_attempts = []

    def no_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError("Real-frame OCR must not use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    engine = RapidOcrEngine()
    assert engine.health()["ready"], "Install verified local OCR assets before the real-frame test"
    payload = Path(os.environ["LOCAL_OCR_DOCCAM_FRAME"]).read_bytes()
    assert cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (1080, 1920)
    service = LocalOcrService(tmp_path / "real-frame.db", [engine])
    done = service.run(service.enqueue(payload)["job_id"], payload)
    assert done["status"] == "completed", done.get("error_code")
    result = done["result"]
    assert result["quality"]["transforms"]["orientation_degrees"] == 180
    blocks = result["question_blocks"]
    assert {block["page_index"] for block in blocks} == {0, 1}
    for block in blocks:
        x, _, width, _ = block["bbox_xywh"]
        assert x + width < 1030 if block["page_index"] == 0 else x > 1030
    target = next(block for block in blocks if "4. 选出下列单词" in block["text"])
    assert target["text"].splitlines()[1:] == ["A. grape", "B. make", "C. like"]
    assert target["needs_manual_boundary_check"] is False
    assert target["boundary_review_reasons"] == []
    matching = [block for block in blocks if block["block_kind"] == "matching_exercise"]
    assert len(matching) == 1
    assert "E. It's under the book." in matching[0]["text"]
    assert "2It was red just now." not in matching[0]["text"]
    fragment = next(block for block in blocks if block["text"] == "the classroom.")
    assert fragment["block_kind"] == "fragment"
    assert fragment["needs_manual_boundary_check"] is True
    assert result["correction_mark_candidates"]
    assert result["wrong_question_candidates"] == []
    assert result["proven_wrong_questions"] == []
    assert result["manual_review"]["required"] is True
    assert result["privacy"]["raw_image_uploaded"] is False
    restored = LocalOcrService(service.path, [engine]).get(done["job_id"])["result"]
    assert restored == result
    assert network_attempts == []


@pytest.mark.skipif(not os.environ.get("LOCAL_OCR_CLIPPED_FRAME"), reason="opt-in clipped static frame, no live capture")
def test_clipped_real_spread_never_combines_ruler_with_other_page_dialogue(tmp_path: Path, monkeypatch) -> None:
    network_attempts = []

    def no_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError("Static-frame OCR must stay local")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    payload = Path(os.environ["LOCAL_OCR_CLIPPED_FRAME"]).read_bytes()
    service = LocalOcrService.default(tmp_path / "clipped-frame.db")
    done = service.run(service.enqueue(payload)["job_id"], payload)
    assert done["status"] == "completed", done.get("error_code")
    result = done["result"]
    assert result["quality"]["transforms"]["orientation_degrees"] == 180
    blocks = result["question_blocks"]
    assert {block["page_index"] for block in blocks} == {0, 1}
    ruler = next(block for block in blocks if "ruler" in block["text"])
    assert ruler["text"] == "1. —Is it a ruler? —No."
    assert ruler["page_index"] == 0
    assert ruler["needs_manual_boundary_check"] is True
    assert all("Mike:" not in block["text"] for block in blocks if block["page_index"] == 0)
    assert all("ruler" not in block["text"] for block in blocks if block["page_index"] == 1)
    assert all(block["needs_manual_boundary_check"] for block in blocks)
    assert not any("grape" in block["text"] for block in blocks)
    assert result["wrong_question_candidates"] == []
    assert result["proven_wrong_questions"] == []
    assert result["manual_review"]["required"] is True
    assert result["pipeline_version"] == 3
    assert LocalOcrService.default(service.path).get(done["job_id"])["result"] == result
    assert network_attempts == []


def test_local_ocr_cancel_unavailable_and_quality_warnings(tmp_path: Path) -> None:
    payload = image_bytes(900, 400, blur=True)
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()])
    job = service.enqueue(payload)
    assert service.cancel(job["job_id"])["status"] == "cancelled"
    assert service.run(job["job_id"], payload)["status"] == "cancelled"
    unavailable = LocalOcrService(tmp_path / "unavailable.db", [OfflineEngine()])
    failed = unavailable.run(unavailable.enqueue(payload)["job_id"], payload)
    assert failed["status"] == "failed"
    result = service.run(service.enqueue(payload, "blur-page")["job_id"], payload)["result"]
    assert "possible_double_page_manual_split_required" in result["quality"]["issues"]
    assert result["manual_review"]["required"] is True


def test_formula_provider_is_routed_only_to_symbol_regions_and_never_auto_applied(tmp_path: Path) -> None:
    provider = SpawnFixtureFormulaProvider("success")
    payload = image_bytes()
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()], formula_providers=[provider])

    result = service.run(service.enqueue(payload)["job_id"], payload)["result"]

    assert result["lines"][0]["text"] == "1. 计算125×32"  # printed OCR remains untouched
    region = result["formula_routing"]["regions"][0]
    assert region["candidate_latex"] == "x=2"
    assert region["auto_applied"] is False
    assert region["status"] == "manual_edit_required"
    assert result["formula_routing"]["auto_accept_enabled"] is False
    assert result["formula_routing"]["resource_observation"]["route"] == "cpu"
    assert region["latency_seconds"] == .001
    assert "formula_candidate_requires_manual_edit" in result["manual_review"]["reasons"]
    assert result["manual_review"]["required"] is True


def test_missing_formula_provider_keeps_formula_region_in_manual_edit_fallback(tmp_path: Path) -> None:
    payload = image_bytes()
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()], formula_providers=[UnavailableFormulaProvider()])

    result = service.run(service.enqueue(payload)["job_id"], payload)["result"]

    region = result["formula_routing"]["regions"][0]
    assert region["status"] == "manual_edit_required"
    assert region["reason"] == "formula_provider_unavailable_or_failed_manual_edit_required"
    assert region["attempts"] == [{"provider": "missing-formula-cpu", "status": "unavailable", "reason": "model_manifest_missing"}]


def _no_new_formula_workers(before: set[int]) -> bool:
    return not {child.pid for child in multiprocessing.active_children() if child.pid not in before}


def test_blocked_formula_provider_times_out_without_leaking_spawn_worker(tmp_path: Path) -> None:
    payload = image_bytes()
    before = {child.pid for child in multiprocessing.active_children()}
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()], formula_providers=[SpawnFixtureFormulaProvider("block")], formula_timeout_seconds=.1)

    result = service.run(service.enqueue(payload)["job_id"], payload)["result"]

    attempt = result["formula_routing"]["regions"][0]["attempts"][0]
    assert attempt["status"] == "timed_out"
    assert result["formula_routing"]["regions"][0].get("candidate_latex") is None
    assert _no_new_formula_workers(before)


def test_formula_worker_crash_degrades_to_manual_edit_without_candidate(tmp_path: Path) -> None:
    payload = image_bytes()
    before = {child.pid for child in multiprocessing.active_children()}
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()], formula_providers=[SpawnFixtureFormulaProvider("crash")], formula_timeout_seconds=.5)

    result = service.run(service.enqueue(payload)["job_id"], payload)["result"]

    attempt = result["formula_routing"]["regions"][0]["attempts"][0]
    assert attempt["status"] == "failed"
    assert result["formula_routing"]["regions"][0].get("candidate_latex") is None
    assert _no_new_formula_workers(before)


def test_formula_worker_launch_failure_keeps_printed_ocr_and_manual_formula_review(
    tmp_path: Path, monkeypatch,
) -> None:
    payload = image_bytes()
    service = LocalOcrService(
        tmp_path / "ocr.db",
        [FakeEngine()],
        formula_providers=[SpawnFixtureFormulaProvider("success")],
    )

    def unavailable_start(_process):
        raise OSError("synthetic process resource exhaustion")

    monkeypatch.setattr("multiprocessing.process.BaseProcess.start", unavailable_start)
    receipt = service.run(service.enqueue(payload)["job_id"], payload)

    assert receipt["status"] == "completed"
    result = receipt["result"]
    assert result["lines"][0]["text"] == "1. 计算125×32"
    attempt = result["formula_routing"]["regions"][0]["attempts"][0]
    assert attempt == {
        "provider": "fixture-formula-worker",
        "status": "failed",
        "reason": "OSError",
    }
    assert result["formula_routing"]["regions"][0]["status"] == "manual_edit_required"
    assert result["formula_routing"]["regions"][0].get("candidate_latex") is None


def test_formula_worker_memory_limit_terminates_child_before_candidate_write(tmp_path: Path) -> None:
    payload = image_bytes()
    before = {child.pid for child in multiprocessing.active_children()}
    service = LocalOcrService(
        tmp_path / "ocr.db", [FakeEngine()], formula_providers=[SpawnFixtureFormulaProvider("block")],
        formula_timeout_seconds=1, formula_max_rss_bytes=1,
    )

    result = service.run(service.enqueue(payload)["job_id"], payload)["result"]

    attempt = result["formula_routing"]["regions"][0]["attempts"][0]
    assert attempt["status"] == "memory_limit"
    assert result["formula_routing"]["regions"][0].get("candidate_latex") is None
    assert _no_new_formula_workers(before)


def test_formula_provider_cooldown_prevents_repeated_blocked_workers(tmp_path: Path) -> None:
    provider = SpawnFixtureFormulaProvider("block")
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()], formula_providers=[provider], formula_timeout_seconds=.05)
    for offset, request in enumerate(("first", "second")):
        payload = image_bytes(width=400 + offset)
        service.run(service.enqueue(payload, request)["job_id"], payload)
    third_payload = image_bytes(width=402)
    third = service.run(service.enqueue(third_payload, "third")["job_id"], third_payload)["result"]

    assert third["formula_routing"]["regions"][0]["attempts"][0]["status"] == "cooling_down"
    assert not multiprocessing.active_children()


def test_cancelling_formula_worker_stores_no_candidate_or_cached_result(tmp_path: Path) -> None:
    payload = image_bytes()
    service = LocalOcrService(tmp_path / "ocr.db", [FakeEngine()], formula_providers=[SpawnFixtureFormulaProvider("block")], formula_timeout_seconds=5)
    job = service.enqueue(payload, "cancel-formula")
    holder: dict[str, dict] = {}
    thread = Thread(target=lambda: holder.setdefault("result", service.run(job["job_id"], payload)))
    thread.start()
    time.sleep(.15)
    cancelled = service.cancel(job["job_id"])
    thread.join(3)

    assert cancelled["status"] == "cancelled"
    assert holder["result"]["status"] == "cancelled"
    assert service.enqueue(payload, "after-cancel")["status"] == "queued"
    assert not multiprocessing.active_children()


def test_layout_formula_candidates_are_diagnostic_only_not_provider_or_review_eligible(tmp_path: Path) -> None:
    page = np.full((260, 640, 3), 255, dtype=np.uint8)
    cv2.putText(page, "x", (70, 170), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 0), 4)
    cv2.putText(page, "2", (128, 105), cv2.FONT_HERSHEY_SIMPLEX, .9, (0, 0, 0), 2)
    cv2.putText(page, "+ 3 = 7", (165, 170), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 4)

    diagnostic = _layout_formula_diagnostics(page, [])
    candidates = diagnostic["regions"]

    assert candidates
    assert all(item["detection"] == "layout_math_candidate_unverified" for item in candidates)
    assert diagnostic["diagnostic_only"] is True
    assert diagnostic["unverified"] is True
    assert diagnostic["provider_eligible"] is False
    assert diagnostic["review_eligible"] is False
    assert diagnostic["affects_ocr_text"] is False
    assert diagnostic["affects_mistake_or_knowledge"] is False

    service = LocalOcrService(
        tmp_path / "formula-layout-no-provider.db",
        [FakeEngine()],
        formula_providers=[SpawnFixtureFormulaProvider("block")],
    )
    routed = service._route_formula_regions(page, [], lambda: False)
    assert routed["regions"] == []
    assert routed["manual_review_reasons"] == []
    assert routed["layout_diagnostics"]["candidate_count"] == len(candidates)


def test_layout_diagnostics_do_not_route_non_formula_pages_to_provider_or_review(tmp_path: Path) -> None:
    pages = {
        "blank": np.full((260, 640, 3), 255, dtype=np.uint8),
        "ordinary_chinese_paragraph": np.full((260, 640, 3), 255, dtype=np.uint8),
        "table": np.full((260, 640, 3), 255, dtype=np.uint8),
        "geometry": np.full((260, 640, 3), 255, dtype=np.uint8),
        "red_marking": np.full((260, 640, 3), 255, dtype=np.uint8),
        "handwriting": np.full((260, 640, 3), 255, dtype=np.uint8),
    }
    for y in (50, 90, 130, 170):
        cv2.line(pages["ordinary_chinese_paragraph"], (30, y), (590, y), (0, 0, 0), 3)
    for x in (40, 180, 320, 460, 600):
        cv2.line(pages["table"], (x, 30), (x, 220), (0, 0, 0), 2)
    for y in (30, 90, 150, 220):
        cv2.line(pages["table"], (40, y), (600, y), (0, 0, 0), 2)
    cv2.polylines(pages["geometry"], [np.array([[90, 210], [320, 40], [560, 210]], dtype=np.int32)], True, (0, 0, 0), 3)
    cv2.line(pages["red_marking"], (250, 80), (390, 190), (0, 0, 220), 7)
    cv2.line(pages["red_marking"], (390, 80), (250, 190), (0, 0, 220), 7)
    for offset in range(0, 140, 28):
        cv2.line(pages["handwriting"], (60 + offset, 180), (90 + offset, 100 + (offset % 35)), (0, 0, 0), 3)

    service = LocalOcrService(
        tmp_path / "formula-layout-page-types.db",
        [FakeEngine()],
        formula_providers=[SpawnFixtureFormulaProvider("block")],
    )
    ordinary_lines = [{"text": "阅读理解段落", "confidence": .95, "box": [[0, 0], [600, 0], [600, 20], [0, 20]]}]
    for page in pages.values():
        routed = service._route_formula_regions(page, ordinary_lines, lambda: False)
        assert routed["regions"] == []
        assert routed["manual_review_reasons"] == []
        assert routed["layout_diagnostics"]["diagnostic_only"] is True
        assert routed["layout_diagnostics"]["provider_eligible"] is False
        assert routed["layout_diagnostics"]["review_eligible"] is False
        assert routed["layout_diagnostics"]["affects_ocr_text"] is False
        assert routed["layout_diagnostics"]["affects_mistake_or_knowledge"] is False


def test_layout_diagnostics_cap_candidates_and_downscale_oversized_page() -> None:
    page = np.full((260, 1800, 3), 255, dtype=np.uint8)
    for x in range(20, 1700, 110):
        cv2.putText(page, "x2+3", (x, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
    diagnostic = _layout_formula_diagnostics(page, [])
    assert diagnostic["candidate_count"] <= diagnostic["candidate_limit"] == 8
    assert all(item["x2"] - item["x1"] >= diagnostic["min_width"] for item in diagnostic["regions"])
    oversized = _layout_formula_diagnostics(np.full((1500, 1500, 3), 255, dtype=np.uint8), [])
    assert oversized["status"] == "completed"
    assert oversized["resized_to_budget"] is True
    assert oversized["analysis_scale"] < 1




def test_rapidocr_missing_packaged_models_is_reported_without_starting_engine(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "rapidocr" / "__init__.py"
    package.parent.mkdir()
    package.write_text("", encoding="utf-8")
    monkeypatch.setattr("home_study_agent.local_ocr.service.find_spec", lambda _: SimpleNamespace(origin=str(package)))
    engine = RapidOcrEngine()

    health = engine.health()

    assert health["ready"] is False
    assert health["reason"] == "rapidocr_models_missing"
    assert engine._engine is None


def test_rapidocr_uses_explicit_existing_model_paths_without_default_download_route(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "rapidocr" / "__init__.py"
    model_dir = package.parent / "models"
    model_dir.mkdir(parents=True)
    package.write_text("", encoding="utf-8")
    for name in RapidOcrEngine.required_model_files:
        (model_dir / name).write_bytes(b"local-model")
    captured: dict[str, object] = {}
    classifier_calls: list[bool] = []

    class FakeRapidOCR:
        def __init__(self, *, params: dict) -> None:
            captured.update(params)

        def __call__(self, image: np.ndarray, *, use_cls: bool):
            classifier_calls.append(use_cls)
            return SimpleNamespace(txts=[], scores=[], boxes=[])

    monkeypatch.setattr("home_study_agent.local_ocr.service.find_spec", lambda _: SimpleNamespace(origin=str(package)))
    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=FakeRapidOCR))
    engine = RapidOcrEngine()

    assert engine.recognize(np.zeros((100, 100, 3), dtype=np.uint8), "cpu") == []
    assert engine.recognize_page_orientation(np.zeros((100, 100, 3), dtype=np.uint8), "cpu") == []
    assert engine.recognize(np.zeros((100, 100, 3), dtype=np.uint8), "cpu") == []
    assert classifier_calls == [True, False, True]
    assert captured == {
        "Global.model_root_dir": str(model_dir),
        "Det.model_path": str(model_dir / "PP-OCRv6_det_small.onnx"),
        "Det.limit_side_len": 1280,
        "Cls.model_path": str(model_dir / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
        "Rec.model_path": str(model_dir / "PP-OCRv6_rec_small.onnx"),
    }
    assert engine.health()["implicit_network_download"] is False


def test_local_ocr_falls_back_from_preferred_route_engine(tmp_path: Path) -> None:
    payload = image_bytes()
    service = LocalOcrService(tmp_path / "ocr.db", [BrokenGpuEngine(), FakeEngine()])
    done = service.run(service.enqueue(payload, preferred_route="gpu")["job_id"], payload)
    assert done["status"] == "completed"
    assert done["result"]["engine"] == "fake-local"
    assert done["result"]["engine_attempts"][0]["status"] == "failed"


def test_local_ocr_releases_database_file_handles_between_operations(tmp_path: Path) -> None:
    database_path = tmp_path / "ocr.db"
    service = LocalOcrService(database_path, [FakeEngine()])
    service.enqueue(image_bytes())

    moved_path = tmp_path / "ocr-moved.db"
    database_path.replace(moved_path)
    moved_path.replace(database_path)


def test_local_ocr_quarantines_timeout_then_falls_back_without_waiting(tmp_path: Path) -> None:
    slow_completed = multiprocessing.Event()

    class ObservableSlowEngine(FakeEngine):
        name = "stalled-cpu"

        def recognize(self, image: np.ndarray, route: str) -> list[dict]:
            time.sleep(.5)
            slow_completed.set()
            return super().recognize(image, route)

    payload = image_bytes()
    service = LocalOcrService(tmp_path / "ocr.db", [ObservableSlowEngine(), FakeEngine()], timeout_seconds=.01)
    started = time.monotonic()
    done = service.run(service.enqueue(payload)["job_id"], payload)
    elapsed = time.monotonic() - started
    assert done["status"] == "completed"
    assert done["result"]["engine"] == "fake-local"
    assert done["result"]["engine_attempts"][0]["status"] == "timed_out"
    assert not slow_completed.is_set()
    assert elapsed < .4
    assert next(item for item in service.health()["engines"] if item["name"] == "stalled-cpu")["quarantined"] is True


def test_stability_gate_never_opens_a_device() -> None:
    gate = FrameStabilityGate(required_stable_frames=2)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    assert gate.observe(frame)["ready"] is False
    gate.observe(frame)
    assert gate.observe(frame)["ready"] is True
    assert capture_eligibility(document_camera_lease_id=None, primary_task_active=False)["code"] == "document_camera_lease_required"
    assert capture_eligibility(document_camera_lease_id="lease-1", primary_task_active=True)["code"] == "device_busy"
    assert capture_eligibility(document_camera_lease_id="lease-1", primary_task_active=False)["allowed"] is True
