"""Optional, local-only equation-image-to-LaTeX providers.

No provider is required for the baseline household PC.  The main OCR workflow keeps
formula regions in manual-review mode until a parent has installed an audited local
checkpoint.  This module intentionally never downloads a model during inference.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import Protocol

import cv2
import numpy as np

from .model_assets import verify_model_manifest


class FormulaProviderUnavailable(RuntimeError):
    """An optional formula recognizer or its audited checkpoint is unavailable."""


@dataclass(frozen=True)
class FormulaPrediction:
    latex: str
    latency_seconds: float
    provider: str


class FormulaRecognitionProvider(Protocol):
    name: str

    def health(self) -> dict: ...

    def recognize(self, image_bytes: bytes) -> FormulaPrediction: ...

    def worker_spec(self) -> dict: ...


class UnavailableFormulaProvider:
    """Configuration-safe fallback that keeps OCR available for human editing."""

    def __init__(self, configured_name: str, reason: str):
        self.name = f"unavailable:{configured_name[:80]}"
        self.reason = reason

    def health(self) -> dict:
        return {"ready": False, "provider": self.name, "reason": self.reason, "cpu_only": True, "implicit_network_download": False}

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        raise FormulaProviderUnavailable(self.reason)

    def worker_spec(self) -> dict:
        return {"kind": "unavailable", "configured_name": self.name, "reason": self.reason}


def build_local_formula_provider(
    provider_name: str,
    model_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> FormulaRecognitionProvider | None:
    """Create only an explicitly configured local provider.

    This function neither downloads nor probes a model registry. A configured
    but missing MFR cache remains an unavailable provider so the pipeline can
    explain the manual-edit fallback without silently using a cloud service.
    """
    name = provider_name.strip().lower()
    if name in {"", "disabled", "none"}:
        return None
    if name == "pix2text_mfr_onnx":
        return Pix2TextMfrOnnxProvider(
            model_dir or Path("__missing_pix2text_mfr_model__"),
            manifest_path or Path("__missing_pix2text_mfr_manifest__.json"),
        )
    if name == "rapidocr_baseline":
        return RapidOcrFormulaBaselineProvider()
    return UnavailableFormulaProvider(name or "empty", f"unsupported_local_formula_provider:{provider_name}")


def provider_from_worker_spec(spec: dict) -> FormulaRecognitionProvider:
    """Recreate a provider inside an isolated spawn worker, with no pickled model."""
    kind = str(spec.get("kind", ""))
    if kind == "pix2text_mfr_onnx":
        return Pix2TextMfrOnnxProvider(Path(str(spec["model_dir"])), Path(str(spec["manifest_path"])))
    if kind == "pix2tex":
        return Pix2TexFormulaProvider(Path(str(spec["checkpoint"])))
    if kind == "rapidocr_baseline":
        return RapidOcrFormulaBaselineProvider()
    if kind == "unavailable":
        return UnavailableFormulaProvider(str(spec.get("configured_name", "unknown")), str(spec.get("reason", "provider_unavailable")))
    # Extensions are code-configured only (never supplied by an HTTP request).
    # The worker accepts a stable allowlist key, never an arbitrary module path.
    if kind == "local_factory":
        factory_key = str(spec.get("factory_key", ""))
        target = LOCAL_FORMULA_WORKER_FACTORY_ALLOWLIST.get(factory_key)
        if target is None:
            raise FormulaProviderUnavailable(f"formula_worker_factory_not_allowlisted:{factory_key or 'missing'}")
        module = import_module(target[0])
        factory = getattr(module, target[1])
        return factory(dict(spec.get("kwargs") or {}))
    raise FormulaProviderUnavailable(f"unsupported_formula_worker_spec:{kind}")


class Pix2TexFormulaProvider:
    """CPU-only pix2tex adapter, gated by a locally supplied checkpoint path.

    pix2tex code is MIT according to its installed package metadata.  Its checkpoint
    is intentionally not bundled or fetched here: the installer must independently
    record its origin, licence, hash, and size before setting this provider up.
    """

    name = "pix2tex_cpu"

    def __init__(self, checkpoint: Path):
        self.checkpoint = Path(checkpoint)
        self._model = None

    def health(self) -> dict:
        try:
            self._ensure_available()
        except FormulaProviderUnavailable as error:
            return {"ready": False, "provider": self.name, "reason": str(error), "cpu_only": True}
        return {"ready": True, "provider": self.name, "cpu_only": True, "checkpoint_bytes": self.checkpoint.stat().st_size}

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        self._ensure_available()
        if self._model is None:
            from munch import Munch
            from pix2tex.cli import LatexOCR

            settings = Path(__import__("pix2tex").__file__).resolve().parent / "model" / "settings" / "config.yaml"
            self._model = LatexOCR(Munch({"config": str(settings), "checkpoint": str(self.checkpoint), "no_cuda": True, "no_resize": False}))
        from PIL import Image

        started = perf_counter()
        with Image.open(BytesIO(image_bytes)) as image:
            latex = str(self._model(image.convert("RGB")))
        return FormulaPrediction(latex=latex, latency_seconds=perf_counter() - started, provider=self.name)

    def _ensure_available(self) -> None:
        if not self.checkpoint.is_file() or self.checkpoint.stat().st_size <= 0:
            raise FormulaProviderUnavailable("pix2tex_checkpoint_missing_or_empty; keep manual_formula_edit enabled")
        try:
            import pix2tex  # noqa: F401
            import torch  # noqa: F401
        except ImportError as error:
            raise FormulaProviderUnavailable("pix2tex_cpu_dependencies_not_installed; keep manual_formula_edit enabled") from error

    def worker_spec(self) -> dict:
        return {"kind": "pix2tex", "checkpoint": str(self.checkpoint)}


class Pix2TextMfrOnnxProvider:
    """Official Pix2Text MFR-1.5 pure-formula recognizer using local ONNX CPU assets.

    It deliberately loads only a caller-provided directory and validates the
    generated download manifest first.  ``local_files_only`` makes a missing
    cache an explicit health failure rather than an invisible network request.
    """

    name = "pix2text_mfr_1_5_onnx_cpu"
    repo_id = "breezedeus/pix2text-mfr-1.5"

    def __init__(self, model_dir: Path, manifest_path: Path | None = None):
        self.model_dir = Path(model_dir)
        self.manifest_path = Path(manifest_path) if manifest_path else self.model_dir / "model-manifest.json"
        self._processor = None
        self._model = None
        self._verified_manifest: dict | None = None
        self._verified_signature: tuple[tuple[str, int, int], ...] | None = None

    def health(self) -> dict:
        manifest, failure = self._read_verified_manifest()
        if failure:
            return {"ready": False, "provider": self.name, "reason": failure, "cpu_only": True, "implicit_network_download": False}
        try:
            from optimum.onnxruntime import ORTModelForVision2Seq  # noqa: F401
            from transformers import TrOCRProcessor  # noqa: F401
        except ImportError:
            return {"ready": False, "provider": self.name, "reason": "pix2text_mfr_onnx_dependencies_not_installed", "cpu_only": True, "implicit_network_download": False}
        return {
            "ready": True,
            "provider": self.name,
            "cpu_only": True,
            "implicit_network_download": False,
            "revision": manifest["revision"],
            "model_bytes": manifest["total_bytes"],
            "license": manifest["license"],
        }

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        self._ensure_available()
        if self._model is None or self._processor is None:
            from optimum.onnxruntime import ORTModelForVision2Seq
            from transformers import TrOCRProcessor

            self._processor = TrOCRProcessor.from_pretrained(str(self.model_dir), local_files_only=True)
            self._model = ORTModelForVision2Seq.from_pretrained(str(self.model_dir), local_files_only=True, use_cache=False)
        from PIL import Image

        started = perf_counter()
        with Image.open(BytesIO(image_bytes)) as image:
            pixel_values = self._processor(images=image.convert("RGB"), return_tensors="pt").pixel_values
        generated_ids = self._model.generate(pixel_values, max_new_tokens=512)
        latex = self._processor.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()
        return FormulaPrediction(latex=latex, latency_seconds=perf_counter() - started, provider=self.name)

    def _ensure_available(self) -> None:
        health = self.health()
        if not health["ready"]:
            raise FormulaProviderUnavailable(f"{health['reason']}; keep manual_formula_edit enabled")

    def _read_verified_manifest(self) -> tuple[dict, str | None]:
        if not self.model_dir.is_dir() or not self.manifest_path.is_file():
            return {}, "pix2text_mfr_model_or_manifest_missing"
        try:
            import json
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, "pix2text_mfr_manifest_unreadable"
        if manifest.get("repo_id") != self.repo_id or manifest.get("license") != "MIT" or not str(manifest.get("revision", "")):
            return {}, "pix2text_mfr_manifest_identity_invalid"
        signature = _model_asset_signature(self.model_dir, self.manifest_path, manifest)
        if signature is None:
            return {}, "pix2text_mfr_model_files_missing"
        if self._verified_manifest is not None and signature == self._verified_signature:
            return self._verified_manifest, None
        failures = verify_model_manifest(self.model_dir, manifest)
        if failures:
            return {}, f"pix2text_mfr_manifest_verification_failed:{failures[0]}"
        self._verified_manifest = manifest
        self._verified_signature = signature
        return manifest, None

    def worker_spec(self) -> dict:
        return {"kind": "pix2text_mfr_onnx", "model_dir": str(self.model_dir), "manifest_path": str(self.manifest_path)}


def _model_asset_signature(model_dir: Path, manifest_path: Path, manifest: dict) -> tuple[tuple[str, int, int], ...] | None:
    """Invalidate cached SHA verification whenever the local asset set changes."""
    paths = [manifest_path]
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None
        paths.append(model_dir / item["path"])
    try:
        return tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths)
    except OSError:
        return None


class RapidOcrFormulaBaselineProvider:
    """A measurable local baseline, explicitly *not* a formula-to-LaTeX model.

    It exists so a missing specialist checkpoint does not leave MathWriting entirely
    unevaluated. Its output is ordinary OCR text and must never be surfaced as a
    mathematical parse or replace the manual formula editor.
    """

    name = "rapidocr_printed_baseline_not_formula_model"

    def __init__(self) -> None:
        from .service import RapidOcrEngine

        self.engine = RapidOcrEngine()

    def health(self) -> dict:
        health = self.engine.health()
        return {**health, "provider": self.name, "formula_specialist": False, "manual_formula_edit_required": True}

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("formula_image_decode_failed")
        started = perf_counter()
        lines = self.engine.recognize(image, "cpu")
        return FormulaPrediction(latex=" ".join(str(line.get("text", "")) for line in lines).strip(), latency_seconds=perf_counter() - started, provider=self.name)

    def worker_spec(self) -> dict:
        return {"kind": "rapidocr_baseline"}


def _pix2tex_from_worker_spec(values: dict) -> Pix2TexFormulaProvider:
    return Pix2TexFormulaProvider(Path(str(values["checkpoint"])))


LOCAL_FORMULA_WORKER_FACTORY_ALLOWLIST = {
    # Test-only helper has no Settings route and is retained solely so spawn
    # regression tests can prove hard termination of a permanently blocked child.
    "test_support": ("home_study_agent.local_ocr.test_support_formula_provider", "make_provider"),
}
