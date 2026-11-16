from __future__ import annotations
from normal_provider_owner import normal_provider_owner

import asyncio
from pathlib import Path

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway, ModelRequest
from home_study_agent.model_runtime_policy import ModelPolicyBlockedError, ModelRuntimePolicyService


def _request(request_type: str) -> ModelRequest:
    return ModelRequest(
        request_id=f"request-{request_type}", child_id="child-a", session_id="session-a",
        request_type=request_type, risk_level="medium", context_manifest={},
        provider_preference="qwen", policy_version="policy-v1", prompt_version="prompt-v1",
        knowledge_version=None, output_type="tutoring", evidence_refs=(),
        messages=({"role": "user", "content": "请帮我。"},),
    )


def _policy(tmp_path: Path) -> ModelRuntimePolicyService:
    database = Database(tmp_path / "agent.db")
    database.initialize()
    policy = ModelRuntimePolicyService(database)
    policy.initialize()
    return policy


def test_budget_enforcement_defers_background_work_but_keeps_child_dialogue_available(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.update_policy({
        "enforcement_mode": "enforce", "daily_cost_limit_cny": 0.1,
        "daily_token_limit": 10_000, "failure_threshold": 4, "cooldown_seconds": 120,
    })
    with policy.database.connect() as connection:
        connection.execute(
            """INSERT INTO model_call(
              call_id, request_id, purpose, provider, model, status, input_chars,
              prompt_version, context_manifest_json, risk_level, provider_preference,
              policy_version, fallback_used, prompt_tokens, completion_tokens,
              estimated_cost_cny, created_at
            ) VALUES ('call-1','request-1','tutoring','qwen','qwen-test','completed',10,
              'v1','{}','medium','qwen','v1',0,9000,1000,0.1,datetime('now'))"""
        )
        connection.commit()

    with pytest.raises(ModelPolicyBlockedError, match="MODEL_BACKGROUND_BUDGET_DEFERRED"):
        policy.authorize("mistake_variant_generation")
    policy.authorize("voice_agent_intent")
    policy.authorize("tutor_hint_level_2")
    status = policy.status()
    assert status["today"]["cost_limit_reached"] is True
    assert status["today"]["token_limit_reached"] is True


def test_route_health_blocks_rate_limited_endpoint_and_success_recovers_it(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.record_failure("qwen", "qwen-primary", "MODEL_HTTP_429")
    assert policy.endpoint_available("qwen", "qwen-primary") is False
    assert policy.status()["routes"][0]["status"] == "rate_limited"

    policy.record_success("qwen", "qwen-primary")
    assert policy.endpoint_available("qwen", "qwen-primary") is True
    assert policy.status()["routes"][0]["status"] == "healthy"


def test_quality_gate_stops_repeated_rejected_background_generation(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.update_policy({
        "enforcement_mode": "enforce", "daily_cost_limit_cny": 20,
        "daily_token_limit": 2_000_000, "failure_threshold": 4, "cooldown_seconds": 120,
    })
    with policy.database.connect() as connection:
        for index in range(5):
            connection.execute(
                """INSERT INTO model_call(
                  call_id, request_id, purpose, provider, model, status, input_chars,
                  prompt_version, context_manifest_json, risk_level, provider_preference,
                  policy_version, verification_status, fallback_used, created_at
                ) VALUES (?, ?, 'mistake_variant_generation', 'qwen', 'qwen-test', 'completed',
                  10, 'v1', '{}', 'medium', 'qwen', 'v1', 'rejected', 0, datetime('now'))""",
                (f"quality-call-{index}", f"quality-request-{index}"),
            )
        connection.commit()

    quality = policy.status()["purpose_quality"][0]
    assert quality["state"] == "blocked" and quality["bad_result_rate"] == 1
    with pytest.raises(ModelPolicyBlockedError, match="MODEL_BACKGROUND_QUALITY_GATE"):
        policy.authorize("mistake_variant_generation")
    policy.authorize("companion_dialogue")


def test_gateway_skips_blocked_primary_and_marks_fallback_usage(tmp_path: Path, normal_provider_owner) -> None:
    policy = _policy(tmp_path)
    policy.record_failure("qwen", "qwen-primary", "MODEL_HTTP_402")
    requested_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_models.append(request.read().decode())
        return httpx.Response(200, request=request, json={
            "choices": [{"message": {"content": "先圈出已知条件。"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 6},
        })

    settings = Settings(
        data_dir=tmp_path, database_path=policy.database.path,
        model_provider="qwen", model_api_key="synthetic-64a04c6ee432d0c9370920d0", model_base_url="https://primary.test/v1",
        model_name="qwen-primary", fallback_model_provider="deepseek",
        fallback_model_api_key="synthetic-6823e438f494077cce88", fallback_model_base_url="https://fallback.test/v1",
        fallback_model_name="deepseek-fallback",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler), runtime_policy=policy)
    try:
        # Generic dialogue still supports provider fallback; dedicated Tutor does not.
        result = asyncio.run(gateway.complete(_request("companion_dialogue")))
    finally:
        asyncio.run(gateway.close())

    assert len(requested_models) == 1 and "deepseek-fallback" in requested_models[0]
    assert result.provider == "deepseek" and result.fallback_used is True
