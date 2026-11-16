from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.child_safety_eval import (
    aggregate_results,
    asr_release_gate,
    character_error_rate,
    parse_case,
)
from home_study_agent.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate local ASR-to-safety routing with synthetic Mandarin audio.")
    default_cases = Path(__file__).parents[1] / "eval" / "child_safety_audio_cases.json"
    parser.add_argument(
        "--cases",
        type=Path,
        default=default_cases,
    )
    parser.add_argument("--model-root", type=Path, default=Path(__file__).parents[1] / "models")
    parser.add_argument("--voice", action="append", dest="voices")
    parser.add_argument("--rate", action="append", type=int, dest="rates")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    if not say or not ffmpeg:
        raise SystemExit("This synthetic-audio development check requires macOS 'say' and ffmpeg; it is not the Windows microphone acceptance test.")
    cases = [parse_case(item) for item in json.loads(args.cases.read_text(encoding="utf-8"))]
    voices = args.voices or ["Tingting"]
    rates = args.rates or [175]

    with tempfile.TemporaryDirectory(prefix="hsc-asr-safety-eval-") as temp_dir:
        data_dir = Path(temp_dir)
        settings = Settings(
            data_dir=data_dir,
            database_path=data_dir / "eval.db",
            local_model_dir=args.model_root.resolve(),
            data_encryption_key=b"a" * 32,
            maintenance_enabled=False,
        )
        results = run_cases(settings, cases, say, ffmpeg, voices, rates, data_dir)
        connection = sqlite3.connect(settings.database_path)
        model_call_count = connection.execute("SELECT COUNT(*) FROM model_call").fetchone()[0]
        connection.close()

    summary = aggregate_results(results)
    asr_failure_count = sum(result["asr_status"] != "completed" for result in results)
    character_error_rates = [float(result["character_error_rate"]) for result in results]
    mean_cer = round(sum(character_error_rates) / len(character_error_rates), 4)
    p95_cer = percentile(character_error_rates, 0.95)
    summary["mean_character_error_rate"] = mean_cer
    summary["p95_character_error_rate"] = p95_cer
    summary["profiles"] = profile_summaries(results)
    gate = asr_release_gate(summary, asr_failure_count, model_call_count, mean_cer, p95_cer)
    built_in_dataset = args.cases.resolve() == default_cases.resolve()
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.cases.resolve()),
        "voices": voices,
        "speech_rates": rates,
        "asr_model": "sherpa-onnx-zipformer-zh-en-2023-11-22",
        "classifier_version": "child_safety_v2",
        "summary": summary,
        "asr_failure_count": asr_failure_count,
        "model_call_count": model_call_count,
        "release_gate": gate,
        "results": results,
        "dataset_provenance": "built_in_synthetic" if built_in_dataset else "caller_supplied_unverified",
        "contains_real_child_data": False if built_in_dataset else "unknown",
        "uses_synthetic_adult_voice": True,
        "is_windows_microphone_acceptance": False,
    }
    output = args.output or Path("artifacts") / f"asr-safety-eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "summary": summary, "asr_failure_count": asr_failure_count, "model_call_count": model_call_count, "release_gate": gate}, ensure_ascii=False))
    if args.strict and not gate["passed"]:
        raise SystemExit(1)


def run_cases(
    settings: Settings,
    cases: list,
    say: str,
    ffmpeg: str,
    voices: list[str],
    rates: list[int],
    data_dir: Path,
) -> list[dict]:
    app = create_app(settings)
    results = []
    with TestClient(app) as client:
        session_id = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "asr-safety-eval-session"},
        ).json()["session_id"]
        for profile_index, (voice, rate) in enumerate(
            (voice, rate) for voice in voices for rate in rates
        ):
            profile_id = f"profile-{profile_index + 1}-rate-{rate}"
            for index, case in enumerate(cases):
                aiff_path = data_dir / f"{profile_index:02d}-{index:02d}.aiff"
                wav_path = data_dir / f"{profile_index:02d}-{index:02d}.wav"
                subprocess.run(
                    [say, "-v", voice, "-r", str(rate), "-o", str(aiff_path), case.transcript],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                subprocess.run(
                    [ffmpeg, "-loglevel", "error", "-y", "-i", str(aiff_path), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                asr = client.post(
                    f"/v1/sessions/{session_id}/asr",
                    json={
                        "wav_base64": base64.b64encode(wav_path.read_bytes()).decode("ascii"),
                        "purpose": "child_safety_synthetic_eval",
                        "client_action_id": f"asr-safety-audio-{profile_id}-{case.case_id}",
                    },
                )
                if asr.status_code != 200:
                    results.append({
                        "case_id": f"{case.case_id}::{profile_id}",
                        "source_case_id": case.case_id,
                        "profile_id": profile_id,
                        "voice": voice,
                        "speech_rate": rate,
                        "expected_risk": case.expected_risk,
                        "predicted_risk": "NONE",
                        "tags": list(case.tags),
                        "passed": False,
                        "asr_status": "failed",
                        "asr_error_code": asr.json().get("code", "UNKNOWN"),
                        "character_error_rate": 1.0,
                    })
                    continue
                transcript = asr.json()["transcript"]
                turn = client.post(
                    f"/v1/sessions/{session_id}/agent-turn",
                    json={
                        "transcript": transcript,
                        "client_action_id": f"asr-safety-turn-{profile_id}-{case.case_id}",
                    },
                ).json()
                predicted = turn["action"].get("risk_level", "NONE")
                results.append({
                    "case_id": f"{case.case_id}::{profile_id}",
                    "source_case_id": case.case_id,
                    "profile_id": profile_id,
                    "voice": voice,
                    "speech_rate": rate,
                    "expected_risk": case.expected_risk,
                    "predicted_risk": predicted,
                    "tags": list(case.tags),
                    "passed": predicted == case.expected_risk,
                    "asr_status": "completed",
                    "recognized_transcript": transcript,
                    "character_error_rate": character_error_rate(case.transcript, transcript),
                })
    return results


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 4)


def profile_summaries(results: list[dict]) -> list[dict]:
    summaries = []
    for profile_id in dict.fromkeys(result["profile_id"] for result in results):
        profile_results = [result for result in results if result["profile_id"] == profile_id]
        summary = aggregate_results(profile_results)
        rates = [float(result["character_error_rate"]) for result in profile_results]
        summaries.append({
            "profile_id": profile_id,
            "voice": profile_results[0]["voice"],
            "speech_rate": profile_results[0]["speech_rate"],
            "accuracy": summary["accuracy"],
            "r3_recall": summary["per_label"]["R3"]["recall"],
            "mean_character_error_rate": round(sum(rates) / len(rates), 4),
            "p95_character_error_rate": percentile(rates, 0.95),
        })
    return summaries


if __name__ == "__main__":
    main()
