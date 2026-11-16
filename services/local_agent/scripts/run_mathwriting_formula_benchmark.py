"""Run a locally installed formula provider on MathWriting without uploading images."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
from time import perf_counter

from home_study_agent.evaluation.shared_ocr_benchmarks import mathwriting_benchmark
from home_study_agent.local_ocr.formula_recognition import (
    FormulaProviderUnavailable,
    Pix2TexFormulaProvider,
    Pix2TextMfrOnnxProvider,
    RapidOcrFormulaBaselineProvider,
)


def _p95(values: list[float]) -> float | None:
    return sorted(values)[max(0, int(len(values) * .95) - 1)] if values else None


def _p50(values: list[float]) -> float | None:
    return sorted(values)[len(values) // 2] if values else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", type=Path, required=True, help="Read-only mathwriting_excerpt directory")
    parser.add_argument("--provider", choices=("pix2text_mfr_onnx", "pix2tex", "rapidocr_baseline"), default="pix2text_mfr_onnx")
    parser.add_argument("--checkpoint", type=Path, help="Audited local pix2tex checkpoint; no download is attempted")
    parser.add_argument("--model-dir", type=Path, help="Verified local Pix2Text MFR-1.5 cache; no network lookup is allowed")
    parser.add_argument("--model-manifest", type=Path, help="Hash inventory for --model-dir")
    parser.add_argument("--predictions-output", type=Path, required=True, help="Local disposable JSONL; do not commit")
    parser.add_argument("--report-output", type=Path, required=True, help="Aggregate-only report")
    parser.add_argument("--limit", type=int, default=0, help="0 means all 500 images")
    args = parser.parse_args()

    cold_started = perf_counter()
    if args.provider == "pix2tex":
        if args.checkpoint is None:
            raise SystemExit("--checkpoint is required for --provider pix2tex")
        provider = Pix2TexFormulaProvider(args.checkpoint)
    elif args.provider == "pix2text_mfr_onnx":
        if args.model_dir is None or args.model_manifest is None:
            raise SystemExit("--model-dir and --model-manifest are required for --provider pix2text_mfr_onnx")
        provider = Pix2TextMfrOnnxProvider(args.model_dir, args.model_manifest)
    else:
        provider = RapidOcrFormulaBaselineProvider()
    health = provider.health()
    if not health["ready"]:
        raise SystemExit(json.dumps({"status": "provider_unavailable", "health": health}, ensure_ascii=False))
    cold_start_seconds = perf_counter() - cold_started
    labels = [json.loads(line) for line in (args.assets_root / "rendered_png" / "labels.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    labels = labels[:args.limit] if args.limit else labels
    args.predictions_output.parent.mkdir(parents=True, exist_ok=True)
    latencies: list[float] = []
    failures = 0
    started = perf_counter()
    with args.predictions_output.open("w", encoding="utf-8") as output:
        for label in labels:
            image_path = args.assets_root / "rendered_png" / str(label.get("split", "unknown")) / f"{label['id']}.png"
            try:
                prediction = provider.recognize(image_path.read_bytes())
                latencies.append(prediction.latency_seconds)
                output.write(json.dumps({"id": label["id"], "latex": prediction.latex}, ensure_ascii=False) + "\n")
            except (FormulaProviderUnavailable, OSError, ValueError) as error:
                failures += 1
                output.write(json.dumps({"id": label["id"], "latex": "", "failure": type(error).__name__}, ensure_ascii=False) + "\n")
    report = mathwriting_benchmark(args.assets_root, args.predictions_output)
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["provider_runtime"] = {
        "provider": provider.name,
        "cpu_only": True,
        "checkpoint_bytes": args.checkpoint.stat().st_size if args.checkpoint else health.get("model_bytes"),
        "attempted_formula_count": len(labels),
        "failure_count": failures,
        "cold_start_seconds": round(cold_start_seconds, 3),
        "latency_p50_seconds": _p50(latencies),
        "latency_p95_seconds": _p95(latencies),
        "wall_seconds": round(perf_counter() - started, 3),
        "peak_rss_bytes": int(max_rss if __import__("sys").platform == "darwin" else max_rss * 1024),
        "manual_formula_edit_fallback": True,
        "formula_specialist": args.provider in {"pix2tex", "pix2text_mfr_onnx"},
        "implicit_network_download": False,
    }
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "completed", "report": str(args.report_output), "runtime": report["provider_runtime"], "metrics": report["metrics"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
