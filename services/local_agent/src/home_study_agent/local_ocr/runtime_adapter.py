from __future__ import annotations

from hashlib import sha256
from typing import Callable

from .resource_budget import CpuResourceQueue, ResourceSnapshot
from .service import LocalOcrService
from .cache_lifecycle import OcrSourceInvalid, resolve_owner


class CpuScheduledMediaAdapter:
    """Small integration seam for OCR/ASR/behavior CPU queue ownership.

    It persists hashes and action IDs only. The existing service remains the
    owner of image/audio bytes, session authorization, and encrypted raw media.
    """

    def __init__(self, queue: CpuResourceQueue, ocr: LocalOcrService):
        self.queue = queue
        self.ocr = ocr
        self.database = None

    def bind_database(self, database):
        self.database = database
        self.ocr.bind_database(database)

    def enqueue_ocr(self, image_bytes: bytes, request_id: str | None = None, *, session_id=None,
                    monitor_generation=None, preview_lease_id=None, preview_signature=None) -> dict:
        owner = None
        if self.database is not None:
            if not request_id:
                raise OcrSourceInvalid('OCR_ACTION_REQUIRED')
            with self.database.connect() as c:
                owner = resolve_owner(c, request_id, session_id=session_id, monitor_generation=monitor_generation,
                                      preview_lease_id=preview_lease_id, preview_signature=preview_signature)
        job = self.ocr.enqueue(image_bytes, request_id=request_id, preferred_route="cpu", **({'owner': owner} if owner else {}))
        task = self.queue.enqueue("ocr", {"ocr_job_id": job["job_id"], "image_hash": sha256(image_bytes).hexdigest()}, task_id=f"ocr:{job['job_id']}")
        return {"job": job, "task": task, "behavior_fps_cap": self.queue.behavior_fps_cap()}

    def enqueue_asr(self, client_action_id: str) -> dict:
        return self.queue.enqueue("asr", {"client_action_id": client_action_id}, task_id=f"asr:{client_action_id}")

    def enqueue_behavior(self, derived_event_id: str) -> dict:
        return self.queue.enqueue("behavior", {"derived_event_id": derived_event_id}, task_id=f"behavior:{derived_event_id}")

    def next(self, snapshot: ResourceSnapshot) -> dict | None:
        return self.queue.next(snapshot)

    def claim_ocr(self, scheduled: dict, snapshot: ResourceSnapshot) -> dict | None:
        """Claim only the OCR reservation created for this capture request."""
        if scheduled.get('job', {}).get('status') == 'completed':
            return scheduled['task']
        if scheduled.get('job', {}).get('status') in {'cancelled', 'quarantined'}:
            raise OcrSourceInvalid('OCR_SOURCE_INVALIDATED')
        if scheduled.get('job', {}).get('status') == 'failed' and scheduled['task']['status'] == 'failed':
            self.queue.retry(scheduled['task']['task_id'])
        return self.queue.claim(scheduled["task"]["task_id"], snapshot)

    def run_ocr_claim(self, claim: dict, image_bytes: bytes) -> dict:
        if claim["task_type"] != "ocr":
            raise ValueError("Claim is not an OCR task")
        job_id = claim["payload"]["ocr_job_id"]
        try:
            result = self.ocr.run(job_id, image_bytes)
        except Exception as error:
            self.queue.finish(claim['task_id'], 'failed', reason=type(error).__name__)
            raise
        terminal = "completed" if result["status"] == "completed" else "cancelled" if result["status"] == "cancelled" else "failed"
        self.queue.finish(claim["task_id"], terminal, reason=result.get("error_code"))
        return result

    def cancel_ocr(self, scheduled: dict, reason: str) -> dict:
        """Cancel both durable OCR records when a caller cannot consume its claim.

        The image itself is still caller-owned; this only clears metadata-only
        OCR and CPU reservations so a mismatched queue claim cannot strand work.
        """
        self.ocr.cancel(scheduled["job"]["job_id"])
        return self.queue.finish(scheduled["task"]["task_id"], "cancelled", reason=reason)

    def run_asr_claim(self, claim: dict, transcribe: Callable[[], dict]) -> dict:
        """Run the existing local ASR callable under its persisted CPU reservation."""
        if claim["task_type"] != "asr":
            raise ValueError("Claim is not an ASR task")
        try:
            result = transcribe()
        except Exception as error:
            self.queue.finish(claim["task_id"], "failed", reason=type(error).__name__)
            raise
        self.queue.finish(claim["task_id"], "completed")
        return result

    def run_behavior_claim(self, claim: dict, update: Callable[[], dict]) -> dict:
        """Run a derived-event state update under its one-thread CPU reservation."""
        if claim["task_type"] != "behavior":
            raise ValueError("Claim is not a behavior task")
        try:
            result = update()
        except Exception as error:
            self.queue.finish(claim["task_id"], "failed", reason=type(error).__name__)
            raise
        self.queue.finish(claim["task_id"], "completed")
        return result
