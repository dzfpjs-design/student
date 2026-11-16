from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import json
import math
import secrets
from uuid import uuid4

from .database import Database
from .repository import UnknownSessionError, utc_now


class DeviceRegistry:
    LEASE_SECONDS = 180
    QUALIFICATION_SECONDS = {
        "quick_dual": 60,
        "dual_concurrency": 30 * 60,
        "camera_stability": 60 * 60,
        "document_stability": 60 * 60,
    }

    def __init__(self, database: Database):
        self.database = database
        self._qualification_completion_tokens: dict[str, str] = {}

    def register(self, session_id: str, device_type: str, system_id_hash: str, capabilities: dict) -> dict:
        if device_type not in {"camera", "document_camera", "microphone"}:
            raise InvalidDeviceTypeError(device_type)
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT child_id, status FROM daily_session WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not session:
                raise UnknownSessionError(session_id)
            if session["status"] != "active":
                raise DeviceSessionEndedError()
            binding = connection.execute(
                "SELECT system_id_hash, label FROM device_binding WHERE device_type = ?",
                (device_type,),
            ).fetchone()
            if binding and binding["system_id_hash"] != system_id_hash:
                raise DeviceBindingMismatchError(device_type, binding["label"])
            existing = connection.execute(
                "SELECT device_id FROM device_profile WHERE device_type = ? AND system_id_hash = ?",
                (device_type, system_id_hash),
            ).fetchone()
            device_id = existing["device_id"] if existing else str(uuid4())
            profile = {
                **capabilities,
                "physical_power_state": "unknown",
                "verified_at": now.isoformat(),
                "profile_version": "renderer_media_v1",
            }
            connection.execute(
                """
                INSERT INTO device_profile(device_id, device_type, system_id_hash, capabilities_json, last_seen_at, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                ON CONFLICT(device_id) DO UPDATE SET capabilities_json = excluded.capabilities_json,
                  last_seen_at = excluded.last_seen_at, status = 'active'
                """,
                (device_id, device_type, system_id_hash, json.dumps(profile, ensure_ascii=False), now.isoformat()),
            )
            connection.execute(
                """
                UPDATE sensor_lease SET status = 'released', released_at = ?, release_reason = 'superseded'
                WHERE session_id = ? AND device_type = ? AND status = 'active'
                """,
                (now.isoformat(), session_id, device_type),
            )
            lease_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO sensor_lease(
                  lease_id, session_id, device_id, device_type, status, opened_at,
                  last_renewed_at, expires_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (lease_id, session_id, device_id, device_type, now.isoformat(), now.isoformat(), (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat()),
            )
            # Opening a UVC track does not prove that it supplies usable frames.
            # Video incidents remain open until the current lease confirms one.
            if device_type == "microphone":
                connection.execute(
                    """UPDATE device_incident SET status = 'resolved', resolved_at = ?
                    WHERE child_id = ? AND device_type = ? AND status = 'open'""",
                    (now.isoformat(), session["child_id"], device_type),
                )
            connection.commit()
        return {"lease_id": lease_id, "device_id": device_id, "status": "active", "expires_at": (now + timedelta(seconds=self.LEASE_SECONDS)).isoformat()}

    def bindings(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT device_type, system_id_hash, label, capabilities_json,
                       verified_at, updated_at
                FROM device_binding ORDER BY device_type
                """
            ).fetchall()
        return [
            {**dict(row), "capabilities": json.loads(row["capabilities_json"])}
            for row in rows
        ]

    def save_video_bindings(self, bindings: list[dict], client_action_id: str) -> list[dict]:
        by_type = {item["device_type"]: item for item in bindings}
        if not by_type or len(by_type) != len(bindings):
            raise InvalidDeviceBindingError("每类视频设备只能指定一台。")
        for item in bindings:
            normalized_label = " ".join(str(item.get("label") or "").lower().replace("_", " ").replace("-", " ").split())
            if any(token in normalized_label for token in ("integrated", "built in", "builtin", "internal", "集成", "内置")):
                raise InvalidDeviceBindingError("观察摄像头和高拍仪必须使用外接设备，不能绑定电脑集成摄像头。")
        if len(bindings) == 2 and len({item["system_id_hash"] for item in bindings}) != 2:
            raise InvalidDeviceBindingError("观察摄像头和高拍仪不能使用同一个视频设备。")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_id FROM device_binding_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                return self.bindings()
            if connection.execute(
                "SELECT 1 FROM device_qualification_run WHERE status = 'active' LIMIT 1"
            ).fetchone():
                connection.rollback()
                raise ActiveDeviceQualificationError()
            if connection.execute(
                "SELECT 1 FROM observation_calibration_run WHERE status = 'active' LIMIT 1"
            ).fetchone():
                connection.rollback()
                raise ActiveDeviceQualificationError()
            current = {
                row["device_type"]: row["system_id_hash"]
                for row in connection.execute(
                    "SELECT device_type, system_id_hash FROM device_binding"
                ).fetchall()
            }
            final_identities = {**current, **{key: item["system_id_hash"] for key, item in by_type.items()}}
            if len(set(final_identities.values())) != len(final_identities):
                connection.rollback()
                raise InvalidDeviceBindingError("观察摄像头和高拍仪不能使用同一个视频设备。")
            for device_type in by_type:
                connection.execute("DELETE FROM device_binding WHERE device_type = ?", (device_type,))
                connection.execute(
                    "UPDATE device_profile SET status = 'unbound' WHERE device_type = ?",
                    (device_type,),
                )
            for device_type, item in by_type.items():
                capabilities = self._validated_binding_capabilities(item.get("capabilities") or {})
                encoded_capabilities = json.dumps(capabilities, ensure_ascii=False)
                connection.execute(
                    """
                    INSERT INTO device_binding(
                      device_type, system_id_hash, label, capabilities_json,
                      verified_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_type,
                        item["system_id_hash"],
                        item["label"][:200],
                        encoded_capabilities,
                        item["verified_at"],
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO device_profile(
                      device_id, device_type, system_id_hash, capabilities_json,
                      last_seen_at, status
                    ) VALUES (?, ?, ?, ?, ?, 'bound')
                    ON CONFLICT(device_type, system_id_hash) DO UPDATE SET
                      capabilities_json = excluded.capabilities_json,
                      last_seen_at = excluded.last_seen_at,
                      status = 'bound'
                    """,
                    (
                        str(uuid4()), device_type, item["system_id_hash"], encoded_capabilities,
                        item["verified_at"],
                    ),
                )
            connection.execute(
                "INSERT INTO device_binding_event VALUES (?, ?, ?, ?)",
                (str(uuid4()), json.dumps(bindings, ensure_ascii=False), now, client_action_id),
            )
            connection.commit()
        return self.bindings()

    @staticmethod
    def _validated_binding_capabilities(capabilities: dict) -> dict:
        if not isinstance(capabilities, dict):
            raise InvalidDeviceBindingError("设备能力档案格式无效。")
        encoded = json.dumps(capabilities, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 50_000:
            raise InvalidDeviceBindingError("设备能力档案过大。")

        def inspect(value, path: tuple[str, ...] = ()) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = str(key).lower()
                    if any(token in normalized for token in ("serial", "pnp_device_id", "instance_id")) and not normalized.endswith("_hash"):
                        raise InvalidDeviceBindingError("设备档案不得保存原始序列号或PNP标识。")
                    inspect(nested, (*path, str(key)))
            elif isinstance(value, list):
                if len(value) > 100:
                    raise InvalidDeviceBindingError("设备能力档案列表过长。")
                for nested in value:
                    inspect(nested, path)
            elif isinstance(value, str) and len(value) > 500:
                raise InvalidDeviceBindingError("设备能力档案字段过长。")

        inspect(capabilities)
        return capabilities

    def start_qualification(self, mode: str, client_action_id: str) -> dict:
        if mode not in self.QUALIFICATION_SECONDS:
            raise InvalidDeviceQualificationError("不支持的设备验收模式。")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM device_qualification_run WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                result = self._qualification_row(existing)
                if existing["status"] == "active":
                    result["completion_token"] = self._qualification_completion_tokens.setdefault(
                        existing["run_id"], secrets.token_urlsafe(32)
                    )
                return result
            if connection.execute(
                "SELECT 1 FROM daily_session WHERE status = 'active' LIMIT 1"
            ).fetchone():
                connection.rollback()
                raise ActiveLearningSessionQualificationError()
            if connection.execute(
                "SELECT 1 FROM device_qualification_run WHERE status = 'active' LIMIT 1"
            ).fetchone():
                connection.rollback()
                raise ActiveDeviceQualificationError()
            if connection.execute(
                "SELECT 1 FROM observation_calibration_run WHERE status = 'active' LIMIT 1"
            ).fetchone():
                connection.rollback()
                raise ActiveDeviceQualificationError()
            bindings = {
                row["device_type"]: row["system_id_hash"]
                for row in connection.execute(
                    "SELECT device_type, system_id_hash FROM device_binding"
                ).fetchall()
            }
            required_roles = (
                {"camera"} if mode == "camera_stability"
                else {"document_camera"} if mode == "document_stability"
                else {"camera", "document_camera"}
            )
            if not required_roles.issubset(bindings):
                connection.rollback()
                missing = "观察摄像头" if required_roles == {"camera"} else "高拍仪" if required_roles == {"document_camera"} else "观察摄像头和高拍仪"
                raise InvalidDeviceQualificationError(f"请先绑定并验证{missing}。")
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO device_qualification_run(
                  run_id, mode, status, expected_seconds, camera_system_id_hash,
                  document_system_id_hash, started_at, client_action_id
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    mode,
                    self.QUALIFICATION_SECONDS[mode],
                    bindings.get("camera", ""),
                    bindings.get("document_camera", ""),
                    now,
                    client_action_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM device_qualification_run WHERE run_id = ?", (run_id,)
            ).fetchone()
        completion_token = secrets.token_urlsafe(32)
        self._qualification_completion_tokens[run_id] = completion_token
        return {**self._qualification_row(row), "completion_token": completion_token}

    def allows_qualification_completion(self, run_id: str, completion_token: str | None) -> bool:
        expected = self._qualification_completion_tokens.get(run_id)
        return bool(
            expected
            and completion_token
            and hmac.compare_digest(expected, completion_token)
        )

    def finish_qualification(
        self,
        run_id: str,
        outcome: str,
        metrics: dict,
        failure_code: str | None,
        client_action_id: str,
    ) -> dict:
        if outcome not in {"passed", "failed", "cancelled"}:
            raise InvalidDeviceQualificationError("不支持的设备验收结果。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            repeated = connection.execute(
                "SELECT * FROM device_qualification_run WHERE finish_client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if repeated:
                connection.rollback()
                return self._qualification_row(repeated)
            row = connection.execute(
                "SELECT * FROM device_qualification_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise UnknownDeviceQualificationError()
            if row["status"] != "active":
                connection.rollback()
                raise DeviceQualificationFinishedError()
            completed_at = utc_now()
            try:
                server_elapsed_seconds = max(
                    0.0,
                    (datetime.fromisoformat(completed_at) - datetime.fromisoformat(row["started_at"])).total_seconds(),
                )
            except (TypeError, ValueError):
                server_elapsed_seconds = 0.0
            metrics = {
                **metrics,
                "validation": {
                    "policy_version": "device_qualification_v2",
                    "server_elapsed_seconds": round(server_elapsed_seconds, 3),
                },
            }
            encoded_metrics = json.dumps(metrics, ensure_ascii=False)
            if len(encoded_metrics.encode("utf-8")) > 100_000:
                connection.rollback()
                raise InvalidDeviceQualificationError("设备验收指标过大。")
            if outcome == "passed":
                validation_failure = self._qualification_metrics_failure(
                    row["mode"], row["expected_seconds"], metrics, server_elapsed_seconds
                )
                if validation_failure:
                    outcome = "failed"
                    failure_code = validation_failure
            connection.execute(
                """
                UPDATE device_qualification_run SET status = ?, metrics_json = ?,
                  failure_code = ?, completed_at = ?, finish_client_action_id = ?
                WHERE run_id = ?
                """,
                (
                    outcome,
                    encoded_metrics,
                    failure_code,
                    completed_at,
                    client_action_id,
                    run_id,
                ),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM device_qualification_run WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._qualification_row(updated)

    @staticmethod
    def _qualification_metrics_failure(
        mode: str,
        expected_seconds: int,
        metrics: dict,
        server_elapsed_seconds: float,
    ) -> str | None:
        elapsed = metrics.get("elapsed_seconds")
        if (
            not DeviceRegistry._finite_number(elapsed)
            or elapsed + 1 < expected_seconds
            or not DeviceRegistry._finite_number(server_elapsed_seconds)
            or server_elapsed_seconds + 1 < expected_seconds
        ):
            return "TEST_INCOMPLETE"
        required_roles = (
            ("camera",) if mode == "camera_stability"
            else ("document_camera",) if mode == "document_stability"
            else ("camera", "document_camera")
        )
        devices = metrics.get("devices")
        if not isinstance(devices, dict):
            return "NO_FRAME_SAMPLES"
        for role in required_roles:
            item = devices.get(role)
            if not isinstance(item, dict):
                return "NO_FRAME_SAMPLES"
            samples = item.get("samples")
            advancing = item.get("advancing_samples")
            frozen = item.get("frozen_samples")
            max_consecutive_frozen = item.get("max_consecutive_frozen")
            if item.get("ended") is True:
                return "DEVICE_DISCONNECTED"
            if not isinstance(samples, int) or samples <= 0 or not isinstance(frozen, int) or frozen < 0:
                return "NO_FRAME_SAMPLES"
            if samples < max(1, math.floor(expected_seconds * 0.98)):
                return "INSUFFICIENT_FRAME_SAMPLES"
            if (
                not isinstance(advancing, int)
                or advancing < 0
                or advancing + frozen != samples
                or not isinstance(max_consecutive_frozen, int)
                or max_consecutive_frozen < 0
            ):
                return "INVALID_FRAME_METRICS"
            if frozen > max(3, math.ceil(samples * 0.005)) or max_consecutive_frozen > 3:
                return "FRAME_STALLED"
            frame_callback_supported = item.get("frame_callback_supported")
            frame_count = item.get("frame_count")
            if not isinstance(frame_callback_supported, bool):
                return "INVALID_FRAME_METRICS"
            if (
                not isinstance(frame_count, int)
                or frame_count < 0
                or (frame_callback_supported and frame_count < advancing)
            ):
                return "INVALID_FRAME_METRICS"
            for key in ("width", "height", "frame_rate"):
                if not DeviceRegistry._finite_number(item.get(key)) or item[key] <= 0:
                    return "DEVICE_CAPABILITIES_INCOMPLETE"
            content_change = item.get("content_change_samples")
            low_variation = item.get("low_variation_samples")
            if (
                not isinstance(content_change, int)
                or not isinstance(low_variation, int)
                or content_change < 0
                or low_variation < 0
                or content_change + low_variation > samples
            ):
                return "INVALID_FRAME_METRICS"
        return None

    @staticmethod
    def _finite_number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def recover_interrupted_qualifications(self) -> int:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                """
                UPDATE device_qualification_run SET status = 'interrupted',
                  failure_code = 'APP_RESTART', completed_at = ?
                WHERE status = 'active'
                """,
                (utc_now(),),
            ).rowcount
            connection.commit()
        return count

    def qualification_summary(self) -> dict:
        binding_rows = self.bindings()
        current = {item["device_type"]: item["system_id_hash"] for item in binding_rows}
        profile_missing = {
            role: self._profile_missing_fields(
                next((item.get("capabilities") or {} for item in binding_rows if item["device_type"] == role), {})
            )
            for role in ("camera", "document_camera")
        }
        profiles_ready = set(current) == {"camera", "document_camera"} and not any(profile_missing.values())
        runs = self.qualification_runs()
        required_modes = ("dual_concurrency", "camera_stability", "document_stability")
        required = []
        for mode in required_modes:
            identity_matches = (
                (lambda run: run["camera_system_id_hash"] == current.get("camera"))
                if mode == "camera_stability"
                else (lambda run: run["document_system_id_hash"] == current.get("document_camera"))
                if mode == "document_stability"
                else (lambda run: run["camera_system_id_hash"] == current.get("camera") and run["document_system_id_hash"] == current.get("document_camera"))
            )
            matching = next(
                (
                    run
                    for run in runs
                    if run["mode"] == mode
                    and run["status"] in {"passed", "failed"}
                    and identity_matches(run)
                ),
                None,
            )
            required.append(
                {
                    "mode": mode,
                    "status": matching["status"] if matching else "not_run",
                    "completed_at": matching["completed_at"] if matching else None,
                }
            )
        return {
            "bindings_ready": set(current) == {"camera", "document_camera"},
            "profiles_ready": profiles_ready,
            "profile_missing": profile_missing,
            "acceptance_status": "passed" if profiles_ready and all(item["status"] == "passed" for item in required) else "pending",
            "required": required,
            "recent_runs": [self._public_qualification(run) for run in runs[:10]],
        }

    @staticmethod
    def _profile_missing_fields(capabilities: dict) -> list[str]:
        missing = []
        required = {
            "manufacturer": "manufacturer",
            "model": "model",
            "driver_version": "driver_version",
            "capture_protocol": "capture_protocol",
            "ptz_capability": "ptz_capability",
            "audio_capability": "audio_capability",
        }
        for key, label in required.items():
            value = capabilities.get(key)
            normalized = value.strip() if isinstance(value, str) else ""
            if not normalized or normalized == "unverified" or normalized.endswith("_unverified"):
                missing.append(label)
        has_usb_identity = all(
            isinstance(capabilities.get(key), str) and len(capabilities[key].strip()) == 4
            for key in ("usb_vid", "usb_pid")
        )
        has_network_identity = isinstance(capabilities.get("network_identifier_hash"), str) and len(capabilities["network_identifier_hash"]) >= 16
        if not has_usb_identity and not has_network_identity:
            missing.append("hardware_identity")
        return missing

    def qualification_runs(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM device_qualification_run
                ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                  COALESCE(completed_at, started_at) DESC
                """
            ).fetchall()
        return [self._qualification_row(row) for row in rows]

    def has_active_qualification(self) -> bool:
        with self.database.connect() as connection:
            return bool(connection.execute(
                "SELECT 1 FROM device_qualification_run WHERE status = 'active' LIMIT 1"
            ).fetchone())

    @staticmethod
    def _qualification_row(row) -> dict:
        return {**dict(row), "metrics": json.loads(row["metrics_json"])}

    @staticmethod
    def _public_qualification(run: dict) -> dict:
        return {
            key: run[key]
            for key in (
                "run_id", "mode", "status", "expected_seconds", "metrics",
                "failure_code", "started_at", "completed_at",
            )
        }

    def record_incident(
        self,
        session_id: str,
        device_type: str,
        error_code: str,
        message: str,
        client_action_id: str,
    ) -> dict:
        if device_type not in {"camera", "document_camera", "microphone"}:
            raise InvalidDeviceTypeError(device_type)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT di.* FROM device_incident_report dir
                JOIN device_incident di ON di.incident_id = dir.incident_id
                WHERE dir.client_action_id = ?
                """,
                (client_action_id,),
            ).fetchone()
            if not existing:
                existing = connection.execute(
                    "SELECT * FROM device_incident WHERE client_action_id = ?",
                    (client_action_id,),
                ).fetchone()
            if existing:
                connection.execute(
                    "INSERT OR IGNORE INTO device_incident_report VALUES (?, ?, ?, ?)",
                    (str(uuid4()), existing["incident_id"], client_action_id, utc_now()),
                )
                connection.commit()
                return dict(existing)
            session = connection.execute(
                "SELECT child_id FROM daily_session WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                connection.rollback()
                raise UnknownSessionError(session_id)
            now = utc_now()
            ongoing = connection.execute(
                """
                SELECT * FROM device_incident
                WHERE session_id = ? AND device_type = ? AND error_code = ? AND status = 'open'
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (session_id, device_type, error_code[:80]),
            ).fetchone()
            if ongoing:
                connection.execute(
                    "INSERT INTO device_incident_report VALUES (?, ?, ?, ?)",
                    (str(uuid4()), ongoing["incident_id"], client_action_id, now),
                )
                connection.commit()
                return dict(ongoing)
            incident_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO device_incident(
                  incident_id, child_id, session_id, device_type, error_code,
                  message, status, occurred_at, resolved_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, NULL, ?)
                """,
                (
                    incident_id,
                    session["child_id"],
                    session_id,
                    device_type,
                    error_code[:80],
                    message[:500],
                    now,
                    client_action_id,
                ),
            )
            connection.execute(
                "INSERT INTO device_incident_report VALUES (?, ?, ?, ?)",
                (str(uuid4()), incident_id, client_action_id, now),
            )
            connection.commit()
            return dict(connection.execute(
                "SELECT * FROM device_incident WHERE incident_id = ?", (incident_id,)
            ).fetchone())

    def confirm_frame(self, lease_id: str, width: int, height: int) -> dict:
        """Persist only frame availability, never camera pixels or behavior claims."""
        if any(type(value) is not int or not 1 <= value <= 16384 for value in (width, height)):
            raise ValueError("usable video dimensions are required")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""SELECT l.*, s.child_id, s.status AS session_status,
                    p.system_id_hash FROM sensor_lease l
                JOIN daily_session s ON s.session_id=l.session_id
                JOIN device_profile p ON p.device_id=l.device_id WHERE l.lease_id=?""", (lease_id,)).fetchone()
            if (not row or row['device_type'] not in {'camera', 'document_camera'}
                    or row['status'] != 'active' or row['session_status'] != 'active'
                    or datetime.fromisoformat(row['expires_at']) <= datetime.now(timezone.utc)):
                raise UnknownSensorLeaseError(lease_id)
            latest = connection.execute("""SELECT lease_id FROM sensor_lease
                WHERE session_id=? AND device_type=? AND status='active'
                ORDER BY opened_at DESC, rowid DESC LIMIT 1""", (row['session_id'], row['device_type'])).fetchone()
            binding = connection.execute('SELECT system_id_hash FROM device_binding WHERE device_type=?', (row['device_type'],)).fetchone()
            if latest['lease_id'] != lease_id or binding and binding['system_id_hash'] != row['system_id_hash']:
                raise UnknownSensorLeaseError(lease_id)
            # SQLite julianday rounds sub-millisecond times; compare full
            # timestamps so a newer failure cannot be erased by an older lease.
            opened = datetime.fromisoformat(row['opened_at'])
            incidents = connection.execute("""SELECT i.incident_id, i.occurred_at, r.reported_at
                FROM device_incident i LEFT JOIN device_incident_report r ON r.incident_id=i.incident_id
                WHERE i.child_id=? AND i.device_type=? AND i.status='open'""",
                (row['child_id'], row['device_type'])).fetchall()
            latest_reports = {}
            for incident in incidents:
                reported = datetime.fromisoformat(incident['reported_at'] or incident['occurred_at'])
                key = incident['incident_id']
                latest_reports[key] = max(reported, latest_reports.get(key, reported))
            count = 0
            for incident_id, reported in latest_reports.items():
                if reported <= opened:
                    count += connection.execute("""UPDATE device_incident SET status='resolved', resolved_at=?
                        WHERE incident_id=? AND status='open'""", (now, incident_id)).rowcount
            connection.commit()
        return {'lease_id': lease_id, 'status': 'frame_confirmed', 'resolved_incidents': count}

    def renew(self, lease_id: str) -> dict:
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM sensor_lease WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if not row or row["status"] != "active":
                raise UnknownSensorLeaseError(lease_id)
            expires = now + timedelta(seconds=self.LEASE_SECONDS)
            connection.execute(
                "UPDATE sensor_lease SET last_renewed_at = ?, expires_at = ? WHERE lease_id = ?",
                (now.isoformat(), expires.isoformat(), lease_id),
            )
            connection.commit()
        return {"lease_id": lease_id, "status": "active", "expires_at": expires.isoformat()}

    def release(self, lease_id: str, reason: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM sensor_lease WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if not row:
                raise UnknownSensorLeaseError(lease_id)
            connection.execute(
                """
                UPDATE sensor_lease SET status = 'released', released_at = ?, release_reason = ?
                WHERE lease_id = ? AND status = 'active'
                """,
                (now, reason, lease_id),
            )
            connection.commit()
        return {"lease_id": lease_id, "status": "released", "application_reading": False, "physical_power_state": "unknown"}

    def release_session(self, session_id: str, reason: str, device_type: str | None = None) -> int:
        if device_type is not None and device_type not in {"camera", "document_camera", "microphone"}:
            raise InvalidDeviceTypeError(device_type)
        now = utc_now()
        with self.database.connect() as connection:
            count = connection.execute(
                """
                UPDATE sensor_lease SET status = 'released', released_at = ?, release_reason = ?
                WHERE session_id = ? AND status = 'active'
                  AND (? IS NULL OR device_id IN (SELECT device_id FROM device_profile WHERE device_type = ?))
                """,
                (now, reason, session_id, device_type, device_type),
            ).rowcount
            connection.commit()
        return count

    def statuses(self) -> list[dict]:
        self.expire_stale()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT dp.device_type, dp.system_id_hash, dp.capabilities_json, dp.last_seen_at, dp.status,
                       EXISTS(SELECT 1 FROM sensor_lease sl WHERE sl.device_id = dp.device_id AND sl.status = 'active') AS application_reading
                FROM device_profile dp ORDER BY dp.last_seen_at DESC
                """
            ).fetchall()
        return [{**dict(row), "capabilities": json.loads(row["capabilities_json"]), "physical_power_state": "unknown"} for row in rows]

    def open_incidents(self, session_id: str | None) -> dict[str, dict]:
        """Return the most recent unresolved device-open failure for one session.

        This is deliberately session-scoped.  A past failure must not make a
        different child's home screen claim that a camera is currently busy.
        """
        if not session_id:
            return {}
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT device_type, error_code, message, occurred_at
                FROM device_incident
                WHERE session_id = ? AND status = 'open'
                ORDER BY occurred_at DESC
                """,
                (session_id,),
            ).fetchall()
        incidents: dict[str, dict] = {}
        for row in rows:
            incidents.setdefault(row["device_type"], dict(row))
        return incidents

    def expire_stale(self) -> int:
        now = utc_now()
        with self.database.connect() as connection:
            count = connection.execute(
                """
                UPDATE sensor_lease SET status = 'expired', released_at = ?, release_reason = 'lease_timeout'
                WHERE status = 'active' AND expires_at < ?
                """,
                (now, now),
            ).rowcount
            connection.commit()
        return count


class InvalidDeviceTypeError(Exception):
    pass


class DeviceSessionEndedError(Exception):
    pass


class UnknownSensorLeaseError(Exception):
    pass


class InvalidDeviceBindingError(Exception):
    pass


class DeviceBindingMismatchError(Exception):
    def __init__(self, device_type: str, expected_label: str):
        self.device_type = device_type
        self.expected_label = expected_label


class InvalidDeviceQualificationError(Exception):
    pass


class ActiveLearningSessionQualificationError(Exception):
    pass


class ActiveDeviceQualificationError(Exception):
    pass


class UnknownDeviceQualificationError(Exception):
    pass


class DeviceQualificationFinishedError(Exception):
    pass
