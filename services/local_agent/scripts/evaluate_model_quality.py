from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_eval import aggregate_results, evaluate_answer, parse_case, release_gate


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the real tutor path with synthetic questions.")
    parser.add_argument("--cases", type=Path, default=Path(__file__).parents[1] / "eval" / "tutor_cases.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--input-cny-per-million", type=float, default=float(os.environ.get("HSC_EVAL_INPUT_CNY_PER_MILLION", "0")))
    parser.add_argument("--output-cny-per-million", type=float, default=float(os.environ.get("HSC_EVAL_OUTPUT_CNY_PER_MILLION", "0")))
    args = parser.parse_args()

    base_settings = Settings.from_environment()
    if not base_settings.model_api_key:
        raise SystemExit("HSC_MODEL_API_KEY is required; credentials are never written to the report.")
    cases = [parse_case(item) for item in json.loads(args.cases.read_text(encoding="utf-8"))]
    if args.limit > 0:
        cases = cases[: args.limit]

    with tempfile.TemporaryDirectory(prefix="hsc-model-eval-") as temp_dir:
        data_dir = Path(temp_dir)
        settings = replace(base_settings, data_dir=data_dir, database_path=data_dir / "eval.db", data_encryption_key=b"e" * 32)
        results = run_cases(settings, cases)

    summary = aggregate_results(results, args.input_cny_per_million, args.output_cny_per_million)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.cases.resolve()),
        "provider_preference": base_settings.model_provider,
        "model_preference": base_settings.model_name,
        "fallback_configured": bool(base_settings.fallback_model_api_key),
        "pricing_input_cny_per_million": args.input_cny_per_million,
        "pricing_output_cny_per_million": args.output_cny_per_million,
        "summary": summary,
        "release_gate": release_gate(summary),
        "results": results,
        "contains_credentials": False,
        "contains_real_child_data": False,
    }
    output = args.output or Path("artifacts") / f"model-eval-{base_settings.model_provider}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "summary": summary, "release_gate": report["release_gate"]}, ensure_ascii=False))
    if args.strict and not report["release_gate"]["passed"]:
        raise SystemExit(1)


def run_cases(settings: Settings, cases: list) -> list[dict]:
    results: list[dict] = []
    groups = (
        ("child-primary-4", [case for case in cases if case.grade.startswith(("四", "五", "六"))]),
        ("child-junior-3", [case for case in cases if not case.grade.startswith(("四", "五", "六"))]),
    )
    for child_id, group in groups:
        if not group:
            continue
        group_settings = replace(
            settings,
            database_path=settings.data_dir / f"eval-{child_id}.db",
        )
        app = create_app(group_settings)
        with TestClient(app) as client:
            session_id = prepare_study_session(client, child_id)
            for case in group:
                _run_case(client, group_settings.database_path, session_id, case, results)
    return results


def _run_case(client: TestClient, database_path: Path, session_id: str, case, results: list[dict]) -> None:
    action_id = f"eval-{case.case_id}-{uuid4()}"
    response = client.post(
        f"/v1/sessions/{session_id}/tutor",
        json={
            "subject": case.subject,
            "question": case.question,
            "hint_level": case.hint_level,
            "client_action_id": action_id,
        },
    )
    audit = latest_model_call(database_path)
    if response.status_code != 200:
        body = response.json()
        results.append({
            "case_id": case.case_id,
            "status": "failed",
            "error_code": body.get("code", "UNKNOWN"),
            "retryable": body.get("retryable"),
            "latency_ms": audit.get("latency_ms"),
            "provider": audit.get("provider"),
            "model": audit.get("model"),
        })
        return
    body = response.json()
    quality = evaluate_answer(case, body["answer"])
    results.append({
        "case_id": case.case_id,
        "grade": case.grade,
        "subject": case.subject,
        "hint_level": case.hint_level,
        "status": "completed",
        "provider": body["provider"],
        "model": body["model"],
        "answer": body["answer"],
        "verification_status": body["verification_status"],
        "safety_status": body["safety_status"],
        "latency_ms": audit.get("latency_ms"),
        "prompt_tokens": audit.get("prompt_tokens"),
        "completion_tokens": audit.get("completion_tokens"),
        "fallback_used": bool(audit.get("fallback_used")),
        **quality,
    })


def prepare_study_session(client: TestClient, child_id: str = "child-junior-3") -> str:
    action_suffix = child_id.replace("child-", "")
    session = client.post("/v1/sessions", json={"child_id": child_id, "client_action_id": f"eval-session-{action_suffix}"}).json()
    session_id = session["session_id"]
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "check_in", "target_state": "classroom_recall", "reason": "eval", "client_action_id": "eval-transition-recall"})
    client.post(f"/v1/sessions/{session_id}/recalls", json={"subject": "数学", "summary": "这是不含真实孩子数据的合成评测会话。", "client_action_id": "eval-recall"})
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "eval", "client_action_id": "eval-transition-homework"})
    client.post(f"/v1/sessions/{session_id}/homework", json={"subject": "其他", "title": "合成讲题评测", "expected_minutes": 30, "client_action_id": "eval-homework"})
    client.post(f"/v1/sessions/{session_id}/plan/generate", json={"client_action_id": "eval-plan"})
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "plan_review", "target_state": "study", "reason": "eval", "client_action_id": "eval-transition-study"})
    return session_id


def latest_model_call(path: Path) -> dict:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT provider, model, status, latency_ms, prompt_tokens, completion_tokens, error_code, fallback_used FROM model_call ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    connection.close()
    return dict(row) if row else {}


if __name__ == "__main__":
    main()
