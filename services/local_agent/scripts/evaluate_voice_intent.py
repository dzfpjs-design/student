from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway, ModelRequest
from home_study_agent.voice_intent import (
    CLOUD_INTENT_TIMEOUT_SECONDS,
    POLICY_VERSION,
    PROMPT_VERSION,
    InvalidVoiceIntentOutputError,
    build_classifier_messages,
    is_voice_intent_prompt_injection,
    parse_classifier_output,
)
from home_study_agent.voice_intent_eval import (
    aggregate_voice_intent_results,
    voice_intent_release_gate,
)


async def run_cases(settings: Settings, cases: list[dict]) -> list[dict]:
    gateway = ModelGateway(settings)
    results: list[dict] = []
    try:
        for case in cases:
            request_id = str(uuid4())
            if is_voice_intent_prompt_injection(case["utterance"]):
                results.append({
                    "case_id": case["case_id"],
                    "state": case["state"],
                    "case_kind": case["case_kind"],
                    "expected_intent": case["expected_intent"],
                    "status": "completed",
                    "schema_valid": True,
                    "predicted_intent": "supportive_chat",
                    "confidence": 1.0,
                    "correct": case["expected_intent"] == "supportive_chat",
                    "would_execute": False,
                    "provider": "local_policy",
                    "model": "voice_intent_prompt_guard_v1",
                    "latency_ms": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "fallback_used": False,
                })
                continue
            messages = build_classifier_messages(case["state"], case["utterance"][:800])
            request = ModelRequest(
                request_id=request_id,
                child_id="synthetic-eval-child",
                session_id=f"synthetic-{case['state']}",
                request_type="voice_agent_intent_eval",
                risk_level="low",
                context_manifest={
                    "data_source": "synthetic_evaluation",
                    "current_state": case["state"],
                    "raw_audio_or_video": False,
                },
                provider_preference=settings.model_provider,
                policy_version=POLICY_VERSION,
                prompt_version=PROMPT_VERSION,
                knowledge_version=None,
                output_type="dialogue",
                evidence_refs=(),
                messages=messages,
            )
            base = {
                "case_id": case["case_id"],
                "state": case["state"],
                "case_kind": case["case_kind"],
                "expected_intent": case["expected_intent"],
            }
            try:
                transport = await asyncio.wait_for(
                    gateway.complete(request),
                    timeout=CLOUD_INTENT_TIMEOUT_SECONDS,
                )
                candidate = parse_classifier_output(transport.content, case["state"])
                would_execute = (
                    candidate.intent != "supportive_chat" and candidate.confidence >= 0.82
                )
                results.append({
                    **base,
                    "status": "completed",
                    "schema_valid": True,
                    "predicted_intent": candidate.intent,
                    "confidence": round(candidate.confidence, 3),
                    "correct": candidate.intent == case["expected_intent"],
                    "would_execute": would_execute,
                    "provider": transport.provider,
                    "model": transport.model,
                    "latency_ms": transport.latency_ms,
                    "prompt_tokens": transport.prompt_tokens,
                    "completion_tokens": transport.completion_tokens,
                    "fallback_used": transport.fallback_used,
                })
            except InvalidVoiceIntentOutputError as error:
                results.append({**base, "status": "completed", "schema_valid": False, "error_code": str(error)})
            except asyncio.TimeoutError:
                results.append({**base, "status": "failed", "schema_valid": False, "error_code": "VOICE_INTENT_TIMEOUT"})
            except Exception as error:
                results.append({**base, "status": "failed", "schema_valid": False, "error_code": getattr(error, "code", type(error).__name__)})
    finally:
        await gateway.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the real bounded voice intent classifier with synthetic text.")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parents[1] / "eval" / "voice_intent_cases.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--input-cny-per-million", type=float, default=float(os.environ.get("HSC_EVAL_INPUT_CNY_PER_MILLION", "0")))
    parser.add_argument("--output-cny-per-million", type=float, default=float(os.environ.get("HSC_EVAL_OUTPUT_CNY_PER_MILLION", "0")))
    args = parser.parse_args()

    settings = Settings.from_environment()
    if not settings.model_api_key:
        raise SystemExit("HSC_MODEL_API_KEY is required; credentials are never written to the report.")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.limit > 0:
        cases = cases[: args.limit]
    results = asyncio.run(run_cases(settings, cases))
    summary = aggregate_voice_intent_results(
        results, args.input_cny_per_million, args.output_cny_per_million
    )
    gate = voice_intent_release_gate(summary)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.cases.resolve()),
        "provider_preference": settings.model_provider,
        "model_preference": settings.voice_intent_model_name or settings.model_name,
        "fallback_configured": bool(settings.fallback_model_api_key),
        "summary": summary,
        "release_gate": gate,
        "results": results,
        "contains_credentials": False,
        "contains_real_child_data": False,
    }
    output = args.output or Path("artifacts") / f"voice-intent-eval-{settings.model_provider}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "summary": summary, "release_gate": gate}, ensure_ascii=False))
    if args.strict and not gate["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
