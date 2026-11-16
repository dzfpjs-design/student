"""Local-only resilient document OCR pipeline."""

from .service import LocalOcrService, OcrEngine, OcrEngineUnavailable
from .adapter import OcrArtifactAdapter
from .resource_budget import CpuResourceQueue, ResourceSnapshot
from .runtime_adapter import CpuScheduledMediaAdapter
from .workflow import LocalOcrWorkflow, OcrWorkflowDeferredError, OcrWorkflowFailedError
from .review_queue import InvalidOcrReviewValueError, OcrReviewActionConflictError, OcrReviewQueue
from .formula_recognition import FormulaRecognitionProvider, FormulaProviderUnavailable, UnavailableFormulaProvider, build_local_formula_provider

__all__ = ["LocalOcrService", "OcrEngine", "OcrEngineUnavailable", "OcrArtifactAdapter", "CpuResourceQueue", "ResourceSnapshot", "CpuScheduledMediaAdapter", "LocalOcrWorkflow", "OcrWorkflowDeferredError", "OcrWorkflowFailedError", "OcrReviewQueue", "InvalidOcrReviewValueError", "OcrReviewActionConflictError", "FormulaRecognitionProvider", "FormulaProviderUnavailable", "UnavailableFormulaProvider", "build_local_formula_provider"]
