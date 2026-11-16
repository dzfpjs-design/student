"""CPU-first runtime policy for the supported Windows household PC baseline."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4


MINIMUM_PC = {
    "platform": "Windows", "architecture": "x64", "minimum_logical_cpus": 8,
    "requires_avx2": True, "minimum_memory_gb": 16, "minimum_nvme_gb": 512,
    "recommended_memory_gb": 32, "recommended_nvme_gb": 1024,
}
TASK_BUDGETS = {
    "behavior": {"threads": 1, "memory_mb": 400, "route": "cpu", "priority": 3},
    "asr": {"threads": 2, "memory_mb": 900, "route": "cpu", "priority": 2},
    "ocr": {"threads": 3, "memory_mb": 1800, "route": "cpu", "priority": 1},
}
MAX_MANAGED_THREADS = 6  # leaves two logical threads for UI, camera I/O, and SQLite
MAX_MANAGED_MEMORY_MB = 4096
STEADY_SYSTEM_CPU_LIMIT_PERCENT = 65.0
SYSTEM_MEMORY_LIMIT_MB = 12 * 1024


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float
    memory_mb: int


class CpuResourceQueue:
    """Durable metadata-only task queue with CPU resource reservations.

    It deliberately stores no media payload. Device capture and media bytes stay
    in their owning local process; this queue persists only task scheduling state.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cpu_task(task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, reason TEXT);
            CREATE INDEX IF NOT EXISTS idx_cpu_task_status ON cpu_task(status, task_type, created_at);
            """)

    def enqueue(self, task_type: str, payload: dict | None = None, task_id: str | None = None) -> dict:
        if task_type not in TASK_BUDGETS:
            raise ValueError("Unsupported managed task type")
        task_id = task_id or str(uuid4())
        with self._connect() as db:
            existing = db.execute("SELECT * FROM cpu_task WHERE task_id = ?", (task_id,)).fetchone()
            if not existing:
                db.execute("INSERT INTO cpu_task VALUES (?, ?, 'queued', ?, ?, NULL, NULL, NULL)", (task_id, task_type, json.dumps(payload or {}, ensure_ascii=False), _now()))
                db.commit()
                existing = db.execute("SELECT * FROM cpu_task WHERE task_id = ?", (task_id,)).fetchone()
            return _task(existing)

    def next(self, snapshot: ResourceSnapshot) -> dict | None:
        """Reserve only a safe next task. OCR overload degrades behavior to 0.25 FPS."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            running = [dict(row) for row in db.execute("SELECT task_type FROM cpu_task WHERE status = 'running'")]
            used_threads = sum(TASK_BUDGETS[row["task_type"]]["threads"] for row in running)
            used_memory = sum(TASK_BUDGETS[row["task_type"]]["memory_mb"] for row in running)
            queued = db.execute("SELECT * FROM cpu_task WHERE status = 'queued' ORDER BY CASE task_type WHEN 'behavior' THEN 3 WHEN 'asr' THEN 2 ELSE 1 END DESC, created_at").fetchall()
            for candidate in queued:
                budget = TASK_BUDGETS[candidate["task_type"]]
                if snapshot.memory_mb + budget["memory_mb"] > SYSTEM_MEMORY_LIMIT_MB:
                    continue
                if used_threads + budget["threads"] > MAX_MANAGED_THREADS or used_memory + budget["memory_mb"] > MAX_MANAGED_MEMORY_MB:
                    continue
                if snapshot.cpu_percent > STEADY_SYSTEM_CPU_LIMIT_PERCENT and candidate["task_type"] == "ocr":
                    continue
                db.execute("UPDATE cpu_task SET status = 'running', started_at = ? WHERE task_id = ?", (_now(), candidate["task_id"]))
                db.commit()
                result = _task(db.execute("SELECT * FROM cpu_task WHERE task_id = ?", (candidate["task_id"],)).fetchone())
                result["budget"] = budget
                result["behavior_fps_cap"] = 0.25 if candidate["task_type"] == "ocr" else 0.5
                return result
        return None

    def claim(self, task_id: str, snapshot: ResourceSnapshot) -> dict | None:
        """Atomically reserve one known task without consuming another caller's work."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            candidate = db.execute("SELECT * FROM cpu_task WHERE task_id=? AND status='queued'", (task_id,)).fetchone()
            if not candidate:
                db.commit()
                return None
            budget = TASK_BUDGETS[candidate["task_type"]]
            running = [dict(row) for row in db.execute("SELECT task_type FROM cpu_task WHERE status='running'")]
            used_threads = sum(TASK_BUDGETS[row["task_type"]]["threads"] for row in running)
            used_memory = sum(TASK_BUDGETS[row["task_type"]]["memory_mb"] for row in running)
            allowed = (snapshot.memory_mb + budget["memory_mb"] <= SYSTEM_MEMORY_LIMIT_MB
                       and used_threads + budget["threads"] <= MAX_MANAGED_THREADS
                       and used_memory + budget["memory_mb"] <= MAX_MANAGED_MEMORY_MB
                       and not (snapshot.cpu_percent > STEADY_SYSTEM_CPU_LIMIT_PERCENT and candidate["task_type"] == "ocr"))
            if not allowed:
                db.commit()
                return None
            updated = db.execute("UPDATE cpu_task SET status='running',started_at=? WHERE task_id=? AND status='queued'", (_now(), task_id))
            if updated.rowcount != 1:
                db.commit()
                return None
            db.commit()
            result = _task(db.execute("SELECT * FROM cpu_task WHERE task_id=?", (task_id,)).fetchone())
            result["budget"] = budget
            result["behavior_fps_cap"] = .25 if candidate["task_type"] == "ocr" else .5
            return result

    def retry(self, task_id: str) -> dict:
        """Return a terminal task to the durable queue without duplicating media."""
        with self._connect() as db:
            db.execute("UPDATE cpu_task SET status = 'queued', started_at = NULL, completed_at = NULL, reason = NULL WHERE task_id = ? AND status IN ('failed', 'cancelled')", (task_id,))
            db.commit()
            row = db.execute("SELECT * FROM cpu_task WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise KeyError("Unknown task")
            return _task(row)

    def recover_orphaned(self, now: datetime | None = None, max_running_seconds: int = 45) -> int:
        """After a service restart, return stale CPU reservations to the queue."""
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=max_running_seconds)).isoformat()
        with self._connect() as db:
            cursor = db.execute("UPDATE cpu_task SET status = 'queued', started_at = NULL, reason = 'worker_restart_recovered' WHERE status = 'running' AND started_at < ?", (cutoff,))
            db.commit()
            return cursor.rowcount

    def behavior_fps_cap(self) -> float:
        """OCR reservation lowers only analysis cadence; it never controls camera preview."""
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM cpu_task WHERE task_type = 'ocr' AND status IN ('queued', 'running') LIMIT 1").fetchone()
        return .25 if row else .5

    def finish(self, task_id: str, status: str = "completed", reason: str | None = None) -> dict:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Invalid task terminal status")
        with self._connect() as db:
            db.execute("UPDATE cpu_task SET status = ?, completed_at = ?, reason = ? WHERE task_id = ? AND status IN ('queued', 'running')", (status, _now(), reason, task_id))
            db.commit()
            row = db.execute("SELECT * FROM cpu_task WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise KeyError("Unknown task")
            return _task(row)

    @staticmethod
    def validate_behavior_input(width: int, height: int, fps: float) -> dict:
        if width > 640 or height > 640 or fps > 2:
            return {"accepted": False, "code": "behavior_input_budget_exceeded", "max_width": 640, "max_height": 640, "max_fps": 2}
        return {"accepted": True, "route": "cpu", "fps_cap": min(fps, .5)}

    @staticmethod
    def validate_ocr_input(width: int, height: int, capture_mode: str) -> dict:
        if capture_mode != "single_frame":
            return {"accepted": False, "code": "ocr_requires_single_frame_capture"}
        # The deployed document camera's verified native mode is 4160x3120.
        # Keep the admission bound at that exact single-frame budget rather
        # than rejecting a native frame just above cinema/DCI 4K dimensions.
        if width * height > 4160 * 3120:
            return {"accepted": False, "code": "ocr_frame_above_4k_budget"}
        return {"accepted": True, "route": "cpu", "capture_mode": "single_frame"}

    @staticmethod
    def validate_model(name: str, parameter_count_billion: float | None, modality: str) -> dict:
        if parameter_count_billion and parameter_count_billion >= 7:
            return {"accepted": False, "code": "local_7b_or_larger_model_forbidden"}
        if modality == "continuous_video_vlm":
            return {"accepted": False, "code": "continuous_video_vlm_forbidden"}
        return {"accepted": True, "route": "cpu", "optional_accelerators": ["openvino", "directml", "npu"]}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()


def _task(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
