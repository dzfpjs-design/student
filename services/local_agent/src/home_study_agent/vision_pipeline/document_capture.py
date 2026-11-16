from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FrameStabilityGate:
    """Pure frame gate; callers own the camera lease and pass copied frames.

    It never opens a camera device, avoiding conflict with the primary sensor
    service. A capture can occur only after `required_stable_frames` samples.
    """

    required_stable_frames: int = 4
    max_mean_difference: float = 3.5
    _previous: np.ndarray | None = None
    _stable_frames: int = 0

    def observe(self, frame: np.ndarray) -> dict:
        if frame is None or frame.size == 0:
            return {"ready": False, "reason": "empty_frame", "stable_frames": 0}
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 120))
        difference = None
        if self._previous is not None:
            difference = float(cv2.absdiff(gray, self._previous).mean())
            self._stable_frames = self._stable_frames + 1 if difference <= self.max_mean_difference else 0
        self._previous = gray
        return {"ready": self._stable_frames >= self.required_stable_frames,
                "reason": "stable" if self._stable_frames >= self.required_stable_frames else "waiting_for_stable_frame",
                "stable_frames": self._stable_frames, "mean_difference": difference}


def capture_eligibility(*, document_camera_lease_id: str | None, primary_task_active: bool) -> dict:
    """Explicitly reject camera access before a frame source is touched.

    Device discovery/leases remain owned by ``device_registry``. This lightweight
    guard makes a missing device or another task's exclusive lease observable to
    the OCR caller instead of falling back to a competing camera open.
    """
    if primary_task_active:
        return {"allowed": False, "code": "device_busy", "retryable": True}
    if not document_camera_lease_id:
        return {"allowed": False, "code": "document_camera_lease_required", "retryable": True}
    return {"allowed": True, "code": "ready", "lease_id": document_camera_lease_id}
