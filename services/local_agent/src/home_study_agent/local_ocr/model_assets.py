"""Integrity metadata for explicit, locally stored OCR model assets.

Model acquisition is deliberately separate from inference.  These helpers never
contact a model host and let startup reject an incomplete or changed cache.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path


_IGNORED_PARTS = {".git", ".cache", "__pycache__"}
_IGNORED_NAMES = {"model-manifest.json"}


def build_model_manifest(model_dir: Path, *, repo_id: str, revision: str, license_name: str) -> dict:
    """Return a deterministic, weight-free inventory for a downloaded model."""
    root = Path(model_dir).resolve()
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in _IGNORED_NAMES or any(part in _IGNORED_PARTS for part in path.relative_to(root).parts):
            continue
        files.append({
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return {
        "schema_version": 1,
        "repo_id": repo_id,
        "revision": revision,
        "license": license_name,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "files": files,
        "inference_network_policy": "offline_only; inference accepts an explicit local path and never downloads",
    }


def verify_model_manifest(model_dir: Path, manifest: dict) -> list[str]:
    """Return compact integrity failures without exposing model contents."""
    root = Path(model_dir).resolve()
    failures: list[str] = []
    for item in manifest.get("files", []):
        relative = str(item.get("path", ""))
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            failures.append(f"missing:{relative}")
            continue
        if candidate.stat().st_size != item.get("bytes"):
            failures.append(f"size_mismatch:{relative}")
            continue
        if _sha256(candidate) != item.get("sha256"):
            failures.append(f"sha256_mismatch:{relative}")
    return failures


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
