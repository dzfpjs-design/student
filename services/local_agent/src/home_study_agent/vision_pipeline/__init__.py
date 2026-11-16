"""Device-independent frame quality and stability checks for document capture."""

from .document_capture import FrameStabilityGate, capture_eligibility

__all__ = ["FrameStabilityGate", "capture_eligibility"]
