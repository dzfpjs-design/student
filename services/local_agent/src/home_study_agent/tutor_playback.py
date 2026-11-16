"""Durable segment delivery, never evidence of understanding or learning.

One open checkpoint per session. begin/previous/restart are paused; only an
explicit resume authorizes playback. Clients serialize actual started/ended
callbacks, retain all binding tokens, and stop audio on a conflict. Duration
hints are presentation metadata, never clocks or completion evidence.

runtime_epoch is internal: default is this process's boot identity. A multiworker
host must inject one shared, fresh boot epoch into all workers, not an HTTP flag.
First access after a boot change fences callbacks and pauses the unfinished
segment. Reconstructing a service within the same process is not a restart.

Only tutor_playback_checkpoint and tutor_playback_event are written. Their JSON
is local presentation data (possibly protected course text), not tutor source,
assessment, audit learning evidence, or cloud-safe data. Integration must include
these tables in privacy export/delete. Deleting a tutor_turn cascades both.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from typing import Literal
from uuid import uuid4

from .database import Database
from .repository import utc_now
from .tutor_blackboard import ALLOWED_TONES, PRESENTATION_VERSION, _validate_block


_PROCESS_NONCE = str(uuid4())
_COMMANDS = {"pause", "resume", "previous", "restart", "cancel"}
_OPEN = {"paused", "playing"}
_MAX_JSON_BYTES = 128 * 1024


class TutorPlaybackError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise TutorPlaybackError("INVALID_PRESENTATION_JSON") from exc


def _hash(value, namespace: str) -> str:
    return namespace + ":" + sha256(_json(value).encode()).hexdigest()


def _text(value, maximum=200) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(ord(char) < 32 and char not in "\n\r\t" for char in value))


def _presentation(canonical: dict, supplied: dict | None) -> dict:
    """Preserve the server prefix exactly; only local text appendices may differ."""
    value = canonical if supplied is None else supplied
    if not isinstance(value, dict) or len(_json(value).encode()) > _MAX_JSON_BYTES:
        raise TutorPlaybackError("INVALID_PRESENTATION")
    base = canonical.get("segments")
    segments = value.get("segments")
    if (canonical.get("version") != PRESENTATION_VERSION or not isinstance(base, list)
            or not 1 <= len(base) <= 8 or not isinstance(segments, list)
            or not len(base) <= len(segments) <= len(base) + 16
            or not set(canonical) <= {"version", "source", "sync_mode", "subject", "title", "segments",
                                     "repair_warnings", "degraded_reason"}
            or not _text(canonical.get("title"), 100) or not _text(canonical.get("source"), 100)
            or canonical.get("sync_mode") != "segment_estimate"
            or not isinstance(canonical.get("subject"), str) or len(canonical["subject"]) > 100):
        raise TutorPlaybackError("INVALID_PRESENTATION")
    if "degraded_reason" in canonical and not _text(canonical["degraded_reason"], 200):
        raise TutorPlaybackError("INVALID_PRESENTATION_METADATA")
    warnings = canonical.get("repair_warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 16 or any(not _text(w, 200) for w in warnings):
        raise TutorPlaybackError("INVALID_PRESENTATION_METADATA")
    if (_json(segments[:len(base)]) != _json(base)
            or _json({k: v for k, v in value.items() if k not in {"source", "segments"}})
            != _json({k: v for k, v in canonical.items() if k not in {"source", "segments"}})):
        raise TutorPlaybackError("PRESENTATION_PREFIX_CONFLICT")
    source = "model_plus_local_diagnostic" if len(segments) > len(base) else canonical.get("source")
    if value.get("source") != source:
        raise TutorPlaybackError("INVALID_PRESENTATION_SOURCE")
    seen = set()
    for index, item in enumerate(segments):
        if (not isinstance(item, dict) or not _text(item.get("segment_id"))
                or item["segment_id"] in seen or not _text(item.get("narration"), 8000)):
            raise TutorPlaybackError("INVALID_PRESENTATION_SEGMENT")
        seen.add(item["segment_id"])
        if (set(item) != {"segment_id", "narration", "duration_hint_ms", "blocks"}
                or type(item["duration_hint_ms"]) is not int or not 0 <= item["duration_hint_ms"] <= 60000
                or not isinstance(item["blocks"], list) or not 1 <= len(item["blocks"]) <= 6):
            raise TutorPlaybackError("INVALID_PRESENTATION_APPENDIX")
        if index < len(base):
            try:
                for block in item["blocks"]:
                    if _json(_validate_block(block)) != _json(block):
                        raise ValueError("non-canonical block")
            except (TypeError, ValueError) as exc:
                raise TutorPlaybackError("INVALID_PRESENTATION_BLOCK") from exc
            continue
        if not item["segment_id"].startswith(("local-course-", "local-diagnostic-")):
            raise TutorPlaybackError("INVALID_PRESENTATION_APPENDIX")
        for block in item["blocks"]:
            if (not isinstance(block, dict) or not {"kind", "text"} <= set(block) <= {"kind", "text", "tone"}
                    or not isinstance(block["kind"], str) or block["kind"] not in {"heading", "text", "question"}
                    or not isinstance(block.get("tone", "chalk"), str)
                    or block.get("tone", "chalk") not in ALLOWED_TONES or not _text(block["text"], 8000)):
                raise TutorPlaybackError("INVALID_PRESENTATION_APPENDIX")
    return json.loads(_json(value))


class TutorPlaybackService:
    def __init__(self, database: Database, *, runtime_epoch: str | None = None):
        self.database = database
        self.runtime_epoch = runtime_epoch if runtime_epoch is not None else f"{os.getpid()}:{_PROCESS_NONCE}"
        if not _text(self.runtime_epoch):
            raise TutorPlaybackError("INVALID_RUNTIME_EPOCH")
        with database.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS tutor_playback_checkpoint (
                    playback_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES tutor_turn(turn_id) ON DELETE CASCADE,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                    source_version TEXT NOT NULL,
                    presentation_version TEXT NOT NULL,
                    presentation_json TEXT NOT NULL,
                    runtime_epoch TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    status TEXT NOT NULL CHECK(status IN
                        ('paused','playing','playback_finished','cancelled','invalidated')),
                    segment_index INTEGER NOT NULL CHECK(segment_index >= 0),
                    segment_started INTEGER NOT NULL CHECK(segment_started IN (0,1)),
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tutor_playback_open
                    ON tutor_playback_checkpoint(session_id) WHERE status IN ('paused','playing');
                CREATE TABLE IF NOT EXISTS tutor_playback_event (
                    event_id TEXT PRIMARY KEY,
                    playback_id TEXT NOT NULL REFERENCES tutor_playback_checkpoint(playback_id) ON DELETE CASCADE,
                    client_action_id TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    segment_index INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tutor_playback_event_parent
                    ON tutor_playback_event(playback_id);
            """)

    @contextmanager
    def _transaction(self):
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except TutorPlaybackError:
                # Only guard invalidation/recovery precedes rejected input. Persist
                # those fences even when the first post-crash request is a late end.
                c.commit()
                raise
            else:
                c.commit()

    @staticmethod
    def _session(c, session_id):
        row = c.execute("""SELECT ds.*, cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp ON cp.child_id=ds.child_id WHERE ds.session_id=?""", (session_id,)).fetchone()
        if (not row or row["status"] != "active" or row["child_status"] != "active"
                or row["state"] in {"evolving", "completed"}):
            raise TutorPlaybackError("SESSION_INACTIVE")
        return row

    @staticmethod
    def _source(c, session, turn_id):
        row = c.execute("SELECT * FROM tutor_turn WHERE turn_id=?", (turn_id,)).fetchone()
        if not row or (row["session_id"], row["child_id"]) != (session["session_id"], session["child_id"]):
            raise TutorPlaybackError("SOURCE_OWNERSHIP_CONFLICT")
        if row["status"] != "completed" or row["safety_status"] != "passed":
            raise TutorPlaybackError("SOURCE_UNAVAILABLE")
        from .teaching_presentation_scope import replaced_by_question
        if replaced_by_question(c, session['session_id'], session['child_id'], row['created_at']):
            raise TutorPlaybackError('SOURCE_QUESTION_CHANGED')
        if row["plan_item_id"]:
            owner = c.execute("""SELECT dp.child_id, dp.session_id FROM plan_item pi
                JOIN daily_plan dp ON dp.plan_id=pi.plan_id WHERE pi.plan_item_id=?""",
                (row["plan_item_id"],)).fetchone()
            if not owner or (owner["session_id"], owner["child_id"]) != (session["session_id"], session["child_id"]):
                raise TutorPlaybackError("SOURCE_OWNERSHIP_CONFLICT")
        try:
            envelope = json.loads(row["output_envelope_json"] or "null")
            if not isinstance(envelope, dict) or not isinstance(envelope.get('result'), dict):
                raise ValueError('invalid envelope')
            from .reviewed_tutor_projection import is_projected
            saved_target=(envelope.get('local_teaching_control') or {}).get('question_target') or {}
            if is_projected(saved_target):
                from .teaching_question_target import resolve_question_target
                current_target=resolve_question_target(c,session['session_id'],subject=row['subject'],
                    question=row['question'],parent_turn_id=turn_id)
                if not is_projected(current_target) or current_target['fingerprint']!=saved_target.get('fingerprint'):
                    raise ValueError('reviewed teaching source changed')
            from .mistake_teaching_authority import validate_saved_teaching, MistakeTeachingRevokedError
            try:
                validate_saved_teaching(c, row, envelope)
            except MistakeTeachingRevokedError as error:
                raise ValueError('mistake dialogue changed') from error
            from .tutor_check_coaching import validate_saved
            validate_saved(c,row,envelope)
            if envelope['result'].get('local_visual'):
                from .visual_teaching import read_turn
                read_turn(c, session['session_id'], turn_id)
            if envelope.get('document_revisit'):
                from .document_revisit_feedback import read_saved
                read_saved(c,row)
            board = envelope["result"]["blackboard"]
            if not isinstance(board, dict):
                raise ValueError("not a board")
            board = _presentation(board, None)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise TutorPlaybackError("SOURCE_PRESENTATION_UNAVAILABLE") from exc
        return _hash(dict(row), "tutor_source_v1"), board

    @staticmethod
    def _owned(c, session, playback_id):
        row = c.execute("SELECT * FROM tutor_playback_checkpoint WHERE playback_id=?", (playback_id,)).fetchone()
        if not row or (row["session_id"], row["child_id"]) != (session["session_id"], session["child_id"]):
            raise TutorPlaybackError("PLAYBACK_OWNERSHIP_CONFLICT")
        return dict(row)

    def _guard(self, c, session, row):
        if row["status"] == "invalidated":
            return row
        try:
            version, _ = self._source(c, session, row["turn_id"])
        except TutorPlaybackError:
            version = None
        if version != row["source_version"]:
            row.update(status="invalidated", segment_started=0, reason="source_changed_or_withdrawn",
                       generation=row["generation"] + 1, revision=row["revision"] + 1)
            self._save(c, row)
        elif row["runtime_epoch"] != self.runtime_epoch and row["status"] in _OPEN:
            row.update(status="paused", segment_started=0, reason="runtime_restarted", runtime_epoch=self.runtime_epoch,
                       generation=row["generation"] + 1, revision=row["revision"] + 1)
            self._save(c, row)
        return row

    @staticmethod
    def _save(c, row):
        row["updated_at"] = utc_now()
        c.execute("""UPDATE tutor_playback_checkpoint SET status=:status, generation=:generation,
            revision=:revision, segment_index=:segment_index, segment_started=:segment_started,
            reason=:reason, runtime_epoch=:runtime_epoch, updated_at=:updated_at WHERE playback_id=:playback_id""", row)

    @staticmethod
    def _view(row, replayed=False):
        keys = ("playback_id", "turn_id", "child_id", "session_id", "source_version", "presentation_version",
                "generation", "revision", "status", "segment_index", "reason")
        return {**{k: row[k] for k in keys}, "segment_started": bool(row["segment_started"]),
                "presentation": None if row["status"] == "invalidated" else json.loads(row["presentation_json"]),
                "replayed": replayed, "evidence_kind": "playback_only"}

    @staticmethod
    def _receipt(c, action, request):
        if not _text(action):
            raise TutorPlaybackError("INVALID_ACTION_ID")
        receipt = c.execute("SELECT * FROM tutor_playback_event WHERE client_action_id=?", (action,)).fetchone()
        if receipt and receipt["request_hash"] != _hash(request, "playback_request_v1"):
            raise TutorPlaybackError("PAYLOAD_CONFLICT")
        return receipt

    @staticmethod
    def _record(c, row, action, operation, request):
        c.execute("""INSERT INTO tutor_playback_event(event_id,playback_id,client_action_id,operation,
            request_hash,generation,revision,segment_index,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (str(uuid4()), row["playback_id"], action, operation, _hash(request, "playback_request_v1"),
             row["generation"], row["revision"], row["segment_index"], utc_now()))

    def begin(self, session_id: str, *, turn_id: str, client_action_id: str,
              presentation: dict | None = None, source_version: str | None = None) -> dict:
        request = {"session_id": session_id, "turn_id": turn_id, "operation": "begin",
                   "presentation": presentation, "source_version": source_version}
        with self._transaction() as c:
            session = self._session(c, session_id)
            receipt = self._receipt(c, client_action_id, request)
            if receipt:
                row = self._guard(c, session, self._owned(c, session, receipt["playback_id"]))
                if row["status"] == "invalidated":
                    raise TutorPlaybackError("SOURCE_INVALIDATED")
                return self._view(row, True)
            version, canonical = self._source(c, session, turn_id)
            if source_version is not None and source_version != version:
                raise TutorPlaybackError("SOURCE_VERSION_CONFLICT")
            board = _presentation(canonical, presentation)
            existing = c.execute("SELECT * FROM tutor_playback_checkpoint WHERE session_id=? AND status IN ('paused','playing')",
                                 (session_id,)).fetchone()
            if existing and self._guard(c, session, dict(existing))["status"] in _OPEN:
                raise TutorPlaybackError("ACTIVE_PLAYBACK_EXISTS")
            now = utc_now()
            row = {"playback_id": str(uuid4()), "turn_id": turn_id, "child_id": session["child_id"],
                   "session_id": session_id, "source_version": version,
                   "presentation_version": _hash(board, "tutor_presentation_v1"), "presentation_json": _json(board),
                   "runtime_epoch": self.runtime_epoch, "generation": 1, "revision": 1, "status": "paused",
                   "segment_index": 0, "segment_started": 0, "reason": "awaiting_explicit_resume",
                   "created_at": now, "updated_at": now}
            c.execute("""INSERT INTO tutor_playback_checkpoint VALUES(:playback_id,:turn_id,:child_id,:session_id,
                :source_version,:presentation_version,:presentation_json,:runtime_epoch,:generation,:revision,
                :status,:segment_index,:segment_started,:reason,:created_at,:updated_at)""", row)
            self._record(c, row, client_action_id, "begin", request)
            return self._view(row)

    def read(self, session_id: str, *, playback_id: str | None = None) -> dict | None:
        with self._transaction() as c:
            session = self._session(c, session_id)
            if playback_id is None:
                latest = c.execute("SELECT playback_id FROM tutor_playback_checkpoint WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                                   (session_id,)).fetchone()
                if not latest:
                    return None
                playback_id = latest["playback_id"]
            return self._view(self._guard(c, session, self._owned(c, session, playback_id)))

    def control(self, session_id: str, *, playback_id: str, source_version: str, generation: int,
                expected_revision: int, command: Literal["pause", "resume", "previous", "restart", "cancel"],
                client_action_id: str) -> dict:
        if not isinstance(command, str) or command not in _COMMANDS:
            raise TutorPlaybackError("INVALID_COMMAND")
        return self._mutate(session_id, playback_id, source_version, generation, expected_revision,
                            "control", command, None, client_action_id)

    def record_segment(self, session_id: str, *, playback_id: str, source_version: str, generation: int,
                       expected_revision: int, segment_id: str, event: Literal["started", "ended"],
                       client_action_id: str) -> dict:
        if not isinstance(event, str) or event not in {"started", "ended"} or not _text(segment_id):
            raise TutorPlaybackError("INVALID_SEGMENT_EVENT")
        return self._mutate(session_id, playback_id, source_version, generation, expected_revision,
                            "segment", event, segment_id, client_action_id)

    def _mutate(self, session_id, playback_id, source_version, generation, expected_revision,
                operation, value, segment_id, action):
        if type(generation) is not int or generation < 1 or type(expected_revision) is not int or expected_revision < 1:
            raise TutorPlaybackError("INVALID_BINDING")
        request = dict(session_id=session_id, playback_id=playback_id, source_version=source_version,
                       generation=generation, expected_revision=expected_revision,
                       operation=operation, value=value, segment_id=segment_id)
        with self._transaction() as c:
            session = self._session(c, session_id)
            row = self._guard(c, session, self._owned(c, session, playback_id))
            if row["status"] == "invalidated":
                raise TutorPlaybackError("SOURCE_INVALIDATED")
            if source_version != row["source_version"]:
                raise TutorPlaybackError("SOURCE_VERSION_CONFLICT")
            receipt = self._receipt(c, action, request)
            # A control's successful receipt belongs to its resulting generation,
            # not the input generation. Older receipts never authorize a new lease.
            if (receipt["generation"] if receipt else generation) != row["generation"]:
                raise TutorPlaybackError("STALE_GENERATION")
            if receipt:
                return self._view(row, True)
            if expected_revision != row["revision"]:
                raise TutorPlaybackError("REVISION_CONFLICT")
            if row["status"] == "cancelled":
                raise TutorPlaybackError("TERMINAL_PLAYBACK")
            segments = json.loads(row["presentation_json"])["segments"]
            if operation == "control":
                if row["status"] == "playback_finished" and value in {"pause", "resume"}:
                    raise TutorPlaybackError("PLAYBACK_FINISHED")
                if value in {"previous", "restart"} and c.execute("""SELECT 1 FROM tutor_playback_checkpoint
                    WHERE session_id=? AND playback_id!=? AND status IN ('paused','playing')""",
                    (session_id, playback_id)).fetchone():
                    raise TutorPlaybackError("ACTIVE_PLAYBACK_EXISTS")
                index = row["segment_index"]
                if value == "previous":
                    index = max(0, index - 1)
                elif value == "restart":
                    index = 0
                row.update(generation=row["generation"] + 1, segment_started=0, segment_index=index,
                           runtime_epoch=self.runtime_epoch,
                           status="playing" if value == "resume" else "cancelled" if value == "cancel" else "paused",
                           reason="explicit_" + value)
            else:
                if row["status"] != "playing":
                    raise TutorPlaybackError("NOT_PLAYING")
                index = row["segment_index"]
                if (index >= len(segments) or segments[index]["segment_id"] != segment_id
                        or bool(row["segment_started"]) != (value == "ended")):
                    raise TutorPlaybackError("SEGMENT_ORDER")
                row["segment_started"] = int(value == "started")
                row["reason"] = None
                if value == "ended":
                    row["segment_index"] += 1
                    if row["segment_index"] == len(segments):
                        row.update(status="playback_finished", reason="all_segments_delivered")
            row["revision"] += 1
            self._save(c, row)
            self._record(c, row, action, operation + ":" + value, request)
            return self._view(row)
