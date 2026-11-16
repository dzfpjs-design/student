from __future__ import annotations

import json
from pathlib import Path

from home_study_agent.local_ocr.model_assets import build_model_manifest, verify_model_manifest


def test_model_manifest_records_hashes_and_detects_tampering(tmp_path: Path) -> None:
    model_dir = tmp_path / "mfr"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"offline": true}', encoding="utf-8")
    (model_dir / "onnx").mkdir()
    (model_dir / "onnx" / "decoder.onnx").write_bytes(b"onnx-weights")

    manifest = build_model_manifest(
        model_dir,
        repo_id="breezedeus/pix2text-mfr-1.5",
        revision="fixed-commit",
        license_name="MIT",
    )

    assert manifest["revision"] == "fixed-commit"
    assert {item["path"] for item in manifest["files"]} == {"config.json", "onnx/decoder.onnx"}
    assert verify_model_manifest(model_dir, manifest) == []
    (model_dir / "config.json").write_bytes(b"X" * 17)
    assert "sha256_mismatch:config.json" in verify_model_manifest(model_dir, manifest)


def test_model_manifest_excludes_git_and_generated_manifest_files(tmp_path: Path) -> None:
    model_dir = tmp_path / "mfr"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"weights")
    (model_dir / ".git").mkdir()
    (model_dir / ".git" / "config").write_text("private", encoding="utf-8")
    (model_dir / "model-manifest.json").write_text(json.dumps({"old": True}), encoding="utf-8")

    manifest = build_model_manifest(model_dir, repo_id="repo", revision="rev", license_name="MIT")

    assert [item["path"] for item in manifest["files"]] == ["model.bin"]
