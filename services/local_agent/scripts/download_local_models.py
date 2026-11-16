from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from urllib.request import urlopen


ASR_ARCHIVE = "sherpa-onnx-zipformer-zh-en-2023-11-22.tar.bz2"
ASR_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{ASR_ARCHIVE}"
ASR_DIR = "sherpa-onnx-zipformer-zh-en-2023-11-22"
MANIFEST_PATH = Path(__file__).parents[1] / "model-manifest.json"


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("model_dir") != ASR_DIR or manifest.get("source_url") != ASR_URL:
        raise RuntimeError("ASR model manifest does not match the pinned release")
    return manifest


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_files(model_dir: Path, files: dict) -> list[str]:
    failures = []
    for name, expected in files.items():
        path = model_dir / name
        if not path.is_file():
            failures.append(f"missing {name}")
        elif path.stat().st_size < int(expected["minimum_bytes"]):
            failures.append(f"truncated {name}")
        elif sha256(path) != expected["sha256"]:
            failures.append(f"checksum mismatch {name}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).parents[1] / "models")
    parser.add_argument("--force", action="store_true", help="redownload the pinned model release")
    args = parser.parse_args()
    manifest = load_manifest()
    required_files = manifest["files"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    model_dir = output / ASR_DIR
    retained_entries = set(required_files) | {"test_wavs"}
    if not args.force and not verify_files(model_dir, required_files):
        for path in model_dir.iterdir():
            if path.name not in retained_entries:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        print(f"ASR model already ready: {model_dir}")
        return

    shutil.rmtree(model_dir, ignore_errors=True)

    archive = output / ASR_ARCHIVE
    print(f"Downloading {ASR_URL}")
    with urlopen(ASR_URL, timeout=60) as response, archive.open("wb") as target:
        shutil.copyfileobj(response, target)
    with tarfile.open(archive, "r:bz2") as bundle:
        root = output.resolve()
        for member in bundle.getmembers():
            destination = (output / member.name).resolve()
            if root not in destination.parents and destination != root:
                raise RuntimeError("Unsafe path in model archive")
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise RuntimeError(f"Unsupported entry in model archive: {member.name}")
        bundle.extractall(output)
    archive.unlink()

    for path in model_dir.iterdir():
        if path.name not in retained_entries:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    failures = verify_files(model_dir, required_files)
    if failures:
        shutil.rmtree(model_dir, ignore_errors=True)
        raise RuntimeError(f"ASR model verification failed: {', '.join(failures)}")
    print(f"ASR model ready: {model_dir}")


if __name__ == "__main__":
    main()
