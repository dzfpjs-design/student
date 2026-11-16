from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
from hashlib import sha256
import sqlite3

from .service import BehaviorModelService, ObservableEvent, ObservationCaptureStale


class BehaviorAppIntegration:
    """Maps durable application events into the longitudinal local model."""

    OBSERVATION_MAP = {
        "present": "presence.seated", "away": "presence.away", "writing_like": "work.writing",
        "paused_like": "work.stall",
    }

    def __init__(self, service: BehaviorModelService, database):
        self.service = service
        self.database = database

    def begin_observation_stream(self, session_id: str, *, stream_epoch: str, client_action_id: str) -> dict:
        session = self._coverage_session(session_id, require_enabled=True)
        return self.service.begin_observation_stream(session["child_id"], session_id,
                    stream_epoch=stream_epoch, at=_now(), client_action_id=client_action_id)

    def capture_admission(self, session_id, capture, *, connection=None, expected=None):
        # Resolve child from the original session, never from browser metadata.
        try:
            session = self._coverage_session(session_id, require_enabled=False, connection=connection)
        except KeyError as error:
            raise ObservationCaptureStale("Observation session no longer exists") from error
        return self.service.capture_admission(session["child_id"], session_id, capture,
                                              connection=connection, expected=expected)

    def record_coverage_sample(self, session_id: str, sample: dict, *, connection=None) -> dict:
        """Internal engine-to-model bridge; do not accept a browser's sample JSON."""
        session = self._coverage_session(session_id, require_enabled=True, connection=connection)
        return self.service.record_observation_sample(session["child_id"], session_id, sample, connection=connection)

    def stop_observation_stream(
        self, session_id: str, *, stream_epoch: str, sequence: int,
        reason: str, client_action_id: str,
    ) -> dict:
        session = self._coverage_session(session_id, require_enabled=False)
        return self.service.stop_observation_stream(session["child_id"], session_id,
                    stream_epoch=stream_epoch, sequence=sequence, reason=reason,
                    at=_now(), client_action_id=client_action_id)

    def _coverage_session(self, session_id: str, *, require_enabled: bool, connection=None) -> dict:
        with (self.database.connect() if connection is None else nullcontext(connection)) as db:
            row = db.execute("SELECT child_id,status,observation_status FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown coverage session")
        if require_enabled and (row["status"] != "active" or row["observation_status"] != "enabled"):
            raise ValueError("Coverage requires an active, observation-enabled session")
        return dict(row)

    def observation(self, session_id: str, analysis: dict, *, connection=None) -> dict | None:
        # Scene quality and a session-local person baseline are prerequisites for
        # every longitudinal inference.  A dark, misaligned, skipped, or
        # otherwise unready frame remains local quality telemetry only.
        if analysis.get("observation_ready") is not True:
            return None
        # Shadow mode may retain a reversible local observation/state event so
        # the qualification and correction surfaces remain useful, but it is a
        # hard boundary for behavioral hypotheses/reminders. Qualified shadow
        # exposure is recorded separately through record_coverage_sample; it
        # never supplies a confirmed behavioral numerator or intervention effect.
        if analysis.get("shadow", True):
            return None
        state_event_id = analysis.get("state_event_id")
        event_type = self.OBSERVATION_MAP.get(analysis.get("state"))
        if not state_event_id or not event_type:
            return None
        session = self._session(session_id, connection=connection)
        alternatives = {
            "away": ("Camera may be misaligned or the child may be briefly out of view.",),
            "writing_like": ("Motion may be page adjustment rather than writing.",),
            "paused_like": ("The child may be thinking or reading silently.",),
            "present": ("Presence does not establish attention or emotion.",),
        }.get(analysis["state"], ())
        return self.service.ingest(ObservableEvent(
            event_id=f"observation:{state_event_id}", child_id=session["child_id"], session_id=session_id,
            occurred_at=analysis.get("observed_at") or _now(), event_type=event_type, source="local_vision",
            confidence=float(analysis["confidence"]), source_refs=(state_event_id,),
            missing_sources=("app_interaction", "child_self_report"),
            payload={
                "metrics": analysis.get("metrics", {}),
                "shadow": analysis.get("shadow", True),
                "model_version": str(analysis.get("model_version", "observation_unknown")),
            },
            alternative_explanations=alternatives,
        ), connection=connection)

    def can_offer_intervention(self, session_id: str, analysis: dict, now: datetime | None = None, *, connection=None) -> bool:
        """Apply session guards without maintaining a second prompt budget.

        ObservationEngine qualifies evidence and BehaviorModelService owns every
        cooldown/frequency decision.  This adapter only checks gentle mode, a live
        task, and a child-initiated snooze before asking the model for a suggestion.
        """
        if not analysis.get("prompt_candidate") or analysis.get("shadow", True):
            return False
        return self._session_allows_intervention(session_id, now, connection=connection)

    def can_offer_document_intervention(self, session_id: str, signal: dict, now: datetime | None = None) -> bool:
        # Historical OCR revisions do not prove continuous observation.  Old
        # persisted signals may still say otherwise and must not prompt.
        return False

    def _session_allows_intervention(self, session_id: str, now: datetime | None = None, *, connection=None) -> bool:
        now = now or datetime.now(timezone.utc)
        with (self.database.connect() if connection is None else nullcontext(connection)) as connection:
            control = connection.execute(
                """SELECT ds.snooze_until, fr.observation_mode
                   FROM daily_session ds JOIN family_rule fr ON fr.child_id = ds.child_id
                   WHERE ds.session_id = ?""", (session_id,),
            ).fetchone()
            active_item = connection.execute(
                """SELECT pi.plan_item_id FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                   WHERE dp.session_id = ? AND pi.status = 'active' LIMIT 1""", (session_id,),
            ).fetchone()
        if not control or control["observation_mode"] != "gentle" or not active_item:
            return False
        if control["snooze_until"]:
            try:
                return datetime.fromisoformat(control["snooze_until"]) <= now
            except ValueError:
                return False
        return True

    def plan(self, session_id: str, event_type: str, client_action_id: str, payload: dict | None = None) -> dict:
        session = self._session(session_id)
        return self.service.ingest(ObservableEvent(
            event_id=f"plan:{client_action_id}", child_id=session["child_id"], session_id=session_id, occurred_at=_now(),
            event_type=event_type, source="plan", confidence=.95, source_refs=(client_action_id,),
            missing_sources=("local_vision",), payload=payload or {},
            alternative_explanations=("Plan events record workflow progress, not effort or attention.",),
        ))

    def plan_item(self, plan_item_id: str, event_type: str, client_action_id: str, payload: dict | None = None) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT dp.session_id FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id WHERE pi.plan_item_id = ?",
                (plan_item_id,),
            ).fetchone()
        if not row:
            raise KeyError("Unknown plan item")
        return self.plan(row["session_id"], event_type, client_action_id, payload)

    def child_correction(self, session_id: str, text: str, client_action_id: str, correction: dict | None = None) -> dict:
        session = self._session(session_id)
        correction = correction or {}
        target = correction.get("state_event_id")
        if target:
            self.service.retract_event(f"observation:{target}", session["child_id"], str(correction.get("correction_id", client_action_id)), text)
        return self.service.ingest(ObservableEvent(
            event_id=f"child-correction:{client_action_id}", child_id=session["child_id"], session_id=session_id,
            occurred_at=_now(), event_type="child.check_in", source="child_self_report", confidence=1.0,
            source_refs=(client_action_id,), missing_sources=("independent_confirmation",), payload={"correction": text, "retracted_observation": bool(target)},
            alternative_explanations=("Child report can retract recent observable evidence; it is not converted into a stable label.",),
        ))

    def ocr_completed(self, session_id: str, artifact: dict, client_action_id: str) -> dict:
        """Align a document OCR completion without copying page text or pixels."""
        session = self._session(session_id)
        pipeline = artifact.get("details", {}).get("local_pipeline", {})
        manual_review = pipeline.get("manual_review", {})
        return self.service.ingest(ObservableEvent(
            event_id=f"ocr:{client_action_id}", child_id=session["child_id"], session_id=session_id,
            occurred_at=artifact.get("created_at") or _now(), event_type="document.ocr_completed", source="local_ocr",
            confidence=float(artifact.get("confidence", 0.0)), source_refs=(str(artifact["artifact_id"]),),
            missing_sources=("child_text_confirmation",),
            payload={
                "status": str(artifact.get("status", "unknown")),
                "manual_review_required": bool(manual_review.get("required", True)),
                "question_block_count": len(pipeline.get("question_blocks", [])),
            },
            alternative_explanations=("OCR completion records local document processing, not correctness of printed, handwritten, or formula content.",),
        ))

    def document_revision(self, session_id: str, revision: dict, client_action_id: str) -> dict | None:
        """A changed OCR result alone proves neither writing nor a stall."""
        return None

    def retract_unreliable_document_signals(self, limit: int = 20) -> dict:
        """Exclude old OCR-only behavior inferences, retaining their audit trail.

        Call after behavior model initialization and legacy import. Independent
        child, plan, and camera observations are deliberately not retracted.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid document signal repair batch size')
        with self.database.connect() as db:
            rows = db.execute("""SELECT oe.event_id, oe.child_id FROM observable_event oe
                LEFT JOIN behavior_retraction br ON br.event_id=oe.event_id
                WHERE oe.source='local_document_progress'
                  AND oe.event_type IN ('work.writing','work.stall') AND br.event_id IS NULL
                ORDER BY oe.event_id LIMIT ?""", (limit,)).fetchall()
        repaired = failed = 0
        for row in rows:
            correction_id = 'system-document-revision-' + sha256(row['event_id'].encode()).hexdigest()
            try:
                outcome = self.service.retract_event(row['event_id'], row['child_id'], correction_id,
                    'OCR revisions alone do not establish continuous visibility, writing, or a stall.')
                repaired += outcome.get('status') == 'retracted'
            except (ValueError, sqlite3.Error):
                # A simultaneous child correction or a temporary database
                # failure must not abort the rest of this bounded batch.
                # Unretracted events remain eligible for a later retry.
                failed += 1
        return {'examined': len(rows), 'repaired': repaired, 'failed': failed}

    def audio_transcribed(self, session_id: str, voice_input: dict, client_action_id: str) -> dict:
        """Align local ASR metadata without placing a child's transcript in behavior evidence."""
        session = self._session(session_id)
        return self.service.ingest(ObservableEvent(
            event_id=f"asr:{client_action_id}", child_id=session["child_id"], session_id=session_id,
            occurred_at=voice_input.get("created_at") or _now(), event_type="audio.transcribed",
            source="cloud_asr" if voice_input.get("cloud_used") else "local_asr",
            confidence=1.0, source_refs=(str(voice_input["voice_input_id"]),),
            missing_sources=("semantic_intent_confirmation",),
            payload={
                "purpose": str(voice_input.get("purpose", "unknown")),
                "duration_ms": int(voice_input.get("duration_ms", 0)),
            },
            alternative_explanations=("ASR completion proves only that local audio was transcribed; it does not infer help, attention, fatigue, or emotion.",),
        ))

    def _session(self, session_id: str, *, connection=None) -> dict:
        with (self.database.connect() if connection is None else nullcontext(connection)) as connection:
            row = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            raise KeyError("Unknown session")
        return dict(row)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
