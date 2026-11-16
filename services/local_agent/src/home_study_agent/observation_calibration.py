from __future__ import annotations

import json
from uuid import uuid4

from .database import Database
from .observation_engine import ObservationEngine
from .repository import UnknownChildError, utc_now


class ObservationCalibrationService:
    def __init__(self, database: Database, engine: ObservationEngine):
        self.database = database
        self.engine = engine

    def start(self, child_id: str, client_action_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT run_id FROM observation_calibration_run WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                return self.get(existing["run_id"])
            child = connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ? AND status = 'active'", (child_id,)
            ).fetchone()
            if not child:
                connection.rollback()
                raise UnknownChildError(child_id)
            if connection.execute(
                "SELECT 1 FROM daily_session WHERE status = 'active' LIMIT 1"
            ).fetchone():
                connection.rollback()
                raise ObservationCalibrationConflictError("请先结束当前学习会话，再校准观察摄像头。")
            if connection.execute(
                "SELECT 1 FROM device_qualification_run WHERE status = 'active' LIMIT 1"
            ).fetchone() or connection.execute(
                "SELECT 1 FROM observation_calibration_run WHERE status = 'active' LIMIT 1"
            ).fetchone():
                connection.rollback()
                raise ObservationCalibrationConflictError("已有设备检查正在进行，请先结束后再试。")
            binding = connection.execute(
                "SELECT system_id_hash FROM device_binding WHERE device_type = 'camera'"
            ).fetchone()
            if not binding:
                connection.rollback()
                raise ObservationCalibrationInputError("请先绑定并验证观察摄像头。")
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO observation_calibration_run(
                  run_id, child_id, camera_system_id_hash, status, result_json,
                  failure_code, started_at, completed_at, client_action_id
                ) VALUES (?, ?, ?, 'active', '{}', NULL, ?, NULL, ?)
                """,
                (run_id, child_id, binding["system_id_hash"], now, client_action_id),
            )
            connection.commit()
        return self.get(run_id)

    def assess_and_finish(self, run_id: str, frame_bytes: bytes) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM observation_calibration_run WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise UnknownObservationCalibrationError(run_id)
        if row["status"] != "active":
            return self.get(run_id)
        result = self.engine.assess_calibration(frame_bytes)
        status = "passed" if result["calibration_ready"] else "failed"
        failure_code = None
        if status == "failed":
            scene_status = result["scene_quality"]["status"]
            failure_code = (
                f"SCENE_{scene_status.upper()}" if scene_status != "ready" else "PERSON_NOT_VISIBLE"
            )
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM observation_calibration_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not current:
                connection.rollback()
                raise UnknownObservationCalibrationError(run_id)
            if current["status"] == "active":
                connection.execute(
                    """
                    UPDATE observation_calibration_run
                    SET status = ?, result_json = ?, failure_code = ?, completed_at = ?
                    WHERE run_id = ?
                    """,
                    (status, json.dumps(result, ensure_ascii=False), failure_code, now, run_id),
                )
            connection.commit()
        return self.get(run_id)

    def cancel(self, run_id: str, failure_code: str = "PARENT_CANCELLED") -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM observation_calibration_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                raise UnknownObservationCalibrationError(run_id)
            if row["status"] == "active":
                connection.execute(
                    """
                    UPDATE observation_calibration_run
                    SET status = 'cancelled', failure_code = ?, completed_at = ? WHERE run_id = ?
                    """,
                    (failure_code[:80], now, run_id),
                )
                connection.commit()
        return self.get(run_id)

    def summary(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            rows = connection.execute(
                """
                SELECT run_id FROM observation_calibration_run
                WHERE child_id = ? ORDER BY started_at DESC LIMIT 10
                """,
                (child_id,),
            ).fetchall()
        items = [self.get(row["run_id"]) for row in rows]
        return {
            "latest": items[0] if items else None,
            "active": next((item for item in items if item["status"] == "active"), None),
            "recent_runs": items,
        }

    def get(self, run_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, child_id, camera_system_id_hash, status, result_json,
                       failure_code, started_at, completed_at
                FROM observation_calibration_run WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if not row:
                raise UnknownObservationCalibrationError(run_id)
            binding = connection.execute(
                "SELECT system_id_hash FROM device_binding WHERE device_type = 'camera'"
            ).fetchone()
        return {
            "run_id": row["run_id"],
            "child_id": row["child_id"],
            "status": row["status"],
            "result": json.loads(row["result_json"]),
            "failure_code": row["failure_code"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "current_binding": bool(binding and binding["system_id_hash"] == row["camera_system_id_hash"]),
        }

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE observation_calibration_run
                SET status = 'interrupted', failure_code = 'APP_RESTART', completed_at = ?
                WHERE status = 'active'
                """,
                (now,),
            )
            connection.commit()
            return cursor.rowcount


class ObservationCalibrationInputError(Exception):
    pass


class ObservationCalibrationConflictError(Exception):
    pass


class UnknownObservationCalibrationError(Exception):
    pass
