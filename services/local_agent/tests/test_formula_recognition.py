import json
from pathlib import Path

from home_study_agent.local_ocr.formula_recognition import Pix2TexFormulaProvider, Pix2TextMfrOnnxProvider, RapidOcrFormulaBaselineProvider, UnavailableFormulaProvider, build_local_formula_provider
from home_study_agent.local_ocr.model_assets import build_model_manifest


def test_pix2tex_provider_does_not_download_or_initialize_without_checkpoint(tmp_path: Path) -> None:
    provider = Pix2TexFormulaProvider(tmp_path / "unreviewed-weights.pth")
    health = provider.health()
    assert health["ready"] is False
    assert health["cpu_only"] is True
    assert "manual_formula_edit" in health["reason"]


def test_pix2text_mfr_requires_verified_local_manifest_before_loading_dependencies(tmp_path: Path) -> None:
    model_dir = tmp_path / "mfr"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    manifest = build_model_manifest(model_dir, repo_id="breezedeus/pix2text-mfr-1.5", revision="commit", license_name="MIT")
    manifest_path = tmp_path / "mfr-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    provider = Pix2TextMfrOnnxProvider(model_dir, manifest_path)

    health = provider.health()

    assert health["implicit_network_download"] is False
    assert health["reason"] == "pix2text_mfr_onnx_dependencies_not_installed" or health["ready"] is True
    (model_dir / "config.json").write_text('{"changed": true}', encoding="utf-8")
    assert provider.health()["reason"].startswith("pix2text_mfr_manifest_verification_failed")


def test_pix2text_mfr_reuses_verified_hash_health_until_a_local_file_changes(tmp_path: Path, monkeypatch) -> None:
    model_dir = tmp_path / "mfr"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    manifest = build_model_manifest(model_dir, repo_id="breezedeus/pix2text-mfr-1.5", revision="commit", license_name="MIT")
    manifest_path = tmp_path / "mfr-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    provider = Pix2TextMfrOnnxProvider(model_dir, manifest_path)
    calls = 0

    def verified(_: Path, __: dict) -> list[str]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr("home_study_agent.local_ocr.formula_recognition.verify_model_manifest", verified)
    provider._read_verified_manifest()
    provider._read_verified_manifest()
    assert calls == 1
    (model_dir / "config.json").write_text('{"changed": true}', encoding="utf-8")
    provider._read_verified_manifest()
    assert calls == 2


def test_formula_provider_factory_is_explicit_and_never_downloads() -> None:
    assert build_local_formula_provider("disabled") is None
    missing_mfr = build_local_formula_provider("pix2text_mfr_onnx")
    assert isinstance(missing_mfr, Pix2TextMfrOnnxProvider)
    assert missing_mfr.health()["implicit_network_download"] is False
    assert isinstance(build_local_formula_provider("rapidocr_baseline"), RapidOcrFormulaBaselineProvider)
    unsupported = build_local_formula_provider("unknown")
    assert isinstance(unsupported, UnavailableFormulaProvider)
    assert unsupported.health()["reason"] == "unsupported_local_formula_provider:unknown"
