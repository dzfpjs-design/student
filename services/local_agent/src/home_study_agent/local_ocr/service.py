from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from importlib.util import find_spec
import json
from pathlib import Path
import re
import sqlite3
from threading import BoundedSemaphore
from time import monotonic
from typing import Protocol
from uuid import uuid4

import cv2
import numpy as np

from .resource_budget import CpuResourceQueue
from .formula_recognition import FormulaRecognitionProvider
from .formula_worker import run_formula_provider_isolated
from .cache_lifecycle import (
    OcrActionConflict, OcrSourceInvalid, assert_current, ensure_schema, reject_deleted,
)


FORMULA_SYMBOLS = frozenset("=+-×÷*/^√()[]{}<>≤≥≠≈∑∫π")
FORMULA_REGION_PADDING = 12
LAYOUT_FORMULA_MIN_WIDTH = 100
LAYOUT_FORMULA_MIN_HEIGHT = 20
LAYOUT_FORMULA_MAX_CANDIDATES_PER_PAGE = 8
LAYOUT_FORMULA_MAX_DIAGNOSTIC_PIXELS = 2_000_000
FORMULA_PROVIDER_TIMEOUT_SECONDS = 5.0
FORMULA_PROVIDER_MAX_RSS_BYTES = 1_500_000_000
FORMULA_PROVIDER_MAX_CONCURRENCY = 1
FORMULA_PROVIDER_FAILURE_LIMIT = 2
FORMULA_PROVIDER_COOLDOWN_SECONDS = 60.0
# The audited MFR-1.5 baseline reaches only 2.6% strict MathWriting matches.
# No locally configured provider is presently eligible to auto-apply a LaTeX
# prediction; every formula candidate remains an editable review item.
FORMULA_AUTO_ACCEPT_ENABLED = False
OCR_PIPELINE_VERSION = 9


class OcrEngineUnavailable(RuntimeError):
    pass


class OcrJobCancelled(RuntimeError):
    pass


class OcrJobBusy(RuntimeError):
    pass


class OcrEngine(Protocol):
    name: str
    capability: set[str]

    def health(self) -> dict: ...
    def recognize(self, image: np.ndarray, route: str) -> list[dict]: ...


class RapidOcrEngine:
    name = "rapidocr-onnx"
    capability = {"printed_zh", "printed_en", "digits"}
    required_model_files = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx")

    def __init__(self) -> None:
        self._engine = None

    def health(self) -> dict:
        model_dir = self._model_directory()
        if model_dir is None:
            return {"ready": False, "reason": "rapidocr_not_installed", "capability": sorted(self.capability), "implicit_network_download": False}
        missing = [name for name in self.required_model_files if not (model_dir / name).is_file()]
        if missing:
            return {"ready": False, "reason": "rapidocr_models_missing", "missing_models": missing,
                    "route": "cpu", "capability": sorted(self.capability), "implicit_network_download": False}
        return {"ready": True, "route": "cpu", "capability": sorted(self.capability), "model_asset_count": len(self.required_model_files),
                "implicit_network_download": False}

    @staticmethod
    def _model_directory() -> Path | None:
        spec = find_spec("rapidocr")
        return Path(spec.origin).resolve().parent / "models" if spec and spec.origin else None

    def recognize(self, image: np.ndarray, route: str) -> list[dict]:
        return self._recognize(image, use_cls=True)

    def recognize_page_orientation(self, image: np.ndarray, route: str) -> list[dict]:
        # Crop-level 180 correction hides a wrong full-page trial and reverses
        # paragraph order. Direct capture keeps its existing crop correction.
        return self._recognize(image, use_cls=False)

    def _recognize(self, image: np.ndarray, *, use_cls: bool) -> list[dict]:
        if self._engine is None:
            health = self.health()
            if not health["ready"]:
                raise OcrEngineUnavailable(str(health["reason"]))
            try:
                from rapidocr import RapidOCR
                # RapidOCR defaults to a downloader whenever a task's
                # ``model_path`` is absent.  Supplying each verified local
                # path keeps model acquisition outside the child workflow:
                # a missing/tampered asset becomes a health failure instead
                # of an unannounced network request.
                model_dir = self._model_directory()
                if model_dir is None:
                    raise OcrEngineUnavailable("rapidocr_not_installed")
                self._engine = RapidOCR(params={
                    "Global.model_root_dir": str(model_dir),
                    "Det.model_path": str(model_dir / "PP-OCRv6_det_small.onnx"),
                    # The default 736px short side loses small print in the
                    # document camera's negotiated 1280x720 stream. The target
                    # PC has enough CPU/RAM for a 1280px detector short side;
                    # the existing 2000px whole-image guard still caps memory.
                    "Det.limit_side_len": 1280,
                    "Cls.model_path": str(model_dir / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
                    "Rec.model_path": str(model_dir / "PP-OCRv6_rec_small.onnx"),
                })
            except ImportError as error:
                raise OcrEngineUnavailable("RapidOCR is not installed locally") from error
        result = self._engine(image, use_cls=use_cls)
        texts = list(result.txts) if result.txts is not None else []
        scores = list(result.scores) if result.scores is not None else []
        boxes = list(result.boxes) if result.boxes is not None else []
        return [{"text": str(text), "confidence": float(scores[i]) if i < len(scores) else 0.0,
                 "box": np.asarray(boxes[i]).round().astype(int).tolist() if i < len(boxes) else None,
                 "kind": "printed"} for i, text in enumerate(texts)]


@dataclass
class LocalOcrService:
    """Durable local OCR jobs with cache, fallback, cancellation, and audit data.

    Images are passed in memory. This store writes only hash, quality metadata,
    derived text/regions, and errors; raw page bytes remain outside this module.
    """

    path: Path
    engines: list[OcrEngine]
    timeout_seconds: float = 12.0
    formula_providers: list[FormulaRecognitionProvider] = field(default_factory=list)
    formula_timeout_seconds: float = FORMULA_PROVIDER_TIMEOUT_SECONDS
    formula_max_rss_bytes: int = FORMULA_PROVIDER_MAX_RSS_BYTES

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._quarantined_engines: set[str] = set()
        self._formula_semaphore = BoundedSemaphore(FORMULA_PROVIDER_MAX_CONCURRENCY)
        self._formula_failure_count: dict[str, int] = {}
        self._formula_cooldown_until: dict[str, float] = {}

    @classmethod
    def default(cls, path: Path, formula_provider: FormulaRecognitionProvider | None = None) -> "LocalOcrService":
        return cls(path, [RapidOcrEngine()], formula_providers=[formula_provider] if formula_provider else [])

    def health(self) -> dict:
        return {"local_only": True, "engines": [{"name": engine.name, **engine.health(), "quarantined": engine.name in self._quarantined_engines} for engine in self.engines],
                "formula_providers": [{"name": provider.name, **provider.health()} for provider in self.formula_providers],
                "handwriting": "manual_correction_required_without_local_handwriting_model",
                "math": "formula_regions_are_always_manual_edit_until_a_future_provider_passes_acceptance"}

    def bind_database(self, database) -> None:
        # Production bodies share the authoritative privacy/restore transaction.
        # The previous standalone file is not imported or trusted as owned data.
        self.path = database.path
        self.database = database
        self._initialize()

    def enqueue(self, image_bytes: bytes, request_id: str | None = None, preferred_route: str = "cpu", *, owner: dict | None = None) -> dict:
        image_hash = sha256(image_bytes).hexdigest()
        job_id = str(uuid4())
        request_id = request_id or job_id
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            reject_deleted(db, request_id)
            if owner is None:
                owner = dict(child_id=None, session_id=None, parent_artifact_id=None,
                             owner_key='isolated', owner_epoch=None, monitor_generation=None)
            old = db.execute("SELECT * FROM ocr_job WHERE request_id = ?", (request_id,)).fetchone()
            if old:
                if old['image_hash'] != image_hash or old['owner_key'] != owner['owner_key'] or old['preferred_route'] != preferred_route:
                    raise OcrActionConflict('OCR_ACTION_CONFLICT')
                if old['status'] == 'quarantined':
                    raise OcrSourceInvalid(old['error_code'] or 'OCR_LEGACY_QUARANTINED')
                assert_current(db, old)
                if old['pipeline_version'] != OCR_PIPELINE_VERSION:
                    raise OcrSourceInvalid('OCR_PIPELINE_VERSION_INVALIDATED')
                return self._job_dict(old)
            assert_current(db, {**owner, 'image_hash': image_hash})
            cached = db.execute("SELECT result_json FROM ocr_cache WHERE owner_key=? AND image_hash = ?", (owner['owner_key'], image_hash)).fetchone()
            if cached and json.loads(cached["result_json"]).get("pipeline_version") != OCR_PIPELINE_VERSION:
                cached = None
            db.execute("""INSERT INTO ocr_job(job_id,request_id,image_hash,preferred_route,status,created_at,
                child_id,session_id,parent_artifact_id,owner_key,owner_epoch,monitor_generation,pipeline_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (job_id, request_id, image_hash, preferred_route,
                'completed' if cached else 'queued', _utcnow(), owner['child_id'], owner['session_id'],
                owner['parent_artifact_id'], owner['owner_key'], owner['owner_epoch'], owner['monitor_generation'], OCR_PIPELINE_VERSION))
            if cached:
                db.execute("UPDATE ocr_job SET result_json = ?, completed_at = ? WHERE job_id = ?", (cached["result_json"], _utcnow(), job_id))
            db.commit()
            return self._job_dict(db.execute("SELECT * FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone())

    def run(self, job_id: str, image_bytes: bytes) -> dict:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone()
            if not job:
                raise KeyError("Unknown OCR job")
            if sha256(image_bytes).hexdigest() != job["image_hash"]:
                raise OcrActionConflict('OCR_IMAGE_CONFLICT')
            reject_deleted(db, job['request_id'])
            if job['status'] == 'quarantined':
                raise OcrSourceInvalid(job['error_code'] or 'OCR_QUARANTINED')
            assert_current(db, job)
            if job['pipeline_version'] != OCR_PIPELINE_VERSION:
                raise OcrSourceInvalid('OCR_PIPELINE_VERSION_INVALIDATED')
            if job["status"] in {"completed", "cancelled"}:
                return self._job_dict(job)
            if job['status'] == 'running':
                from datetime import datetime, timezone
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(job['started_at'])).total_seconds()
                if age < max(60, self.timeout_seconds * 4):
                    raise OcrJobBusy('OCR_ALREADY_RUNNING')
            token = str(uuid4())
            db.execute("UPDATE ocr_job SET status = 'running', started_at = ?,run_token=?,error_code=NULL WHERE job_id = ?", (_utcnow(), token, job_id))
            db.commit()
        try:
            result = self._process(image_bytes, job["preferred_route"], cancelled=lambda: self._is_cancelled(job_id, token))
        except Exception as error:  # result is durable and retryable; never exposes raw bytes
            with self._connect() as db:
                current = db.execute("SELECT status,run_token FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone()
                if current and current['run_token'] == token and current['status'] == 'running':
                    db.execute("UPDATE ocr_job SET status = 'failed', error_code = ?, completed_at = ?,run_token=NULL WHERE job_id = ?", (type(error).__name__, _utcnow(), job_id))
                    db.commit()
            return self.get(job_id) if current else self._invalidated_result(job)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT * FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone()
            if not current or current['run_token'] != token or current['status'] != 'running':
                return self._invalidated_result(job)
            try:
                assert_current(db, current)
            except OcrSourceInvalid:
                db.execute("UPDATE ocr_job SET status='cancelled',run_token=NULL,error_code='OCR_SOURCE_INVALIDATED' WHERE job_id=?", (job_id,))
                db.commit()
                return self._invalidated_result(job)
            serialized = json.dumps(result, ensure_ascii=False)
            db.execute("UPDATE ocr_job SET status = 'completed', result_json = ?, completed_at = ?,run_token=NULL WHERE job_id = ?", (serialized, _utcnow(), job_id))
            db.execute("INSERT OR REPLACE INTO ocr_cache VALUES (?, ?, ?, ?)", (current['owner_key'], job['image_hash'], serialized, _utcnow()))
            db.commit()
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT status FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Unknown OCR job")
            if row["status"] in {"queued", "running"}:
                db.execute("UPDATE ocr_job SET status = 'cancelled', completed_at = ?,run_token=NULL WHERE job_id = ?", (_utcnow(), job_id))
                db.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Unknown OCR job")
            try:
                reject_deleted(db, row['request_id'])
                assert_current(db, row)
            except OcrSourceInvalid:
                return self._invalidated_result(row)
            return self._job_dict(row)

    @staticmethod
    def _invalidated_result(row):
        return {**dict(row), 'status': 'cancelled', 'result_json': None, 'result': None,
                'error_code': 'OCR_SOURCE_INVALIDATED', 'progress': 1}

    def _process(self, image_bytes: bytes, preferred_route: str, cancelled=lambda: False) -> dict:
        if len(image_bytes) < 32:
            raise ValueError("empty_or_incomplete_image")
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None or min(image.shape[:2]) < 80:
            raise ValueError("undecodable_or_too_small_image")
        admission = CpuResourceQueue.validate_ocr_input(image.shape[1], image.shape[0], "single_frame")
        if not admission["accepted"]:
            raise ValueError(admission["code"])
        prepared, quality, prepared_color = _prepare_page(image)
        orientation_trials: list[dict] = []
        recognized: list[tuple[float, int, OcrEngine, str, list[dict], list[dict], np.ndarray, np.ndarray, bool]] = []
        for degrees in (0, 180):
            oriented = _rotate_page(prepared, degrees)
            engine, route, candidate_lines, attempts = self._recognize_with_fallback(oriented, preferred_route, cancelled=cancelled)
            if engine is not None:
                score = _recognition_score(candidate_lines)
                recognized.append((score, degrees, engine, route, candidate_lines, attempts, oriented,
                                   _rotate_page(prepared_color, degrees), False))
                orientation_trials.append(_orientation_trial(degrees, candidate_lines, score, engine, False))
        if not recognized:
            raise OcrEngineUnavailable("No local OCR engine is healthy")
        best = max(recognized, key=_page_orientation_rank)
        global_trials = any(callable(getattr(item[2], 'recognize_page_orientation', None)) for item in recognized)
        if global_trials or _orientation_needs_sideways_trials(best[4]):
            for degrees in (90, 270):
                oriented = _rotate_page(prepared, degrees)
                engine, route, candidate_lines, attempts = self._recognize_with_fallback(oriented, preferred_route, cancelled=cancelled)
                if engine is not None:
                    score = _recognition_score(candidate_lines)
                    recognized.append((score, degrees, engine, route, candidate_lines, attempts, oriented,
                                       _rotate_page(prepared_color, degrees), False))
                    orientation_trials.append(_orientation_trial(degrees, candidate_lines, score, engine, False))
            best = max(recognized, key=_page_orientation_rank)
        if _mirror_needs_trials(best[4]):
            mirrored = cv2.flip(prepared, 1)
            mirrored_color = cv2.flip(prepared_color, 1)
            for degrees in (0, 90, 180, 270):
                oriented = _rotate_page(mirrored, degrees)
                engine, route, candidate_lines, attempts = self._recognize_with_fallback(
                    oriented, preferred_route, cancelled=cancelled,
                )
                if engine is not None:
                    score = _recognition_score(candidate_lines)
                    recognized.append((score, degrees, engine, route, candidate_lines, attempts, oriented,
                                       _rotate_page(mirrored_color, degrees), True))
                    orientation_trials.append(_orientation_trial(degrees, candidate_lines, score, engine, True))
            best = max(recognized, key=_page_orientation_rank)
        quality['orientation_evidence'] = _page_orientation_evidence(recognized, best)
        if quality['orientation_evidence']['ambiguous']:
            quality['issues'].extend(quality['orientation_evidence']['review_reasons'])
            quality['issues'].append('orientation_ambiguous')
            quality['passed'] = False
        _, orientation_degrees, engine, route, lines, attempts, prepared, prepared_color, mirror_horizontal = best
        if prepared_color.shape[1] / max(1, prepared_color.shape[0]) > 1.55:
            quality['issues'].append('possible_double_page_manual_split_required')
            quality['passed'] = False
        orientation_ambiguous = quality['orientation_evidence']['ambiguous']
        quality["transforms"]["orientation_degrees"] = orientation_degrees
        quality["transforms"]["mirror_horizontal"] = mirror_horizontal
        quality["transforms"]["orientation_auto_corrected"] = (orientation_degrees != 0 or mirror_horizontal) and not orientation_ambiguous
        quality["transforms"]["mirror_auto_corrected"] = mirror_horizontal and not orientation_ambiguous
        quality["transforms"]["orientation"] = (
            f"candidate_{orientation_degrees}_degrees_manual_review"
            if orientation_ambiguous else f"auto_{orientation_degrees}_degrees"
        )
        quality["orientation_trials"] = orientation_trials
        from .text_geometry import build
        try:
            text_geometry = build(lines, frame_sha256=sha256(image_bytes).hexdigest(),
                frame_size=[image.shape[1],image.shape[0]],
                prepared_size=quality['transforms']['prepared_size'],
                frame_to_prepared=quality['transforms']['frame_to_prepared'],degrees=orientation_degrees,
                mirror_horizontal=mirror_horizontal)
        except (ValueError, TypeError, KeyError, np.linalg.LinAlgError):
            # Preserve OCR for local review, but never fabricate missing geometry.
            text_geometry = None
        if engine is None:
            raise OcrEngineUnavailable("No local OCR engine is healthy")
        confidence = round(sum(line["confidence"] for line in lines) / len(lines), 3) if lines else 0.0
        questions = _segment_questions(lines, prepared.shape[1], prepared.shape[0])
        if confidence < .78:
            for block in questions:
                block["boundary_review_reasons"].append("low_frame_ocr_confidence")
                block["needs_manual_boundary_check"] = True
        correction_candidates = _marked_question_candidates(prepared_color, questions, lines=lines)
        # Legacy consumers treat this list as suspected errors and use confidence
        # alone to bypass page-quality warnings. Never feed ticks or fragments to it.
        wrong_question_candidates = [item for item in correction_candidates
                                     if item["mark_kind"] == "cross_like" and not item["needs_manual_boundary_check"]]
        boundary_reasons = sorted({reason for block in questions for reason in block["boundary_review_reasons"]})
        # A missing exercise number does not make manuscript text unreadable.
        # Keep the fragment/manual boundary gate below; it still cannot become
        # an automatically accepted question or a proven mistake.
        if set(boundary_reasons) - {"missing_question_start"}:
            quality["issues"].append("question_boundary_review_required")
            quality["passed"] = False
        formula_routing = self._route_formula_regions(prepared, lines, cancelled)
        formula_manual_reasons = formula_routing["manual_review_reasons"]
        if formula_manual_reasons:
            quality['issues'].append('formula_review_required')
            quality['passed'] = False
        review_reasons = boundary_reasons + (["correction_marks_require_review"] if correction_candidates else [])
        return {"pipeline_version": OCR_PIPELINE_VERSION, "text_geometry": text_geometry,
                "engine": engine.name, "route": route, "engine_attempts": attempts, "quality": quality, "lines": lines,
                "question_blocks": questions, "wrong_question_candidates": wrong_question_candidates, "confidence": confidence,
                "correction_mark_candidates": correction_candidates,
                "proven_wrong_questions": [],
                "manual_review": {"required": not quality["passed"] or confidence < .78 or bool(formula_manual_reasons) or bool(review_reasons),
                                  "reasons": quality["issues"] + (["low_ocr_confidence"] if confidence < .78 else []) + formula_manual_reasons + review_reasons,
                                  "correction_mark_candidates": correction_candidates,
                                  "auto_accept_allowed": False,
                                  "handwriting": "not inferred; add/edit fields manually unless a local handwriting engine is installed",
                                  "math": "formula candidates require human formula editing; no current provider may auto-apply LaTex"},
                "formula_routing": formula_routing,
                "privacy": {"raw_image_uploaded": False, "stored": "derived local text, regions, metrics only"}}

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            ensure_schema(db)
            db.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict:
        output = dict(row)
        raw = output.pop('result_json')
        output["result"] = json.loads(raw) if raw and output['status'] != 'quarantined' else None
        output["progress"] = {"queued": 0, "running": 0.2, "completed": 1, "failed": 1, "cancelled": 1, 'quarantined': 1}[output["status"]]
        return output

    def _recognize_with_fallback(self, image: np.ndarray, preferred_route: str, *, cancelled=lambda: False) -> tuple[OcrEngine | None, str, list[dict], list[dict]]:
        if cancelled():
            raise OcrJobCancelled('ocr_job_cancelled')
        candidates: list[tuple[OcrEngine, str]] = []
        for engine in self.engines:
            health = engine.health()
            if not health.get("ready") or engine.name in self._quarantined_engines:
                continue
            routes = health.get("routes", [health.get("route", "cpu")])
            route = preferred_route if preferred_route in routes else "cpu" if "cpu" in routes else routes[0]
            candidates.append((engine, route))
        candidates.sort(key=lambda item: item[1] != preferred_route)
        attempts: list[dict] = []
        last_error: Exception | None = None
        for engine, route in candidates:
            if cancelled():
                raise OcrJobCancelled('ocr_job_cancelled')
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-ocr")
            try:
                recognize = getattr(engine, 'recognize_page_orientation', engine.recognize)
                lines = pool.submit(recognize, image, route).result(timeout=self.timeout_seconds)
                # One canonical list must feed text, scoring, segmentation and geometry.
                lines = [line for line in lines if str(line.get('text', '')).strip()]
                attempts.append({"engine": engine.name, "route": route, "status": "completed"})
                pool.shutdown(wait=True)
                if cancelled():
                    raise OcrJobCancelled('ocr_job_cancelled')
                return engine, route, lines, attempts
            except OcrJobCancelled:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            except TimeoutError:
                self._quarantined_engines.add(engine.name)
                pool.shutdown(wait=False, cancel_futures=True)
                last_error = TimeoutError("ocr_timeout")
                attempts.append({"engine": engine.name, "route": route, "status": "timed_out", "error": "TimeoutError"})
            except Exception as error:
                last_error = error
                pool.shutdown(wait=False, cancel_futures=True)
                attempts.append({"engine": engine.name, "route": route, "status": "failed", "error": type(error).__name__})
        if last_error:
            raise last_error
        return None, "cpu", [], attempts

    def _route_formula_regions(self, image: np.ndarray, lines: list[dict], cancelled) -> dict:
        regions = _formula_candidate_regions(image, lines)
        layout_diagnostics = _layout_formula_diagnostics(image, regions)
        provider_health = [{"name": provider.name, **provider.health()} for provider in self.formula_providers]
        routed: list[dict] = []
        for region in regions:
            crop = image[region["y1"]:region["y2"], region["x1"]:region["x2"]]
            encoded, payload = cv2.imencode(".png", crop)
            item = {
                "bbox_xywh": [region["x1"], region["y1"], region["x2"] - region["x1"], region["y2"] - region["y1"]],
                "detection": region["detection"],
                "source_text_confidence": region["confidence"],
                "provider_eligible": region["provider_eligible"],
                "review_eligible": region["review_eligible"],
                "status": "manual_edit_required",
                "auto_applied": False,
                "reason": "formula_auto_accept_disabled_due_to_unqualified_local_accuracy",
                "attempts": [],
            }
            if not encoded:
                item["reason"] = "formula_region_encode_failed_manual_edit_required"
                routed.append(item)
                continue
            if not region["provider_eligible"]:
                item["reason"] = "layout_candidate_requires_manual_formula_classification"
                routed.append(item)
                continue
            for provider in self.formula_providers:
                health = provider.health()
                if not health.get("ready"):
                    item["attempts"].append({"provider": provider.name, "status": "unavailable", "reason": health.get("reason", "provider_not_ready")})
                    continue
                cooldown_remaining = max(0.0, self._formula_cooldown_until.get(provider.name, 0.0) - monotonic())
                if cooldown_remaining:
                    item["attempts"].append({"provider": provider.name, "status": "cooling_down", "cooldown_seconds": round(cooldown_remaining, 1)})
                    continue
                worker_spec = getattr(provider, "worker_spec", None)
                if not callable(worker_spec):
                    item["attempts"].append({"provider": provider.name, "status": "unavailable", "reason": "provider_missing_spawn_worker_spec"})
                    continue
                if not self._formula_semaphore.acquire(blocking=False):
                    item["attempts"].append({"provider": provider.name, "status": "concurrency_limited"})
                    continue
                try:
                    outcome = run_formula_provider_isolated(
                        worker_spec(), payload.tobytes(), timeout_seconds=self.formula_timeout_seconds,
                        max_rss_bytes=self.formula_max_rss_bytes, cancelled=cancelled,
                    )
                finally:
                    self._formula_semaphore.release()
                status = str(outcome.get("status"))
                attempt = {"provider": provider.name, "status": status}
                if outcome.get("reason"):
                    attempt["reason"] = outcome["reason"]
                if outcome.get("peak_rss_bytes") is not None:
                    attempt["peak_rss_bytes"] = outcome["peak_rss_bytes"]
                item["attempts"].append(attempt)
                if status == "cancelled":
                    raise OcrJobCancelled("formula_provider_cancelled")
                if status == "completed":
                    item["candidate_latex"] = str(outcome.get("latex", ""))
                    item["provider"] = str(outcome.get("provider", provider.name))
                    item["latency_seconds"] = round(float(outcome.get("latency_seconds", 0.0)), 3)
                    self._formula_failure_count.pop(provider.name, None)
                    self._formula_cooldown_until.pop(provider.name, None)
                    break
                if status in {"timed_out", "memory_limit", "crashed", "failed"}:
                    self._record_formula_failure(provider.name)
            if not item["attempts"]:
                item["reason"] = "formula_provider_not_configured_manual_edit_required"
            elif "provider" not in item:
                item["reason"] = "formula_provider_unavailable_or_failed_manual_edit_required"
            routed.append(item)
        return {
            "cpu_only": True,
            "resource_observation": {
                "route": "cpu",
                "process_rss_bytes": _process_rss_bytes(),
                "boundary": "Process RSS includes the local OCR process; it is not a per-region allocation.",
            },
            "auto_accept_enabled": FORMULA_AUTO_ACCEPT_ENABLED,
            "providers": provider_health,
            "regions": routed,
            "layout_diagnostics": layout_diagnostics,
            "manual_review_reasons": ["formula_candidate_requires_manual_edit"] if routed else [],
        }

    def _record_formula_failure(self, provider_name: str) -> None:
        count = self._formula_failure_count.get(provider_name, 0) + 1
        self._formula_failure_count[provider_name] = count
        if count >= FORMULA_PROVIDER_FAILURE_LIMIT:
            self._formula_cooldown_until[provider_name] = monotonic() + FORMULA_PROVIDER_COOLDOWN_SECONDS

    def _is_cancelled(self, job_id: str, token: str | None = None) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT status,run_token FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone()
            return not row or row['status'] in {'cancelled', 'quarantined'} or token is not None and row['run_token'] != token


def _page_content_quality(gray: np.ndarray) -> tuple[list[str], dict]:
    """Pixel-only warning evidence, never an assertion that all text survived."""
    scale = min(1.0, 960 / max(gray.shape))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page = max(contours, key=cv2.contourArea, default=None)
    mask = np.zeros_like(gray)
    if page is not None and cv2.contourArea(page) >= gray.size * .3:
        cv2.drawContours(mask, [page], -1, 255, cv2.FILLED)
    else:
        mask[:] = 255
    # Exclude the paper/frame boundary from text contrast and sharpness evidence.
    mask = cv2.erode(mask, np.ones((9, 9), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
    contrast = cv2.subtract(background, gray)
    contrast[mask == 0] = 0
    ink = (contrast > 6).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    keep = (stats[:, cv2.CC_STAT_AREA] >= 4) & (stats[:, cv2.CC_STAT_HEIGHT] >= 3) & (stats[:, cv2.CC_STAT_WIDTH] >= 2)
    keep[0] = False
    ink = keep[labels].astype(bool)
    values = contrast[ink]
    ink_contrast = float(np.percentile(values, 90)) if values.size else 0.0
    metrics = {'content_quality_version': 1, 'analysis_pixels': int(gray.size),
               'ink_pixels': int(values.size), 'ink_contrast': round(ink_contrast, 1),
               'internal_bright_gaps': 0, 'aligned_bright_gaps': 0, 'faded_gap_edges': 0}
    if values.size < 12:
        return ['content_unverifiable'], metrics
    if ink_contrast < 40:
        return ['low_contrast'], metrics

    # Long INTERNAL holes in ink bands are unlike the large white margin of a
    # healthy page. Aligned holes across lines suggest local washout; a single
    # hole or full-height column is ambiguous and stays explicitly reviewable.
    occupied = np.flatnonzero(ink.sum(axis=1) >= 2)
    if occupied.size == 0:
        return ['content_unverifiable'], metrics
    bands = np.split(occupied, np.flatnonzero(np.diff(occupied) > 2) + 1)
    gaps = []
    faded_edges = 0
    band_count = 0
    for rows in bands:
        top, bottom = int(rows[0]), int(rows[-1]) + 1
        height = bottom - top
        if height < 4:
            continue
        band_count += 1
        columns = np.flatnonzero(ink[top:bottom].any(axis=0))
        for index in np.flatnonzero(np.diff(columns) > max(32, height * 2.5)):
            left, right = int(columns[index]) + 1, int(columns[index + 1])
            if float((gray[top:bottom, left:right] > 248).mean()) >= .9:
                gaps.append((band_count, left, right))
                rim = np.concatenate((contrast[top:bottom, max(0, left-12):left].ravel(),
                                      contrast[top:bottom, right:right+12].ravel()))
                rim = rim[rim > 6]
                if rim.size >= 12 and np.percentile(rim, 75) < ink_contrast * .35:
                    faded_edges += 1
                if len(gaps) >= 128:
                    metrics.update(internal_bright_gaps=len(gaps), gap_analysis_limited=True)
                    return ['content_integrity_uncertain'], metrics
    aligned = 0
    for band, left, right in gaps:
        matching = {other_band for other_band, other_left, other_right in gaps
                    if min(right, other_right) - max(left, other_left) >= min(right-left, other_right-other_left) * .5}
        aligned = max(aligned, len(matching))
    metrics.update(internal_bright_gaps=len(gaps), aligned_bright_gaps=aligned, faded_gap_edges=faded_edges)
    if faded_edges or 2 <= aligned < band_count:
        return ['glare'], metrics
    if gaps:
        return ['content_integrity_uncertain'], metrics
    return [], metrics


def _prepare_page(image: np.ndarray) -> tuple[np.ndarray, dict, np.ndarray]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    glare_ratio = float((gray > 248).mean())
    shadow_ratio = float((gray < 50).mean())
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    page = max(contours, key=cv2.contourArea, default=None)
    page_ratio = (cv2.contourArea(page) / (image.shape[0] * image.shape[1])) if page is not None else 0.0
    issues, content_metrics = _page_content_quality(gray)
    if sharpness < 18: issues.append("blurred")
    if brightness < 45: issues.append("too_dark")
    if shadow_ratio > .35: issues.append("heavy_shadow")
    if page_ratio < .30: issues.append("page_not_detected")
    working = image
    perspective_applied = False
    frame_to_prepared = np.eye(3)
    if page is not None and page_ratio >= .30:
        corners = cv2.approxPolyDP(page, .02 * cv2.arcLength(page, True), True)
        if len(corners) == 4:
            working, frame_to_prepared = _perspective_crop(image, corners.reshape(4, 2).astype("float32"), return_transform=True)
            perspective_applied = True
    normalized = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(cv2.cvtColor(working, cv2.COLOR_BGR2GRAY))
    return cv2.cvtColor(normalized, cv2.COLOR_GRAY2BGR), {"passed": not issues, "issues": issues,
            "metrics": {"sharpness": round(sharpness, 1), "brightness": round(brightness, 1), "glare_ratio": round(glare_ratio, 4), "shadow_ratio": round(shadow_ratio, 4), "page_ratio": round(page_ratio, 3), **content_metrics},
            "transforms": {"perspective_correction": perspective_applied, "orientation": "unverified_0_degrees",
                "frame_to_prepared": frame_to_prepared.tolist(), "prepared_size": [working.shape[1],working.shape[0]]},
            "stages": ["decode", "page_detection", "crop_and_perspective_correction", "contrast_shadow_normalization", "orientation_review", "layout_segmentation"]}, working


def _rotate_page(image: np.ndarray, degrees: int) -> np.ndarray:
    if degrees == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


def _recognition_score(lines: list[dict]) -> float:
    meaningful = [line for line in lines if str(line.get("text", "")).strip()]
    if not meaningful:
        return 0.0
    weights = [max(1, min(80, len(str(line.get("text", "")).strip()))) for line in meaningful]
    weighted_confidence = sum(float(line.get("confidence", 0.0)) * weight for line, weight in zip(meaningful, weights)) / sum(weights)
    character_count = sum(weights)
    return weighted_confidence + min(character_count, 800) / 20_000 + min(len(meaningful), 40) / 4_000


def _orientation_trial(degrees: int, lines: list[dict], score: float, engine=None,
                       mirror_horizontal: bool = False) -> dict:
    confidence = sum(float(line.get("confidence", 0.0)) for line in lines) / len(lines) if lines else 0.0
    return {
        "degrees": degrees,
        "mirror_horizontal": mirror_horizontal,
        "score": round(score, 4),
        "line_count": len(lines),
        "mean_confidence": round(confidence, 4),
        "text_character_count": sum(len(str(line.get("text", "")).strip()) for line in lines),
        "horizontal_text_fraction": round(_horizontal_text_fraction(lines), 4),
        "engine": engine.name if engine is not None else None,
        "global_orientation_contract": callable(getattr(engine, 'recognize_page_orientation', None)),
    }


def _horizontal_text_fraction(lines: list[dict]) -> float:
    total = horizontal = 0
    for line in lines:
        count = len(str(line.get('text', '')).strip())
        box = _line_bbox(line)
        if count < 3 or box is None:
            continue
        total += count
        if box[2] >= box[3] * 1.5:
            horizontal += count
    return horizontal / total if total else 0.0


def _page_orientation_rank(item) -> tuple:
    # RapidOCR also rotates tall crops by 90 before recognition. Original box
    # geometry distinguishes those sideways columns from upright text lines.
    # Confidence alone is unsafe: an isolated heading can score higher than a
    # correctly oriented full page. Text coverage therefore participates in
    # the rank but is capped so repeated OCR noise cannot dominate.
    return (_horizontal_text_fraction(item[4]) >= .7, _orientation_quality_score(item[4]), -abs(item[1]))


def _candidate_mirror(item) -> bool:
    return len(item) > 8 and item[8] is True


def _orientation_quality_score(lines: list[dict]) -> float:
    characters, confidence = _orientation_text_summary(lines)
    coverage = min(1.0, characters / 400.0)
    horizontal = _horizontal_text_fraction(lines)
    return confidence * .65 + coverage * .25 + horizontal * .10


def _orientation_text_summary(lines: list[dict]) -> tuple[int, float]:
    meaningful = [line for line in lines if str(line.get('text', '')).strip()]
    counts = [len(str(line['text']).strip()) for line in meaningful]
    weights = [min(80, count) for count in counts]
    confidence = sum(float(line.get('confidence', 0.0)) * weight
                     for line, weight in zip(meaningful, weights)) / max(1, sum(weights))
    return sum(counts), confidence

def _page_orientation_evidence(recognized, best) -> dict:
    supports_global = callable(getattr(best[2], 'recognize_page_orientation', None))
    same_engine = all(item[2] is best[2] for item in recognized)
    reasons = []
    if not supports_global or not same_engine:
        reasons.append('orientation_engine_unverified')
    selected_mirror = _candidate_mirror(best)
    eligible = sorted((item for item in recognized if _candidate_mirror(item) == selected_mirror
                       and _horizontal_text_fraction(item[4]) >= .7),
                      key=_page_orientation_rank, reverse=True)
    completed_directions = {item[1] for item in recognized if _candidate_mirror(item) == selected_mirror}
    if (completed_directions != {0, 90, 180, 270} or not eligible
            or len(eligible) > 1
            and _orientation_quality_score(eligible[0][4]) - _orientation_quality_score(eligible[1][4]) < .05):
        reasons.append('orientation_direction_unverified')
    mirror_states = {_candidate_mirror(item) for item in recognized}
    if mirror_states == {False, True}:
        mirror_best = {state: max((item for item in recognized if _candidate_mirror(item) == state),
                                  key=_page_orientation_rank) for state in mirror_states}
        if abs(_orientation_quality_score(mirror_best[True][4])
               - _orientation_quality_score(mirror_best[False][4])) < .05:
            reasons.append('mirror_direction_unverified')
    selected_count, selected_confidence = _orientation_text_summary(best[4])
    competing_counts = [count for item in recognized if item is not best
                       for count, confidence in [_orientation_text_summary(item[4])]
                       if confidence >= selected_confidence - .05]
    competing_count = max(competing_counts, default=0)
    # Geometry can rank a crop upright, but cannot prove that the rest of a page
    # disappeared. Short/square text remains a coverage veto, not direction proof.
    if competing_count >= max(selected_count * 2, selected_count + 4):
        reasons.append('orientation_text_coverage_loss')
    return {
        'method': 'whole_page_trials_without_crop_classifier' if supports_global and same_engine else 'unverified_engine_trials',
        'selected_engine': best[2].name, 'selected_supports_global_contract': supports_global,
        'same_engine_trials': same_engine, 'minimum_score_margin': .05,
        'selected_quality_score': round(_orientation_quality_score(best[4]), 4),
        'completed_directions': sorted(completed_directions),
        'completed_mirror_states': sorted(mirror_states),
        'mirror_horizontal': selected_mirror,
        'ambiguous': bool(reasons), 'review_reasons': reasons,
        'horizontal_text_fraction': round(_horizontal_text_fraction(best[4]), 4),
        'selected_text_characters': selected_count,
        'credible_competing_text_characters': competing_count,
    }


def _orientation_needs_sideways_trials(lines: list[dict]) -> bool:
    if not lines:
        return True
    confidence = sum(float(line.get("confidence", 0.0)) for line in lines) / len(lines)
    characters = sum(len(str(line.get("text", "")).strip()) for line in lines)
    return confidence < .76 or characters < 12 or _horizontal_text_fraction(lines) < .7


def _mirror_needs_trials(lines: list[dict]) -> bool:
    meaningful = [line for line in lines if str(line.get("text", "")).strip()]
    if not meaningful:
        return True
    confidence = sum(float(line.get("confidence", 0.0)) for line in meaningful) / len(meaningful)
    text = "".join(str(line.get("text", "")) for line in meaningful)
    return confidence < .82 or "�" in text


def _has_formula_evidence(text: str) -> bool:
    # Hyphens, slashes and grouping punctuation also occur in ordinary prose.
    # This only routes potential formulas for review; it never validates them.
    if any(char in FORMULA_SYMBOLS - frozenset('-/()[]{}') for char in text):
        return True
    atom = r'(?:\d+(?:\.\d+)?|[A-Za-z])'
    if re.search(rf'(?<![\w.]){atom}\s*[-/]\s*{atom}(?![\w.])', text):
        return True
    if re.search(rf'(?<!\w)-\s*{atom}(?!\w)', text):
        return True
    argument = r'(?:\d+(?:\.\d+)?|[A-Za-z][A-Za-z0-9_]{0,30})'
    if re.search(rf'(?<!\w)[A-Za-z]\s*\(\s*{atom}\s*\)', text):
        return True
    if re.search(rf'(?<!\w)(?:[fghFGH]|sin|cos|tan|log|ln|sqrt|abs)\s*\(\s*{argument}\s*\)', text):
        return True
    if re.search(rf'(?<![\w.])\d+(?:\.\d+)?\s*\(\s*{argument}\s*\)', text):
        return True
    if re.search(rf'\(\s*{argument}\s*\)\s*\(\s*{argument}\s*\)', text):
        return True
    if re.search(rf'[\[({{]\s*{atom}(?:\s*,\s*{atom})+\s*[\])}}]', text):
        return True
    return bool(re.fullmatch(rf'\s*[\[({{]\s*{atom}\s*[\])}}]\s*', text))


def _formula_candidate_regions(image: np.ndarray, lines: list[dict]) -> list[dict]:
    """Find only conservative, text-backed formula crops; never inspect people."""
    regions: list[dict] = []
    height, width = image.shape[:2]
    for line in lines:
        text = str(line.get("text", ""))
        box = line.get("box")
        if not box or not _has_formula_evidence(text):
            continue
        points = np.asarray(box, dtype=np.int32).reshape(-1, 2)
        if len(points) < 2:
            continue
        x1 = max(0, int(points[:, 0].min()) - FORMULA_REGION_PADDING)
        y1 = max(0, int(points[:, 1].min()) - FORMULA_REGION_PADDING)
        x2 = min(width, int(points[:, 0].max()) + FORMULA_REGION_PADDING + 1)
        y2 = min(height, int(points[:, 1].max()) + FORMULA_REGION_PADDING + 1)
        if x2 - x1 < 16 or y2 - y1 < 16:
            continue
        regions.append({
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "confidence": float(line.get("confidence", 0.0)),
            "detection": "printed_line_contains_math_symbols", "provider_eligible": True, "review_eligible": True,
        })
    return regions


def _layout_formula_diagnostics(image: np.ndarray, existing: list[dict]) -> dict:
    """Conservative visual layout candidates, not a formula classifier.

    These are bounded internal diagnostics only. They do not invoke providers,
    create family-review work, alter OCR text, or feed mistakes/knowledge.
    """
    page_height, page_width = image.shape[:2]
    analysis_scale = min(1.0, (LAYOUT_FORMULA_MAX_DIAGNOSTIC_PIXELS / (page_width * page_height)) ** .5)
    if analysis_scale < 1.0:
        analysis_image = cv2.resize(image, None, fx=analysis_scale, fy=analysis_scale, interpolation=cv2.INTER_AREA)
        analysis_existing = [{
            **item,
            "x1": int(item["x1"] * analysis_scale), "y1": int(item["y1"] * analysis_scale),
            "x2": int(item["x2"] * analysis_scale), "y2": int(item["y2"] * analysis_scale),
        } for item in existing]
    else:
        analysis_image = image
        analysis_existing = existing
    base = {
        "diagnostic_only": True,
        "unverified": True,
        "provider_eligible": False,
        "review_eligible": False,
        "affects_ocr_text": False,
        "affects_mistake_or_knowledge": False,
        "candidate_limit": LAYOUT_FORMULA_MAX_CANDIDATES_PER_PAGE,
        "min_width": LAYOUT_FORMULA_MIN_WIDTH,
        "min_height": LAYOUT_FORMULA_MIN_HEIGHT,
        "analysis_scale": round(analysis_scale, 5),
        "resized_to_budget": analysis_scale < 1.0,
    }
    gray = cv2.cvtColor(analysis_image, cv2.COLOR_BGR2GRAY)
    ink = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)[1]
    grouped = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (45, 19)))
    contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[dict] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < LAYOUT_FORMULA_MIN_WIDTH or height < LAYOUT_FORMULA_MIN_HEIGHT or width / max(height, 1) < 2:
            continue
        analysis_height, analysis_width = analysis_image.shape[:2]
        if width > analysis_width * .95 or height > analysis_height * .35:
            continue
        x1, y1 = max(0, x - FORMULA_REGION_PADDING), max(0, y - FORMULA_REGION_PADDING)
        x2, y2 = min(analysis_width, x + width + FORMULA_REGION_PADDING), min(analysis_height, y + height + FORMULA_REGION_PADDING)
        if any(_intersection_over_smaller_area((x1, y1, x2, y2), (item["x1"], item["y1"], item["x2"], item["y2"])) > .7 for item in analysis_existing):
            continue
        crop = ink[y1:y2, x1:x2]
        component_count, _, stats, _ = cv2.connectedComponentsWithStats(crop, connectivity=8)
        glyph_heights = [int(stat[cv2.CC_STAT_HEIGHT]) for stat in stats[1:] if stat[cv2.CC_STAT_AREA] >= 4]
        density = float((crop > 0).mean())
        # Mixed glyph heights (for example, superscripts/subscripts/fraction
        # bars) are a weak but useful layout signal. It is deliberately called
        # "unverified" and never treated as a mathematical parse.
        if component_count < 4 or density < .008 or (max(glyph_heights, default=0) - min(glyph_heights, default=0)) < 4:
            continue
        candidates.append({
            "x1": int(x1 / analysis_scale), "y1": int(y1 / analysis_scale),
            "x2": min(page_width, int(np.ceil(x2 / analysis_scale))), "y2": min(page_height, int(np.ceil(y2 / analysis_scale))),
            "confidence": 0.0,
            "detection": "layout_math_candidate_unverified",
        })
    bounded = sorted(candidates, key=lambda item: (item["y1"], item["x1"]))[:LAYOUT_FORMULA_MAX_CANDIDATES_PER_PAGE]
    return {**base, "status": "completed", "candidate_count": len(bounded), "regions": bounded,
            "truncated": len(candidates) > len(bounded)}


def _layout_formula_candidate_regions(image: np.ndarray, existing: list[dict]) -> list[dict]:
    """Compatibility helper for local diagnostics/tests; never a routing input."""
    return _layout_formula_diagnostics(image, existing)["regions"]


def _intersection_over_smaller_area(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    x1, y1 = max(first[0], second[0]), max(first[1], second[1])
    x2, y2 = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    first_area = max(1, (first[2] - first[0]) * (first[3] - first[1]))
    second_area = max(1, (second[2] - second[0]) * (second[3] - second[1]))
    return intersection / min(first_area, second_area)


def _process_rss_bytes() -> int | None:
    """Best-effort portable process RSS without introducing a dependency."""
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except (ImportError, OSError):
        pass
    try:
        import resource
        import sys
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, OSError):
        pass
    try:
        import ctypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return int(counters.WorkingSetSize) if ok else None
    except (AttributeError, OSError):
        return None


def _perspective_crop(image: np.ndarray, corners: np.ndarray, *, return_transform=False):
    ordered = _order_corners(corners)
    width = max(int(np.linalg.norm(ordered[1] - ordered[0])), int(np.linalg.norm(ordered[2] - ordered[3])))
    height = max(int(np.linalg.norm(ordered[3] - ordered[0])), int(np.linalg.norm(ordered[2] - ordered[1])))
    if min(width, height) < 80:
        return (image, np.eye(3)) if return_transform else image
    destination = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
    transform = cv2.getPerspectiveTransform(ordered, destination)
    cropped = cv2.warpPerspective(image, transform, (width, height))
    return (cropped, transform) if return_transform else cropped


def _order_corners(corners: np.ndarray) -> np.ndarray:
    ordered = np.zeros((4, 2), dtype="float32")
    sums = corners.sum(axis=1)
    differences = np.diff(corners, axis=1).reshape(-1)
    ordered[0] = corners[np.argmin(sums)]
    ordered[2] = corners[np.argmax(sums)]
    ordered[1] = corners[np.argmin(differences)]
    ordered[3] = corners[np.argmax(differences)]
    return ordered


def _segment_questions(lines: list[dict], page_width: int | None = None, page_height: int | None = None) -> list[dict]:
    ordered = _reading_order(lines, page_width, page_height)
    blocks: list[dict] = []
    current: list[dict] = []
    current_column: int | None = None
    section_context = ""
    section_bank_open = False
    matching_section = False
    for line in ordered:
        text = line.get("text", "").lstrip()
        marker = _starts_question_or_section(text)
        section = bool(re.match(r"^[一二三四五六七八九十百]+[、．.]", text))
        column = int(line.get("_column", 0))
        if current and (column != current_column or section or (marker and not matching_section)):
            blocks.append(_block(current, page_width, page_height, section_context, matching_section))
            current = []
        if column != current_column:
            section_context = ""
            section_bank_open = False
            matching_section = False
        if section:
            matching_section = bool(re.search(r"栏.*(?:对应|应答|答语)|连线|\bmatch\b", text, re.I))
            section_bank_open = bool(re.search(r"方框|短文|阅读|上下文|过程", text))
            section_context = text if section_bank_open else ""
        elif marker:
            section_bank_open = False
        elif section_bank_open:
            section_context += "\n" + text
        # Repeated option letters usually mean a question number was missed by
        # OCR. Preserve the orphan bank for review instead of appending it to
        # the preceding question (common at the cropped bottom of a spread).
        option = re.match(r"^([A-F])[.．、]\s*", text)
        if current and not matching_section and option and not marker:
            previous_options = [re.match(r"^([A-F])[.．、]\s*", item["text"]) for item in current]
            if any(item and item[1] == option[1] for item in previous_options):
                blocks.append(_block(current, page_width, page_height, section_context))
                current = []
        current_column = column
        current.append(line)
    if current:
        blocks.append(_block(current, page_width, page_height, section_context, matching_section))
    return blocks


def _starts_question_or_section(text: str) -> bool:
    """Recognize common Chinese workbook numbering without splitting A/B/C options."""
    if re.match(r"^[（(]?\d{1,3}[）).、．\s]", text):
        return True
    if re.match(r"^[（(]\s*[A-Fa-f]?\s*[）)]\s*\d{1,3}[）).、．\s]", text):
        return True
    return bool(re.match(r"^[一二三四五六七八九十百]+[、．.]", text))


def _block(lines: list[dict], page_width: int | None = None, page_height: int | None = None,
           context: str = "", matching: bool = False) -> dict:
    bbox = _combined_bbox(lines)
    text = "\n".join(line["text"] for line in lines)
    first = lines[0]["text"].strip()
    section = bool(re.match(r"^[一二三四五六七八九十百]+[、．.]", first))
    kind = "matching_exercise" if matching else "section" if section else "question" if _starts_question_or_section(first) else "fragment"
    reasons = []
    if kind == "fragment":
        reasons.append("missing_question_start")
    if kind in {"matching_exercise", "section"} or context:
        reasons.append("shared_exercise_context_requires_review")
    if kind == "question":
        body = re.sub(r"^[（(]\s*[A-Fa-f]?\s*[）)]\s*(?=\d)", "", first)
        body = re.sub(r"^[（(]?\d{1,3}[）).、．\s]\s*", "", body)
        if not re.search(r"[\w\u4e00-\u9fff]", body) or re.match(r"^[.,，、．]", body) or re.fullmatch(r"[A-Fa-f\s()（）_]+", body):
            reasons.append("malformed_question_start")
    if any(_line_bbox(line) is None for line in lines):
        reasons.append("missing_line_geometry")
    if any(line.get("_page_layout_unresolved") for line in lines):
        reasons.append("page_layout_unresolved")
    if len({line.get("_column", 0) for line in lines}) > 1:
        reasons.append("cross_page_question_block")
    if any(float(line.get("confidence", 0.0)) < .7 for line in lines):
        reasons.append("uncertain_question_text")
    if bbox:
        x, y, w, h = bbox
        margin_x = max(3, int((page_width or 0) * .01))
        margin_y = max(3, int((page_height or 0) * .01))
        if (page_width and (x <= margin_x or x + w >= page_width - margin_x)) or (page_height and (y <= margin_y or y + h >= page_height - margin_y)):
            reasons.append("text_clipped_at_frame_edge")
    return {"text": text, "line_count": len(lines), "block_kind": kind,
            "confidence": round(sum(float(line.get("confidence", 0.0)) for line in lines) / len(lines), 3),
            "bbox_xywh": list(bbox) if bbox else None,
            "page_index": int(lines[0].get("_column", 0)),
            "page_x_range": lines[0].get("_page_x_range"),
            "page_boundary": lines[0].get("_page_boundary"),
            "context_text": context,
            "boundary_review_reasons": reasons,
            "needs_manual_boundary_check": bool(reasons),
            "auto_accept_allowed": False}


def _reading_order(lines: list[dict], page_width: int | None, page_height: int | None = None) -> list[dict]:
    enriched: list[dict] = []
    for original in lines:
        line = dict(original)
        bbox = _line_bbox(line)
        line["_bbox"] = bbox
        enriched.append(line)
    # A tilted book has a diagonal gutter: x + slope*y is constant along it.
    # Project polygons before looking for whitespace. Axis-aligned envelopes
    # can overlap across pages even though the polygons are well separated.
    slopes = []
    for line in enriched:
        if line["_bbox"] and np.asarray(line.get("box")).shape == (4, 2):
            points = _order_corners(np.asarray(line["box"], dtype=np.float32))
            dx, dy = points[1] - points[0]
            if dx >= 80 and abs(dy / dx) < .35:
                slopes.append(float(dy / dx))
    gutter_slope = float(np.median(slopes)) if slopes else 0.0
    for line in enriched:
        bbox = line["_bbox"]
        if bbox:
            points = np.asarray(line["box"], dtype=np.float64).reshape(-1, 2)
            projected = points[:, 0] + gutter_slope * points[:, 1]
            line["_layout_x_range"] = (float(projected.min()), float(projected.max()))
    column_split = _page_gutter(enriched, page_width)
    boxes = [line["_bbox"] for line in enriched if line["_bbox"]]
    unresolved = False
    if column_split is None and page_width and len(boxes) >= 2:
        left_count = sum(x + w / 2 < page_width * .4 for x, _, w, _ in boxes)
        right_count = sum(x + w / 2 > page_width * .6 for x, _, w, _ in boxes)
        occupied_height = max(y + h for _, y, _, h in boxes) - min(y for _, y, _, _ in boxes)
        occupied_width = max(x + w for x, _, w, _ in boxes) - min(x for x, _, _, _ in boxes)
        # Failure to find a gutter is not evidence that a wide, multi-lane
        # layout is a single page. Do not let it produce trusted boundaries.
        unresolved = left_count >= 1 and right_count >= 1 and (
            bool(page_height and page_width / page_height > 1.4)
            or occupied_width / max(occupied_height, 1) > 1.4)
    extent_y = page_height or max((box[1] + box[3] for box in boxes), default=0)
    split_limits = (sorted((column_split, column_split - gutter_slope * extent_y))
                    if column_split is not None else None)
    for line in enriched:
        projected = line.get("_layout_x_range", (0.0, 0.0))
        center_x = sum(projected) / 2
        line["_column"] = int(column_split is not None and center_x >= column_split)
        line["_page_layout_unresolved"] = unresolved
        line["_page_boundary"] = ({"offset_x": column_split, "x_per_y": -gutter_slope,
                                  "coordinate_space": "oriented_prepared_image"}
                                 if column_split is not None else None)
        # Retain a conservative original-coordinate envelope for consumers;
        # mark ownership below uses the actual diagonal boundary instead.
        line["_page_x_range"] = [max(0, split_limits[0]) if line["_column"] else 0,
                                  page_width if line["_column"] or split_limits is None else min(page_width, split_limits[1])]
    ordered = []
    for column in sorted({line["_column"] for line in enriched}):
        page_lines = [line for line in enriched if line["_column"] == column]
        slopes = []
        heights = []
        for line in page_lines:
            if line["_bbox"] and np.asarray(line.get("box")).shape == (4, 2):
                points = _order_corners(np.asarray(line["box"], dtype=np.float32))
                dx, dy = points[1] - points[0]
                if dx >= 80 and abs(dy / dx) < .35:
                    slopes.append(float(dy / dx))
                    heights.append(float(np.linalg.norm(points[3] - points[0])))
        slope = float(np.median(slopes)) if slopes else 0.0
        tolerance = max(3.0, float(np.median(heights)) * .4) if heights else 5.0
        for line in page_lines:
            bbox = line["_bbox"]
            line["_reading_y"] = bbox[1] - slope * bbox[0] if bbox else 10**9
            if bbox and np.asarray(line.get("box")).shape == (4, 2):
                points = _order_corners(np.asarray(line["box"], dtype=np.float32))
                top_center = (points[0] + points[1]) / 2
                line["_reading_y"] = float(top_center[1] - slope * top_center[0])
        page_lines.sort(key=lambda line: line["_reading_y"])
        # Sort within skew-corrected rows, so a right-hand choice cannot slip
        # beneath the next question just because the camera is tilted.
        while page_lines:
            row_y = page_lines[0]["_reading_y"]
            count = next((i for i, line in enumerate(page_lines) if line["_reading_y"] > row_y + tolerance), len(page_lines))
            ordered.extend(sorted(page_lines[:count], key=lambda line: (line["_bbox"] or (10**9,))[0]))
            page_lines = page_lines[count:]
    return ordered


def _page_gutter(lines: list[dict], page_width: int | None) -> float | None:
    """Use whitespace between deskewed polygon extents, not text centers.

    A question or section heading spanning internal matching/option columns
    rules out that split. No midpoint fallback may bisect a recognized line.
    """
    ranges = sorted(line["_layout_x_range"] for line in lines if line.get("_layout_x_range"))
    if not page_width or len(ranges) < 4:
        return None
    right = ranges[0][1]
    gaps = []
    for index, (x, end) in enumerate(ranges[1:], 1):
        middle = (right + x) / 2
        if (x - right >= page_width * .01 and page_width * .25 < middle < page_width * .75
                and index >= 2 and len(ranges) - index >= 2):
            gaps.append((x - right, middle))
        right = max(right, end)
    return max(gaps)[1] if gaps else None


def _line_bbox(line: dict) -> tuple[int, int, int, int] | None:
    box = line.get("box")
    if box is None:
        return None
    try:
        points = np.asarray(box, dtype=np.float64).reshape(-1, 2)
    except (ValueError, TypeError):
        return None
    if len(points) < 2 or not np.isfinite(points).all():
        return None
    x1, y1 = int(points[:, 0].min()), int(points[:, 1].min())
    x2, y2 = int(points[:, 0].max()), int(points[:, 1].max())
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def _combined_bbox(lines: list[dict]) -> tuple[int, int, int, int] | None:
    boxes = [box for line in lines if (box := _line_bbox(line)) is not None]
    if not boxes:
        return None
    x1 = min(box[0] for box in boxes)
    y1 = min(box[1] for box in boxes)
    x2 = max(box[0] + box[2] for box in boxes)
    y2 = max(box[1] + box[3] for box in boxes)
    return x1, y1, x2 - x1, y2 - y1


def _marked_question_candidates(image: np.ndarray, blocks: list[dict], *, lines: list[dict] | None = None) -> list[dict]:
    """Locate correction observations; neither red ink nor an X proves an error."""
    if image.size == 0 or not blocks:
        return []
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Warm desk lamps and phone auto-white-balance move ordinary red pen ink
    # toward orange (H 13-20) or magenta (H 160-167). Keep the threshold broad
    # enough for those real scenes, then reject page-edge/background blobs and
    # assign each remaining stroke to exactly one nearest question.
    # Saturation 60 excludes a warm cream page/background (typically < 60)
    # while retaining the dim red pen measured in the real document-camera
    # frame and the warm-light regression fixture.
    red = cv2.inRange(hsv, np.array([0, 60, 45]), np.array([20, 255, 255]))
    red |= cv2.inRange(hsv, np.array([160, 60, 45]), np.array([179, 255, 255]))
    # Opening erases thin, dim real pen strokes and turns ticks into unrelated
    # dots. Close only small gaps and classify the resulting connected stroke.
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height, width = image.shape[:2]
    page_area = height * width
    marks = []
    for contour in contours:
        area = cv2.contourArea(contour)
        x, y, w, h = cv2.boundingRect(contour)
        ink_area = cv2.countNonZero(red[y:y + h, x:x + w])
        if area < 6 or ink_area > page_area * .012:
            continue
        if x <= 1 or y <= 1 or x + w >= width - 1 or y + h >= height - 1:
            continue
        if w * h > page_area * .04 or (w > width * .45 and h < 30):
            continue
        marks.append((x, y, w, h))
    if not marks:
        return []
    padding_x = max(24, int(width * .035))
    padding_y = max(18, int(height * .025))
    assigned: dict[int, list[tuple[int, int, int, int]]] = {}
    unambiguous: set[tuple[int, int, int, int]] = set()
    for mark in marks:
        nearest: tuple[float, int] | None = None
        eligible = 0
        for index, block in enumerate(blocks):
            bbox = block.get("bbox_xywh")
            if not bbox:
                continue
            page_range = block.get("page_x_range")
            boundary = block.get("page_boundary")
            if boundary:
                mx, my, mw, mh = mark
                projected = [px - boundary["x_per_y"] * py
                             for px in (mx, mx + mw) for py in (my, my + mh)]
                if (block.get("page_index") == 0 and max(projected) >= boundary["offset_x"]
                        or block.get("page_index") == 1 and min(projected) <= boundary["offset_x"]):
                    continue
            elif page_range and page_range[1] is not None:
                if mark[0] < page_range[0] or mark[0] + mark[2] > page_range[1]:
                    continue
            x, y, w, h = (int(value) for value in bbox)
            gap_x = max(x - (mark[0] + mark[2]), mark[0] - (x + w), 0)
            gap_y = max(y - (mark[1] + mark[3]), mark[1] - (y + h), 0)
            if gap_x > padding_x or gap_y > padding_y:
                continue
            eligible += 1
            vertical_centers = abs((mark[1] + mark[3] / 2) - (y + h / 2))
            score = (gap_x / max(padding_x, 1)) ** 2 + (gap_y / max(padding_y, 1)) ** 2
            score += .12 * vertical_centers / max(h, 20)
            if nearest is None or score < nearest[0]:
                nearest = (score, index)
        if nearest is not None:
            assigned.setdefault(nearest[1], []).append(mark)
            if eligible == 1:
                unambiguous.add(mark)

    ranked: list[dict] = []
    for index, block in enumerate(blocks):
        bbox = block.get("bbox_xywh")
        if not bbox:
            continue
        x, y, w, h = (int(value) for value in bbox)
        matching = assigned.get(index, [])
        if not matching:
            continue
        red_pixels = int(sum(cv2.countNonZero(red[mark_y:mark_y + mark_h, mark_x:mark_x + mark_w]) for mark_x, mark_y, mark_w, mark_h in matching))
        kinds = [_correction_mark_kind(red[my:my + mh, mx:mx + mw]) for mx, my, mw, mh in matching]
        mark_kind = "cross_like" if "cross_like" in kinds else "unclassified_red_ink"
        boundary_check = bool(block.get("needs_manual_boundary_check", True))
        # This is a conservative selection score, not a probability of being
        # wrong. Keep below legacy consumers' .72 "clear question" threshold.
        confidence = min(.49 if boundary_check else .69, float(block.get("confidence", 0.0)))
        facts = None
        if (not boundary_check and mark_kind == "cross_like" and len(matching) == 1
                and matching[0] in unambiguous and lines):
            facts = _marked_answer_facts(hsv, block, lines, matching[0])
        ranked.append({
            "text": str(block.get("text", "")).strip(),
            "confidence": round(confidence, 3),
            "ocr_confidence": float(block.get("confidence", 0.0)),
            "bbox_xywh": [x, y, w, h],
            "red_pixel_count": red_pixels,
            "red_mark_count": len(matching),
            "mark_kind": mark_kind,
            "evidence": "red_correction_mark_near_question" if mark_kind == "cross_like" and not boundary_check else "red_ink_near_text_correctness_unknown",
            "correctness": "unverified",
            "status": "recapture_or_review_required" if boundary_check else "correction_review_required",
            "needs_manual_boundary_check": boundary_check,
            "boundary_review_reasons": block.get("boundary_review_reasons", []),
            "page_index": block.get("page_index", 0),
            "auto_accept_allowed": False,
            "requires_child_confirmation": True,
            "question_binding_ambiguous": any(mark not in unambiguous for mark in matching),
            **({"strict_capture_facts": facts} if facts else {}),
        })
    return sorted(ranked, key=lambda item: (item["confidence"], item["red_pixel_count"]), reverse=True)[:12]


_EXPLICIT_WRITTEN_ANSWER = re.compile(r"^(?:答(?:案)?\s*[:：]\s*(.+)|[=＝]\s*(.+))$")


def _marked_answer_facts(hsv, block, lines, mark):
    """A blue answer line and adjacent red cross qualify separately from OCR proximity."""
    question_lines = str(block.get("text") or "").splitlines()
    if len(question_lines) < 2 or block.get("confidence", 0.0) < .90:
        return None
    x, y, width, height = block["bbox_xywh"]

    def line_matches(text):
        matches = []
        for line in lines:
            if line.get("text") != text:
                continue
            box = _line_bbox(line)
            if box and x <= box[0] and y <= box[1] and box[0]+box[2] <= x+width and box[1]+box[3] <= y+height:
                matches.append((line, box))
        return matches

    mx, my, mw, mh = mark
    # OCR may emit the teacher's cross as a separate X. Remove it only when
    # the OCR glyph falls inside this exact red component, never a printed X.
    if len(question_lines) >= 3 and re.fullmatch(r"[xX×✗✘╳]", question_lines[-1].strip()):
        mark_lines = line_matches(question_lines[-1])
        if len(mark_lines) == 1:
            _, (gx, gy, gw, gh) = mark_lines[0]
            overlap = max(0, min(gx+gw,mx+mw)-max(gx,mx)) * max(0, min(gy+gh,my+mh)-max(gy,my))
            if overlap >= min(gw*gh, mw*mh)*.65 and mx-4 <= gx+gw/2 <= mx+mw+4:
                question_lines.pop()
    answer_line = question_lines[-1]
    answer = _EXPLICIT_WRITTEN_ANSWER.fullmatch(answer_line.strip())
    if not answer:
        return None
    question = "\n".join(question_lines[:-1]).strip()
    if len(question) < 4:
        return None
    matches = line_matches(answer_line)
    if len(matches) != 1:
        return None
    line, (ax, ay, aw, ah) = matches[0]
    if line.get("confidence", 0.0) < .90:
        return None
    if not (ax+aw-3 <= mx <= ax+aw+max(35,width*.22) and 0 < min(ay+ah,my+mh)-max(ay,my)
            and abs(ay+ah/2-my-mh/2) <= max(5, ah*.65)):
        return None
    answer_pixels = hsv[ay:ay+ah, ax:ax+aw]
    blue = cv2.countNonZero(cv2.inRange(answer_pixels, np.array([95,85,45]), np.array([145,255,255])))
    dark_or_colored = np.count_nonzero((answer_pixels[:,:,2] < 175) | (answer_pixels[:,:,1] > 85))
    if blue < 12 or blue < dark_or_colored * .35:
        return None
    return {
        "question_text": question,
        "answer_text": (answer[1] or answer[2]).strip(),
        "confidence": min(float(block["confidence"]), float(line["confidence"])),
        "crop_bbox_xywh": [x, min(y,my), max(x+width,mx+mw)-x, max(y+height,my+mh)-min(y,my)],
        "mark_bbox_xywh": list(mark),
    }


def _correction_mark_kind(mask: np.ndarray) -> str:
    """Only a four-armed intersection is cross-like; a tick's arms meet at ends.

    This is deliberately not a handwriting/correctness classifier. Underlines,
    ticks, circles and fragmented marks remain unclassified review evidence.
    """
    height, width = mask.shape
    if min(height, width) < 10:
        return "unclassified_red_ink"
    segments = cv2.HoughLinesP(mask, 1, np.pi / 180, threshold=max(8, min(height, width) // 3),
                             minLineLength=max(10, max(height, width) * .45), maxLineGap=3)
    if segments is None:
        return "unclassified_red_ink"
    diagonals = []
    for segment in segments[:80, 0]:
        x1, y1, x2, y2 = map(float, segment)
        dx, dy = x2 - x1, y2 - y1
        if dx and .35 < abs(dy / dx) < 3:
            diagonals.append((np.array([x1, y1]), np.array([dx, dy])))
    for index, (origin, direction) in enumerate(diagonals):
        for other, vector in diagonals[index + 1:]:
            if direction[0] * direction[1] * vector[0] * vector[1] >= 0:
                continue
            matrix = np.column_stack((direction, -vector))
            if abs(np.linalg.det(matrix)) < 1:
                continue
            first, second = np.linalg.solve(matrix, other - origin)
            if .25 < first < .75 and .25 < second < .75:
                return "cross_like"
    return "unclassified_red_ink"


def _rectangles_touch(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    return first[0] < second[2] and first[2] > second[0] and first[1] < second[3] and first[3] > second[1]


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
