"""Evaluate CPU ASR input resilience with public/synthetic non-private audio.

The evaluator derives deterministic noisy, far-field-approximation and clipped
variants in memory. It never stores source WAVs, generated WAVs, or ASR text;
the JSON report contains only source hash, transcript length/hash, latency and
failure codes. It is a development signal-quality evaluation, not evidence of
child-ASR accuracy or speaker verification.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import wave

import numpy as np

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.repository import StudyRepository


SEED = 20260828


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("input must be mono PCM16 WAV")
        rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return rate, samples


def as_wav(sample_rate: int, samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1, 1)
    stream = BytesIO()
    with wave.open(stream, "wb") as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(sample_rate)
        target.writeframes((clipped * 32767).astype(np.int16).tobytes())
    return stream.getvalue()


def variants(samples: np.ndarray) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(SEED)
    rms = max(float(np.sqrt(np.mean(samples * samples))), 1e-5)
    noise = rng.normal(0, 1, len(samples)).astype(np.float32)
    def with_snr(decibels: float) -> np.ndarray:
        return samples + noise * (rms / (10 ** (decibels / 20)))
    # A short, fixed synthetic reflection is only a repeatable far-field
    # approximation; it is not a measured household-room impulse response.
    reflected = np.convolve(samples, np.array([0.55, 0.24, 0.12], dtype=np.float32), mode="same")
    return {
        "clean_public": samples,
        "synthetic_noise_20db": with_snr(20),
        "synthetic_noise_10db": with_snr(10),
        "synthetic_far_field_approx": reflected + noise * (rms / (10 ** (16 / 20))),
        "synthetic_clipping": np.clip(samples * 2.2, -1, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate local ASR with deterministic non-private capture perturbations.")
    parser.add_argument("--wav", type=Path, required=True, help="Public or synthetic mono PCM16 WAV; never copied")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sample_rate, source = read_wav(args.wav)
    source_bytes = args.wav.read_bytes()
    results: list[dict] = []
    with TemporaryDirectory(prefix="hsc-voice-resilience-") as temporary:
        root = Path(temporary)
        settings = Settings(data_dir=root, database_path=root / "evaluation.db", local_model_dir=args.model_dir, maintenance_enabled=False)
        database = Database(settings.database_path); database.initialize()
        repository = StudyRepository(database); repository.seed_family()
        session_id = repository.start_session("child-primary-4", "public-voice-resilience") ["session_id"]
        media = LocalMediaAI(database, root, args.model_dir)
        for name, value in variants(source).items():
            started = perf_counter()
            try:
                outcome = media.transcribe(session_id, as_wav(sample_rate, value), "public_capture_eval", f"capture-resilience-{name}", False)
                transcript = outcome["transcript"]
                results.append({"case": name, "status": "completed", "latency_ms": round((perf_counter() - started) * 1000, 1), "transcript_chars": len(transcript), "transcript_sha256": sha256(transcript.encode("utf-8")).hexdigest()})
            except Exception as error:  # Preserve the real error type, not audio/text.
                results.append({"case": name, "status": "failed", "latency_ms": round((perf_counter() - started) * 1000, 1), "error_code": getattr(error, "code", type(error).__name__)})
    report = {
        "evaluation": "hsc_voice_capture_resilience_v1",
        "seed": SEED,
        "source": {"sha256": sha256(source_bytes).hexdigest(), "public_or_synthetic_required": True, "audio_persisted": False, "transcript_persisted": False},
        "variants": {"synthetic_noise_20db": "deterministic white noise", "synthetic_noise_10db": "deterministic white noise", "synthetic_far_field_approx": "fixed short reflection plus 16 dB noise; not room measurement", "synthetic_clipping": "amplitude stress case"},
        "results": results,
        "acceptance": "clean_public must complete; degraded cases are measured, not silently treated as passes",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0 if any(item["case"] == "clean_public" and item["status"] == "completed" for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
