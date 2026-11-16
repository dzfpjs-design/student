from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from .database import Database
from .model_gateway import ModelResponseError
from .repository import utc_now


BACKGROUND_REQUEST_MARKERS = (
    "report", "variant", "specialized_supply", "curriculum_internalization",
    "atomic_knowledge", "knowledge_structuring", "paper_scoring_authority_draft",
    "child_daily_analysis",
)


class ModelRuntimePolicyService:
    """Durable budget and route-health policy shared by every cloud-model use."""

    def __init__(self, database: Database):
        self.database = database

    def initialize(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_runtime_policy (
                  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                  enforcement_mode TEXT NOT NULL CHECK(enforcement_mode IN ('monitor', 'enforce')),
                  daily_cost_limit_cny REAL NOT NULL CHECK(daily_cost_limit_cny > 0),
                  daily_token_limit INTEGER NOT NULL CHECK(daily_token_limit > 0),
                  failure_threshold INTEGER NOT NULL CHECK(failure_threshold BETWEEN 1 AND 20),
                  cooldown_seconds INTEGER NOT NULL CHECK(cooldown_seconds BETWEEN 30 AND 3600),
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_route_state (
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN (
                    'healthy', 'degraded', 'rate_limited', 'quota_exhausted', 'auth_failed'
                  )),
                  consecutive_failures INTEGER NOT NULL DEFAULT 0,
                  last_error_code TEXT,
                  blocked_until TEXT,
                  last_success_at TEXT,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(provider, model)
                );
                """
            )
            connection.execute(
                """INSERT OR IGNORE INTO model_runtime_policy(
                  singleton_id, enforcement_mode, daily_cost_limit_cny, daily_token_limit,
                  failure_threshold, cooldown_seconds, updated_at
                ) VALUES (1, 'monitor', 20.0, 2000000, 4, 120, ?)""",
                (utc_now(),),
            )
            connection.commit()

    def policy(self) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_runtime_policy WHERE singleton_id = 1"
            ).fetchone()
        result = dict(row)
        result.pop("singleton_id", None)
        return result

    def update_policy(self, values: dict) -> dict:
        current = self.policy()
        merged = {**current, **values}
        if merged["enforcement_mode"] not in {"monitor", "enforce"}:
            raise ValueError("模型费用策略只支持观察或强制模式。")
        if not 0.1 <= float(merged["daily_cost_limit_cny"]) <= 10_000:
            raise ValueError("每日费用上限必须在0.1至10000元之间。")
        if not 10_000 <= int(merged["daily_token_limit"]) <= 100_000_000:
            raise ValueError("每日 token 上限必须在1万至1亿之间。")
        if not 1 <= int(merged["failure_threshold"]) <= 20:
            raise ValueError("连续失败阈值必须在1至20之间。")
        if not 30 <= int(merged["cooldown_seconds"]) <= 3600:
            raise ValueError("降级冷却时间必须在30秒至1小时之间。")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE model_runtime_policy SET enforcement_mode=?, daily_cost_limit_cny=?,
                  daily_token_limit=?, failure_threshold=?, cooldown_seconds=?, updated_at=?
                  WHERE singleton_id=1""",
                (
                    merged["enforcement_mode"], float(merged["daily_cost_limit_cny"]),
                    int(merged["daily_token_limit"]), int(merged["failure_threshold"]),
                    int(merged["cooldown_seconds"]), now,
                ),
            )
            connection.execute(
                """INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                   VALUES (lower(hex(randomblob(16))), 'parent', 'model_runtime_policy_updated', NULL, ?, ?)""",
                (json.dumps({
                    "enforcement_mode": merged["enforcement_mode"],
                    "daily_cost_limit_cny": float(merged["daily_cost_limit_cny"]),
                    "daily_token_limit": int(merged["daily_token_limit"]),
                    "failure_threshold": int(merged["failure_threshold"]),
                    "cooldown_seconds": int(merged["cooldown_seconds"]),
                }), now),
            )
            connection.commit()
        return self.policy()

    def status(self) -> dict:
        policy = self.policy()
        with self.database.connect() as connection:
            usage = connection.execute(
                """SELECT COUNT(*) AS calls,
                          COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0) AS tokens,
                          COALESCE(SUM(estimated_cost_cny), 0) AS cost,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures,
                          SUM(CASE WHEN fallback_used=1 THEN 1 ELSE 0 END) AS fallbacks
                   FROM model_call WHERE date(created_at, 'localtime')=date('now', 'localtime')"""
            ).fetchone()
            routes = [dict(row) for row in connection.execute(
                "SELECT * FROM model_route_state ORDER BY provider, model"
            ).fetchall()]
            purpose_rows = [dict(row) for row in connection.execute(
                """SELECT purpose, COUNT(*) AS calls,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures,
                          SUM(CASE WHEN verification_status='rejected' THEN 1 ELSE 0 END) AS rejected,
                          SUM(CASE WHEN fallback_used=1 THEN 1 ELSE 0 END) AS fallbacks,
                          ROUND(AVG(latency_ms), 1) AS average_latency_ms,
                          COALESCE(SUM(estimated_cost_cny), 0) AS estimated_cost_cny
                   FROM model_call
                   WHERE created_at >= datetime('now', '-7 days')
                   GROUP BY purpose ORDER BY calls DESC, purpose"""
            ).fetchall()]
        cost = float(usage["cost"] or 0)
        tokens = int(usage["tokens"] or 0)
        return {
            "policy": policy,
            "today": {
                "calls": int(usage["calls"] or 0), "tokens": tokens, "estimated_cost_cny": cost,
                "failures": int(usage["failures"] or 0), "fallbacks": int(usage["fallbacks"] or 0),
                "cost_limit_reached": cost >= float(policy["daily_cost_limit_cny"]),
                "token_limit_reached": tokens >= int(policy["daily_token_limit"]),
            },
            "routes": [self._normalized_route(row) for row in routes],
            "purpose_quality": [self._purpose_quality(row) for row in purpose_rows],
        }

    def authorize(self, request_type: str) -> None:
        status = self.status()
        if status["policy"]["enforcement_mode"] != "enforce":
            return
        over_limit = status["today"]["cost_limit_reached"] or status["today"]["token_limit_reached"]
        if over_limit and self._is_background(request_type):
            raise ModelPolicyBlockedError("MODEL_BACKGROUND_BUDGET_DEFERRED")
        quality = next(
            (item for item in status["purpose_quality"] if item["purpose"] == request_type), None
        )
        if quality and quality["state"] == "blocked" and self._is_background(request_type):
            raise ModelPolicyBlockedError("MODEL_BACKGROUND_QUALITY_GATE")

    def endpoint_available(self, provider: str, model: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, blocked_until FROM model_route_state WHERE provider=? AND model=?",
                (provider, model),
            ).fetchone()
        if not row or row["status"] == "healthy":
            return True
        if row["status"] in {"quota_exhausted", "auth_failed"}:
            return False
        if row["blocked_until"]:
            return datetime.fromisoformat(row["blocked_until"]) <= datetime.now(timezone.utc)
        return True

    def record_success(self, provider: str, model: str) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO model_route_state(provider, model, status, consecutive_failures,
                     last_error_code, blocked_until, last_success_at, updated_at)
                   VALUES (?, ?, 'healthy', 0, NULL, NULL, ?, ?)
                   ON CONFLICT(provider, model) DO UPDATE SET status='healthy', consecutive_failures=0,
                     last_error_code=NULL, blocked_until=NULL, last_success_at=excluded.last_success_at,
                     updated_at=excluded.updated_at""",
                (provider, model, now, now),
            )
            connection.commit()

    def record_failure(self, provider: str, model: str, error_code: str) -> None:
        policy = self.policy()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT consecutive_failures FROM model_route_state WHERE provider=? AND model=?",
                (provider, model),
            ).fetchone()
            failures = int(existing[0] if existing else 0) + 1
            status = "degraded"
            blocked_until = None
            if error_code == "MODEL_HTTP_401" or error_code == "MODEL_HTTP_403":
                status = "auth_failed"
            elif error_code == "MODEL_HTTP_402":
                status = "quota_exhausted"
            elif error_code == "MODEL_HTTP_429":
                status = "rate_limited"
                blocked_until = self._future(int(policy["cooldown_seconds"]))
            elif failures >= int(policy["failure_threshold"]):
                blocked_until = self._future(int(policy["cooldown_seconds"]))
            now = utc_now()
            connection.execute(
                """INSERT INTO model_route_state(provider, model, status, consecutive_failures,
                     last_error_code, blocked_until, last_success_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                   ON CONFLICT(provider, model) DO UPDATE SET status=excluded.status,
                     consecutive_failures=excluded.consecutive_failures,
                     last_error_code=excluded.last_error_code, blocked_until=excluded.blocked_until,
                     updated_at=excluded.updated_at""",
                (provider, model, status, failures, error_code, blocked_until, now),
            )
            connection.commit()

    @staticmethod
    def _future(seconds: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _is_background(request_type: str) -> bool:
        value = request_type.lower()
        return any(marker in value for marker in BACKGROUND_REQUEST_MARKERS)

    @staticmethod
    def _normalized_route(row: dict) -> dict:
        result = dict(row)
        if result["blocked_until"] and result["status"] in {"degraded", "rate_limited"}:
            if datetime.fromisoformat(result["blocked_until"]) <= datetime.now(timezone.utc):
                result["status"] = "degraded_recoverable"
        return result

    @staticmethod
    def _purpose_quality(row: dict) -> dict:
        result = dict(row)
        calls = max(1, int(result["calls"] or 0))
        failures = int(result["failures"] or 0)
        rejected = int(result["rejected"] or 0)
        bad_rate = max(failures, rejected) / calls
        average_latency = float(result["average_latency_ms"] or 0)
        if calls >= 5 and bad_rate >= 0.5:
            state = "blocked"
        elif bad_rate >= 0.2 or average_latency >= 10_000:
            state = "watch"
        else:
            state = "healthy"
        result.update({
            "calls": calls, "failures": failures, "rejected": rejected,
            "fallbacks": int(result["fallbacks"] or 0),
            "average_latency_ms": average_latency,
            "estimated_cost_cny": float(result["estimated_cost_cny"] or 0),
            "bad_result_rate": round(bad_rate, 3), "state": state,
        })
        return result


class ModelPolicyBlockedError(ModelResponseError):
    def __init__(self, code: str):
        super().__init__(code, fallback_eligible=False)
