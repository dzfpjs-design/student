from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import json
import math
import re
from typing import Any
from uuid import uuid4

from .database import Database
from .repository import utc_now


EVENT_TYPES = {"no_page", "stabilizing", "writing", "occluded", "page_changed", "stable", "capture_failed"}
PREVIEW_SESSION_STATES = {"check_in", "classroom_recall", "homework_capture", "plan_review", "study"}
DOCUMENT_ANALYSIS_STATES = {"homework_capture", "plan_review", "study"}


class DocumentMonitorGenerationConflictError(ValueError):
    code = "DOCUMENT_MONITOR_GENERATION_CONFLICT"

    def __init__(self):
        super().__init__(self.code)


class DocumentMonitoringService:
    """Persists frame-free writing progress and references local OCR artifacts."""

    def __init__(self, database: Database):
        self.database = database
        with self.database.connect() as connection:
            from .document_monitoring_schema import ensure_document_monitor_parents
            ensure_document_monitor_parents(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_monitor_event (
                  event_id TEXT PRIMARY KEY,
                  monitor_id TEXT NOT NULL REFERENCES document_monitor_session(monitor_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  event_type TEXT NOT NULL,
                  confidence REAL NOT NULL,
                  metrics_json TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_document_monitor_event_session
                  ON document_monitor_event(session_id, observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_document_page_revision_session
                  ON document_page_revision(session_id, revision_index DESC);
                CREATE TABLE IF NOT EXISTS observed_question (
                  question_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  question_fingerprint TEXT NOT NULL,
                  canonical_text TEXT NOT NULL,
                  first_revision_id TEXT NOT NULL REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                  latest_revision_id TEXT NOT NULL REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                  latest_artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                  occurrence_count INTEGER NOT NULL DEFAULT 1,
                  marked_review_count INTEGER NOT NULL DEFAULT 0,
                  mistake_candidate_id TEXT REFERENCES mistake_capture_candidate(candidate_id) ON DELETE SET NULL,
                  first_seen_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  UNIQUE(child_id, question_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS observed_question_occurrence (
                  occurrence_id TEXT PRIMARY KEY,
                  question_id TEXT NOT NULL REFERENCES observed_question(question_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  revision_id TEXT NOT NULL REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                  artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                  confidence REAL NOT NULL,
                  marked_for_review INTEGER NOT NULL DEFAULT 0,
                  observed_at TEXT NOT NULL,
                  UNIQUE(question_id, revision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_observed_question_child
                  ON observed_question(child_id, last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_observed_question_occurrence_revision
                  ON observed_question_occurrence(revision_id);
                CREATE TABLE IF NOT EXISTS observed_answer_evidence (
                  visual_answer_evidence_id TEXT PRIMARY KEY,
                  occurrence_id TEXT NOT NULL UNIQUE REFERENCES observed_question_occurrence(occurrence_id) ON DELETE CASCADE,
                  question_id TEXT NOT NULL REFERENCES observed_question(question_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  revision_id TEXT NOT NULL REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                  artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                  answer_text TEXT NOT NULL,
                  grading_marks_json TEXT NOT NULL,
                  evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('handwritten','grading_mark','mixed')),
                  evidence_json TEXT NOT NULL,
                  correctness REAL CHECK(correctness IS NULL),
                  scoring_status TEXT NOT NULL CHECK(scoring_status='unverified'),
                  hint_level INTEGER CHECK(hint_level IS NULL),
                  independent INTEGER CHECK(independent IS NULL),
                  elapsed_seconds INTEGER CHECK(elapsed_seconds IS NULL),
                  confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
                  cloud_transmission_allowed INTEGER NOT NULL DEFAULT 0 CHECK(cloud_transmission_allowed=0),
                  observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observed_answer_evidence_child
                  ON observed_answer_evidence(child_id, observed_at DESC);
                CREATE TRIGGER IF NOT EXISTS observed_answer_evidence_no_update
                BEFORE UPDATE ON observed_answer_evidence
                BEGIN SELECT RAISE(ABORT, 'immutable observed answer evidence'); END;
                CREATE TABLE IF NOT EXISTS document_monitor_request (
                  client_action_id TEXT PRIMARY KEY,
                  monitor_id TEXT NOT NULL REFERENCES document_monitor_session(monitor_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  operation TEXT NOT NULL CHECK(operation IN ('event','revision')),
                  request_sha256 TEXT NOT NULL,
                  event_id TEXT REFERENCES document_monitor_event(event_id) ON DELETE CASCADE,
                  revision_id TEXT REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                  CHECK((operation='event' AND event_id IS NOT NULL AND revision_id IS NULL)
                     OR (operation='revision' AND revision_id IS NOT NULL AND event_id IS NULL))
                );
                """
            )
            monitor_columns = {row['name'] for row in connection.execute('PRAGMA table_info(document_monitor_session)')}
            if not {'generation', 'generation_started_at'} <= monitor_columns:
                raise ValueError('DOCUMENT_MONITOR_MIGRATION_REQUIRED')
            revision_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(document_page_revision)")
            }
            if "study_signal_json" not in revision_columns:
                raise ValueError('DOCUMENT_MONITOR_MIGRATION_REQUIRED')
            from .answer_attempt_ledger import ensure_visual_promotion_schema
            ensure_visual_promotion_schema(connection)
            self._backfill_question_observations(connection)
            connection.commit()

    @staticmethod
    def _backfill_question_observations(connection: Any) -> None:
        """Idempotently migrate previously accepted OCR revisions into the question bank.

        A page that is still under the camera may keep the same content signature
        for hours.  Waiting for a changed page would therefore strand every
        revision accepted before the question-bank tables were introduced.
        Only revisions with no recorded occurrences are considered, and the
        normal observation writer retains the same evidence and dedupe rules.
        """
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_artifact'"
        ).fetchone():
            return
        rows = connection.execute(
            """SELECT r.revision_id,r.created_at,
                      a.artifact_id,a.child_id,a.session_id,a.extracted_text,
                      a.confidence,a.details_json,a.status
               FROM document_page_revision AS r
               JOIN capture_artifact AS a ON a.artifact_id=r.artifact_id
               WHERE NOT EXISTS (
                       SELECT 1 FROM observed_question_occurrence AS occurrence
                       WHERE occurrence.revision_id=r.revision_id
                     )
                  OR (NOT EXISTS (
                        SELECT 1 FROM observed_answer_evidence AS answer
                        WHERE answer.revision_id=r.revision_id
                      ) AND (
                        a.details_json LIKE '%handwritten%'
                        OR a.details_json LIKE '%grading_mark%'
                        OR a.details_json LIKE '%wrong_question_candidates%'
                      ))
               ORDER BY r.created_at,r.revision_index"""
        ).fetchall()
        for row in rows:
            DocumentMonitoringService._record_question_observations(
                connection, row, row["revision_id"], row["created_at"]
            )

    def start(self, session_id: str, client_action_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Keep the physical document-camera preview available throughout an
            # active daily session. OCR and evidence writes remain protected by
            # _active_study_session/_active_monitor, so opening the preview
            # during check-in cannot create learning conclusions prematurely.
            session = self._active_session(session_id, connection)
            existing = connection.execute(
                "SELECT monitor_id,child_id FROM document_monitor_session WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing:
                if existing["child_id"] != session["child_id"]:
                    raise ValueError("document monitor session ownership mismatch")
                connection.execute(
                    """UPDATE document_monitor_session SET status='active',ended_at=NULL,
                       updated_at=?,generation_started_at=?,generation=generation+1 WHERE monitor_id=?""",
                    (now, now, existing["monitor_id"]),
                )
                monitor_id = existing["monitor_id"]
            else:
                monitor_id = str(uuid4())
                connection.execute(
                    """INSERT INTO document_monitor_session(
                         monitor_id,child_id,session_id,status,started_at,updated_at,generation_started_at
                       ) VALUES(?,?,?,'active',?,?,?)""",
                    (monitor_id, session["child_id"], session_id, now, now, now),
                )
            connection.commit()
        return self.status(session_id)

    def pause(self, session_id: str, client_action_id: str) -> dict:
        return self._set_status(session_id, "paused", False)

    def resume(self, session_id: str, client_action_id: str) -> dict:
        return self._set_status(session_id, "active", False)

    def stop(self, session_id: str, client_action_id: str) -> dict:
        return self._set_status(session_id, "stopped", True)

    def record_event(
        self,
        session_id: str,
        event_type: str,
        confidence: float,
        metrics: dict[str, Any],
        client_action_id: str,
        observed_at: str | None = None,
    ) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError("unsupported document monitor event")
        if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
            raise ValueError("invalid document monitor confidence")
        sanitized_metrics = self._metrics(metrics)
        request_hash = self._request_hash(client_action_id, {
            "event_type": event_type, "confidence": float(confidence),
            "metrics": metrics, "observed_at": observed_at,
        })
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            monitor = self._active_monitor(connection, session_id)
            recorded_id = self._replayed_request(connection, monitor, client_action_id, "event", request_hash)
            if recorded_id:
                return self._event(connection, recorded_id)
            replay = connection.execute(
                "SELECT * FROM document_monitor_event WHERE client_action_id=?", (client_action_id,)
            ).fetchone()
            if replay:
                if (replay["session_id"] != session_id or replay["child_id"] != monitor["child_id"]
                        or replay["monitor_id"] != monitor["monitor_id"]
                        or replay["event_type"] != event_type or replay["confidence"] != confidence
                        or json.loads(replay["metrics_json"]) != sanitized_metrics
                        or (observed_at is not None and replay["observed_at"] != observed_at)):
                    raise ValueError("document monitor action payload conflict")
                self._record_request(connection, monitor, client_action_id, "event", request_hash, replay["event_id"])
                connection.commit()
                return self._event(connection, replay["event_id"])
            event_id = str(uuid4())
            now = observed_at or utc_now()
            connection.execute(
                """INSERT INTO document_monitor_event(
                     event_id,monitor_id,child_id,session_id,event_type,confidence,
                     metrics_json,observed_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (event_id, monitor["monitor_id"], monitor["child_id"], session_id,
                 event_type, confidence, json.dumps(sanitized_metrics, ensure_ascii=False), now, client_action_id),
            )
            connection.execute(
                "UPDATE document_monitor_session SET current_state=?,updated_at=? WHERE monitor_id=?",
                (event_type, now, monitor["monitor_id"]),
            )
            self._record_request(connection, monitor, client_action_id, "event", request_hash, event_id)
            connection.commit()
            return self._event(connection, event_id)

    def record_revision(
        self, session_id: str, artifact_id: str, content_signature: str, client_action_id: str,
        *, expected_generation: int | None = None, _on_recorded=None,
    ) -> dict:
        if not content_signature or len(content_signature) > 512:
            raise ValueError("invalid document content signature")
        payload = {
            "artifact_id": artifact_id, "content_signature": content_signature,
        }
        if expected_generation is not None:
            payload["expected_generation"] = expected_generation
        request_hash = self._request_hash(client_action_id, payload)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            monitor = self._active_monitor(connection, session_id)
            # The OCR caller captures this epoch before starting work. Validate
            # inside the write transaction, including retries of a saved result.
            if expected_generation is not None and (
                type(expected_generation) is not int or expected_generation < 0
                or monitor["generation"] != expected_generation
            ):
                raise DocumentMonitorGenerationConflictError()
            recorded_id = self._replayed_request(connection, monitor, client_action_id, "revision", request_hash)
            if recorded_id:
                result = self._revision(connection, recorded_id)
                if _on_recorded is not None:
                    _on_recorded(connection, result)
                connection.commit()
                return result
            replay = connection.execute(
                "SELECT * FROM document_page_revision WHERE client_action_id=?", (client_action_id,)
            ).fetchone()
            if replay:
                if (replay["session_id"] != session_id or replay["child_id"] != monitor["child_id"]
                        or replay["monitor_id"] != monitor["monitor_id"]
                        or replay["artifact_id"] != artifact_id or replay["content_signature"] != content_signature):
                    raise ValueError("document monitor action payload conflict")
                self._record_request(connection, monitor, client_action_id, "revision", request_hash, replay["revision_id"])
                result = self._revision(connection, replay["revision_id"])
                if _on_recorded is not None:
                    _on_recorded(connection, result)
                connection.commit()
                return result
            artifact = connection.execute(
                """SELECT artifact_id,child_id,session_id,artifact_type,extracted_text,confidence,status,details_json
                   FROM capture_artifact WHERE artifact_id=?""",
                (artifact_id,),
            ).fetchone()
            if not artifact or artifact["session_id"] != session_id or artifact["child_id"] != monitor["child_id"] or artifact["artifact_type"] != "document_ocr":
                raise ValueError("OCR artifact does not belong to this document monitor")
            if artifact["status"] not in {"completed", "quality_warning"}:
                raise ValueError("OCR artifact is not complete")
            duplicate = connection.execute(
                "SELECT * FROM document_page_revision WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if duplicate:
                if (duplicate["monitor_id"] != monitor["monitor_id"] or duplicate["session_id"] != session_id
                        or duplicate["child_id"] != monitor["child_id"]
                        or duplicate["content_signature"] != content_signature):
                    raise ValueError("document revision artifact payload conflict")
                # Bind the alternate action ID too, so it cannot later identify another page.
                self._record_request(connection, monitor, client_action_id, "revision", request_hash, duplicate["revision_id"])
                result = self._revision(connection, duplicate["revision_id"])
                if _on_recorded is not None:
                    _on_recorded(connection, result)
                connection.commit()
                return result
            previous = connection.execute(
                """SELECT r.revision_id,r.revision_index,r.created_at,r.change_summary_json,
                          a.extracted_text
                   FROM document_page_revision r JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                   WHERE r.monitor_id=? ORDER BY r.revision_index DESC LIMIT 1""",
                (monitor["monitor_id"],),
            ).fetchone()
            current_text = self._normalized_text(artifact["extracted_text"] or "")
            previous_text = self._normalized_text(previous["extracted_text"] if previous else "")
            change_summary = self._change_summary(previous_text, current_text)
            revision_id = str(uuid4())
            now = utc_now()
            confidence = float(artifact["confidence"] or 0)
            review_required = confidence < .75
            study_signal = self._study_signal(
                connection,
                monitor["monitor_id"],
                previous,
                change_summary,
                confidence,
                review_required,
                now,
            )
            connection.execute(
                """INSERT INTO document_page_revision(
                     revision_id,monitor_id,child_id,session_id,artifact_id,revision_index,
                     content_signature,content_sha256,previous_revision_id,change_summary_json,
                     ocr_confidence,review_required,study_signal_json,created_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (revision_id, monitor["monitor_id"], monitor["child_id"], session_id, artifact_id,
                 (previous["revision_index"] + 1 if previous else 1), content_signature,
                 hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
                 previous["revision_id"] if previous else None,
                 json.dumps(change_summary, ensure_ascii=False), confidence, int(review_required),
                 json.dumps(study_signal, ensure_ascii=False), now,
                client_action_id),
            )
            self._record_question_observations(connection, artifact, revision_id, now)
            connection.execute(
                "UPDATE document_monitor_session SET current_state='stable',updated_at=? WHERE monitor_id=?",
                (now, monitor["monitor_id"]),
            )
            self._record_request(connection, monitor, client_action_id, "revision", request_hash, revision_id)
            result = self._revision(connection, revision_id)
            if _on_recorded is not None:
                _on_recorded(connection, result)
            connection.commit()
            return result

    def status(self, session_id: str) -> dict:
        with self.database.connect() as connection:
            monitor = connection.execute(
                "SELECT * FROM document_monitor_session WHERE session_id=?", (session_id,)
            ).fetchone()
            if not monitor:
                return {"status": "not_started", "session_id": session_id, "generation": 0, "latest_event": None, "latest_revision": None}
            latest_event = connection.execute(
                "SELECT event_id FROM document_monitor_event WHERE monitor_id=? ORDER BY observed_at DESC LIMIT 1",
                (monitor["monitor_id"],),
            ).fetchone()
            latest_revision = connection.execute(
                "SELECT revision_id FROM document_page_revision WHERE monitor_id=? ORDER BY revision_index DESC LIMIT 1",
                (monitor["monitor_id"],),
            ).fetchone()
            cloud_image_calls = 0
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_vision_request'"
            ).fetchone():
                # Count persisted requests, not artifact flags or duplicate references.
                cloud_image_calls = connection.execute(
                    """SELECT COUNT(*) FROM document_vision_request AS request
                       WHERE request.child_id=? AND request.status IN ('reserved','succeeded','failed')
                         AND EXISTS (
                           SELECT 1 FROM capture_artifact AS artifact
                           WHERE artifact.child_id=request.child_id AND artifact.session_id=?
                             AND json_extract(
                               CASE WHEN json_valid(artifact.details_json)
                                    THEN artifact.details_json ELSE '{}' END,
                               '$.cloud_vision_fallback.request_id'
                             )=request.request_id
                         )""",
                    (monitor["child_id"], session_id),
                ).fetchone()[0]
            return {
                **dict(monitor),
                "latest_event": self._event(connection, latest_event["event_id"]) if latest_event else None,
                "latest_revision": self._revision(connection, latest_revision["revision_id"]) if latest_revision else None,
                "raw_frames_stored": False,
                "cloud_image_calls": cloud_image_calls,
            }

    def _set_status(self, session_id: str, status: str, ended: bool) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            monitor = self._monitor(connection, session_id)
            if status == "active":
                session = self._active_session(session_id, connection)
                if monitor["child_id"] != session["child_id"]:
                    raise ValueError("document monitor session ownership mismatch")
            if monitor["status"] != status:
                connection.execute(
                    """UPDATE document_monitor_session SET status=?,updated_at=?,ended_at=?,generation_started_at=?,
                       generation=generation+1 WHERE monitor_id=?""",
                    (status, now, now if ended else None, now, monitor["monitor_id"]),
                )
            connection.commit()
        return self.status(session_id)

    def _active_session(self, session_id: str, connection: Any = None) -> Any:
        if connection is None:
            with self.database.connect() as opened:
                return self._active_session(session_id, opened)
        session = connection.execute(
            "SELECT session_id,child_id,status,state,ended_at FROM daily_session WHERE session_id=?", (session_id,)
        ).fetchone()
        if (not session or session["status"] != "active" or session["state"] not in PREVIEW_SESSION_STATES
                or session["ended_at"] is not None):
            raise ValueError("document preview requires an active study session workflow")
        return session

    def _active_study_session(self, session_id: str, connection: Any = None) -> Any:
        if connection is None:
            with self.database.connect() as opened:
                return self._active_study_session(session_id, opened)
        session = connection.execute(
            "SELECT session_id,child_id,status,state,ended_at FROM daily_session WHERE session_id=?", (session_id,)
        ).fetchone()
        if not session or session["status"] != "active" or session["state"] not in DOCUMENT_ANALYSIS_STATES or session["ended_at"] is not None:
            raise ValueError("document monitoring requires an active study session")
        return session

    def _active_monitor(self, connection: Any, session_id: str) -> Any:
        session = self._active_study_session(session_id, connection)
        if session['state'] != 'study':
            allowed = connection.execute('''SELECT 1 FROM daily_session s JOIN sensor_lease l ON l.session_id=s.session_id
                WHERE s.session_id=? AND s.child_id=? AND s.observation_status='enabled'
                AND l.device_type='document_camera' AND l.status='active' AND julianday(l.expires_at)>julianday('now') LIMIT 1''',
                (session_id, session['child_id'])).fetchone()
            if not allowed:
                raise ValueError('document camera authorization or lease unavailable')
        monitor = self._monitor(connection, session_id)
        if monitor["child_id"] != session["child_id"]:
            raise ValueError("document monitor session ownership mismatch")
        if monitor["status"] != "active":
            raise ValueError("document monitor is not active")
        return monitor

    @staticmethod
    def _request_hash(client_action_id: str, payload: dict[str, Any]) -> str:
        if not isinstance(client_action_id, str) or not client_action_id.strip() or len(client_action_id) > 200:
            raise ValueError("invalid document monitor action ID")
        try:
            serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ValueError("invalid document monitor request payload") from None
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _replayed_request(connection: Any, monitor: Any, action_id: str, operation: str, request_hash: str) -> str | None:
        row = connection.execute(
            "SELECT * FROM document_monitor_request WHERE client_action_id=?", (action_id,)
        ).fetchone()
        if row:
            if (row["session_id"] != monitor["session_id"] or row["child_id"] != monitor["child_id"]
                    or row["monitor_id"] != monitor["monitor_id"] or row["operation"] != operation
                    or row["request_sha256"] != request_hash):
                raise ValueError("document monitor action payload conflict")
            return row["event_id"] if operation == "event" else row["revision_id"]
        other_table = "document_page_revision" if operation == "event" else "document_monitor_event"
        if connection.execute(f"SELECT 1 FROM {other_table} WHERE client_action_id=?", (action_id,)).fetchone():
            raise ValueError("document monitor action payload conflict")
        return None

    @staticmethod
    def _record_request(connection: Any, monitor: Any, action_id: str, operation: str, request_hash: str, result_id: str) -> None:
        connection.execute("""INSERT INTO document_monitor_request
            (client_action_id,monitor_id,child_id,session_id,operation,request_sha256,event_id,revision_id)
            VALUES (?,?,?,?,?,?,?,?)""", (
            action_id, monitor["monitor_id"], monitor["child_id"], monitor["session_id"], operation, request_hash,
            result_id if operation == "event" else None, result_id if operation == "revision" else None,
        ))

    @staticmethod
    def _monitor(connection: Any, session_id: str) -> Any:
        monitor = connection.execute(
            "SELECT * FROM document_monitor_session WHERE session_id=?", (session_id,)
        ).fetchone()
        if not monitor:
            raise ValueError("document monitor has not started")
        return monitor

    @staticmethod
    def _metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        allowed = {"motion_score", "skin_ratio", "paper_ratio", "brightness", "contrast", "raw_frame_stored", "capture_reason"}
        result = {key: value for key, value in metrics.items() if key in allowed and isinstance(value, (str, int, float, bool))}
        result["raw_frame_stored"] = False
        return result

    @staticmethod
    def _normalized_text(text: str) -> str:
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:12000]

    @staticmethod
    def _question_fingerprint(text: str) -> str:
        compact = re.sub(r"\s+", "", text).casefold()
        return hashlib.sha256(compact.encode("utf-8")).hexdigest()

    @staticmethod
    def _cloud_question_blocks(details: dict[str, Any]) -> list[dict[str, Any]]:
        """Group grounded cloud OCR blocks into complete numbered questions.

        The cloud path only receives the privacy-gated binary page region.  Its
        result remains unverified evidence and is never sufficient for mastery,
        but it is materially safer than turning unreadable local OCR fragments
        into invented questions.
        """
        cloud = details.get("cloud_vision_fallback")
        if not isinstance(cloud, dict) or cloud.get("status") not in {"succeeded", "needs_review"}:
            return []
        privacy = cloud.get("privacy")
        if not isinstance(privacy, dict) or privacy.get("original_frame_uploaded") is not False:
            return []
        blocks = cloud.get("blocks")
        if not isinstance(blocks, list):
            return []
        grouped: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        numbered = re.compile(r"^\s*\d{1,3}\s*[.、．)]\s*\S")

        def flush() -> None:
            if not current:
                return
            printed = [item for item in current if item.get("source") not in {"handwritten", "grading_mark"}]
            answer = [item for item in current if item.get("source") in {"handwritten", "grading_mark"}]
            texts = [DocumentMonitoringService._normalized_text(str(item.get("text") or "")) for item in printed]
            text = "\n".join(value for value in texts if value)
            if len(text) < 4:
                return
            confidences = [float(item["confidence"]) for item in current
                           if isinstance(item.get("confidence"), (int, float)) and math.isfinite(item["confidence"])]
            grouped.append({
                "text": text,
                "block_kind": "question",
                "needs_manual_boundary_check": True,
                "confidence": min(confidences) if confidences else 0.0,
                "source_kind": "cloud_binary_page_ocr",
                "answer_evidence": answer,
            })

        for block in blocks:
            if not isinstance(block, dict) or block.get("kind") not in {"text", "formula"}:
                continue
            text = DocumentMonitoringService._normalized_text(str(block.get("text") or ""))
            if not text:
                continue
            source = block.get("source")
            if numbered.match(text) and source not in {"handwritten", "grading_mark"}:
                flush()
                current = [block]
            elif current:
                current.append(block)
        flush()
        return grouped

    @staticmethod
    def _answer_evidence(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if not isinstance(item, dict) or item.get("source") not in {"handwritten", "grading_mark"}:
                continue
            text = DocumentMonitoringService._normalized_text(str(item.get("text") or ""))[:500]
            confidence = item.get("confidence")
            if not text or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
                continue
            clean = {
                "source": item["source"], "text": text,
                "confidence": round(min(1.0, max(0.0, float(confidence))), 4),
            }
            bbox = item.get("bbox")
            if (isinstance(bbox, list) and len(bbox) == 4
                    and all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox)):
                clean["bbox"] = [round(float(value), 6) for value in bbox]
            result.append(clean)
        return result[:12]

    @staticmethod
    def _record_answer_evidence(
        connection: Any, *, occurrence_id: str, question_id: str, artifact: Any,
        revision_id: str, answer_evidence: list[dict[str, Any]], now: str,
    ) -> None:
        evidence = DocumentMonitoringService._answer_evidence(answer_evidence)
        if not evidence:
            return
        handwritten = [item["text"] for item in evidence if item["source"] == "handwritten"]
        grading = [item["text"] for item in evidence if item["source"] == "grading_mark"]
        kind = "mixed" if handwritten and grading else ("handwritten" if handwritten else "grading_mark")
        connection.execute(
            """INSERT OR IGNORE INTO observed_answer_evidence(
                 visual_answer_evidence_id,occurrence_id,question_id,child_id,session_id,
                 revision_id,artifact_id,answer_text,grading_marks_json,evidence_kind,
                 evidence_json,correctness,scoring_status,hint_level,independent,
                 elapsed_seconds,confidence,cloud_transmission_allowed,observed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'unverified',NULL,NULL,NULL,?,0,?)""",
            (str(uuid4()), occurrence_id, question_id, artifact["child_id"], artifact["session_id"],
             revision_id, artifact["artifact_id"], "\n".join(handwritten)[:4000],
             json.dumps(grading, ensure_ascii=False), kind,
             json.dumps(evidence, ensure_ascii=False, sort_keys=True),
             min(item["confidence"] for item in evidence), now),
        )

    @staticmethod
    def _record_question_observations(connection: Any, artifact: Any, revision_id: str, now: str) -> None:
        """Persist complete local question blocks and queue marked ones once.

        The scoring and crop publication step runs as part of the accepting
        revision transaction. A mark without bound answer facts stays in review.
        """
        from .mistake_capture import MistakeCaptureService

        try:
            details = json.loads(artifact["details_json"] or "{}")
        except (TypeError, ValueError):
            return
        # A candidate includes pixels from older instants. Neither normal
        # processing nor restart/backfill may turn it into question evidence.
        if isinstance(details, dict) and details.get('monitor_capture_kind') == 'multiframe_candidate':
            return
        pipeline = details.get("local_pipeline") if isinstance(details, dict) else None
        blocks = pipeline.get("question_blocks") if isinstance(pipeline, dict) else None
        blocks = blocks if isinstance(blocks, list) else []
        candidates = []
        local_engine = pipeline.get("engine") if isinstance(pipeline, dict) else None
        local_route = pipeline.get("route") if isinstance(pipeline, dict) else None
        strict_facts = {}
        corrections = {}
        orientation = ((pipeline.get('quality') or {}).get('orientation_evidence') or {}) if isinstance(pipeline, dict) else {}
        strict_source = artifact['status'] == 'completed' and orientation.get('ambiguous') is False
        for mark in (pipeline.get("correction_mark_candidates") or []) if isinstance(pipeline, dict) else []:
            if not isinstance(mark, dict) or mark.get("mark_kind") != "cross_like":
                continue
            key = DocumentMonitoringService._normalized_text(str(mark.get("text") or ""))
            corrections[key] = mark
            facts = mark.get("strict_capture_facts")
            if (strict_source and not mark.get("needs_manual_boundary_check", True) and isinstance(facts, dict)
                    and isinstance(facts.get("question_text"), str) and isinstance(facts.get("answer_text"), str)
                    and type(facts.get("confidence")) in (int, float) and .90 <= facts["confidence"] <= 1):
                strict_facts[key] = facts
        for block in blocks:
            if not isinstance(block, dict):
                continue
            key = DocumentMonitoringService._normalized_text(str(block.get("text") or ""))
            if block.get("needs_manual_boundary_check", True):
                if key in corrections:
                    candidates.append({**block, "source_kind": "local_ocr"})
                continue
            is_current = block.get("block_kind") == "question"
            bbox = block.get("bbox_xywh")
            is_legacy_rapidocr = (
                "block_kind" not in block
                and local_engine == "rapidocr-onnx"
                and local_route == "cpu"
                and isinstance(block.get("line_count"), int)
                and block["line_count"] > 0
                and isinstance(bbox, list)
                and len(bbox) >= 4
                and all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox[:4])
            )
            if is_current or is_legacy_rapidocr:
                candidates.append({
                    **block,
                    "source_kind": "local_ocr",
                    "question_block_schema": (
                        "rapidocr_question_block_v1" if is_legacy_rapidocr else "current"
                    ),
                })
        candidates.extend(DocumentMonitoringService._cloud_question_blocks(details))
        if not candidates:
            return
        page_text = DocumentMonitoringService._normalized_text(str(artifact["extracted_text"] or ""))
        marked = {
            DocumentMonitoringService._normalized_text(item["question_text"])
            for item in MistakeCaptureService._marked_wrong_questions(details)
        }
        marked_evidence = {
            DocumentMonitoringService._normalized_text(item["question_text"]): [{
                "source": "grading_mark",
                "text": str(item.get("mark_kind") or "visible_mark"),
                "confidence": item.get("confidence", 0.0),
            }]
            for item in MistakeCaptureService._marked_wrong_questions(details)
        }
        recorded_fingerprints: set[str] = set()
        for index, block in enumerate(candidates):
            question_text = DocumentMonitoringService._normalized_text(str(block.get("text") or ""))
            if len(question_text) < 2:
                continue
            if block.get("source_kind") == "local_ocr" and question_text not in page_text:
                continue
            source_text = question_text
            facts = strict_facts.get(source_text) if block.get("source_kind") == "local_ocr" else None
            if facts:
                question_text = DocumentMonitoringService._normalized_text(facts["question_text"])
            confidence = block.get("confidence")
            confidence = float(confidence) if isinstance(confidence, (int, float)) and math.isfinite(confidence) else 0.0
            confidence = round(min(1.0, max(0.0, confidence)), 4)
            fingerprint = DocumentMonitoringService._question_fingerprint(question_text)
            if fingerprint in recorded_fingerprints:
                continue
            recorded_fingerprints.add(fingerprint)
            existing = connection.execute(
                "SELECT * FROM observed_question WHERE child_id=? AND question_fingerprint=?",
                (artifact["child_id"], fingerprint),
            ).fetchone()
            question_id = existing["question_id"] if existing else str(uuid4())
            is_marked = source_text in marked or source_text in corrections
            if not existing:
                connection.execute(
                    """INSERT INTO observed_question(
                         question_id,child_id,question_fingerprint,canonical_text,
                         first_revision_id,latest_revision_id,latest_artifact_id,
                         occurrence_count,marked_review_count,first_seen_at,last_seen_at
                       ) VALUES(?,?,?,?,?,?,?,0,0,?,?)""",
                    (question_id, artifact["child_id"], fingerprint, question_text,
                     revision_id, revision_id, artifact["artifact_id"], now, now),
                )
            occurrence_id = str(uuid4())
            inserted = connection.execute(
                """INSERT OR IGNORE INTO observed_question_occurrence(
                     occurrence_id,question_id,child_id,revision_id,artifact_id,
                     confidence,marked_for_review,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (occurrence_id, question_id, artifact["child_id"], revision_id,
                 artifact["artifact_id"], confidence, int(is_marked), now),
            ).rowcount
            if inserted:
                connection.execute(
                    """UPDATE observed_question SET latest_revision_id=?,latest_artifact_id=?,
                         occurrence_count=occurrence_count+1,
                         marked_review_count=marked_review_count+?,last_seen_at=?
                       WHERE question_id=?""",
                    (revision_id, artifact["artifact_id"], int(is_marked), now, question_id),
                )
            else:
                recorded = connection.execute(
                    """SELECT occurrence_id FROM observed_question_occurrence
                       WHERE question_id=? AND revision_id=? AND child_id=?""",
                    (question_id, revision_id, artifact["child_id"]),
                ).fetchone()
                if not recorded:
                    continue
                occurrence_id = recorded["occurrence_id"]
            answer_evidence = list(block.get("answer_evidence") or [])
            if facts:
                answer_evidence.extend([
                    {"source": "handwritten", "text": facts["answer_text"], "confidence": facts["confidence"]},
                    {"source": "grading_mark", "text": "cross_like", "confidence": facts["confidence"],
                     "bbox": facts.get("mark_bbox_xywh")},
                ])
            else:
                answer_evidence.extend(marked_evidence.get(source_text, []))
            DocumentMonitoringService._record_answer_evidence(
                connection, occurrence_id=occurrence_id, question_id=question_id,
                artifact=artifact, revision_id=revision_id,
                answer_evidence=answer_evidence, now=now,
            )
            if not is_marked:
                continue
            linked = connection.execute(
                "SELECT mistake_candidate_id FROM observed_question WHERE question_id=?",
                (question_id,),
            ).fetchone()[0]
            if linked:
                continue
            equivalent_fingerprints = {fingerprint, DocumentMonitoringService._question_fingerprint(source_text)}
            duplicate = next((row for row in connection.execute(
                "SELECT candidate_id,question_text FROM mistake_capture_candidate WHERE child_id=?",
                (artifact["child_id"],),
            ).fetchall() if DocumentMonitoringService._question_fingerprint(row["question_text"]) in equivalent_fingerprints), None)
            candidate_id = duplicate["candidate_id"] if duplicate else str(uuid4())
            if not duplicate:
                bbox = facts.get("crop_bbox_xywh") if facts else block.get("bbox_xywh")
                region = dict(zip(("x", "y", "width", "height"), bbox[:4])) if (
                    isinstance(bbox, list) and len(bbox) >= 4
                    and all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox[:4])
                ) else None
                connection.execute(
                    """INSERT INTO mistake_capture_candidate(
                         candidate_id,artifact_id,child_id,question_index,question_text,
                         region_json,confidence,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
                    (candidate_id, artifact["artifact_id"], artifact["child_id"], index,
                     question_text, json.dumps(region, ensure_ascii=False) if region else None,
                     min(confidence, .69) if block.get("needs_manual_boundary_check") else confidence, now, now),
                )
                connection.execute(
                    """INSERT INTO mistake_capture_candidate_event(
                         event_id,candidate_id,child_id,event_type,payload_json,created_at,client_action_id
                       ) VALUES(?,?,?,'prepared',?,?,?)""",
                    (str(uuid4()), candidate_id, artifact["child_id"],
                     json.dumps({"question_text": question_text, "source": "continuous_document_monitor",
                                 "correctness": "unverified_mark_for_review"}, ensure_ascii=False),
                     now, f"auto-marked:{revision_id}:{fingerprint[:16]}"),
                )
            connection.execute(
                "UPDATE observed_question SET mistake_candidate_id=? WHERE question_id=?",
                (candidate_id, question_id),
            )

    @staticmethod
    def _change_summary(previous: str, current: str) -> dict:
        previous_lines = previous.splitlines()
        current_lines = current.splitlines()
        matcher = SequenceMatcher(None, previous_lines, current_lines, autojunk=False)
        added: list[str] = []
        removed: list[str] = []
        for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
            if tag in {"insert", "replace"}:
                added.extend(current_lines[right_start:right_end])
            if tag in {"delete", "replace"}:
                removed.extend(previous_lines[left_start:left_end])
        return {
            "similarity": round(matcher.ratio(), 4),
            "added_lines": added[:20],
            "removed_lines": removed[:20],
            "changed": previous != current,
        }

    @staticmethod
    def _study_signal(
        connection: Any,
        monitor_id: str,
        previous: Any,
        change_summary: dict[str, Any],
        confidence: float,
        review_required: bool,
        observed_at: str,
    ) -> dict[str, Any]:
        if review_required:
            return {
                "kind": "needs_review",
                "confidence": confidence,
                "may_prompt_child": False,
                "reason": "OCR或公式手写证据不足，不能判断学习进展。",
            }
        if not previous:
            return {
                "kind": "baseline",
                "confidence": confidence,
                "may_prompt_child": False,
                "reason": "已建立本页基线，尚无前后变化可比较。",
            }
        if change_summary["changed"]:
            return {
                "kind": "progress_observed",
                "confidence": confidence,
                "may_prompt_child": False,
                "reason": "页面文字发生变化，只记录可观察进展，不判断正误。",
            }
        # OCR revisions are isolated observations.  Their timestamps cannot
        # establish that a page remained visible, much less that a child was
        # stuck: the camera may have been paused, blocked, or off in between.
        # Do not turn the gap into a duration or a behavioral intervention.
        return {
            "kind": "observation_incomplete",
            "confidence": confidence,
            "may_prompt_child": False,
            "reason": "两次识别未发现可靠文字变化；无法确认期间页面持续可见或孩子是否需要帮助。",
        }

    @staticmethod
    def _event(connection: Any, event_id: str) -> dict:
        row = connection.execute("SELECT * FROM document_monitor_event WHERE event_id=?", (event_id,)).fetchone()
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        return result

    @staticmethod
    def _revision(connection: Any, revision_id: str) -> dict:
        row = connection.execute(
            """SELECT r.*,a.extracted_text,a.details_json AS artifact_details,
                      a.local_path AS artifact_local_path,
                      a.child_id AS artifact_child,a.session_id AS artifact_session
               FROM document_page_revision r
               JOIN capture_artifact a ON a.artifact_id=r.artifact_id WHERE r.revision_id=?""",
            (revision_id,),
        ).fetchone()
        result = dict(row)
        from .mistake_capture import MistakeCaptureService
        details = result.pop('artifact_details')
        artifact_local_path = result.pop('artifact_local_path')
        artifact_child, artifact_session = result.pop('artifact_child'), result.pop('artifact_session')
        owned = artifact_child == result['child_id'] and artifact_session == result['session_id']
        # A marker only routes a local confirmation; it never proves an error.
        try:
            details = json.loads(details or '{}')
        except (ValueError, TypeError):
            details = {}
        page_source = details.get('page_source') if isinstance(details, dict) else None
        if isinstance(page_source, dict):
            allowed = {
                key: page_source[key] for key in (
                    'version', 'status', 'sha256', 'byte_length', 'media_type', 'encrypted',
                    'local_only', 'cloud_transmission_allowed', 'retention_reason'
                ) if key in page_source
            }
            if not artifact_local_path:
                allowed['status'] = 'deleted'
            result['page_source'] = allowed
        text = DocumentMonitoringService._normalized_text(result['extracted_text'] or '')
        unchanged = hashlib.sha256(text.encode()).hexdigest() == result['content_sha256']
        try:
            candidates = MistakeCaptureService._marked_wrong_questions(details) if isinstance(details, dict) and owned and unchanged else []
        except (ValueError, TypeError, OverflowError):
            candidates = []
        result['marked_question_count'] = sum(
            DocumentMonitoringService._normalized_text(item['question_text']) in text for item in candidates)
        observed = connection.execute(
            """SELECT COUNT(*) AS observed_count,
                      SUM(CASE WHEN occurrence.marked_for_review=1 THEN 1 ELSE 0 END) AS marked_count,
                      SUM(CASE WHEN candidate.status='pending' THEN 1 ELSE 0 END) AS queued_count
               FROM observed_question_occurrence AS occurrence
               JOIN observed_question AS question ON question.question_id=occurrence.question_id
               LEFT JOIN mistake_capture_candidate AS candidate ON candidate.candidate_id=question.mistake_candidate_id
                    AND candidate.child_id=occurrence.child_id
               WHERE occurrence.revision_id=? AND occurrence.child_id=?""",
            (revision_id, result['child_id']),
        ).fetchone()
        result['observed_question_count'] = int(observed['observed_count'] or 0)
        result['marked_question_count'] = max(result['marked_question_count'], int(observed['marked_count'] or 0))
        result['queued_mistake_candidate_count'] = int(observed['queued_count'] or 0)
        result['auto_saved_mistakes'] = []
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mistake_question_crop'").fetchone():
            result['auto_saved_mistakes'] = [dict(mistake_id=row['mistake_id'], crop_available=True)
                for row in connection.execute('''SELECT crop.mistake_id FROM mistake_question_crop AS crop
                    JOIN mistake_case AS mistake USING(mistake_id,child_id)
                    WHERE crop.revision_id=? AND crop.child_id=? AND mistake.status<>'excluded'
                    ORDER BY crop.mistake_id''', (revision_id, result['child_id']))]
        result['observed_answer_evidence_count'] = connection.execute(
            "SELECT COUNT(*) FROM observed_answer_evidence WHERE revision_id=? AND child_id=?",
            (revision_id, result['child_id']),
        ).fetchone()[0]
        result["change_summary"] = json.loads(result.pop("change_summary_json"))
        result["study_signal"] = json.loads(result.pop("study_signal_json"))
        result["review_required"] = bool(result["review_required"])
        result["raw_image_transmitted"] = False
        return result
