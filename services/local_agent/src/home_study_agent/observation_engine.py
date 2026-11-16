from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from copy import copy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import json

import cv2
import numpy as np

from .database import Database
from .repository import UnknownSessionError, utc_now
from .behavior_model.outcome_evidence import COVERAGE_VERSION, task_context_snapshot, validate_coverage_sample


@dataclass
class SessionObservation:
    previous_gray: np.ndarray | None = None
    state: str = "unknown"
    state_started_at: datetime | None = None
    last_motion_at: datetime | None = None
    no_face_started_at: datetime | None = None
    face_seen_at: datetime | None = None
    last_persisted_at: datetime | None = None
    last_analyzed_at: datetime | None = None
    last_state_event_id: str | None = None
    last_state_event_state: str | None = None
    suppressed_states: set[str] | None = None


class ObservationEngine:
    def __init__(self, database: Database):
        self.database = database
        self._sessions: dict[str, SessionObservation] = {}
        self._coverage_analysis: dict[str, dict] = {}
        self._coverage_producer_epoch = str(uuid4())
        self._capture_openings: dict[str, str] = {}
        self._deferred_events = None
        self._now = lambda: datetime.now(timezone.utc)
        self._face = None
        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
            detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if not detector.empty():
                self._face = detector

    def analyze(self, session_id: str, frame_bytes: bytes) -> dict:
        session = self._active_session(session_id)
        frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[0] < 100 or frame.shape[1] < 120:
            raise InvalidObservationFrameError("观察帧无法解码或尺寸过小。")
        now = self._now()
        tracker = self._sessions.setdefault(session_id, SessionObservation(state_started_at=now, last_motion_at=now))
        if tracker.last_analyzed_at and (now - tracker.last_analyzed_at).total_seconds() < 0.5:
            return self._remember_coverage_analysis(session_id, {
                "state": tracker.state, "state_event_id": None, "observed_at": now.isoformat(), "confidence": 0.0,
                "reason": "观察分析按 0.5FPS 常态预算跳过本帧，摄像头预览不受影响。", "duration_ms": 0,
                "model_version": "opencv_rules_v1", "shadow": True, "intervention": None, "raw_frame_stored": False,
                "observation_ready": False, "analysis_skipped": "fps_budget", "scene_quality": {"status": "not_evaluated", "message": "本帧未进入分析。"},
                "person_baseline": {"status": "not_evaluated", "message": "本帧未进入分析。"}, "metrics": {"input_fps_cap": 0.5},
            }, now)
        source_height, source_width = frame.shape[:2]
        scale = min(1.0, 640 / max(source_width, source_height))
        if scale < 1:
            frame = cv2.resize(frame, (round(source_width * scale), round(source_height * scale)), interpolation=cv2.INTER_AREA)
        tracker.last_analyzed_at = now
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        scene_quality = self._scene_quality(brightness, sharpness)
        motion = 0.0
        if scene_quality[0] == "ready":
            if tracker.previous_gray is not None:
                previous = cv2.resize(tracker.previous_gray, (gray.shape[1], gray.shape[0]))
                motion = float(cv2.absdiff(gray, previous).mean())
            else:
                # The first usable frame establishes a fresh stillness clock.
                # Time spent in an unusable scene is not behavioral evidence.
                tracker.last_motion_at = now
            tracker.previous_gray = gray
        else:
            # A dark, overexposed, or blurred frame is not comparable motion
            # evidence.  Clearing it prevents the next usable frame from being
            # misread as writing merely because lighting changed.
            tracker.previous_gray = None

        state, confidence, reason = self._classify(gray, brightness, sharpness, motion, tracker, now)
        person_baseline = (
            ("established", "当前会话已在取景中确认过人物基线。")
            if tracker.face_seen_at is not None
            else ("awaiting_person", "请让孩子在常坐位置正对镜头几秒，确认取景后再开始观察。")
        )
        observation_ready = scene_quality[0] == "ready" and person_baseline[0] == "established"
        with self.database.connect() as connection:
            rule = connection.execute(
                "SELECT observation_mode FROM family_rule WHERE child_id = ?",
                (session["child_id"],),
            ).fetchone()
        shadow = not rule or rule["observation_mode"] != "gentle"
        if tracker.suppressed_states and state in tracker.suppressed_states:
            confidence = min(confidence, 0.35)
            reason += " 孩子曾纠正同类候选，已降低置信度。"
        if motion >= 2.0:
            tracker.last_motion_at = now
        changed = state != tracker.state
        if changed:
            tracker.state = state
            tracker.state_started_at = now
        duration_ms = round((now - (tracker.state_started_at or now)).total_seconds() * 1000)
        # Until both the scene and this session's person baseline qualify, even
        # a reversible state event would contradict the child-facing promise
        # that we are only checking framing.  Persist the separate, frame-free
        # qualification record instead; do not create an "unknown" behavior
        # record for darkness or a camera that has not seen the child yet.
        should_persist = observation_ready and (
            changed
            or tracker.last_persisted_at is None
            or (now - tracker.last_persisted_at).total_seconds() >= 60
        )
        state_event_id = None
        observed_at = utc_now()
        if should_persist:
            state_event_id = str(uuid4())
            if self._deferred_events is not None:
                self._deferred_events.append(("""
                    INSERT INTO child_state_event(
                      state_event_id, child_id, session_id, state, confidence, reason,
                      duration_ms, model_version, shadow, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'opencv_rules_v1', ?, ?)
                    """,
                    (state_event_id, session["child_id"], session_id, state, confidence, reason, duration_ms, int(shadow), observed_at),))
            else:
                with self.database.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO child_state_event(
                          state_event_id, child_id, session_id, state, confidence, reason,
                          duration_ms, model_version, shadow, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'opencv_rules_v1', ?, ?)
                        """,
                        (state_event_id, session["child_id"], session_id, state, confidence, reason, duration_ms, int(shadow), observed_at),
                    )
                    connection.commit()
            tracker.last_persisted_at = now
            tracker.last_state_event_id = state_event_id
            tracker.last_state_event_state = state
        # This engine only qualifies/persists observable evidence.  The longitudinal
        # behavior model owns every active prompt, cooldown, and frequency cap.
        prompt_candidate = observation_ready and not shadow and state == "paused_like" and confidence >= .5 and duration_ms >= 60_000
        return self._remember_coverage_analysis(session_id, {
            "state": state,
            "state_event_id": state_event_id,
            "observed_at": observed_at,
            "confidence": confidence,
            "reason": reason,
            "duration_ms": duration_ms,
            "model_version": "opencv_rules_v1",
            "shadow": shadow,
            "intervention": None,
            "prompt_candidate": prompt_candidate,
            "raw_frame_stored": False,
            "observation_ready": observation_ready,
            "scene_quality": {
                "status": scene_quality[0],
                "message": scene_quality[1],
            },
            "person_baseline": {
                "status": person_baseline[0],
                "message": person_baseline[1],
            },
            "metrics": {"brightness": round(brightness, 1), "sharpness": round(sharpness, 1), "motion": round(motion, 2), "input_width": int(frame.shape[1]), "input_height": int(frame.shape[0]), "source_width": int(source_width), "source_height": int(source_height), "input_fps_cap": 0.5},
        }, now)

    def prepare_capture(self, session_id: str, frame_bytes: bytes, opening_event_id: str | None = None):
        """Analyze privately. Neither a rejected frame nor a preview changes live memory/DB."""
        prepared = copy(self)
        previous = self._sessions.get(session_id) if self._capture_openings.get(session_id) == opening_event_id and opening_event_id else None
        prepared._sessions = {session_id: copy(previous)} if previous else {}
        prepared._coverage_analysis = {}
        prepared._capture_openings = {}
        prepared._deferred_events = []
        prepared._capture_parent = self
        prepared._capture_session = session_id
        prepared._capture_opening = opening_event_id
        analysis = prepared.analyze(session_id, frame_bytes)
        return prepared, analysis

    def persist_capture(self, session_id: str, analysis: dict, connection) -> None:
        if self._deferred_events is None or self._capture_session != session_id or not connection.in_transaction:
            raise RuntimeError("Prepared observation transaction required")
        for sql, parameters in self._deferred_events:
            connection.execute(sql, parameters)
        # Task context and trusted claim must use the same write snapshot as admission.
        self._remember_coverage_analysis(session_id, analysis, datetime.fromisoformat(self._coverage_analysis[session_id]["received_at"]), connection=connection)

    def accept_capture(self, session_id: str, prepared) -> None:
        if prepared._capture_parent is not self or prepared._capture_session != session_id or not prepared._capture_opening:
            raise RuntimeError("Prepared observation owner mismatch")
        self._sessions[session_id] = prepared._sessions[session_id]
        self._coverage_analysis[session_id] = prepared._coverage_analysis[session_id]
        self._capture_openings[session_id] = prepared._capture_opening

    def framing_only(self, session_id: str, frame_bytes: bytes) -> dict:
        _, result = self.prepare_capture(session_id, frame_bytes)
        result.pop("coverage_analysis_id", None)
        result.update(state="unknown", state_event_id=None, confidence=0.0, observation_ready=False,
                      shadow=True, prompt_candidate=False, intervention=None, analysis_scope="framing_only",
                      reason="缺采样绑定仅检查取景；本帧不会记录行为、人物基线或提醒。",
                      person_baseline={"status": "not_evaluated", "message": "缺采样绑定仅检查取景。"},
                      coverage_record={"status": "unavailable", "reason": "CAPTURE_CLOCK_MISSING"},
                      qualification={"status": "framing_only", "scene_quality": result["scene_quality"]["status"],
                                     "person_baseline": "not_evaluated", "longitudinal_inference_enabled": False,
                                     "raw_frame_stored": False}, longitudinal_recorded=False)
        return result

    def _remember_coverage_analysis(self, session_id: str, result: dict, received_at: datetime, *, connection=None) -> dict:
        """Retain only the latest frame-free verdict, not caller-provided quality."""
        analysis_id = str(uuid4())
        result["coverage_analysis_id"] = analysis_id
        session = self._active_session(session_id, connection=connection)
        with (self.database.connect() if connection is None else nullcontext(connection)) as db:
            tasks = db.execute(
                """SELECT pi.plan_item_id,pi.subject,pi.task_type,pi.cognitive_load,pi.source_type,
                   dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
                   JOIN daily_session ds ON ds.session_id=dp.session_id
                   WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active' AND pi.status='active'
                     AND ds.state='study' AND ds.status='active'""",
                (session_id, session["child_id"]),
            ).fetchall()
        try:
            context = task_context_snapshot(dict(tasks[0])) if len(tasks) == 1 else None
        except (KeyError, TypeError, ValueError):
            context = None
        tracker = self._sessions.get(session_id)
        self._coverage_analysis[session_id] = {
            "producer_epoch": self._coverage_producer_epoch,
            "analysis_id": analysis_id, "child_id": session["child_id"], "received_at": received_at.isoformat(),
            "quality": {"observation_ready": result.get("observation_ready") is True,
                        "scene_quality": result.get("scene_quality", {}).get("status", "not_evaluated"),
                        "person_baseline": result.get("person_baseline", {}).get("status", "not_evaluated"),
                        "shadow": result.get("shadow") is not False,
                        "analysis_skipped": bool(result.get("analysis_skipped"))},
            "state": result["state"], "confidence": result["confidence"], "model_version": result["model_version"],
            "task_context": context,
            "state_event_id": (tracker.last_state_event_id if tracker and tracker.last_state_event_state == result["state"] else None),
        }
        return result

    def build_coverage_sample(
        self, session_id: str, *, stream_epoch: str, sequence: int, captured_at: str,
        capture_monotonic_ms: int, analysis: dict, plan_item_id: str | None = None, connection=None,
    ) -> dict:
        """Call immediately after analyze; analysis must be that engine's latest result.

        The collector supplies a fresh-frame sequence and clocks, never quality,
        received_at, child identity, or task context. HTTP wiring remains external.
        """
        self._active_session(session_id, connection=connection)
        trusted = self._coverage_analysis.get(session_id)
        if not trusted or analysis.get("coverage_analysis_id") != trusted["analysis_id"]:
            raise ValueError("Coverage requires this engine's latest analysis")
        context = trusted["task_context"]
        if plan_item_id is not None and (context is None or context["plan_item_id"] != plan_item_id):
            raise ValueError("Coverage task does not match the analyzed session")
        sample = {"schema_version": COVERAGE_VERSION, "session_id": session_id,
                  "stream_epoch": stream_epoch, "sequence": sequence, "captured_at": captured_at,
                  "capture_monotonic_ms": capture_monotonic_ms, **trusted}
        # Return a detached JSON value so caller changes cannot mutate the trusted verdict.
        sample = json.loads(json.dumps(sample, allow_nan=False))
        validate_coverage_sample(sample)
        return sample

    def assess_calibration(self, frame_bytes: bytes) -> dict:
        frame = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[0] < 100 or frame.shape[1] < 120:
            raise InvalidObservationFrameError("校准画面无法解码或尺寸过小。")
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        scene_status, scene_message = self._scene_quality(brightness, sharpness)
        face_count = 0
        if self._face is not None and scene_status == "ready":
            small = cv2.resize(gray, None, fx=0.5, fy=0.5)
            faces = self._face.detectMultiScale(
                small, scaleFactor=1.15, minNeighbors=4, minSize=(30, 30)
            )
            face_count = len(faces)
        person_visible = face_count > 0
        calibration_ready = scene_status == "ready" and person_visible
        if self._face is None:
            person_message = "本地人脸检测组件未就绪，不能完成座位校准。"
        elif person_visible:
            person_message = "画面中检测到正面人物；正式学习时仍会重新建立会话人物基线。"
        else:
            person_message = "画面中没有检测到正面人物，请让孩子坐在常用位置并面向镜头。"
        return {
            "calibration_ready": calibration_ready,
            "scene_quality": {"status": scene_status, "message": scene_message},
            "person_visible": person_visible,
            "person_message": person_message,
            "metrics": {
                "brightness": round(brightness, 1),
                "sharpness": round(sharpness, 1),
                "width": int(frame.shape[1]),
                "height": int(frame.shape[0]),
            },
            "model_version": "opencv_calibration_v1",
            "raw_frame_stored": False,
        }

    def release(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._coverage_analysis.pop(session_id, None)
        self._capture_openings.pop(session_id, None)

    def begin_qualification(self, session_id: str) -> dict:
        """Start a fresh, frame-free qualification attempt for this session.

        A renderer restart must not inherit an in-memory person baseline.  The
        durable record is therefore useful for explaining the last result, but
        never authorizes behavior evidence by itself.
        """
        session = self._active_session(session_id)
        with self.database.connect() as connection:
            self.begin_qualification_in(connection, session_id)
            connection.commit()
        return self.qualification(session_id)

    def begin_qualification_in(self, connection, session_id: str) -> None:
        """Existing frame-free qualification SQL, borrowing the caller's transaction."""
        session = connection.execute("SELECT session_id, child_id, status, observation_status FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
        if not session:
            raise UnknownSessionError(session_id)
        if session["status"] != "active":
            raise InvalidObservationFrameError("学习会话已经结束。")
        if session["observation_status"] != "enabled":
            raise ObservationPausedError("观察已暂停；恢复后才会继续处理新画面。")
        now = utc_now()
        connection.execute(
            """
            INSERT INTO observation_qualification(
              session_id, child_id, status, scene_quality, person_baseline,
              reason, source, attempt_count, qualified_at, updated_at
            ) VALUES (?, ?, 'qualifying', 'not_evaluated', 'not_evaluated', ?, 'renderer', 1, NULL, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              status = 'qualifying', scene_quality = 'not_evaluated',
              person_baseline = 'not_evaluated', reason = excluded.reason,
              source = 'renderer', attempt_count = observation_qualification.attempt_count + 1,
              qualified_at = NULL, updated_at = excluded.updated_at
            """,
            (session_id, session["child_id"], "正在本机确认取景和常坐位置；通过前不会记录行为状态或发出提醒。", now),
        )

    def record_qualification_from_analysis(self, session_id: str, analysis: dict, *, connection=None) -> dict:
        """Persist only the structured quality verdict from one local frame."""
        owns_connection = connection is None
        if analysis.get("analysis_skipped"):
            return self.qualification(session_id, connection=connection)
        session = self._active_session(session_id, connection=connection)
        scene = str((analysis.get("scene_quality") or {}).get("status") or "not_evaluated")
        baseline = str((analysis.get("person_baseline") or {}).get("status") or "not_evaluated")
        if scene not in {"not_evaluated", "ready", "too_dark", "too_bright", "blurred", "unusable_frame"}:
            scene = "not_evaluated"
        if baseline not in {"not_evaluated", "established", "awaiting_person"}:
            baseline = "not_evaluated"
        ready = analysis.get("observation_ready") is True
        reason = str(analysis.get("reason") or "本机取景尚未通过确认。")[:500]
        status = "qualified" if ready else "unqualified"
        now = utc_now()
        with (self.database.connect() if connection is None else nullcontext(connection)) as connection:
            connection.execute(
                """
                INSERT INTO observation_qualification(
                  session_id, child_id, status, scene_quality, person_baseline,
                  reason, source, attempt_count, qualified_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'engine', 0, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  status = excluded.status, scene_quality = excluded.scene_quality,
                  person_baseline = excluded.person_baseline, reason = excluded.reason,
                  source = 'engine',
                  qualified_at = CASE WHEN excluded.status = 'qualified' THEN excluded.updated_at ELSE NULL END,
                  updated_at = excluded.updated_at
                """,
                (session_id, session["child_id"], status, scene, baseline, reason, now if ready else None, now),
            )
            if owns_connection:
                connection.commit()
        return self.qualification(session_id, connection=None if owns_connection else connection)

    def record_renderer_unavailable(self, session_id: str, scene_quality: str, reason: str, *, connection=None, persist=True) -> dict:
        """Record a browser-side frame-health failure without sending a frame."""
        session = self._active_session(session_id, connection=connection)
        scene = {"frozen": "unusable_frame", "too_dark": "too_dark", "too_bright": "too_bright", "unusable_frame": "unusable_frame"}.get(scene_quality)
        if scene is None:
            raise ValueError("unsupported renderer observation quality")
        now = utc_now()
        if not persist:
            return {**self.qualification(session_id, connection=connection), "status": "unavailable",
                    "scene_quality": scene, "person_baseline": "not_evaluated", "reason": reason[:500],
                    "source": "renderer", "qualified_at": None, "analysis_scope": "framing_only",
                    "longitudinal_inference_enabled": False}
        owns_connection = connection is None
        with (self.database.connect() if connection is None else nullcontext(connection)) as connection:
            connection.execute(
                """
                INSERT INTO observation_qualification(
                  session_id, child_id, status, scene_quality, person_baseline,
                  reason, source, attempt_count, qualified_at, updated_at
                ) VALUES (?, ?, 'unavailable', ?, 'not_evaluated', ?, 'renderer', 0, NULL, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  status = 'unavailable', scene_quality = excluded.scene_quality,
                  person_baseline = 'not_evaluated', reason = excluded.reason,
                  source = 'renderer', qualified_at = NULL, updated_at = excluded.updated_at
                """,
                (session_id, session["child_id"], scene, reason[:500], now),
            )
            if owns_connection:
                connection.commit()
        return self.qualification(session_id, connection=None if owns_connection else connection)

    def pause_qualification(self, session_id: str) -> dict:
        session = self._active_session(session_id, require_enabled=False)
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO observation_qualification(
                  session_id, child_id, status, scene_quality, person_baseline,
                  reason, source, attempt_count, qualified_at, updated_at
                ) VALUES (?, ?, 'paused', 'not_evaluated', 'not_evaluated', ?, 'renderer', 0, NULL, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  status = 'paused', reason = excluded.reason, source = 'renderer',
                  qualified_at = NULL, updated_at = excluded.updated_at
                """,
                (session_id, session["child_id"], "观察已暂停；恢复后会重新确认取景和常坐位置。", now),
            )
            connection.commit()
        return self.qualification(session_id)

    def qualification(self, session_id: str, *, connection=None) -> dict:
        session = self._active_session(session_id, require_enabled=False, connection=connection)
        with (self.database.connect() if connection is None else nullcontext(connection)) as connection:
            row = connection.execute(
                """
                SELECT status, scene_quality, person_baseline, reason, source,
                       attempt_count, qualified_at, updated_at
                FROM observation_qualification
                WHERE session_id = ? AND child_id = ?
                """,
                (session_id, session["child_id"]),
            ).fetchone()
        if row:
            return {**dict(row), "raw_frame_stored": False, "longitudinal_inference_enabled": row["status"] == "qualified"}
        return {
            "status": "qualifying", "scene_quality": "not_evaluated", "person_baseline": "not_evaluated",
            "reason": "等待本机取景确认；通过前不会记录行为状态或发出提醒。", "source": "renderer",
            "attempt_count": 0, "qualified_at": None, "updated_at": None,
            "raw_frame_stored": False, "longitudinal_inference_enabled": False,
        }

    def correct(self, session_id: str, correction_text: str, client_action_id: str) -> dict:
        # Corrections withdraw sources used by frozen reports. Serialize the
        # entire admission and write against their final network submission.
        with self.database.external_effect_guard(timeout=2):
            return self._correct_guarded(session_id, correction_text, client_action_id)

    def _correct_guarded(self, session_id: str, correction_text: str, client_action_id: str) -> dict:
        session = self._active_session(session_id)
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT correction_id FROM observation_correction WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                return self._get_correction(connection, existing["correction_id"])
            latest = connection.execute(
                """
                SELECT state_event_id, state FROM child_state_event
                WHERE session_id = ? ORDER BY observed_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            correction_id = str(uuid4())
            previous_state = latest["state"] if latest else None
            connection.execute(
                """
                INSERT INTO observation_correction(
                  correction_id, child_id, session_id, state_event_id, previous_state,
                  correction_text, effect, occurred_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'confidence_suppressed', ?, ?)
                """,
                (
                    correction_id, session["child_id"], session_id,
                    latest["state_event_id"] if latest else None, previous_state,
                    correction_text, now.isoformat(), client_action_id,
                ),
            )
            rule = connection.execute(
                """
                SELECT observation_mode, misclassification_shadow_threshold
                FROM family_rule WHERE child_id = ?
                """,
                (session["child_id"],),
            ).fetchone()
            recent_corrections = connection.execute(
                """
                SELECT COUNT(*) FROM observation_correction
                WHERE child_id = ? AND occurred_at >= ?
                """,
                (
                    session["child_id"],
                    (now - timedelta(days=7)).isoformat(),
                ),
            ).fetchone()[0]
            if (
                rule
                and rule["observation_mode"] == "gentle"
                and recent_corrections >= rule["misclassification_shadow_threshold"]
            ):
                connection.execute(
                    """
                    UPDATE family_rule SET observation_mode = 'shadow', version = version + 1,
                      updated_at = ? WHERE child_id = ?
                    """,
                    (now.isoformat(), session["child_id"]),
                )
                connection.execute(
                    "UPDATE observation_correction SET effect = 'auto_shadowed' WHERE correction_id = ?",
                    (correction_id,),
                )
                connection.execute(
                    """
                    INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                    VALUES (?, 'system', 'observation_auto_shadowed', ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        session["child_id"],
                        '{"window_days":7,"reason":"misclassification_threshold_reached"}',
                        now.isoformat(),
                    ),
                )
            connection.commit()
            result = self._get_correction(connection, correction_id)
        tracker = self._sessions.setdefault(session_id, SessionObservation())
        if tracker.suppressed_states is None:
            tracker.suppressed_states = set()
        if previous_state:
            tracker.suppressed_states.add(previous_state)
        return result

    @staticmethod
    def _get_correction(connection, correction_id: str) -> dict:
        row = connection.execute(
            """
            SELECT correction_id, session_id, state_event_id, previous_state, correction_text, effect, occurred_at
            FROM observation_correction WHERE correction_id = ?
            """,
            (correction_id,),
        ).fetchone()
        return dict(row)

    def _classify(self, gray, brightness: float, sharpness: float, motion: float, tracker: SessionObservation, now: datetime) -> tuple[str, float, str]:
        if brightness < 25 or brightness > 245:
            return "unknown", 0.2, "画面过暗或过亮，不能可靠判断。"
        if sharpness < 8:
            return "unknown", 0.25, "画面模糊，不能可靠判断。"
        if self._face is None:
            return "unknown", 0.1, "本地人脸检测组件未就绪，暂不判断孩子状态。"
        small = cv2.resize(gray, None, fx=0.5, fy=0.5)
        faces = self._face.detectMultiScale(small, scaleFactor=1.15, minNeighbors=4, minSize=(30, 30))
        if len(faces) == 0:
            if tracker.face_seen_at is None:
                tracker.no_face_started_at = None
                return "unknown", 0.3, "当前会话尚未看到正面人脸，不能区分孩子离开和镜头未对准。"
            if tracker.no_face_started_at is None:
                tracker.no_face_started_at = now
            elapsed = (now - tracker.no_face_started_at).total_seconds()
            return ("away", 0.62, "连续画面未检测到正面人脸，仅作为离开候选。") if elapsed >= 6 else ("unknown", 0.35, "暂未检测到正面人脸，等待更多帧。")
        tracker.no_face_started_at = None
        tracker.face_seen_at = now
        if motion >= 3.0:
            return "writing_like", 0.52, "检测到人物且画面存在持续局部活动，仅标记为书写样候选。"
        still_seconds = (now - (tracker.last_motion_at or now)).total_seconds()
        if still_seconds >= 20:
            return "paused_like", 0.5, "检测到人物且长时间低活动，仅用于温和询问，不解释为走神。"
        return "present", 0.72, "检测到正面人脸。"

    @staticmethod
    def _scene_quality(brightness: float, sharpness: float) -> tuple[str, str]:
        if brightness < 25:
            return "too_dark", "画面太暗，请打开正面照明，并把摄像头转向孩子常坐的位置。"
        if brightness > 245:
            return "too_bright", "画面过亮，请避免镜头正对灯光或窗户。"
        if sharpness < 8:
            return "blurred", "画面持续模糊，请擦拭镜头并重新对准座位。"
        return "ready", "取景和清晰度满足本地观察的基础条件。"

    def _active_session(self, session_id: str, *, require_enabled: bool = True, connection=None) -> dict:
        with (self.database.connect() if connection is None else nullcontext(connection)) as connection:
            row = connection.execute("SELECT session_id, child_id, status, observation_status FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            raise UnknownSessionError(session_id)
        if row["status"] != "active":
            raise InvalidObservationFrameError("学习会话已经结束。")
        if require_enabled and row["observation_status"] != "enabled":
            raise ObservationPausedError("观察已暂停；恢复后才会继续处理新画面。")
        return dict(row)


class InvalidObservationFrameError(Exception):
    pass


class ObservationPausedError(Exception):
    pass
