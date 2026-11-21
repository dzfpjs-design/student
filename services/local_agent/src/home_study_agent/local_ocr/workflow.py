from __future__ import annotations

import json
from pathlib import Path

from .runtime_adapter import CpuScheduledMediaAdapter
from .resource_budget import ResourceSnapshot
from .cache_lifecycle import OcrActionConflict, OcrSourceInvalid, assert_current
from .service import OcrJobBusy


class LocalOcrWorkflow:
    """Connects the CPU-first OCR queue to the existing durable artifact flow.

    The existing ``LocalMediaAI`` remains the sole writer of encrypted raw media
    and ``capture_artifact`` rows. This avoids a second system of record while
    adding structured local-quality evidence before mistake-candidate creation.
    """

    def __init__(self, adapter: CpuScheduledMediaAdapter, media_ai):
        self.adapter = adapter
        self.media_ai = media_ai
        self.adapter.bind_database(media_ai.database)

    def process(self, session_id: str, image_bytes: bytes, client_action_id: str, store_raw: bool,
                monitor_generation: int | None = None, capture_kind: str = 'single_frame') -> dict:
        if capture_kind != 'single_frame':
            raise ValueError('DOCUMENT_COMPOSITE_DISABLED')
        session = self.media_ai._session(session_id)
        with self.media_ai.database.connect() as c:
            existing = c.execute('SELECT child_id,session_id FROM capture_artifact WHERE client_action_id=?', (client_action_id,)).fetchone()
            prior = c.execute('SELECT artifact_id,publication_started FROM ocr_job WHERE request_id=?', (f'pipeline:{client_action_id}',)).fetchone()
            if existing and (existing['child_id'] != session['child_id'] or existing['session_id'] != session_id
                             or not prior or not (prior['artifact_id'] or prior['publication_started'])):
                raise OcrActionConflict('OCR_ARTIFACT_ACTION_CONFLICT')
        scheduled = self.queue_recognition(
            session_id, image_bytes, f"pipeline:{client_action_id}", monitor_generation
        )
        pipeline_job = self.finish_recognition(scheduled, image_bytes)
        # This is intentionally the one existing persistence call: raw storage
        # consent, encryption, backups, artifact deletion, and mistake capture
        # continue to use the established application system of record.
        with self.media_ai.database.external_effect_guard():
            # Only publication holds the guard. Deletion can cancel an in-flight
            # recognizer; its late result cannot cross this second source fence.
            with self.media_ai.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                current = c.execute('SELECT * FROM ocr_job WHERE job_id=?', (pipeline_job['job_id'],)).fetchone()
                if not current or current['status'] != 'completed':
                    raise OcrSourceInvalid('OCR_SOURCE_INVALIDATED')
                assert_current(c, current)
                c.execute('UPDATE ocr_job SET publication_started=1 WHERE job_id=?', (current['job_id'],))
                c.commit()
            artifact = self.media_ai.ocr(session_id, image_bytes, client_action_id, store_raw,
                                         prepared_result={**pipeline_job["result"], '_local_ocr_job_id': pipeline_job['job_id'],
                                                          '_local_pipeline': self._pipeline_details(pipeline_job, scheduled)},
                                         monitor_generation=monitor_generation, capture_kind=capture_kind)
            return self._attach_pipeline(artifact, pipeline_job, scheduled)

    def queue_recognition(self, session_id: str, image_bytes: bytes, request_id: str,
                          monitor_generation: int | None, *, preview_lease_id: str | None = None,
                          preview_signature: str | None = None):
        lease_scope = {'preview_lease_id': preview_lease_id} if preview_lease_id is not None else {}
        if preview_signature is not None:
            lease_scope['preview_signature'] = preview_signature
        return self.adapter.enqueue_ocr(image_bytes, request_id=request_id,
                                        session_id=session_id, monitor_generation=monitor_generation, **lease_scope)

    def finish_recognition(self, scheduled: dict, image_bytes: bytes):
        claim = self.adapter.claim_ocr(scheduled, ResourceSnapshot(cpu_percent=0, memory_mb=0))
        if not claim or claim["task_id"] != scheduled["task"]["task_id"]:
            raise OcrWorkflowDeferredError("OCR is queued behind a protected local task; retry without recapturing the page.")
        try:
            pipeline_job = self.adapter.run_ocr_claim(claim, image_bytes)
        except OcrJobBusy as error:
            raise OcrWorkflowDeferredError('OCR is still running; retry the same capture.') from error
        if pipeline_job["status"] != "completed" or not pipeline_job["result"]:
            raise OcrWorkflowFailedError(pipeline_job.get("error_code") or "LOCAL_OCR_PIPELINE_FAILED")
        return pipeline_job

    @staticmethod
    def _pipeline_details(pipeline_job, scheduled):
        pipeline = pipeline_job["result"]
        return {
            "engine": pipeline["engine"], "route": pipeline["route"], "engine_attempts": pipeline["engine_attempts"],
            "quality": pipeline["quality"], "question_blocks": pipeline["question_blocks"],
            "wrong_question_candidates": pipeline.get("wrong_question_candidates", []),
            "correction_mark_candidates": pipeline.get("correction_mark_candidates", []),
            "proven_wrong_questions": [],
            "manual_review": pipeline["manual_review"], "privacy": pipeline["privacy"],
            "formula_routing": pipeline.get("formula_routing", {}),
            "queue": {"task_id": scheduled["task"]["task_id"], "behavior_fps_cap": scheduled["behavior_fps_cap"]},
            "image_sha256": pipeline_job['image_hash'],
            "text_geometry": pipeline.get('text_geometry'),
        }

    def _attach_pipeline(self, artifact, pipeline_job, scheduled):
        # Also repairs artifacts created by older versions before atomic metadata
        # publication; new artifacts already contain their boundary restrictions.
        with self.media_ai.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT details_json FROM capture_artifact WHERE artifact_id = ?", (artifact["artifact_id"],)).fetchone()
            details = json.loads(row["details_json"] or "{}")
            details["local_pipeline"] = self._pipeline_details(pipeline_job, scheduled)
            connection.execute("UPDATE capture_artifact SET details_json = ? WHERE artifact_id = ?", (json.dumps(details, ensure_ascii=False), artifact["artifact_id"]))
            connection.execute('UPDATE ocr_job SET artifact_id=? WHERE job_id=?', (artifact['artifact_id'], pipeline_job['job_id']))
            connection.commit()
        return self.media_ai._artifact_for_workflow(artifact["artifact_id"])


class OcrWorkflowDeferredError(RuntimeError):
    pass


class OcrWorkflowFailedError(RuntimeError):
    pass
