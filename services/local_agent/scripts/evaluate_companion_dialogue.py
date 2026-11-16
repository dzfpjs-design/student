from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from home_study_agent.companion_dialogue import (
    POLICY_VERSION,
    PROMPT_VERSION,
    build_companion_messages,
    enforce_companion_reply,
    safe_companion_fallback,
)
from home_study_agent.companion_eval import (
    aggregate_companion_results,
    assess_companion_result,
    companion_release_gate,
)
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway, ModelRequest


async def run_cases(settings: Settings, cases: list[dict]) -> list[dict]:
    gateway = ModelGateway(settings)
    results: list[dict] = []
    try:
        for case in cases:
            messages = build_companion_messages(
                grade=case["grade"],
                session_state=case["state"],
                transcript=case["utterance"][:500],
                previous_response=case.get("previous_response"),
            )
            request = ModelRequest(
                request_id=str(uuid4()),
                child_id="synthetic-eval-child",
                session_id=f"synthetic-{case['case_id']}",
                request_type="companion_dialogue",
                risk_level="low",
                context_manifest={"data_source": "synthetic_evaluation", "raw_audio_or_video": False},
                provider_preference=settings.model_provider,
                policy_version=POLICY_VERSION,
                prompt_version=PROMPT_VERSION,
                knowledge_version=None,
                output_type="dialogue",
                evidence_refs=(),
                messages=messages,
            )
            base = {"case_id": case["case_id"], "case_kind": case["case_kind"]}
            try:
                transport = await asyncio.wait_for(gateway.complete(request), timeout=8.0)
                filtered = enforce_companion_reply(transport.content, case["utterance"])
                response = filtered or safe_companion_fallback(case["utterance"])
                assessment = assess_companion_result(case, response)
                results.append({
                    **base, "status": "completed", **assessment,
                    "provider": transport.provider, "model": transport.model,
                    "latency_ms": transport.latency_ms,
                    "prompt_tokens": transport.prompt_tokens,
                    "completion_tokens": transport.completion_tokens,
                    "fallback_used": transport.fallback_used,
                    "output_filtered": filtered is None,
                    "response": response,
                })
            except asyncio.TimeoutError:
                results.append({**base, "status": "failed", "error_code": "COMPANION_TIMEOUT"})
            except Exception as error:
                results.append({
                    **base, "status": "failed",
                    "error_code": getattr(error, "code", type(error).__name__),
                })
    finally:
        await gateway.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate real short companion dialogue on synthetic child-safe text.")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parents[1] / "eval" / "companion_dialogue_cases.json")
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
        cases = cases[:args.limit]
    results = asyncio.run(run_cases(settings, cases))
    summary = aggregate_companion_results(results, args.input_cny_per_million, args.output_cny_per_million)
    gate = companion_release_gate(summary)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.cases.resolve()),
        "provider_preference": settings.model_provider,
        "model_preference": settings.voice_intent_model_name or settings.model_name,
        "fallback_configured": bool(settings.fallback_model_api_key),
        "summary": summary,
        "automated_release_gate": gate,
        "results": results,
        "contains_credentials": False,
        "contains_real_child_data": False,
        "scope_note": "Automatic checks cover bounded safety/latency only; child appropriateness and warmth require human review.",
    }
    output = args.output or Path("artifacts") / f"companion-eval-{settings.model_provider}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "summary": summary, "automated_release_gate": gate}, ensure_ascii=False))
    if args.strict and not gate["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
